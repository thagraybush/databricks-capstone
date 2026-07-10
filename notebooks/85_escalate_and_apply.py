# Databricks notebook source
# MAGIC %md
# MAGIC # 85 — Escalate & apply (the steward loop's system half)
# MAGIC Runs daily BEFORE the report: mines the telemetry corpus for below-gate proposals,
# MAGIC poison conflicts, and NOVEL terms → enqueues them (idempotently) to the Delta
# MAGIC steward queue `autopilot_escalations`, then APPLIES any decisions the steward
# MAGIC approved in the Review Engine (notebook 80) since the last run.
# MAGIC
# MAGIC **Decide ≠ deploy:** humans rule in notebook 80; this notebook is the only thing
# MAGIC that executes those rulings — through the same audit trail as autonomous healings.
# MAGIC
# MAGIC *Architecture note:* the queue's in-workspace system of record is Delta (pure
# MAGIC `spark.sql`, zero binary dependencies). The Lakebase Postgres mirror serves
# MAGIC laptop tooling — binary Postgres drivers proved memory-fragile on Free Edition
# MAGIC serverless (two real OOM incidents; see `docs/runbook.md`).

# COMMAND ----------

import sys
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

dbutils.widgets.text("domain_schema", "workspace.retail")  # noqa: F821
domain_schema = dbutils.widgets.get("domain_schema")  # noqa: F821
QUEUE = f"{domain_schema}.autopilot_escalations"

# COMMAND ----------

# MAGIC %md ## 1 — Mine telemetry
# MAGIC Corrections ("X means Y") become scored proposals; contradictory corrections
# MAGIC become poison conflicts; recurring vocabulary with no governed coverage becomes
# MAGIC novel-term candidates. All pure-python modules — the same code the offline test
# MAGIC suite exercises.

# COMMAND ----------

from genie_autopilot import drift, steward  # noqa: E402
from genie_autopilot.healing import triage  # noqa: E402

rows = [r.asDict() for r in spark.sql(  # noqa: F821
    f"SELECT content AS question, user_id AS user, 'unknown' AS role, "
    f"CAST(created_ts AS DOUBLE) AS ts, message_id, "
    f"COALESCE(feedback_rating, '') AS rated "
    f"FROM {domain_schema}.autopilot_telemetry WHERE content IS NOT NULL"
).collect()]
print(f"telemetry rows: {len(rows)}")

corrections = []
for r in rows:
    parsed = drift.parse_correction(r["question"] or "")
    if parsed:
        term, entity = parsed
        corrections.append(drift.Correction(
            term=term, entity=entity, user=str(r["user"]), role=r["role"],
            ts=r["ts"] or 0.0, source_message_id=r.get("message_id", ""),
        ))
proposals = drift.score_proposals(corrections)
conflicts = drift.detect_conflicts(corrections)
novel = steward.detect_novel_terms(rows, min_occurrences=2)
auto, _review = triage([p for p in proposals if p.term not in conflicts])
escalations = steward.build_escalations(
    proposals=proposals, conflicts=conflicts, novel_terms=novel,
    auto_approved_keys={p.key for p in auto},
)
print(f"{len(corrections)} corrections -> {len(proposals)} proposals, "
      f"{len(conflicts)} conflicts, {len(novel)} novel terms -> {len(escalations)} candidates")

# COMMAND ----------

# MAGIC %md ## 2 — Escalate (idempotent)
# MAGIC MERGE on `proposal_key`: an escalation already in the queue — whatever its
# MAGIC status — is never re-inserted, so daily runs cannot spam the steward. New
# MAGIC candidates land as `pending`.

# COMMAND ----------

import json  # noqa: E402

if escalations:
    def esc(s):
        """Escape a value for use inside a SQL string literal."""
        return str(s).replace("'", "''")[:1500]

    values = ", ".join(
        f"('{esc(e.kind)}:{esc(e.term)}', '{esc(e.term)}', "
        + (f"'{esc(e.entity)}'" if e.entity else "NULL")
        + f", {e.confidence or 0}, {e.distinct_users or 0}, '{esc(e.kind)}', "
        f"'{esc(json.dumps(e.evidence))}')"
        for e in escalations
    )
    result = spark.sql(f"""
        MERGE INTO {QUEUE} t
        USING (SELECT * FROM VALUES {values}
               AS s(proposal_key, term, entity, confidence, distinct_users, kind, evidence)) s
        ON t.proposal_key = s.proposal_key
        WHEN NOT MATCHED THEN INSERT
          (proposal_key, term, entity, confidence, distinct_users, kind, status, evidence, created_at)
          VALUES (s.proposal_key, s.term, s.entity, s.confidence, s.distinct_users,
                  s.kind, 'pending', s.evidence, now())
    """)  # noqa: F821
    display(result)  # noqa: F821 — MERGE metrics: num_inserted_rows = newly escalated
else:
    print("no escalation candidates this run")

# COMMAND ----------

# MAGIC %md ## 3 — Apply steward-approved decisions
# MAGIC Reads `approved` rows, executes the applier for its kind, records the payload,
# MAGIC and transitions the row to `applied` — plus a mirror row in the Delta audit
# MAGIC ledger so the dashboard's Healing Activity shows the human lane.

# COMMAND ----------

from genie_autopilot.healing import uc_comment_sql, uc_tag_sql  # noqa: E402


def _apply_mapping(term, entity):
    """below_gate_proposal: an approved term->metric mapping becomes UC metadata."""
    entity = (entity or "").strip("`")
    if "." not in entity:
        return f"no-op: entity '{entity}' is not table.column-shaped; steward should refine"
    left, column = entity.rsplit(".", 1)
    fq = f"{domain_schema}.{left.split('.')[-1]}"
    for stmt in (uc_comment_sql(fq, column, term), uc_tag_sql(fq, column, term)):
        spark.sql(stmt)  # noqa: F821
    return f"uc_comment+tag applied: '{term}' -> {fq}.{column}"


approved = spark.sql(  # noqa: F821
    f"SELECT id, kind, term, entity, proposal_key FROM {QUEUE} WHERE status = 'approved'"
).collect()

for row in approved:
    if row.kind == "below_gate_proposal":
        payload = _apply_mapping(row.term, row.entity)
    else:
        # poison_conflict / novel_term: the ruling itself is the artifact — glossary
        # or definition authoring flows through the certification workflow next.
        payload = f"steward ruling recorded for '{row.term}' ({row.kind})"
    safe_payload = payload.replace("'", "''")[:500]
    spark.sql(  # noqa: F821 — transition approved -> applied, keep the payload
        f"UPDATE {QUEUE} SET status = 'applied', applied_payload = '{safe_payload}' "
        f"WHERE id = {row.id} AND status = 'approved'"
    )
    term_safe = row.term.replace("'", "")
    key_safe = row.proposal_key.replace("'", "")
    spark.sql(  # noqa: F821 — mirror to the audit ledger (human lane)
        f"INSERT INTO {domain_schema}.autopilot_audit_ledger VALUES "
        f"(current_timestamp(), 'steward_applied', '{term_safe}', "
        f"'{key_safe}', '{safe_payload}', 'applied', 'steward_loop')"
    )
print(f"applied {len(approved)} steward-approved decision(s)")

# COMMAND ----------

final = spark.sql(f"SELECT status, COUNT(*) AS n FROM {QUEUE} GROUP BY status")  # noqa: F821
display(final)  # noqa: F821 — final queue posture for the run log
