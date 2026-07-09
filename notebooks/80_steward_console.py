# Databricks notebook source
# MAGIC %md
# MAGIC # 80 — Steward console (HITL review surface)
# MAGIC The metric steward's approve/reject surface over the Lakebase `hitl_queue`.
# MAGIC
# MAGIC **Non-blocking by design:** users' Genie sessions never wait on this queue.
# MAGIC Genie keeps answering from its *current* certified context while escalations
# MAGIC pend here; a steward decision only changes what the next daily-ops run applies.
# MAGIC
# MAGIC Usage (widgets, or Jobs UI parameter overrides on the `steward_console` job):
# MAGIC - `action=list` — show pending escalations with evidence and a suggested next step
# MAGIC - `action=approve ids=3,7 decided_by=you@corp.com` — approve queue rows 3 and 7
# MAGIC - `action=reject ids=5` — reject queue row 5
# MAGIC
# MAGIC Governance boundary: **decide ≠ deploy.** This notebook records decisions only;
# MAGIC approved items are APPLIED by the next daily-ops run (notebook 30 /
# MAGIC `steward.apply_approved`), which writes the application to the audit ledger.

# COMMAND ----------

# MAGIC %md ## Environment bootstrap
# MAGIC `psycopg` is a binary dependency: the bundle syncs source code, not packages, so
# MAGIC serverless notebooks must install it explicitly before the Lakebase connection.

# COMMAND ----------

# MAGIC %pip install psycopg[binary] --quiet

# COMMAND ----------

dbutils.library.restartPython()  # noqa: F821 — make the fresh package importable

# COMMAND ----------

import json
import sys
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

from genie_autopilot import lakebase  # noqa: E402

dbutils.widgets.dropdown("action", "list", ["list", "approve", "reject"])
dbutils.widgets.text("ids", "")  # comma-separated hitl_queue ids, e.g. "3,7"
dbutils.widgets.text("decided_by", "steward")

action = dbutils.widgets.get("action").strip().lower()
ids_raw = dbutils.widgets.get("ids").strip()
decided_by = dbutils.widgets.get("decided_by").strip() or "steward"

print(f"action={action}  ids={ids_raw!r}  decided_by={decided_by}")

# COMMAND ----------

# MAGIC %md ## Role gate (issue #18)
# MAGIC Only principals holding the `metric_steward` role in `workspace.retail.autopilot_roles`
# MAGIC may decide escalations. The registry is the application-tier RBAC source of truth;
# MAGIC decisions are attested in the audit trail as `human:<email>`.

# COMMAND ----------

from databricks.sdk import WorkspaceClient  # noqa: E402

_w_gate = WorkspaceClient()
_me = _w_gate.current_user.me().user_name
_stewards = {
    r[0]
    for r in spark.sql(  # noqa: F821
        "SELECT principal FROM workspace.retail.autopilot_roles "
        "WHERE role = 'metric_steward' AND revoked_at IS NULL"
    ).collect()
}
if _me not in _stewards:
    print(f"ACCESS DENIED: {_me} does not hold the metric_steward role.")
    print(f"Current stewards: {sorted(_stewards) or '(none assigned)'}")
    dbutils.notebook.exit("not-a-steward")  # noqa: F821
print(f"role gate passed: {_me} is a metric_steward")
decided_by = f"human:{_me}"  # attested identity overrides the widget


# COMMAND ----------

# MAGIC %md ## Connect to Lakebase (`hitl_queue`)
# MAGIC Host discovery via the Postgres REST surface, then a 1-hour OAuth credential
# MAGIC minted against the branch endpoint. The current user's email is the Postgres role.

# COMMAND ----------

from databricks.sdk import WorkspaceClient  # noqa: E402

PROJECT_ID = lakebase.DEFAULT_PROJECT_ID  # genie-autopilot
BRANCH = lakebase.DEFAULT_BRANCH  # production
ENDPOINT_NAME = f"projects/{PROJECT_ID}/branches/{BRANCH}/endpoints/primary"

try:
    import psycopg  # noqa: F401
except Exception as exc:
    print(f"psycopg is not installed in this environment ({exc}).")
    print("Run this notebook from the deployed bundle so the repo package deps are available.")
    dbutils.notebook.exit("skipped: psycopg unavailable")

w = WorkspaceClient()  # ambient in-workspace auth
user_email = w.current_user.me().user_name

host, token = None, None
try:
    # Endpoint host discovery.
    listing = (
        w.api_client.do(
            "GET", f"/api/2.0/postgres/projects/{PROJECT_ID}/branches/{BRANCH}/endpoints"
        )
        or {}
    )
    endpoints = listing.get("endpoints") or []
    if endpoints:
        host = endpoints[0].get("host")

    # Credential minting: typed SDK method when the installed SDK exposes it.
    generate = getattr(getattr(w, "postgres", None), "generate_database_credential", None)
    if callable(generate):
        cred = generate(endpoint=ENDPOINT_NAME)
        token = getattr(cred, "token", None) or (cred.get("token") if isinstance(cred, dict) else None)
    else:
        print("SDK lacks w.postgres.generate_database_credential — using REST/discovery fallback.")
except Exception as exc:
    print(f"Lakebase endpoint/credential discovery hiccup ({exc}) — trying full fallback.")

if not host or not token:
    # lakebase.get_credential does project-level discovery plus a raw-REST token fallback.
    try:
        host, token = lakebase.get_credential(w, PROJECT_ID, BRANCH)
    except Exception as exc:
        print(f"ERROR: could not reach Lakebase project {PROJECT_ID!r}: {exc}")
        print("Provision it first (lakebase.ensure_project) or check workspace permissions.")
        dbutils.notebook.exit("failed: no Lakebase credential")

conn = psycopg.connect(
    host=host,
    user=user_email,
    password=token,
    dbname=lakebase.DEFAULT_DBNAME,
    sslmode="require",
    autocommit=True,
)
lakebase.ensure_schema(conn)
print(f"connected: {user_email} @ {host} / {lakebase.DEFAULT_DBNAME}")

# COMMAND ----------

# MAGIC %md ## Run the requested action

# COMMAND ----------

# What approving each kind of escalation leads to on the next daily-ops run
# (kinds emitted by steward.build_escalations; unknown kinds get the generic line).
NEXT_STEP_BY_KIND = {
    "below_gate_proposal": "approve → daily ops applies the synonym healing that sat below the auto-gate",
    "poison_conflict": "approve → daily ops certifies the term despite the poison-list collision (review evidence!)",
    "novel_term": "approve → daily ops adds the new vocabulary (glossary/synonym) users keep asking for",
    "synonym": "approve → daily ops adds the metric-view / Genie column synonym",
    "glossary": "approve → daily ops adds the term to the Genie space glossary",
    "disambiguation": "approve → daily ops adds a space disambiguation instruction",
}


def _suggested_next_step(kind: str) -> str:
    return NEXT_STEP_BY_KIND.get(
        (kind or "").strip().lower(),
        f"approve → applied by the next daily-ops run (kind={kind or 'unknown'})",
    )


def _evidence_str(evidence) -> str:
    if evidence is None:
        return ""
    text = evidence if isinstance(evidence, str) else json.dumps(evidence, default=str)
    return text if len(text) <= 500 else text[:497] + "..."


def _parse_ids(raw: str) -> list[int]:
    parsed = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            parsed.append(int(tok))
        except ValueError:
            print(f"WARNING: skipping non-numeric id {tok!r}")
    return parsed


if action == "list":
    rows = lakebase.pending(conn)
    print(f"pending escalations: {len(rows)}")
    if rows:
        display_rows = [
            (
                int(r["id"]),
                r.get("kind"),
                r.get("term"),
                r.get("entity"),
                float(r["confidence"]) if r.get("confidence") is not None else None,
                int(r["distinct_users"]) if r.get("distinct_users") is not None else None,
                r.get("created_at"),
                _evidence_str(r.get("evidence")),
                _suggested_next_step(r.get("kind")),
            )
            for r in rows
        ]
        df = spark.createDataFrame(
            display_rows,
            "id LONG, kind STRING, term STRING, entity STRING, confidence DOUBLE, "
            "distinct_users LONG, created_at TIMESTAMP, evidence STRING, "
            "suggested_next_step STRING",
        )
        display(df)
        print("To decide: re-run this notebook with action=approve ids=3,7 (or action=reject).")
        print("Decisions are recorded here; application happens on the next daily-ops run.")
    else:
        print("(queue empty — nothing awaiting review)")

elif action in ("approve", "reject"):
    queue_ids = _parse_ids(ids_raw)
    if not queue_ids:
        print("No valid ids supplied — set the `ids` widget (e.g. ids=3,7) and re-run.")
        dbutils.notebook.exit("skipped: no ids")
    approved = action == "approve"
    for qid in queue_ids:
        lakebase.decide(conn, qid, approved=approved, decided_by=decided_by)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, decided_by, decided_at FROM hitl_queue WHERE id = %s", (qid,)
            )
            row = cur.fetchone()
        if row is None:
            print(f"  id {qid}: NOT FOUND in hitl_queue — no decision recorded")
        elif row[0] == ("approved" if approved else "rejected") and row[1] == decided_by:
            print(f"  id {qid}: {row[0]} by {row[1]} at {row[2]}")
        else:
            print(f"  id {qid}: already decided earlier (status={row[0]}, by={row[1]}) — unchanged")
    print()
    print("Reminder: decide ≠ deploy. Approved items are APPLIED by the next daily-ops run")
    print("(notebook 30 / steward.apply_approved); the audit ledger records the application.")

else:
    print(f"Unknown action {action!r} — expected list | approve | reject.")

conn.close()

# COMMAND ----------

# MAGIC %md ## How this maps to the paved-path story
# MAGIC
# MAGIC The autopilot proposes; the steward disposes. Every escalation in this queue is a
# MAGIC piece of vocabulary the system *observed* users needing but was not confident enough
# MAGIC to certify on its own. A steward approval here turns that observation into **certified
# MAGIC context** — synonyms, glossary terms, disambiguation instructions — which the next
# MAGIC daily-ops run applies to Unity Catalog and the Genie space, and records in
# MAGIC `autopilot_audit_ledger` with the approver's identity. Rejections are equally durable:
# MAGIC they stay in `hitl_queue` as precedent, so the same noisy proposal does not come back.
# MAGIC Decision and application stay separated on purpose — the human moment is cheap and
# MAGIC synchronous, the deployment is governed, batched, and audited.
