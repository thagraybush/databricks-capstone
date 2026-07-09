"""Eval variance study: repeat the stratified benchmark N times on the current
(healed) space and report per-stratum mean ± range. Genie is nondeterministic —
single-run numbers invite the "you got lucky" critique; this quantifies stability.

Usage: GA_GENIE_SPACE_ID=<retail-space> python -m genie_autopilot.phase_f_variance [--runs 3]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import config
from .evals import BenchmarkRunner, load_strata
from .genie_api import GenieAPI

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    if not config.GENIE_SPACE_ID:
        raise SystemExit("Set GA_GENIE_SPACE_ID to the retail space id.")
    api = GenieAPI(config.workspace_client(), config.GENIE_SPACE_ID)
    runner = BenchmarkRunner(api)
    strata_map = load_strata(str(REPO_ROOT / "benchmarks" / "retail_questions.yaml"))

    results: dict[str, list[float]] = {}
    counts: dict[str, list[str]] = {}
    for i in range(args.runs):
        rid = runner.start_run()
        runner.wait(rid)
        scores = runner.stratified(rid, strata_map)
        line = ", ".join(f"{s}: {sc.correct}/{sc.total}" for s, sc in sorted(scores.items()))
        print(f"[run {i + 1}/{args.runs}] {rid[:12]}… {line}")
        for s, sc in scores.items():
            results.setdefault(s, []).append(sc.accuracy)
            counts.setdefault(s, []).append(f"{sc.correct}/{sc.total}")

    lines = []
    for s in sorted(results):
        vals = results[s]
        mean = sum(vals) / len(vals)
        lines.append(
            f"{s:9s}: mean {mean:.0%}  range [{min(vals):.0%}, {max(vals):.0%}]  runs: {', '.join(counts[s])}"
        )
    report = "\n".join(lines)
    print(report)
    with (REPO_ROOT / "docs" / "eval-evidence.md").open("a") as f:
        f.write(
            f"\n## Variance study — {args.runs} repeated eval runs on the healed space\n\n"
            f"```\n{report}\n```\n\nGenie is nondeterministic; stability is reported as "
            "mean ± range across repeats rather than a single-run point estimate.\n"
        )
    print("[done] evidence appended")


if __name__ == "__main__":
    main()
