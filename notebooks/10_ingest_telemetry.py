# Databricks notebook source
# MAGIC %md
# MAGIC # 10_ingest_telemetry
# MAGIC Harvest Genie conversation telemetry into `{domain_schema}` Delta tables.
# MAGIC
# MAGIC Outputs:
# MAGIC - `autopilot_telemetry` — message-level interaction log (question, generated SQL,
# MAGIC   feedback where derivable). Rebuilt with `overwrite` each run: the Conversation API
# MAGIC   is the source of truth, so a full rebuild is idempotent by construction.
# MAGIC - `autopilot_corrections` — structured `term → entity` corrections mined by the
# MAGIC   deterministic parser, MERGEd on `source_message_id` so re-runs never duplicate.
# MAGIC
# MAGIC Auth is ambient (notebook/job context) — no PAT handling here.

# COMMAND ----------

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

from pyspark.sql import types as T  # noqa: E402
from databricks.sdk import WorkspaceClient  # noqa: E402

from genie_autopilot.genie_api import GenieAPI  # noqa: E402
from genie_autopilot.telemetry import harvest_corrections  # noqa: E402

# COMMAND ----------

dbutils.widgets.text("space_id", os.environ.get("GA_GENIE_SPACE_ID", ""))
dbutils.widgets.text("domain_schema", "workspace.retail")
dbutils.widgets.text("roles_json", "{}")  # JSON map: user id -> persona role

space_id = dbutils.widgets.get("space_id").strip()
domain_schema = dbutils.widgets.get("domain_schema").strip()
roles_by_user = {
    str(k): str(v)
    for k, v in json.loads(dbutils.widgets.get("roles_json") or "{}").items()
}

TELEMETRY_TABLE = f"{domain_schema}.autopilot_telemetry"
CORRECTIONS_TABLE = f"{domain_schema}.autopilot_corrections"

# COMMAND ----------

if not space_id:
    print(
        "No space_id provided — nothing to harvest. "
        "Set the space_id widget / job parameter to a Genie space id."
    )
    dbutils.notebook.exit("skipped: blank space_id")

# COMMAND ----------

# Ambient in-workspace auth (NOT config.workspace_client(), which is the local-PAT path).
w = WorkspaceClient()
api = GenieAPI(w, space_id)

# COMMAND ----------

# MAGIC %md ## Full interaction log → `autopilot_telemetry` (overwrite)

# COMMAND ----------

harvested_at = datetime.now(timezone.utc)
telemetry_rows = []
conversations = api.list_conversations()
for conv in conversations:
    conv_id = conv.get("conversation_id") or conv.get("id", "")
    if not conv_id:
        continue
    conv_user = str(conv.get("user_id", "unknown"))
    for msg in api.list_messages(conv_id):
        user = str(msg.get("user_id") or conv_user)
        sql_text = ""
        for att in msg.get("attachments") or []:
            query = att.get("query")
            if isinstance(query, dict) and query.get("query"):
                sql_text = query["query"]
        feedback = msg.get("feedback")
        rating = (
            str(feedback.get("rating", "")) if isinstance(feedback, dict) else str(feedback or "")
        )
        created_ms = msg.get("created_timestamp") or 0
        telemetry_rows.append(
            (
                conv_id,
                str(msg.get("message_id") or msg.get("id") or ""),
                user,
                roles_by_user.get(user, "unknown"),
                str(msg.get("content") or ""),
                sql_text,
                rating,
                datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc) if created_ms else None,
                harvested_at,
            )
        )

TELEMETRY_SCHEMA = T.StructType(
    [
        T.StructField("conversation_id", T.StringType()),
        T.StructField("message_id", T.StringType()),
        T.StructField("user_id", T.StringType()),
        T.StructField("role", T.StringType()),
        T.StructField("content", T.StringType()),
        T.StructField("sql", T.StringType()),
        T.StructField("feedback_rating", T.StringType()),  # derivable feedback, '' if absent
        T.StructField("created_ts", T.TimestampType()),
        T.StructField("harvested_at", T.TimestampType()),
    ]
)

telemetry_df = spark.createDataFrame(telemetry_rows, TELEMETRY_SCHEMA)
(
    telemetry_df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TELEMETRY_TABLE)
)
print(
    f"Wrote {len(telemetry_rows)} messages from {len(conversations)} conversations "
    f"to {TELEMETRY_TABLE}"
)

# COMMAND ----------

# MAGIC %md ## Mined corrections → `autopilot_corrections` (idempotent MERGE)

# COMMAND ----------

corrections = harvest_corrections(api, roles_by_user)

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {CORRECTIONS_TABLE} (
      term STRING,
      entity STRING,
      `user` STRING,
      role STRING,
      ts DOUBLE,
      source_message_id STRING
    ) USING DELTA
    """
)

CORRECTIONS_SCHEMA = T.StructType(
    [
        T.StructField("term", T.StringType()),
        T.StructField("entity", T.StringType()),
        T.StructField("user", T.StringType()),
        T.StructField("role", T.StringType()),
        T.StructField("ts", T.DoubleType()),
        T.StructField("source_message_id", T.StringType()),
    ]
)

batch_rows = [
    (c.term, c.entity, c.user, c.role, float(c.ts), c.source_message_id)
    for c in corrections
    if c.source_message_id  # MERGE key must be non-empty to stay idempotent
]
skipped_no_id = len(corrections) - len(batch_rows)

before = spark.table(CORRECTIONS_TABLE).count()
inserted = 0
if batch_rows:
    batch_df = spark.createDataFrame(batch_rows, CORRECTIONS_SCHEMA).dropDuplicates(
        ["source_message_id"]
    )
    batch_df.createOrReplaceTempView("corrections_batch")
    spark.sql(
        f"""
        MERGE INTO {CORRECTIONS_TABLE} AS t
        USING corrections_batch AS s
        ON t.source_message_id = s.source_message_id
        WHEN NOT MATCHED THEN INSERT
          (term, entity, `user`, role, ts, source_message_id)
        VALUES
          (s.term, s.entity, s.`user`, s.role, s.ts, s.source_message_id)
        """
    )
    inserted = spark.table(CORRECTIONS_TABLE).count() - before

# COMMAND ----------

# MAGIC %md ## Summary

# COMMAND ----------

print("=== 10_ingest_telemetry summary ===")
print(f"space_id:               {space_id}")
print(f"conversations walked:   {len(conversations)}")
print(f"messages harvested:     {len(telemetry_rows)}  -> {TELEMETRY_TABLE} (overwrite)")
print(f"corrections mined:      {len(corrections)}")
print(f"corrections inserted:   {inserted} new  -> {CORRECTIONS_TABLE} (MERGE)")
print(f"corrections total:      {before + inserted}")
if skipped_no_id:
    print(f"corrections skipped:    {skipped_no_id} (missing source_message_id)")
