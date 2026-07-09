# Databricks notebook source
# MAGIC %md
# MAGIC # 40_run_benchmarks
# MAGIC Run a Genie benchmark eval and record accuracy in `autopilot_eval_history`.
# MAGIC
# MAGIC The `phase` widget labels the run (e.g. `baseline`, `post_healing`, `adhoc`) so
# MAGIC before/after comparisons across healing cycles are queryable. If the space has no
# MAGIC benchmarks configured yet, the notebook prints a warning and exits cleanly —
# MAGIC it never fails the flywheel job.

# COMMAND ----------

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

from databricks.sdk import WorkspaceClient  # noqa: E402

from genie_autopilot.evals import BenchmarkRunner  # noqa: E402
from genie_autopilot.genie_api import GenieAPI  # noqa: E402

# COMMAND ----------

dbutils.widgets.text("space_id", os.environ.get("GA_GENIE_SPACE_ID", ""))
dbutils.widgets.text("domain_schema", "workspace.retail")
dbutils.widgets.text("phase", "adhoc")  # e.g. baseline | post_healing | adhoc

space_id = dbutils.widgets.get("space_id").strip()
domain_schema = dbutils.widgets.get("domain_schema").strip()
phase = dbutils.widgets.get("phase").strip() or "adhoc"

EVAL_TABLE = f"{domain_schema}.autopilot_eval_history"

# COMMAND ----------

if not space_id:
    print(
        "No space_id provided — nothing to benchmark. "
        "Set the space_id widget / job parameter to a Genie space id."
    )
    dbutils.notebook.exit("skipped: blank space_id")

# COMMAND ----------

# MAGIC %md ## Run the benchmark eval (best-effort)

# COMMAND ----------

w = WorkspaceClient()  # ambient in-workspace auth
runner = BenchmarkRunner(GenieAPI(w, space_id))

summary = None
try:
    run_id = runner.start_run()
    if not run_id:
        raise RuntimeError("eval-runs endpoint returned no run id")
    print(f"Started eval run {run_id} (phase={phase}); waiting for completion...")
    final = runner.wait(run_id)
    print(f"Eval run finished with state: {final.get('state')}")
    summary = runner.results(run_id)
except Exception as exc:
    print("WARNING: could not execute a benchmark eval run.")
    print(f"  Reason: {exc}")
    print(
        "  Most likely the Genie space has no benchmark questions configured yet "
        "(Space settings -> Benchmarks). Add benchmarks, then re-run this notebook."
    )
    dbutils.notebook.exit("skipped: benchmarks unavailable")

# COMMAND ----------

# MAGIC %md ## Record accuracy in `autopilot_eval_history`

# COMMAND ----------

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {EVAL_TABLE} (
      run_id STRING,
      phase STRING,
      ts TIMESTAMP,
      total INT,
      good INT,
      bad INT,
      manual_review INT,
      accuracy DOUBLE
    ) USING DELTA
    """
)

# Snapshot the previous most-recent row BEFORE appending, for the delta print.
previous = spark.sql(
    f"SELECT phase, accuracy FROM {EVAL_TABLE} ORDER BY ts DESC LIMIT 1"
).collect()

spark.createDataFrame(
    [
        (
            summary.run_id,
            phase,
            datetime.now(timezone.utc),
            int(summary.total),
            int(summary.good),
            int(summary.bad),
            int(summary.manual_review),
            float(summary.accuracy),
        )
    ],
    "run_id STRING, phase STRING, ts TIMESTAMP, total INT, good INT, bad INT, "
    "manual_review INT, accuracy DOUBLE",
).write.mode("append").saveAsTable(EVAL_TABLE)

# COMMAND ----------

print("=== 40_run_benchmarks summary ===")
print(f"run_id:        {summary.run_id}")
print(f"phase:         {phase}")
print(
    f"results:       total={summary.total} good={summary.good} "
    f"bad={summary.bad} manual_review={summary.manual_review}"
)
print(f"accuracy:      {summary.accuracy:.4f}")
if previous:
    prev_phase, prev_acc = previous[0]["phase"], float(previous[0]["accuracy"])
    delta = summary.accuracy - prev_acc
    trend = "(improved)" if delta > 0 else "(regressed)" if delta < 0 else "(flat)"
    print(f"previous:      {prev_acc:.4f} (phase={prev_phase})")
    print(f"delta:         {delta:+.4f} {trend}")
else:
    print("previous:      none (first recorded eval run)")
