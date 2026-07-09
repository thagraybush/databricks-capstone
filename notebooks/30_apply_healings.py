# Databricks notebook source
# MAGIC %md
# MAGIC # 30_apply_healings
# MAGIC Apply approved healings behind the governed gate.
# MAGIC
# MAGIC Flow: read pending proposals → `healing.triage` splits them at the confidence gate →
# MAGIC auto-approved proposals are applied to three surfaces (UC column comment, UC column
# MAGIC tag, Genie space column synonyms) → statuses are MERGEd back and every action lands
# MAGIC in `autopilot_audit_ledger`. Held proposals are printed as the HITL review queue.
# MAGIC
# MAGIC `dry_run=true` (default) prints every action and marks proposals `approved` without
# MAGIC touching UC or the Genie space; a later run with `dry_run=false` picks them up again.

# COMMAND ----------

import json
import os
import sys
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

from databricks.sdk import WorkspaceClient  # noqa: E402

import genie_autopilot.healing as healing  # noqa: E402
from genie_autopilot.drift import Proposal  # noqa: E402
from genie_autopilot.genie_api import GenieAPI  # noqa: E402

# COMMAND ----------

dbutils.widgets.text("domain_schema", "workspace.retail")
dbutils.widgets.text("space_id", os.environ.get("GA_GENIE_SPACE_ID", ""))
dbutils.widgets.text("auto_approve_confidence", "0.75")
dbutils.widgets.text("dry_run", "true")

domain_schema = dbutils.widgets.get("domain_schema").strip()
space_id = dbutils.widgets.get("space_id").strip()
auto_approve_confidence = float(dbutils.widgets.get("auto_approve_confidence"))
dry_run = dbutils.widgets.get("dry_run").strip().lower() in ("true", "1", "yes")

PROPOSALS_TABLE = f"{domain_schema}.autopilot_proposals"
AUDIT_TABLE = f"{domain_schema}.autopilot_audit_ledger"

print(f"dry_run={dry_run}  auto_approve_confidence={auto_approve_confidence}")

# COMMAND ----------

if not spark.catalog.tableExists(PROPOSALS_TABLE):
    print(f"{PROPOSALS_TABLE} does not exist yet — run 20_detect_drift first. Nothing to do.")
    dbutils.notebook.exit("skipped: no proposals table")

# 'approved' rows are dry-run-approved proposals still awaiting a live application,
# so a non-dry run picks up exactly what the dry run signed off on.
pending = [
    Proposal(
        term=r["term"],
        entity=r["entity"],
        confidence=float(r["confidence"]),
        distinct_users=int(r["distinct_users"]),
    )
    for r in spark.sql(
        f"""
        SELECT term, entity, confidence, distinct_users
        FROM {PROPOSALS_TABLE}
        WHERE status IN ('proposed', 'approved')
        """
    ).collect()
]
print(f"Pending proposals: {len(pending)}")

# COMMAND ----------

# MAGIC %md ## Triage at the governance gate

# COMMAND ----------

# triage() reads the module-level gate; align it with the widget before calling.
healing.AUTO_APPROVE_CONFIDENCE = auto_approve_confidence
auto_approved, needs_review = healing.triage(pending)
print(f"auto-approved: {len(auto_approved)}   held for review: {len(needs_review)}")


def resolve_entity(entity: str, schema: str) -> tuple[str, str] | None:
    """Resolve a proposal entity into (fq_table, column); None if not column-shaped.

    'col'                          -> None (nothing to anchor a UC action on)
    'table.col'                    -> '<domain_schema>.table', 'col'
    'schema.table.col'             -> '<catalog>.schema.table', 'col'
    'catalog.schema.table.col'     -> 'catalog.schema.table', 'col'
    """
    parts = [p.strip("`") for p in entity.split(".") if p.strip("`")]
    if len(parts) < 2:
        return None
    column = parts[-1]
    table_parts = parts[:-1]
    if len(table_parts) == 1:
        fq_table = f"{schema}.{table_parts[0]}"
    elif len(table_parts) == 2:
        fq_table = f"{schema.split('.')[0]}.{table_parts[0]}.{table_parts[1]}"
    else:
        fq_table = ".".join(table_parts[-3:])
    return fq_table, column

# COMMAND ----------

# MAGIC %md ## Apply auto-approved healings (UC comment + tag, then Genie synonyms)

# COMMAND ----------

applied: list[Proposal] = []          # at least one landed (or dry-run-planned) action
unresolvable: list[Proposal] = []     # auto-approved but entity is not table.column-shaped
failures: list[tuple[str, str, str]] = []   # (proposal_key, action, error)
audit_rows: list[tuple] = []
synonym_patches: list[tuple[str, str, str]] = []  # (fq_table, column, term)
approver = "auto"

for p in auto_approved:
    resolved = resolve_entity(p.entity, domain_schema)
    if not resolved:
        unresolvable.append(p)
        print(f"HOLD (unresolvable entity, needs a human): {p.term} -> {p.entity}")
        continue
    fq_table, column = resolved
    comment_sql = healing.uc_comment_sql(fq_table, column, p.term)
    tag_sql = healing.uc_tag_sql(fq_table, column, p.term)

    if dry_run:
        print(f"[dry-run] would execute: {comment_sql}")
        print(f"[dry-run] would execute: {tag_sql}")
        print(f"[dry-run] would add Genie synonym '{p.term}' to {fq_table}.{column}")
        applied.append(p)
        continue

    landed = False
    for action, sql_text in (("uc_comment", comment_sql), ("uc_tag", tag_sql)):
        try:
            spark.sql(sql_text)
            landed = True
            audit_rows.append(
                (healing.now(), action, f"{fq_table}.{column}", p.key, sql_text, "applied", approver)
            )
            print(f"applied {action}: {fq_table}.{column} <- '{p.term}'")
        except Exception as exc:
            failures.append((p.key, action, str(exc)))
            audit_rows.append(
                (healing.now(), action, f"{fq_table}.{column}", p.key, sql_text, "failed", approver)
            )
            print(f"WARNING: {action} failed for {fq_table}.{column}: {exc}")

    if landed:
        applied.append(p)
        synonym_patches.append((fq_table, column, p.term))

# COMMAND ----------

# Genie space column synonyms: fetch once, patch all, update once (etag-guarded).
if synonym_patches and not dry_run:
    if not space_id:
        print("WARNING: no space_id — skipping Genie space synonym patch.")
    else:
        try:
            w = WorkspaceClient()  # ambient in-workspace auth
            api = GenieAPI(w, space_id)
            resp = api.get_space()
            serialized = resp.get("serialized_space") or ""
            etag = resp.get("etag") or resp.get("space_etag") or None
            if not serialized:
                raise ValueError("space response contained no serialized_space")
            for fq_table, column, term in synonym_patches:
                serialized = healing.patch_space_column_synonyms(
                    serialized, fq_table, column, [term]
                )
            api.update_space(serialized, etag)
            payload = json.dumps(
                [{"table": t, "column": c, "synonym": s} for t, c, s in synonym_patches]
            )
            audit_rows.append(
                (healing.now(), "space_update", space_id, "batch", payload, "applied", approver)
            )
            print(f"Patched {len(synonym_patches)} column synonym(s) into Genie space {space_id}")
        except Exception as exc:
            print(f"WARNING: Genie space synonym update failed (kept UC changes): {exc}")
            audit_rows.append(
                (healing.now(), "space_update", space_id, "batch", str(exc), "failed", approver)
            )

# COMMAND ----------

# MAGIC %md ## Persist decisions: proposal statuses + audit ledger

# COMMAND ----------

new_status = "approved" if dry_run else "applied"
if applied:
    spark.createDataFrame(
        [(p.term, p.entity) for p in applied], "term STRING, entity STRING"
    ).createOrReplaceTempView("healing_decisions")
    spark.sql(
        f"""
        MERGE INTO {PROPOSALS_TABLE} AS t
        USING healing_decisions AS s
        ON t.term = s.term AND t.entity = s.entity
        WHEN MATCHED THEN UPDATE SET
          t.status = '{new_status}',
          t.updated_at = current_timestamp()
        """
    )
    print(f"Marked {len(applied)} proposal(s) as '{new_status}'")

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
      ts DOUBLE,
      action STRING,
      target STRING,
      proposal_key STRING,
      payload STRING,
      status STRING,
      approver STRING
    ) USING DELTA
    """
)
if audit_rows:
    spark.createDataFrame(
        audit_rows,
        "ts DOUBLE, action STRING, target STRING, proposal_key STRING, "
        "payload STRING, status STRING, approver STRING",
    ).write.mode("append").saveAsTable(AUDIT_TABLE)
    print(f"Appended {len(audit_rows)} record(s) to {AUDIT_TABLE}")

# COMMAND ----------

# MAGIC %md ## HITL review queue + action report

# COMMAND ----------

print("=== HITL review queue (status stays 'proposed') ===")
hitl = needs_review + unresolvable
if not hitl:
    print("(empty)")
for p in hitl:
    reason = "below gate" if p in needs_review else "entity not table.column-shaped"
    print(
        f"  {p.term!r} -> {p.entity!r}   confidence={p.confidence:.4f} "
        f"users={p.distinct_users}   [{reason}]"
    )

print()
print("=== 30_apply_healings action report ===")
print(f"mode:                {'DRY RUN' if dry_run else 'LIVE'}")
print(f"pending proposals:   {len(pending)}")
print(f"auto-approved:       {len(auto_approved)}")
print(f"applied/planned:     {len(applied)} (status -> '{new_status}')")
print(f"held for review:     {len(needs_review)}")
print(f"unresolvable:        {len(unresolvable)}")
print(f"failed actions:      {len(failures)}")
for key, action, err in failures:
    print(f"  FAIL {action} [{key}]: {err}")
