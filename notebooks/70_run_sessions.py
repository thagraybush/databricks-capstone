# Databricks notebook source
# MAGIC %md
# MAGIC # 70 — Nightly persona sessions
# MAGIC Runs a paced batch of multi-turn persona sessions against the retail Genie space
# MAGIC (real Conversation-API traffic: questions, feedback, corrections), then persists
# MAGIC the persona-attribution manifest to Delta. The downstream `ingest_telemetry`
# MAGIC task harvests the full conversation history into `autopilot_telemetry`.
# MAGIC
# MAGIC Fair-use posture: paced ≤5 questions/min by the shared RateLimiter; the
# MAGIC `max_questions` widget caps a night's spend.

# COMMAND ----------

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

dbutils.widgets.text("space_id", os.environ.get("GA_GENIE_SPACE_ID", ""))  # noqa: F821
dbutils.widgets.text("domain_schema", "workspace.retail")  # noqa: F821
dbutils.widgets.text("sessions", "10")  # noqa: F821
dbutils.widgets.text("max_questions", "60")  # noqa: F821

space_id = dbutils.widgets.get("space_id")  # noqa: F821
domain_schema = dbutils.widgets.get("domain_schema")  # noqa: F821
n_sessions = int(dbutils.widgets.get("sessions"))  # noqa: F821
max_questions = int(dbutils.widgets.get("max_questions"))  # noqa: F821

if not space_id:
    print("space_id widget is blank — nothing to do.")
    dbutils.notebook.exit("skipped")  # noqa: F821

# COMMAND ----------

from databricks.sdk import WorkspaceClient  # noqa: E402

from genie_autopilot.genie_api import GenieAPI  # noqa: E402
from genie_autopilot.session_engine import run_sessions  # noqa: E402

w = WorkspaceClient()  # ambient in-workspace auth
api = GenieAPI(w, space_id)

# Vary the seed by date so every night phrases intents differently.
seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
records = run_sessions(
    api, n_sessions=n_sessions, seed=seed, max_questions=max_questions, write_manifest=False
)
ok = sum(1 for r in records if r["rated"] == "POSITIVE")
print(
    f"sessions complete: {len({r['session_id'] for r in records})} sessions, "
    f"{len(records)} interactions, {ok} positive, "
    f"{sum(1 for r in records if r['correction'])} corrections"
)

# COMMAND ----------

# Persist the persona-attribution manifest (append-only) — the honest identity story
# on Free Edition's single PAT: personas are attributed here, joined to telemetry by
# conversation_id.
if records:
    df = spark.createDataFrame(records)  # noqa: F821
    df.write.mode("append").option("mergeSchema", "true").saveAsTable(
        f"{domain_schema}.autopilot_session_manifest"
    )
    print(f"manifest appended: {len(records)} rows → {domain_schema}.autopilot_session_manifest")
