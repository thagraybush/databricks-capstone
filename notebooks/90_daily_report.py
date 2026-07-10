# Databricks notebook source
# MAGIC %md
# MAGIC # 90 — Daily KPI report
# MAGIC The self-healing ecosystem's daily standup: what it learned, what it escalated,
# MAGIC how accurate it is, and how much noise it deflected — computed over the last 24h
# MAGIC (7-day trend where cheap), written one row per KPI to
# MAGIC `{domain_schema}.autopilot_daily_report`, and printed as a 30-second digest.
# MAGIC
# MAGIC Every query is defensive: tables may not exist yet and column sets vary between
# MAGIC environments, so each KPI degrades to NULL with an honest `detail` note rather
# MAGIC than failing the run.


# COMMAND ----------

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

dbutils.widgets.text("domain_schema", "workspace.retail")
dbutils.widgets.text("report_date", "")  # blank = today (current_date())

domain_schema = dbutils.widgets.get("domain_schema").strip()
report_date_raw = dbutils.widgets.get("report_date").strip()

if report_date_raw:
    report_date = datetime.strptime(report_date_raw, "%Y-%m-%d").date()
else:
    report_date = spark.sql("SELECT current_date()").collect()[0][0]

# Window: the 24h ending at report_date's end-of-day, clamped to "now" for today —
# so a morning run reports on the last 24h and a backfill run reports on that day.
now_utc = datetime.now(timezone.utc)
day_end = datetime(report_date.year, report_date.month, report_date.day, tzinfo=timezone.utc)
day_end += timedelta(days=1)
window_end = min(now_utc, day_end)
window_start = window_end - timedelta(hours=24)
week_start = window_end - timedelta(days=7)

AUDIT_TABLE = f"{domain_schema}.autopilot_audit_ledger"
EVAL_TABLE = f"{domain_schema}.autopilot_eval_history"
TELEMETRY_TABLE = f"{domain_schema}.autopilot_telemetry"
ROUTER_TABLE = f"{domain_schema}.router_arm_results"
QUARANTINE_TABLE = f"{domain_schema}.quarantine_events"
REPORT_TABLE = f"{domain_schema}.autopilot_daily_report"

window_str = f"{window_start:%Y-%m-%d %H:%M}Z → {window_end:%Y-%m-%d %H:%M}Z"
print(f"report_date={report_date}  window: {window_str}")

# COMMAND ----------

# MAGIC %md ## Defensive query helpers
# MAGIC Timestamps are compared on epoch seconds (`CAST(col AS DOUBLE)`) so the same
# MAGIC predicate works for the audit ledger's DOUBLE `ts` and real TIMESTAMP columns,
# MAGIC independent of session timezone.

# COMMAND ----------

kpis: list[tuple[str, float | None, str]] = []  # (kpi, value, detail) in digest order


def add_kpi(name: str, value, detail: str = "") -> None:
    kpis.append((name, float(value) if value is not None else None, detail))


def table_columns(table: str) -> list[str]:
    """Column names, or [] when the table is missing/unreadable."""
    try:
        if not spark.catalog.tableExists(table):
            return []
        return [f.name for f in spark.table(table).schema.fields]
    except Exception as exc:
        print(f"WARNING: could not inspect {table}: {exc}")
        return []


def scalar(sql_text: str, default=None):
    """First column of the first row, or `default` on any failure."""
    try:
        rows = spark.sql(sql_text).collect()
        return rows[0][0] if rows else default
    except Exception as exc:
        print(f"WARNING: query failed ({str(exc).splitlines()[0][:160]})")
        return default


def in_window(col: str, start: datetime, end: datetime) -> str:
    """Epoch-seconds window predicate that tolerates DOUBLE or TIMESTAMP columns."""
    return (
        f"CAST({col} AS DOUBLE) >= {start.timestamp():.0f} "
        f"AND CAST({col} AS DOUBLE) < {end.timestamp():.0f}"
    )


# COMMAND ----------

# MAGIC %md ## Audit-ledger KPIs — definitions evolved, terms learned, healing activity

# COMMAND ----------

DEFINITION_ACTIONS = (
    "metric_view_synonyms",
    "space_glossary",
    "uc_comment_tag",
    "disambiguation_instruction",
)
audit_cols = table_columns(AUDIT_TABLE)

if audit_cols:
    actions_in = ", ".join(f"'{a}'" for a in DEFINITION_ACTIONS)
    evolved = scalar(
        f"SELECT COUNT(*) FROM {AUDIT_TABLE} "
        f"WHERE action IN ({actions_in}) AND {in_window('ts', window_start, window_end)}"
    )
    evolved_7d = scalar(
        f"SELECT COUNT(*) FROM {AUDIT_TABLE} "
        f"WHERE action IN ({actions_in}) AND {in_window('ts', week_start, window_end)}"
    )
    add_kpi(
        "definitions_evolved",
        evolved,
        f"ledger rows with action in {'/'.join(DEFINITION_ACTIONS)}; window {window_str}",
    )
    add_kpi("definitions_evolved_7d", evolved_7d, "same action set over the trailing 7 days")

    # Honest approximation: each distinct proposal_key applied through a space_* action
    # is one term the space learned. (Payload count-deltas would be exact but fragile.)
    new_terms = scalar(
        f"SELECT COUNT(DISTINCT proposal_key) FROM {AUDIT_TABLE} "
        f"WHERE action LIKE 'space%' AND {in_window('ts', window_start, window_end)}"
    )
    add_kpi(
        "new_terms_learned",
        new_terms,
        "method: distinct proposal_key with action LIKE 'space%' in window (approximation)",
    )

    try:
        approver_rows = spark.sql(
            f"SELECT COALESCE(approver, 'unknown') AS approver, COUNT(*) AS n "
            f"FROM {AUDIT_TABLE} WHERE {in_window('ts', window_start, window_end)} "
            f"GROUP BY approver ORDER BY n DESC"
        ).collect()
    except Exception as exc:
        print(f"WARNING: healing-by-approver query failed: {exc}")
        approver_rows = []
    if approver_rows:
        for r in approver_rows:
            add_kpi(f"healing_activity_{r['approver']}", r["n"], "ledger actions in window")
    else:
        add_kpi("healing_activity_total", 0, "no ledger actions in window")
else:
    print(f"→ {AUDIT_TABLE} missing — ledger KPIs recorded as NULL")
    add_kpi("definitions_evolved", None, "audit ledger unavailable")
    add_kpi("new_terms_learned", None, "audit ledger unavailable")

# COMMAND ----------

# MAGIC %md ## Steward-queue KPIs (Lakebase) — opened / decided / oldest pending
# MAGIC Same connection pattern as notebook 80, wrapped end-to-end: a scale-to-zero
# MAGIC Lakebase or a missing credential NULLs these three KPIs, never fails the report.

# COMMAND ----------

esc_opened = esc_decided = pending_age_days = None
lakebase_note = f"hitl_queue; window {window_str}"
try:
    import psycopg  # noqa: F401

    from databricks.sdk import WorkspaceClient  # noqa: E402

    from genie_autopilot import lakebase  # noqa: E402

    w = WorkspaceClient()  # ambient in-workspace auth
    user_email = w.current_user.me().user_name

    host, token = None, None
    listing = (
        w.api_client.do(
            "GET",
            f"/api/2.0/postgres/projects/{lakebase.DEFAULT_PROJECT_ID}"
            f"/branches/{lakebase.DEFAULT_BRANCH}/endpoints",
        )
        or {}
    )
    endpoints = listing.get("endpoints") or []
    if endpoints:
        host = endpoints[0].get("host")
    generate = getattr(getattr(w, "postgres", None), "generate_database_credential", None)
    if callable(generate):
        endpoint_name = (
            f"projects/{lakebase.DEFAULT_PROJECT_ID}"
            f"/branches/{lakebase.DEFAULT_BRANCH}/endpoints/primary"
        )
        cred = generate(endpoint=endpoint_name)
        token = getattr(cred, "token", None) or (cred.get("token") if isinstance(cred, dict) else None)
    if not host or not token:
        host, token = lakebase.get_credential(w, lakebase.DEFAULT_PROJECT_ID)

    conn = psycopg.connect(
        host=host,
        user=user_email,
        password=token,
        dbname=lakebase.DEFAULT_DBNAME,
        sslmode="require",
        autocommit=True,
    )
    try:
        lakebase.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM hitl_queue WHERE created_at >= %s AND created_at < %s",
                (window_start, window_end),
            )
            esc_opened = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM hitl_queue WHERE decided_at >= %s AND decided_at < %s",
                (window_start, window_end),
            )
            esc_decided = cur.fetchone()[0]
            cur.execute(
                "SELECT MAX(EXTRACT(EPOCH FROM (now() - created_at))) / 86400.0 "
                "FROM hitl_queue WHERE status = 'pending'"
            )
            pending_age_days = cur.fetchone()[0]
    finally:
        conn.close()
except Exception as exc:
    print(f"→ Lakebase unavailable ({str(exc).splitlines()[0][:160]}) — steward KPIs are NULL")
    lakebase_note = "Lakebase unavailable"

add_kpi("steward_escalations_opened", esc_opened, lakebase_note)
add_kpi("steward_escalations_decided", esc_decided, lakebase_note)
add_kpi(
    "steward_pending_age_max_days",
    pending_age_days,
    "oldest still-pending escalation" if pending_age_days is not None else lakebase_note,
)

# COMMAND ----------

# MAGIC %md ## Eval accuracy — latest per stratum, with delta vs previous run
# MAGIC Handles both eval-history shapes seen in this repo: `(stratum, correct, total,
# MAGIC recorded_at)` and `(accuracy, ts)` — absent columns degrade to sensible defaults.

# COMMAND ----------

eval_cols = table_columns(EVAL_TABLE)
if eval_cols:
    ts_col = "recorded_at" if "recorded_at" in eval_cols else "ts"
    acc_expr = "accuracy" if "accuracy" in eval_cols else "correct / total"
    stratum_expr = "stratum" if "stratum" in eval_cols else "'overall'"
    run_expr = "run_id" if "run_id" in eval_cols else "'unknown'"
    try:
        eval_rows = spark.sql(
            f"""
            WITH ranked AS (
              SELECT {stratum_expr} AS stratum, {acc_expr} AS accuracy,
                     {run_expr} AS run_id,
                     ROW_NUMBER() OVER (
                       PARTITION BY {stratum_expr} ORDER BY {ts_col} DESC
                     ) AS rn
              FROM {EVAL_TABLE}
            )
            SELECT stratum,
                   MAX(CASE WHEN rn = 1 THEN accuracy END) AS latest,
                   MAX(CASE WHEN rn = 2 THEN accuracy END) AS previous,
                   MAX(CASE WHEN rn = 1 THEN run_id END) AS run_id
            FROM ranked GROUP BY stratum ORDER BY stratum
            """
        ).collect()
    except Exception as exc:
        print(f"WARNING: eval accuracy query failed: {exc}")
        eval_rows = []
    for r in eval_rows:
        latest = float(r["latest"]) if r["latest"] is not None else None
        prev = float(r["previous"]) if r["previous"] is not None else None
        delta = f"delta vs prev {latest - prev:+.3f}" if (latest is not None and prev is not None) else "no previous run"
        add_kpi(f"eval_accuracy_latest_{r['stratum']}", latest, f"run_id={r['run_id']}; {delta}")
    if not eval_rows:
        add_kpi("eval_accuracy_latest_overall", None, "no eval runs recorded")
else:
    add_kpi("eval_accuracy_latest_overall", None, f"{EVAL_TABLE} unavailable")

# COMMAND ----------

# MAGIC %md ## Corpus, router arm, and quarantine KPIs

# COMMAND ----------

tel_cols = table_columns(TELEMETRY_TABLE)
if tel_cols:
    corpus = scalar(f"SELECT COUNT(*) FROM {TELEMETRY_TABLE}")
    add_kpi("corpus_size", corpus, "all rows in autopilot_telemetry")
    if "harvested_at" in tel_cols:
        growth = scalar(
            f"SELECT COUNT(*) FROM {TELEMETRY_TABLE} "
            f"WHERE {in_window('harvested_at', window_start, window_end)}"
        )
        add_kpi(
            "corpus_growth_24h",
            growth,
            "rows with harvested_at in window (table is harvest-overwritten, so this "
            "approximates growth by last-harvest recency)",
        )
    else:
        add_kpi("corpus_growth_24h", None, "no harvested_at column")
else:
    add_kpi("corpus_size", None, f"{TELEMETRY_TABLE} unavailable")
    add_kpi("corpus_growth_24h", None, f"{TELEMETRY_TABLE} unavailable")

router_cols = table_columns(ROUTER_TABLE)
if {"answerable", "arm_b_sent"}.issubset(router_cols):
    noise = scalar(
        f"SELECT SUM(CASE WHEN answerable = false AND arm_b_sent = false THEN 1 ELSE 0 END) "
        f"FROM {ROUTER_TABLE}"
    )
    pass_rate = scalar(
        f"SELECT AVG(CASE WHEN answerable = true THEN CAST(arm_b_sent AS DOUBLE) END) "
        f"FROM {ROUTER_TABLE}"
    )
    add_kpi("noise_deflected", noise, "unanswerable questions the router held back (latest arm run)")
    add_kpi("router_pass_rate", pass_rate, "share of answerable questions the router let through")
else:
    add_kpi("noise_deflected", None, f"{ROUTER_TABLE} absent or missing columns")
    add_kpi("router_pass_rate", None, f"{ROUTER_TABLE} absent or missing columns")

quarantine_cols = table_columns(QUARANTINE_TABLE)
if quarantine_cols:
    if "_ingested_at" in quarantine_cols:
        q_rows = scalar(
            f"SELECT COUNT(*) FROM {QUARANTINE_TABLE} "
            f"WHERE {in_window('_ingested_at', window_start, window_end)}"
        )
        add_kpi("quarantine_rows_24h", q_rows, f"_ingested_at in window {window_str}")
    else:
        q_rows = scalar(f"SELECT COUNT(*) FROM {QUARANTINE_TABLE}")
        add_kpi("quarantine_rows_24h", q_rows, "total rows (no _ingested_at column to window on)")
else:
    add_kpi("quarantine_rows_24h", None, f"{QUARANTINE_TABLE} unavailable")

# COMMAND ----------

# MAGIC %md ## Persist — one row per KPI, idempotent on (report_date, kpi)

# COMMAND ----------

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {REPORT_TABLE} (
      report_date DATE,
      kpi STRING,
      value DOUBLE,
      detail STRING
    ) USING DELTA
    """
)

report_rows = [(report_date, k, v, d) for k, v, d in kpis]
spark.createDataFrame(
    report_rows, "report_date DATE, kpi STRING, value DOUBLE, detail STRING"
).createOrReplaceTempView("kpi_rows")
spark.sql(
    f"""
    MERGE INTO {REPORT_TABLE} AS t
    USING kpi_rows AS s
    ON t.report_date = s.report_date AND t.kpi = s.kpi
    WHEN MATCHED THEN UPDATE SET t.value = s.value, t.detail = s.detail
    WHEN NOT MATCHED THEN INSERT (report_date, kpi, value, detail)
      VALUES (s.report_date, s.kpi, s.value, s.detail)
    """
)
print(f"MERGEd {len(report_rows)} KPI rows into {REPORT_TABLE} for {report_date}")

# COMMAND ----------

# MAGIC %md ## The 30-second digest

# COMMAND ----------


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}"


width = max(len(k) for k, _, _ in kpis)
print(f"# Autopilot daily report — {report_date}")
print(f"window: {window_str}")
print()
for name, value, detail in kpis:
    suffix = f"   ({detail})" if detail else ""
    print(f"- {name:<{width}}  {_fmt(value):>8}{suffix}")
print()
nulls = [k for k, v, _ in kpis if v is None]
if nulls:
    print(f"NULL KPIs (source unavailable): {', '.join(nulls)}")
else:
    print("All KPI sources reachable.")

# COMMAND ----------

# MAGIC %md ## Why the system reports on itself
# MAGIC
# MAGIC These KPIs are the flywheel's own OKRs. A self-healing ecosystem is only
# MAGIC trustworthy if it accounts for its learning the way a team member would in
# MAGIC standup: *what I changed* (definitions evolved, terms learned), *what I need a
# MAGIC human for* (escalations opened/decided, oldest pending), *how well I'm doing*
# MAGIC (eval accuracy and its delta), and *what I kept out* (noise deflected,
# MAGIC quarantine rows). One row per KPI per day in `autopilot_daily_report` makes the
# MAGIC learning curve itself a queryable dataset — the system's growth is measured with
# MAGIC the same rigor as the retail metrics it serves.
