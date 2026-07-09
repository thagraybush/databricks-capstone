"""Command-line entrypoints for the flywheel: bootstrap / simulate / detect / heal / eval.

Usage: python -m genie_autopilot.cli <command>
Requires a Free Edition PAT (env DATABRICKS_TOKEN or Keychain service `databricks-fe`)
for every command except `--help`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config
from .drift import Correction
from .evals import BenchmarkRunner
from .fleet import ROLES_BY_PERSONA, run_fleet
from .genie_api import GenieAPI
from .healing import (
    AuditLedger,
    HealingRecord,
    triage,
    uc_comment_sql,
)
from .telemetry import harvest_corrections

REPO_ROOT = Path(__file__).resolve().parents[2]


def _warehouse_id(w) -> str:
    if config.WAREHOUSE_ID:
        return config.WAREHOUSE_ID
    warehouses = list(w.warehouses.list())
    if not warehouses:
        sys.exit("No SQL warehouse found in the workspace.")
    return warehouses[0].id


def _run_sql(w, warehouse_id: str, statement: str) -> None:
    resp = w.statement_execution.execute_statement(
        statement=statement, warehouse_id=warehouse_id, wait_timeout="50s"
    )
    state = resp.status.state.value if resp.status and resp.status.state else "UNKNOWN"
    if state not in ("SUCCEEDED", "PENDING", "RUNNING"):
        raise RuntimeError(f"SQL failed ({state}): {statement[:120]}…")


def _run_sql_file(w, warehouse_id: str, path: Path) -> int:
    """Execute each statement in a .sql file.

    Comment lines are stripped from the whole text first (comments may contain
    semicolons), then statements are split on ';' with awareness of BOTH $$-quoted
    blocks (metric-view YAML) and single-quoted string literals ('' escapes honored) —
    COMMENT ON strings legitimately contain semicolons too."""
    body = "\n".join(
        ln for ln in path.read_text().splitlines() if not ln.strip().startswith("--")
    )
    statements, buf = [], []
    in_dollar = in_string = False
    i = 0
    while i < len(body):
        if not in_string and body.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        ch = body[i]
        if not in_dollar and ch == "'":
            if in_string and body[i + 1 : i + 2] == "'":  # escaped '' inside a string
                buf.append("''")
                i += 2
                continue
            in_string = not in_string
        if ch == ";" and not in_dollar and not in_string:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    for stmt in statements:
        _run_sql(w, warehouse_id, stmt)
    return len(statements)


def _api() -> GenieAPI:
    if not config.GENIE_SPACE_ID:
        sys.exit("Set GA_GENIE_SPACE_ID (the Genie space id) — created during bootstrap.")
    return GenieAPI(config.workspace_client(), config.GENIE_SPACE_ID)


def cmd_bootstrap(_args) -> None:
    w = config.workspace_client()
    wid = _warehouse_id(w)
    print(f"Using warehouse {wid}")
    n = _run_sql_file(w, wid, REPO_ROOT / "sql" / "bootstrap.sql")
    print(f"bootstrap.sql: {n} statements OK")
    inserts = REPO_ROOT / "data_gen" / "output" / "inserts.sql"
    if inserts.exists():
        n = _run_sql_file(w, wid, inserts)
        print(f"inserts.sql: {n} statements OK")
    else:
        print("No data_gen/output/inserts.sql — run `make datagen` first.")
    n = _run_sql_file(w, wid, REPO_ROOT / "sql" / "metric_views.sql")
    print(f"metric_views.sql: {n} statements OK")
    print(
        "\nNext: create the Genie space (UI: New → Genie space → add the two metric views "
        "and three tables) or via API once verified, then export GA_GENIE_SPACE_ID=<id>."
    )


def cmd_simulate(_args) -> None:
    api = _api()
    results = run_fleet(api)
    ok = sum(1 for r in results if r.rated == "POSITIVE")
    print(f"Fleet complete: {ok}/{len(results)} answered correctly at first attempt.")
    for r in results:
        mark = "✓" if r.rated == "POSITIVE" else "✗"
        print(f"  {mark} [{r.persona}] {r.question}")


def cmd_detect(_args) -> None:
    api = _api()
    corrections: list[Correction] = harvest_corrections(api, roles_by_user=ROLES_BY_PERSONA)
    from .drift import score_proposals

    proposals = score_proposals(corrections)
    print(f"{len(corrections)} corrections → {len(proposals)} proposals\n")
    for p in proposals:
        print(f"  {p.confidence:0.2f}  ({p.distinct_users} users)  '{p.term}' → {p.entity}")
    (REPO_ROOT / "proposals.json").write_text(
        json.dumps(
            [
                {
                    "term": p.term,
                    "entity": p.entity,
                    "confidence": p.confidence,
                    "distinct_users": p.distinct_users,
                }
                for p in proposals
            ],
            indent=2,
        )
    )
    print("\nWrote proposals.json")


def cmd_heal(_args) -> None:
    from .drift import Proposal

    raw = json.loads((REPO_ROOT / "proposals.json").read_text())
    proposals = [Proposal(**p) for p in raw]
    auto, review = triage(proposals)
    print(f"{len(auto)} auto-approved, {len(review)} held for human review")

    w = config.workspace_client()
    wid = _warehouse_id(w)
    ledger = AuditLedger(REPO_ROOT / "audit_ledger.jsonl")
    for p in auto:
        # Applier 1: UC column comment when the entity names table.column
        if "." in p.entity:
            table, column = p.entity.rsplit(".", 1)
            fq_table = table if table.count(".") == 2 else config.fq(table)
            sql = uc_comment_sql(fq_table, column, p.term)
            _run_sql(w, wid, sql)
            ledger.append(
                HealingRecord(
                    ts=time.time(), action="uc_comment", target=f"{fq_table}.{column}",
                    proposal_key=p.key, payload=sql, status="applied", approver="auto",
                )
            )
            print(f"  applied UC comment: {p.key}")
        # Applier 2: metric-view synonyms (mapping of entity column → measure handled in Week 2
        # once entity→measure resolution lands; YAML regen utilities are ready in healing.py)
    print("Space-level healing (serialized_space) lands with the Week 2 job.")


def cmd_eval(_args) -> None:
    api = _api()
    runner = BenchmarkRunner(api)
    run_id = runner.start_run()
    print(f"Eval run {run_id} started…")
    runner.wait(run_id)
    summary = runner.results(run_id)
    print(
        f"accuracy={summary.accuracy:.0%}  good={summary.good}  bad={summary.bad}  "
        f"review={summary.manual_review}  (n={summary.total})"
    )


def main() -> None:
    ap = argparse.ArgumentParser(prog="genie-autopilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [
        ("bootstrap", cmd_bootstrap),
        ("simulate", cmd_simulate),
        ("detect", cmd_detect),
        ("heal", cmd_heal),
        ("eval", cmd_eval),
    ]:
        sub.add_parser(name).set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
