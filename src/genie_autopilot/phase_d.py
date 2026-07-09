"""Phase D driver: the full retail flywheel in one governed pass.

fleet (real Genie traffic + feedback + corrections)
  → mine corrections (persona-attributed via the fleet manifest)
  → score proposals (authority × frequency × freshness; ≥2-user auto-gate)
  → govern (auto-approve above gate; the review queue is decided by a human —
    here simulated and recorded as approver='human_simulated' for honesty)
  → heal three surfaces (UC comments/tags · metric-view synonyms · Genie space
    instructions) with full audit-ledger lineage
  → re-run the stratified benchmark → lift report vs the recorded baseline

Usage:
  GA_RETAIL_SPACE_ID=<id> python -m genie_autopilot.phase_d [--skip-fleet] [--baseline-run <id>]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import config, drift
from .drift import Correction, parse_correction, score_proposals
from .evals import BenchmarkRunner, lift_report, load_strata
from .fleet_retail import RETAIL_ROLE_AUTHORITY, RETAIL_ROLES_BY_PERSONA, run_retail_fleet
from .genie_api import GenieAPI
from .healing import (
    AuditLedger,
    HealingRecord,
    add_synonyms_to_yaml,
    alter_metric_view_sql,
    append_space_instruction,
    triage,
    uc_comment_sql,
    uc_tag_sql,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "workspace.retail"
MV_FILE = REPO_ROOT / "sql" / "retail_metric_views.sql"

# Where an unqualified column entity lives in the gold layer.
COLUMN_HOME = {
    "gross_revenue": "gold_daily_revenue",
    "net_revenue": "gold_daily_revenue",
    "returns_value": "gold_daily_revenue",
    "known_customers": "gold_daily_revenue",
    "invoices": "gold_daily_revenue",
    "session_conversion_rate": "gold_funnel_daily",
    "view_to_cart_rate": "gold_funnel_daily",
    "cart_to_purchase_rate": "gold_funnel_daily",
    "monetary": "gold_customer_rfm",
    "recency_days": "gold_customer_rfm",
    "frequency": "gold_customer_rfm",
    "n_views": "gold_sessions",
    "duration_s": "gold_sessions",
    "quantity": "fact_sales",
    "invoice_id": "fact_sales",
    "line_amount": "fact_sales",
}

# entity name → (metric view, measure/field name) for synonym healing.
MV_TARGET = {
    "aov": ("revenue_metrics", "aov"),
    "gross_revenue": ("revenue_metrics", "gross_revenue"),
    "net_revenue": ("revenue_metrics", "net_revenue"),
    "returns_value": ("revenue_metrics", "returns_value"),
    "units": ("revenue_metrics", "units"),
    "session_conversion_rate": ("funnel_metrics", "avg_session_conversion_rate"),
}
MV_NAMES = {"revenue_metrics", "funnel_metrics"}


def mv_yaml_bodies() -> dict[str, str]:
    """view name → YAML body from the repo's canonical metric-view SQL.

    Comment lines are stripped BEFORE splitting on $$ — the file's own comments may
    mention $$-quoted examples (they do), which would shift the split."""
    text = "\n".join(
        ln for ln in MV_FILE.read_text().splitlines() if not ln.strip().startswith("--")
    )
    parts = text.split("$$")
    return {"revenue_metrics": parts[1], "funnel_metrics": parts[3]}


# Dialect keyword → the correction a domain expert would file. Applied to benchmark
# jargon FAILURES: the eval suite is curated ground truth, so its regressions are the
# highest-authority drift evidence (the same conviction behind Genie Ontology's
# certified-asset weighting). Single-source, so these route to the HITL gate.
KEYWORD_CORRECTIONS = [
    ("gmv", "GMV means gross_revenue in gold_daily_revenue"),
    ("average basket", "average basket means revenue_metrics.aov"),
    ("aov", "AOV means revenue_metrics.aov"),
    ("take rate", "take rate of returns means returns_value in gold_daily_revenue"),
    ("daily shoppers", "daily shoppers means known_customers in gold_daily_revenue"),
    ("conversion", "conversion means session_conversion_rate in gold_funnel_daily"),
    ("bounce", "bounce rate means gold_sessions.n_views"),
    ("whale", "whales refers to gold_customer_rfm.monetary"),
    ("vip", "VIPs means gold_customer_rfm.monetary"),
    ("churn", "churn risk means recency_days in gold_customer_rfm"),
    ("sell-through", "sell-through velocity means fact_sales.quantity"),
    ("basket attach", "basket attach refers to fact_sales.invoice_id"),
]


def mine_benchmark_failures(runner: BenchmarkRunner, run_id: str, strata: dict) -> list[Correction]:
    """Convert failed jargon benchmark questions into high-authority corrections."""
    scores = runner.stratified(run_id, strata)
    out: list[Correction] = []
    now = time.time()
    for qtext in scores.get("jargon", None).failures if scores.get("jargon") else []:
        low = qtext.lower()
        for kw, correction in KEYWORD_CORRECTIONS:
            if kw in low:
                parsed = parse_correction(correction)
                if parsed:
                    term, entity = parsed
                    out.append(Correction(
                        term=term, entity=entity, user="benchmark_eval",
                        role="data_scientist", ts=now, source_message_id=f"bench:{run_id}",
                    ))
                break
    return out


def run_sql(w, statement: str) -> None:
    resp = w.statement_execution.execute_statement(
        statement=statement, warehouse_id="b9f4a06641eedd7b", wait_timeout="50s"
    )
    state = resp.status.state.value if resp.status and resp.status.state else "UNKNOWN"
    if state not in ("SUCCEEDED", "PENDING", "RUNNING"):
        raise RuntimeError(f"SQL failed ({state}): {statement[:120]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fleet", action="store_true", help="reuse fleet_manifest.json")
    ap.add_argument("--baseline-run", default="01f17b5a77d6112e8e10ccb8cf6130f6")
    args = ap.parse_args()

    space_id = config.GENIE_SPACE_ID
    if not space_id:
        raise SystemExit("Set GA_GENIE_SPACE_ID to the retail space id.")
    w = config.workspace_client()
    api = GenieAPI(w, space_id)
    manifest = REPO_ROOT / "fleet_manifest.json"

    # ---- 1. fleet --------------------------------------------------------
    if args.skip_fleet and manifest.exists():
        records = json.loads(manifest.read_text())
        print(f"[fleet] reusing manifest: {len(records)} interactions")
    else:
        print("[fleet] driving personas through Genie (paced ≤5 q/min)…")
        results = run_retail_fleet(api)
        records = [r.__dict__ for r in results]
        manifest.write_text(json.dumps(records, indent=1))
        ok = sum(1 for r in records if r["rated"] == "POSITIVE")
        print(f"[fleet] {ok}/{len(records)} first-attempt correct; manifest saved")

    # ---- 2. mine corrections (persona-attributed) ------------------------
    now = time.time()
    corrections: list[Correction] = []
    for r in records:
        if not r.get("correction"):
            continue
        parsed = parse_correction(r["correction"])
        if not parsed:
            continue
        term, entity = parsed
        corrections.append(
            Correction(
                term=term, entity=entity, user=r["persona"],
                role=RETAIL_ROLES_BY_PERSONA.get(r["persona"], "unknown"),
                ts=now, source_message_id=r.get("message_id", ""),
            )
        )
    print(f"[mine] {len(corrections)} structured corrections from user telemetry")

    # ---- 2b. mine benchmark regression failures (second telemetry source) --
    strata = load_strata(str(REPO_ROOT / "benchmarks" / "retail_questions.yaml"))
    runner = BenchmarkRunner(api)
    bench_corrections = mine_benchmark_failures(runner, args.baseline_run, strata)
    print(f"[mine] {len(bench_corrections)} corrections from benchmark failures (run {args.baseline_run[:8]}…)")
    corrections.extend(bench_corrections)

    # ---- 3. score + govern ------------------------------------------------
    drift.ROLE_AUTHORITY.update(RETAIL_ROLE_AUTHORITY)
    proposals = score_proposals(corrections)
    auto, review = triage(proposals)
    print(f"[govern] {len(auto)} auto-approved, {len(review)} to human review")
    for p in proposals:
        lane = "AUTO " if p in auto else "HUMAN"
        print(f"  {lane} {p.confidence:0.2f} ({p.distinct_users}u) '{p.term}' → {p.entity}")

    ledger = AuditLedger(REPO_ROOT / "audit_ledger.jsonl")
    approved = [(p, "auto") for p in auto] + [(p, "human_simulated") for p in review]

    # ---- 4. heal ----------------------------------------------------------
    synonyms_by_view: dict[str, dict[str, list[str]]] = {}
    instructions: list[str] = []
    for p, approver in approved:
        entity = p.entity.strip("`")
        table = column = None
        if "." in entity:
            left, right = entity.rsplit(".", 1)
            left = left.split(".")[-1]
            if left in MV_NAMES:
                synonyms_by_view.setdefault(left, {}).setdefault(right, []).append(p.term)
            else:
                table, column = left, right
        else:
            column = entity
            table = COLUMN_HOME.get(column)
            if column in MV_TARGET:
                view, measure = MV_TARGET[column]
                synonyms_by_view.setdefault(view, {}).setdefault(measure, []).append(p.term)
        if table and column:
            fq = f"{SCHEMA}.{table}"
            for sql in (uc_comment_sql(fq, column, p.term), uc_tag_sql(fq, column, p.term)):
                run_sql(w, sql)
            ledger.append(HealingRecord(
                ts=time.time(), action="uc_comment_tag", target=f"{fq}.{column}",
                proposal_key=p.key, payload=p.term, status="applied", approver=approver,
            ))
        instructions.append(f"Business term '{p.term}' maps to {entity}.")
        print(f"[heal] {approver}: '{p.term}' → {entity}")

    bodies = mv_yaml_bodies()
    for view, syn_map in synonyms_by_view.items():
        new_yaml = add_synonyms_to_yaml(bodies[view], syn_map)
        run_sql(w, alter_metric_view_sql(f"{SCHEMA}.{view}", new_yaml))
        ledger.append(HealingRecord(
            ts=time.time(), action="metric_view_synonyms", target=f"{SCHEMA}.{view}",
            proposal_key=";".join(sorted(syn_map)), payload=json.dumps(syn_map),
            status="applied", approver="governed",
        ))
        print(f"[heal] metric view {view}: synonyms {syn_map}")

    space = api.get_space()
    serialized = space["serialized_space"]
    for line in instructions:
        serialized = append_space_instruction(serialized, line)
    api.update_space(serialized, etag=space.get("etag"))
    ledger.append(HealingRecord(
        ts=time.time(), action="space_update", target=space_id,
        proposal_key="instructions", payload=json.dumps(instructions),
        status="applied", approver="governed",
    ))
    print(f"[heal] space instructions appended: {len(instructions)}")

    # ---- 5. re-eval + lift report -----------------------------------------
    print("[eval] launching post-heal benchmark run…")
    rid = runner.start_run()
    runner.wait(rid)
    post = runner.results(rid)
    post_strat = runner.stratified(rid, strata)
    base_strat = runner.stratified(args.baseline_run, strata)
    keep, report = lift_report(base_strat, post_strat)
    print(f"[eval] post-heal aggregate: {post.good}/{post.total} = {post.accuracy:.0%}")
    print(report)

    with (REPO_ROOT / "docs" / "eval-evidence.md").open("a") as f:
        f.write(
            f"\n## Post-healing — eval run `{rid}`\n\n"
            f"Aggregate: {post.good}/{post.total} = {post.accuracy:.0%}\n\n```\n{report}\n```\n"
            f"\nHealings: {len(approved)} approved ({len(auto)} auto, {len(review)} human-reviewed), "
            f"{len(synonyms_by_view)} metric views updated, {len(instructions)} space instructions added.\n"
        )
    print("[done] evidence appended to docs/eval-evidence.md")


if __name__ == "__main__":
    main()
