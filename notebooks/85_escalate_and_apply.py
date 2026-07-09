# Databricks notebook source
# MAGIC %md
# MAGIC # 85 — Escalate & apply (the steward loop's system half)
# MAGIC Runs daily BEFORE the report: mines the telemetry corpus for below-gate proposals,
# MAGIC poison conflicts, and NOVEL terms → enqueues them to the Lakebase steward queue
# MAGIC (idempotent), then APPLIES any decisions the steward approved since the last run
# MAGIC through the governed appliers. Decide ≠ deploy: humans rule in notebook 80;
# MAGIC this notebook is the only thing that executes those rulings.

# COMMAND ----------

# MAGIC %md ## Environment bootstrap
# MAGIC `psycopg` is a binary dependency: the bundle syncs source code, not packages, so
# MAGIC serverless notebooks must install it explicitly before the Lakebase connection.

# COMMAND ----------

# MAGIC %pip install psycopg[binary] --quiet

# COMMAND ----------

dbutils.library.restartPython()  # noqa: F821 — make the fresh package importable

# COMMAND ----------

import sys
from pathlib import Path

sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

dbutils.widgets.text("domain_schema", "workspace.retail")  # noqa: F821
domain_schema = dbutils.widgets.get("domain_schema")  # noqa: F821

# COMMAND ----------

# --- connect to the Lakebase steward queue (same path as notebook 80) ---------
from databricks.sdk import WorkspaceClient  # noqa: E402

from genie_autopilot import drift, lakebase, steward  # noqa: E402

w = WorkspaceClient()
try:
    import psycopg  # noqa: F401

    host, token = lakebase.get_credential(w, "genie-autopilot")
    conn = lakebase.connect(host, token, w.current_user.me().user_name)
    conn.autocommit = True
    lakebase.ensure_schema(conn)
    print(f"steward queue connected: {host}")
except Exception as exc:
    print(f"→ Lakebase unavailable ({exc}); escalation skipped this run")
    dbutils.notebook.exit("lakebase-unavailable")  # noqa: F821

# COMMAND ----------

# --- 1. mine telemetry: corrections → proposals; questions → novel terms ------
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
print(f"{len(corrections)} corrections → {len(proposals)} proposals, "
      f"{len(conflicts)} conflicts, {len(novel)} novel terms")

# COMMAND ----------

# --- 2. escalate (idempotent: pending keys are skipped) ------------------------
from genie_autopilot.healing import triage  # noqa: E402

auto, _review = triage([p for p in proposals if p.term not in conflicts])
escalations = steward.build_escalations(
    proposals=proposals, conflicts=conflicts, novel_terms=novel,
    auto_approved_keys={p.key for p in auto},
)
enqueued = steward.escalate(conn, escalations)
print(f"{len(escalations)} escalation candidates → {enqueued} newly enqueued")

# COMMAND ----------

# --- 3. apply approved decisions through governed appliers --------------------
# Appliers translate a steward-approved queue row into a workspace change and
# return the payload string recorded in healing_history + the audit ledger.
from genie_autopilot.healing import uc_comment_sql, uc_tag_sql  # noqa: E402


def _apply_mapping(row: dict) -> str:
    """below_gate_proposal: steward approved a term→entity mapping → UC metadata."""
    entity = (row.get("entity") or "").strip("`")
    term = row.get("term") or ""
    if "." not in entity:
        return f"no-op: entity '{entity}' is not table.column-shaped; steward should refine"
    left, column = entity.rsplit(".", 1)
    table = left.split(".")[-1]
    fq = f"{domain_schema}.{table}"
    for stmt in (uc_comment_sql(fq, column, term), uc_tag_sql(fq, column, term)):
        spark.sql(stmt)  # noqa: F821
    return f"uc_comment+tag applied: '{term}' → {fq}.{column}"


def _apply_note_only(row: dict) -> str:
    """poison_conflict / novel_term: the ruling itself is the artifact — glossary
    authoring goes through the next healing batch; here we record the decision."""
    return f"steward ruling recorded for '{row.get('term')}' ({row.get('kind')})"


applied = steward.apply_approved(conn, {
    "below_gate_proposal": _apply_mapping,
    "poison_conflict": _apply_note_only,
    "novel_term": _apply_note_only,
})
ledger_rows = 0
for row in applied:
    spark.sql(  # noqa: F821  — mirror to the Delta audit ledger
        f"INSERT INTO {domain_schema}.autopilot_audit_ledger VALUES "
        f"(current_timestamp(), 'steward_applied', '{row.get('term', '')}', "
        f"'{(row.get('proposal_key') or '').replace(chr(39), '')}', "
        f"'{(row.get('applied_payload') or '')[:500].replace(chr(39), '')}', "
        f"'applied', 'steward_loop')"
    )
    ledger_rows += 1
print(f"applied {len(applied)} approved decisions; {ledger_rows} audit rows written")
conn.close()
