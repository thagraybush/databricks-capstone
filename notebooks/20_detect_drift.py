# Databricks notebook source
# MAGIC %md
# MAGIC # 20_detect_drift
# MAGIC Score semantic-drift proposals from mined corrections.
# MAGIC
# MAGIC Two extraction passes feed the scorer:
# MAGIC 1. Deterministic corrections already persisted by `10_ingest_telemetry`.
# MAGIC 2. LLM pass (`ai_query`, best-effort) over negative-feedback telemetry the parser
# MAGIC    could not structure — degrades to a warning if AI Functions are unavailable.
# MAGIC
# MAGIC Output: `autopilot_proposals`, upserted on (term, entity). Existing decisions
# MAGIC (`approved` / `applied` / `rejected`) are preserved; only scores refresh.

# COMMAND ----------

import json
import sys
import time
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

from genie_autopilot.drift import Correction, parse_correction, score_proposals  # noqa: E402

# COMMAND ----------

dbutils.widgets.text("domain_schema", "workspace.retail")
dbutils.widgets.text("min_distinct_users", "2")

domain_schema = dbutils.widgets.get("domain_schema").strip()
min_distinct_users = int(dbutils.widgets.get("min_distinct_users"))

TELEMETRY_TABLE = f"{domain_schema}.autopilot_telemetry"
CORRECTIONS_TABLE = f"{domain_schema}.autopilot_corrections"
PROPOSALS_TABLE = f"{domain_schema}.autopilot_proposals"

# COMMAND ----------

# MAGIC %md ## Pass 1 — load deterministic corrections

# COMMAND ----------

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {CORRECTIONS_TABLE} (
      term STRING, entity STRING, `user` STRING, role STRING,
      ts DOUBLE, source_message_id STRING
    ) USING DELTA
    """
)

corrections = [
    Correction(
        term=r["term"],
        entity=r["entity"],
        user=r["user"],
        role=r["role"] or "unknown",
        ts=float(r["ts"] or 0.0),
        source_message_id=r["source_message_id"] or "",
    )
    for r in spark.sql(
        f"SELECT term, entity, `user`, role, ts, source_message_id FROM {CORRECTIONS_TABLE}"
    ).collect()
]
print(f"Loaded {len(corrections)} deterministic corrections from {CORRECTIONS_TABLE}")

# COMMAND ----------

# MAGIC %md ## Pass 2 — LLM extraction over unparsed negative feedback (best-effort)

# COMMAND ----------

# ai_query over a temp view of candidates; plain string so the JSON braces are literal.
AI_EXTRACT_OVER_VIEW_SQL = """
SELECT
  message_id,
  user_id,
  role,
  ai_query(
    'databricks-gpt-oss-120b',
    CONCAT(
      'Extract the business term and the physical column or table it should map to ',
      'from this BI feedback. Reply ONLY with compact JSON like ',
      '{"term": "...", "entity": "..."} or the word null if no mapping is present. ',
      'Feedback: ',
      content
    )
  ) AS extracted
FROM drift_llm_candidates
"""

llm_corrections: list[Correction] = []
if spark.catalog.tableExists(TELEMETRY_TABLE):
    candidate_rows = spark.sql(
        f"""
        SELECT t.message_id, t.content, t.user_id, t.role
        FROM {TELEMETRY_TABLE} t
        LEFT ANTI JOIN {CORRECTIONS_TABLE} c
          ON t.message_id = c.source_message_id
        WHERE upper(coalesce(t.feedback_rating, '')) = 'NEGATIVE'
          AND coalesce(t.content, '') != ''
        """
    ).collect()
    unparsed = [r for r in candidate_rows if parse_correction(r["content"]) is None]
    print(
        f"Negative-feedback candidates: {len(candidate_rows)} "
        f"({len(unparsed)} unparsed -> LLM pass)"
    )
    if unparsed:
        spark.createDataFrame(
            [(r["message_id"], r["content"], r["user_id"], r["role"]) for r in unparsed],
            "message_id STRING, content STRING, user_id STRING, role STRING",
        ).createOrReplaceTempView("drift_llm_candidates")
        try:
            extracted_rows = spark.sql(AI_EXTRACT_OVER_VIEW_SQL).collect()
            now_ts = time.time()
            for r in extracted_rows:
                raw = (r["extracted"] or "").strip()
                start, end = raw.find("{"), raw.rfind("}")
                if start < 0 or end <= start:
                    continue
                try:
                    obj = json.loads(raw[start : end + 1])
                except (json.JSONDecodeError, ValueError):
                    continue
                term = str(obj.get("term") or "").strip().lower()
                entity = str(obj.get("entity") or "").strip().strip("`").lower()
                if not term or not entity or term == entity:
                    continue
                llm_corrections.append(
                    Correction(
                        term=term,
                        entity=entity,
                        user=str(r["user_id"]),
                        role=str(r["role"] or "unknown"),
                        ts=now_ts,
                        source_message_id=str(r["message_id"]),
                    )
                )
            print(f"LLM pass extracted {len(llm_corrections)} additional candidate corrections")
        except Exception as exc:
            print(
                "WARNING: ai_query LLM extraction unavailable "
                f"(model/function not reachable: {exc}). "
                "Continuing with parser-only corrections."
            )
else:
    print(f"WARNING: {TELEMETRY_TABLE} not found; skipping LLM pass (run 10_ingest_telemetry first).")

# COMMAND ----------

# MAGIC %md ## Score and upsert proposals

# COMMAND ----------

proposals = score_proposals(corrections + llm_corrections, min_distinct_users=min_distinct_users)
print(f"Scored {len(proposals)} proposals (min_distinct_users={min_distinct_users})")

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {PROPOSALS_TABLE} (
      term STRING,
      entity STRING,
      confidence DOUBLE,
      distinct_users INT,
      evidence_count INT,
      status STRING,
      updated_at TIMESTAMP
    ) USING DELTA
    """
)

if proposals:
    spark.createDataFrame(
        [
            (p.term, p.entity, float(p.confidence), int(p.distinct_users), len(p.evidence))
            for p in proposals
        ],
        "term STRING, entity STRING, confidence DOUBLE, distinct_users INT, evidence_count INT",
    ).createOrReplaceTempView("proposals_batch")
    # Status is intentionally NOT touched on match: already-decided proposals
    # (approved/applied/rejected) keep their decision; only the scores refresh.
    spark.sql(
        f"""
        MERGE INTO {PROPOSALS_TABLE} AS t
        USING proposals_batch AS s
        ON t.term = s.term AND t.entity = s.entity
        WHEN MATCHED THEN UPDATE SET
          t.confidence = s.confidence,
          t.distinct_users = s.distinct_users,
          t.evidence_count = s.evidence_count,
          t.updated_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT
          (term, entity, confidence, distinct_users, evidence_count, status, updated_at)
        VALUES
          (s.term, s.entity, s.confidence, s.distinct_users, s.evidence_count,
           'proposed', current_timestamp())
        """
    )
else:
    print("No corrections to score; proposals table left unchanged.")

# COMMAND ----------

# MAGIC %md ## Scored proposal table

# COMMAND ----------

print("=== 20_detect_drift summary ===")
print(f"deterministic corrections: {len(corrections)}")
print(f"llm-extracted corrections: {len(llm_corrections)}")
print(f"proposals scored:          {len(proposals)}")
spark.sql(
    f"""
    SELECT term, entity, confidence, distinct_users, evidence_count, status, updated_at
    FROM {PROPOSALS_TABLE}
    ORDER BY confidence DESC
    """
).show(50, truncate=False)
