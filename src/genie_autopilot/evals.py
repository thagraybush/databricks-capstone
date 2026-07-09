"""Benchmark evaluation: the regression gate and lift-measurement harness.

Verified against the live Genie eval API (2026-07-09):
  POST /api/2.0/genie/spaces/{s}/eval-runs        {} → {eval_run_id, eval_run_status, num_questions}
  GET  /api/2.0/genie/spaces/{s}/eval-runs/{r}    → eval_run_status RUNNING|DONE, num_correct,
                                                    num_questions, num_needs_review, num_done
  SDK  genie_list_eval_results(space_id, eval_run_id, page_size)   → eval_results[]
  SDK  genie_get_eval_result_details(...)          → assessment GOOD|BAD, assessment_reasons[]

The benchmark suite is STRATIFIED (benchmarks/*.yaml `trap` key):
  clean  — documented, analyst-hardened questions (control: must not regress)
  jargon — bleeding-edge business dialect not yet documented (treatment: the healing target)
  bad    — unanswerable noise (excluded from Genie benchmarks; scored by quality.py instead)

The headline metric is the JARGON-stratum lift with a no-regression check on CLEAN.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .genie_api import GenieAPI

TERMINAL = {"DONE", "FAILED", "CANCELLED", "ERROR"}


@dataclass
class EvalSummary:
    run_id: str
    total: int
    good: int
    bad: int
    manual_review: int

    @property
    def accuracy(self) -> float:
        return round(self.good / self.total, 4) if self.total else 0.0


@dataclass
class StratumScore:
    correct: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)  # question texts that failed

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 4) if self.total else 0.0


def load_strata(yaml_path: str) -> dict[str, str]:
    """question text (lowercased) → stratum name, from a benchmarks/*.yaml file."""
    import yaml

    bm = yaml.safe_load(open(yaml_path))
    out = {}
    for q in bm.get("questions", []):
        if q.get("trap") == "bad" or not q.get("answer_sql"):
            continue
        trap = q.get("trap")
        if trap is True:
            stratum = "jargon"
        elif trap == "collision":
            stratum = "collision"
        else:
            stratum = "clean"
        out[q["q"].strip().lower()] = stratum
    return out


class BenchmarkRunner:
    def __init__(self, api: GenieAPI):
        self.api = api

    # -- run lifecycle -------------------------------------------------------
    def start_run(self, benchmark_question_ids: list[str] | None = None) -> str:
        body: dict = {}
        if benchmark_question_ids:
            body["benchmark_question_ids"] = benchmark_question_ids
        resp = self.api._do("POST", f"{self.api._base()}/eval-runs", body)
        return resp.get("eval_run_id", "")

    def wait(self, run_id: str, timeout_s: int = 1800) -> dict:
        deadline = time.monotonic() + timeout_s
        resp: dict = {}
        while time.monotonic() < deadline:
            resp = self.api._do("GET", f"{self.api._base()}/eval-runs/{run_id}")
            if resp.get("eval_run_status") in TERMINAL:
                return resp
            time.sleep(10)
        raise TimeoutError(f"Eval run {run_id} did not finish in {timeout_s}s")

    def results(self, run_id: str) -> EvalSummary:
        run = self.api._do("GET", f"{self.api._base()}/eval-runs/{run_id}")
        total = run.get("num_questions", 0)
        good = run.get("num_correct", 0)
        review = run.get("num_needs_review", 0)
        done = run.get("num_done", total)
        return EvalSummary(
            run_id=run_id, total=total, good=good,
            bad=max(0, done - good - review), manual_review=review,
        )

    # -- stratified lift measurement ------------------------------------------
    def stratified(self, run_id: str, strata: dict[str, str]) -> dict[str, StratumScore]:
        """Per-stratum scoring via the SDK list + details endpoints."""
        w = self.api.w
        scores: dict[str, StratumScore] = {}
        page_token = None
        while True:
            resp = w.genie.genie_list_eval_results(
                space_id=self.api.space_id, eval_run_id=run_id,
                page_size=50, page_token=page_token,
            )
            for row in resp.eval_results or []:
                r = row.as_dict()
                q = r.get("question")
                qtext = ("".join(q) if isinstance(q, list) else str(q)).strip()
                det = w.genie.genie_get_eval_result_details(
                    space_id=self.api.space_id, eval_run_id=run_id, result_id=r["result_id"]
                ).as_dict()
                stratum = strata.get(qtext.lower(), "unmatched")
                sc = scores.setdefault(stratum, StratumScore())
                sc.total += 1
                if det.get("assessment") == "GOOD":
                    sc.correct += 1
                else:
                    sc.failures.append(qtext)
            page_token = getattr(resp, "next_page_token", None)
            if not page_token:
                break
        return scores


def compare(before: EvalSummary, after: EvalSummary, min_gain: float = 0.0) -> bool:
    """True when the healing cycle may be kept (no aggregate regression)."""
    return after.accuracy >= before.accuracy + min_gain


def lift_report(
    before: dict[str, StratumScore], after: dict[str, StratumScore]
) -> tuple[bool, str]:
    """(keep?, human-readable report). Keep iff jargon improved AND clean did not regress."""
    b_j, a_j = before.get("jargon", StratumScore()), after.get("jargon", StratumScore())
    b_c, a_c = before.get("clean", StratumScore()), after.get("clean", StratumScore())
    keep = a_j.accuracy >= b_j.accuracy and a_c.accuracy >= b_c.accuracy
    report = (
        f"jargon (bleeding-edge dialect): {b_j.correct}/{b_j.total} → {a_j.correct}/{a_j.total} "
        f"({b_j.accuracy:.0%} → {a_j.accuracy:.0%}, lift {a_j.accuracy - b_j.accuracy:+.0%})\n"
        f"clean (control, no-regression):  {b_c.correct}/{b_c.total} → {a_c.correct}/{a_c.total} "
        f"({b_c.accuracy:.0%} → {a_c.accuracy:.0%})\n"
        f"verdict: {'KEEP' if keep else 'ROLLBACK'}"
    )
    return keep, report
