"""Phase E: alias collision, poison terms, learning loops, and the DQ scorecard.

1. Add the collision benchmark stratum to the live space, measure its baseline.
2. Drive the finance/merchandising personas (real Genie traffic) — the poison term
   'sales' yields CONTRADICTORY intents (net_revenue vs quantity).
3. Mine intents (explicit corrections on failures + usage confirmations on passes),
   detect conflicts: poison terms NEVER auto-heal as synonyms — they heal as a
   disambiguation instruction. Non-conflicted collision aliases heal as synonyms.
4. Re-eval: collision lift + no-regression on jargon/clean.
5. Train the query-quality classifier on labeled fleet outcomes; report metrics.
6. Score the DQ layer against the producer's labeled chaos (precision/recall).

Usage: GA_GENIE_SPACE_ID=<retail-space> python -m genie_autopilot.phase_e [--skip-fleet]
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import yaml

from . import config, drift
from .drift import Correction, detect_conflicts, parse_correction, score_proposals
from .evals import BenchmarkRunner, load_strata
from .fleet_retail import (
    COLLISION_PERSONAS,
    QUESTION_LABELS,
    RETAIL_ROLE_AUTHORITY,
    RETAIL_ROLES_BY_PERSONA,
    run_retail_fleet,
)
from .genie_api import GenieAPI
from .healing import AuditLedger, HealingRecord, add_synonyms_to_yaml, alter_metric_view_sql
from .phase_d import mv_yaml_bodies, run_sql

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = REPO_ROOT / "docs" / "eval-evidence.md"


def ensure_benchmarks(api: GenieAPI) -> int:
    """Sync collision benchmark questions from the YAML into the live space."""
    bm = yaml.safe_load(open(REPO_ROOT / "benchmarks" / "retail_questions.yaml"))
    space = api.get_space()
    ser = json.loads(space["serialized_space"])
    questions = ser.setdefault("benchmarks", {}).setdefault("questions", [])
    existing = {"".join(q.get("question", [])).strip().lower() for q in questions}
    added = 0
    for q in bm["questions"]:
        if q.get("trap") == "bad" or not q.get("answer_sql"):
            continue
        if q["q"].strip().lower() in existing:
            continue
        lines = [ln + "\n" for ln in q["answer_sql"].strip().splitlines()]
        lines[-1] = lines[-1].rstrip("\n")
        questions.append({
            "id": uuid.uuid4().hex,
            "question": [q["q"]],
            "answer": [{"format": "SQL", "content": lines}],
        })
        added += 1
    if added:
        questions.sort(key=lambda q: q["id"])
        api.update_space(json.dumps(ser), etag=space.get("etag"))
    return added


def mine_intents(records: list[dict]) -> list[Correction]:
    """Collision-kind intents: explicit corrections on failures, usage confirmations on
    passes (the persona's catalog correction encodes their intent either way)."""
    catalog = {
        (p.name, q[0]): q[2] for p in COLLISION_PERSONAS for q in p.questions if q[2]
    }
    out: list[Correction] = []
    now = time.time()
    for r in records:
        text = catalog.get((r["persona"], r["question"]))
        if not text:
            continue
        parsed = parse_correction(text)
        if not parsed:
            continue
        term, entity = parsed
        out.append(Correction(
            term=term, entity=entity, user=r["persona"],
            role=RETAIL_ROLES_BY_PERSONA.get(r["persona"], "unknown"), ts=now,
            source_message_id=("confirm:" if r["rated"] == "POSITIVE" else "correct:") + r.get("message_id", ""),
        ))
    return out


def heal(api: GenieAPI, w, safe: list, conflicts: dict[str, set[str]]) -> None:
    ledger = AuditLedger(
        REPO_ROOT / "audit_ledger.jsonl",
        delta_table="workspace.retail.autopilot_audit_ledger",
        sql_runner=lambda s: run_sql(w, s),
    )
    # metric-view synonyms for gross_revenue aliases
    syn = {}
    glossary_lines = []
    for p in safe:
        if p.entity.split(".")[-1] == "gross_revenue":
            syn.setdefault("gross_revenue", []).append(p.term)
        glossary_lines.append(f"- '{p.term}' = gross_revenue in gold_daily_revenue.")
    if syn:
        new_yaml = add_synonyms_to_yaml(mv_yaml_bodies()["revenue_metrics"], syn)
        run_sql(w, alter_metric_view_sql("workspace.retail.revenue_metrics", new_yaml))
        ledger.append(HealingRecord(
            ts=time.time(), action="metric_view_synonyms", target="workspace.retail.revenue_metrics",
            proposal_key="collision_aliases", payload=json.dumps(syn), status="applied",
            approver="human_simulated",
        ))
    for term, entities in conflicts.items():
        glossary_lines.append(
            f"- POISON TERM '{term}': ambiguous across teams ({', '.join(sorted(entities))}). "
            f"When a question uses '{term}' without qualification, ask the user which metric "
            "they mean before answering. Never guess."
        )
        ledger.append(HealingRecord(
            ts=time.time(), action="disambiguation_instruction", target=api.space_id,
            proposal_key=f"poison:{term}", payload=json.dumps(sorted(entities)),
            status="applied", approver="human_simulated",
        ))
    space = api.get_space()
    ser = json.loads(space["serialized_space"])
    entry = ser["instructions"]["text_instructions"][0]
    entry["content"] = entry["content"] + glossary_lines
    api.update_space(json.dumps(ser), etag=space.get("etag"))
    print(f"[heal] {len(syn.get('gross_revenue', []))} collision synonyms, "
          f"{len(conflicts)} disambiguation instruction(s), {len(glossary_lines)} glossary lines")


def train_classifier() -> str:
    from .quality import QueryQualityModel, evaluate

    schema_terms = {
        "net_revenue", "gross_revenue", "returns_value", "known_customers", "invoices",
        "sale_date", "country", "product_name", "aov", "units", "sessions", "views",
        "add_to_carts", "purchases", "session_conversion_rate", "monetary",
        "recency_days", "frequency", "quantity", "invoice_id", "n_views", "duration_s",
    }
    questions = list(QUESTION_LABELS)
    labels = ["noise" if QUESTION_LABELS[q] in ("vague", "unanswerable") else "answerable"
              for q in questions]
    model = QueryQualityModel()
    model.fit(questions, labels, schema_terms)
    metrics = evaluate(model, questions, labels, schema_terms)
    routes = {q: model.predict_route(q, schema_terms).decision for q in questions}
    rejected_noise = sum(1 for q, r in routes.items()
                         if r in ("reject", "human_review") and QUESTION_LABELS[q] in ("vague", "unanswerable"))
    total_noise = sum(1 for v in QUESTION_LABELS.values() if v in ("vague", "unanswerable"))
    report = (f"training-set metrics: {metrics}; "
              f"noise filtered (reject|human_review): {rejected_noise}/{total_noise}")
    print(f"[classifier] {report}")
    return report


def dq_scorecard(w) -> str:
    gt = "read_files('/Volumes/workspace/retail/raw/clickstream_labels/ground_truth.jsonl', format => 'json')"
    lines = []

    def q(sql: str) -> list:
        resp = w.statement_execution.execute_statement(
            statement=sql, warehouse_id="b9f4a06641eedd7b", wait_timeout="50s")
        return (resp.result.data_array or []) if resp.result else []

    pii = q(f"""
      WITH gt_pii AS (SELECT DISTINCT event_id FROM {gt} WHERE array_contains(labels, 'pii_leak'))
      SELECT
        (SELECT COUNT(*) FROM workspace.retail.silver_events s JOIN gt_pii g ON s.event_id = g.event_id WHERE s.pii_detected) AS tp,
        (SELECT COUNT(*) FROM workspace.retail.silver_events WHERE pii_detected) AS flagged,
        (SELECT COUNT(*) FROM gt_pii) AS labeled""")
    tp, flagged, labeled = (int(x) for x in pii[0])
    lines.append(f"PII detection: precision {tp}/{flagged} = {tp / max(flagged, 1):.0%}, "
                 f"recall {tp}/{labeled} = {tp / max(labeled, 1):.0%}")

    bots = q(f"""
      WITH bot_sessions_gt AS (
        SELECT DISTINCT s.session_id FROM workspace.retail.silver_events s
        JOIN {gt} g ON s.event_id = g.event_id WHERE array_contains(g.labels, 'bot_session'))
      SELECT
        (SELECT COUNT(*) FROM workspace.retail.gold_sessions gs JOIN bot_sessions_gt b ON gs.session_id = b.session_id WHERE gs.is_bot) AS tp,
        (SELECT COUNT(*) FROM workspace.retail.gold_sessions WHERE is_bot) AS flagged,
        (SELECT COUNT(*) FROM bot_sessions_gt) AS labeled""")
    tp, flagged, labeled = (int(x) for x in bots[0])
    lines.append(f"Bot detection (session level): precision {tp}/{flagged} = {tp / max(flagged, 1):.0%}, "
                 f"recall {tp}/{labeled} = {tp / max(labeled, 1):.0%}")

    counts = q(f"""
      SELECT
        (SELECT COUNT(*) FROM {gt} WHERE array_contains(labels, 'duplicate')) AS gt_dupes,
        (SELECT COUNT(*) FROM workspace.retail.bronze_events)
          - (SELECT COUNT(*) FROM workspace.retail.silver_events)
          - (SELECT COUNT(*) FROM workspace.retail.quarantine_events) AS removed,
        (SELECT COUNT(*) FROM {gt} WHERE array_contains(labels, 'malformed')) AS gt_malformed,
        (SELECT COUNT(*) FROM workspace.retail.quarantine_events) AS quarantined""")
    gt_d, removed, gt_m, quar = (int(x) for x in counts[0])
    lines.append(f"Duplicates: {gt_d} labeled dup emissions, {removed} rows removed by dedup")
    lines.append(f"Malformed: {gt_m} labeled truncations, {quar} rows quarantined "
                 "(quarantine also catches unknown types/bad timestamps)")
    report = "\n".join(lines)
    print("[dq]", report.replace("\n", "\n[dq] "))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fleet", action="store_true")
    args = ap.parse_args()
    if not config.GENIE_SPACE_ID:
        raise SystemExit("Set GA_GENIE_SPACE_ID to the retail space id.")
    w = config.workspace_client()
    api = GenieAPI(w, config.GENIE_SPACE_ID)
    strata = load_strata(str(REPO_ROOT / "benchmarks" / "retail_questions.yaml"))
    runner = BenchmarkRunner(api)
    manifest = REPO_ROOT / "fleet_manifest_e.json"

    added = ensure_benchmarks(api)
    print(f"[bench] {added} collision questions synced to the space")

    print("[eval] collision-stratum baseline…")
    base_rid = runner.start_run()
    runner.wait(base_rid)
    base = runner.stratified(base_rid, strata)

    if args.skip_fleet and manifest.exists():
        records = json.loads(manifest.read_text())
    else:
        results = run_retail_fleet(api, personas=COLLISION_PERSONAS)
        records = [r.__dict__ for r in results]
        manifest.write_text(json.dumps(records, indent=1))
    print(f"[fleet] {len(records)} collision-persona interactions")

    intents = mine_intents(records)
    conflicts = detect_conflicts(intents)
    drift.ROLE_AUTHORITY.update(RETAIL_ROLE_AUTHORITY)
    safe = [p for p in score_proposals(intents) if p.term not in conflicts]
    print(f"[mine] {len(intents)} intents; conflicts: "
          f"{ {t: sorted(e) for t, e in conflicts.items()} or 'none'}")

    heal(api, w, safe, conflicts)

    print("[eval] post-collision-healing run…")
    post_rid = runner.start_run()
    runner.wait(post_rid)
    post = runner.stratified(post_rid, strata)

    rows = []
    for s in ("collision", "jargon", "clean"):
        b, a = base.get(s), post.get(s)
        if b and a:
            rows.append(f"{s:9s}: {b.correct}/{b.total} → {a.correct}/{a.total} "
                        f"({b.accuracy:.0%} → {a.accuracy:.0%})")
    lift = "\n".join(rows)
    print(lift)

    clf_report = train_classifier()
    dq_report = dq_scorecard(w)

    with EVIDENCE.open("a") as f:
        f.write(
            f"\n## Phase E — collision stratum, poison terms, learning loops (runs `{base_rid}` → `{post_rid}`)\n\n"
            f"```\n{lift}\n```\n\n"
            f"Poison terms detected (healed as disambiguation instructions, never synonyms): "
            f"{ {t: sorted(e) for t, e in conflicts.items()} }\n\n"
            f"Query-quality classifier: {clf_report}\n\nDQ scorecard vs producer ground truth:\n\n```\n{dq_report}\n```\n"
        )
    print("[done] evidence appended")


if __name__ == "__main__":
    main()
