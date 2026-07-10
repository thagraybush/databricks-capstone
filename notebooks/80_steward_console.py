# Databricks notebook source
# MAGIC %md
# MAGIC # 🧭 Steward Review Engine
# MAGIC
# MAGIC **You are the metric steward.** This notebook is your complete review surface for
# MAGIC the semantic escalations the autonomous system cannot — and must not — decide alone.
# MAGIC
# MAGIC ## The business process (read once)
# MAGIC
# MAGIC The self-healing semantic layer runs continuously: user questions and feedback flow
# MAGIC in as telemetry, drift detection scores candidate term→metric mappings, and anything
# MAGIC that clears the confidence gate (≥ 0.75, ≥ 2 independent reporters) heals
# MAGIC automatically behind a benchmark regression gate. Everything else lands **here**:
# MAGIC
# MAGIC | Escalation kind | What it means | Your decision |
# MAGIC |---|---|---|
# MAGIC | `below_gate_proposal` | A term→metric mapping reported by too few users or with low confidence | Approve the mapping, or reject it |
# MAGIC | `poison_conflict` | One term means DIFFERENT things to different teams (e.g. 'sales') | Approve = Genie must ask which meaning; never auto-map |
# MAGIC | `novel_term` | Vocabulary users keep saying that has no governed definition | Approve = worth defining (then author the definition); reject = noise |
# MAGIC
# MAGIC **Decide ≠ deploy.** Approving here changes *nothing* immediately. The next
# MAGIC `daily_ops` run applies your approvals through the same benchmark-gated,
# MAGIC audit-logged appliers as every autonomous healing. You are an input to
# MAGIC governance, not a bypass of it. Users are never blocked by this queue —
# MAGIC Genie keeps answering from its current certified context while items pend.
# MAGIC
# MAGIC ## How to run a review session
# MAGIC 1. **Run All** — the notebook gates your role, then shows the queue, charts, and evidence.
# MAGIC 2. Read the *Decisions needed* docket; study evidence for anything unclear.
# MAGIC 3. Record rulings in the `DECISIONS` cell (edit the dict), run it and the apply cell.
# MAGIC 4. The verification cell confirms the queue moved; the audit trail carries your
# MAGIC    attested identity automatically.

# COMMAND ----------

# MAGIC %md ## Step 0 — Role gate
# MAGIC Only principals holding `metric_steward` in `workspace.retail.autopilot_roles`
# MAGIC may record decisions. The registry — not this notebook — is the source of truth
# MAGIC for who stewards are; decisions are attested as `human:<your-email>` regardless
# MAGIC of any manual input, so the audit trail always carries a verified identity.

# COMMAND ----------

from databricks.sdk import WorkspaceClient

ME = WorkspaceClient().current_user.me().user_name
_stewards = {
    r[0]
    for r in spark.sql(  # noqa: F821 — spark is ambient in notebooks
        "SELECT principal FROM workspace.retail.autopilot_roles "
        "WHERE role = 'metric_steward' AND revoked_at IS NULL"
    ).collect()
}
if ME not in _stewards:
    print(f"ACCESS DENIED: {ME} does not hold the metric_steward role.")
    print(f"Current stewards: {sorted(_stewards) or '(none assigned)'}")
    dbutils.notebook.exit("not-a-steward")  # noqa: F821
DECIDED_BY = f"human:{ME}"  # attested identity — used for every ruling below
print(f"✓ role gate passed — reviewing as {DECIDED_BY}")

# COMMAND ----------

# MAGIC %md ## Step 1 — Queue at a glance
# MAGIC SQL is a first-class language here: the same queue any analyst could query.
# MAGIC (`autopilot_escalations` is Delta — the in-workspace system of record. A Lakebase
# MAGIC Postgres mirror serves laptop tooling; see `docs/runbook.md` for why Delta is
# MAGIC SoR on Free Edition serverless — two real OOM incidents with binary drivers.)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT status, kind, COUNT(*) AS n,
# MAGIC        ROUND(AVG(confidence), 2)                    AS avg_confidence,
# MAGIC        MIN(created_at)                              AS oldest,
# MAGIC        MAX(datediff(now(), created_at))             AS max_age_days
# MAGIC FROM workspace.retail.autopilot_escalations
# MAGIC GROUP BY status, kind
# MAGIC ORDER BY status, n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Queue-health rule of thumb:** pending age > 7 days means the steward cadence is
# MAGIC too slow for the rate of business-language drift — review more often or add
# MAGIC stewards (see *Scaling* at the bottom). The daily report tracks this as a KPI.

# COMMAND ----------

# MAGIC %md ## Step 2 — Visual review
# MAGIC Native `display()` charts — click the chart icon on any result to re-pivot.
# MAGIC This is a notebook: make it yours.

# COMMAND ----------

pending = spark.sql(  # noqa: F821
    """
    SELECT id, kind, term, entity, confidence, distinct_users, evidence,
           datediff(now(), created_at) AS age_days
    FROM workspace.retail.autopilot_escalations
    WHERE status = 'pending'
    ORDER BY kind, confidence DESC
    """
)
kind_counts = {r["kind"]: r["count"] for r in pending.groupBy("kind").count().collect()}
print(f"{sum(kind_counts.values())} pending escalations")

# Render the mix as an inline chart (no clicks needed) — matplotlib ships on serverless.
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(7, 3))
ax.barh(list(kind_counts.keys()), list(kind_counts.values()), color="#2e7d64")
ax.set_title("Pending escalations by kind")
ax.set_xlabel("count")
for i, v in enumerate(kind_counts.values()):
    ax.text(v, i, f" {v}", va="center")
plt.tight_layout()
plt.show()

# COMMAND ----------

# Novel terms ranked by how often users actually said them. Frequency is the
# business signal: a term said 7 times is vocabulary; a term said once is noise.
# get_json_object extracts natively from the evidence JSON — no UDF, no warnings.
novel = spark.sql(  # noqa: F821
    """
    SELECT id, term,
           CAST(get_json_object(evidence, '$.occurrences') AS INT) AS occurrences,
           distinct_users, datediff(now(), created_at) AS age_days
    FROM workspace.retail.autopilot_escalations
    WHERE status = 'pending' AND kind = 'novel_term'
    ORDER BY occurrences DESC
    """
)
rows = novel.collect()
if rows:
    import matplotlib.pyplot as plt

    top = rows[:12][::-1]  # top terms, reversed so the biggest bar sits on top
    fig, ax = plt.subplots(figsize=(8, max(3, 0.35 * len(top))))
    ax.barh([r.term for r in top], [r.occurrences or 0 for r in top], color="#5b7fb5")
    ax.set_title("Novel terms by how often users said them")
    ax.set_xlabel("occurrences in failed/corrected questions")
    plt.tight_layout()
    plt.show()
display(novel)  # noqa: F821 — full sortable table beneath the chart

# COMMAND ----------

# MAGIC %md ## Step 3 — Decisions needed (the docket, with evidence)
# MAGIC Read each row: the suggested action derives from its kind; `evidence` shows *why*
# MAGIC it escalated (who reported it, how often, what conflicted). Poison conflicts sort
# MAGIC first — ambiguity is the costliest failure mode to leave unruled.

# COMMAND ----------

docket = spark.sql(  # noqa: F821
    """
    SELECT id, kind, term, entity, ROUND(confidence, 2) AS confidence, distinct_users,
           CASE kind
             WHEN 'below_gate_proposal' THEN 'approve/reject this term→metric mapping'
             WHEN 'poison_conflict'     THEN 'approve = clarify-first rule (never auto-map)'
             WHEN 'novel_term'          THEN 'approve = define & map next; reject = noise'
             ELSE 'review'
           END AS your_decision,
           evidence
    FROM workspace.retail.autopilot_escalations
    WHERE status = 'pending'
    ORDER BY CASE kind WHEN 'poison_conflict' THEN 0
                       WHEN 'below_gate_proposal' THEN 1 ELSE 2 END,
             confidence DESC
    """
)
display(docket)  # noqa: F821

# COMMAND ----------

# MAGIC %md ## Step 3.5 — AI-drafted recommendations (you still decide)
# MAGIC The platform's own LLM (`ai_query` on serverless) triages every pending item and
# MAGIC drafts a ready-to-paste `DECISIONS` dict with a one-line rationale per ruling.
# MAGIC **The draft is triage, not judgment** — copy it into Step 4 and flip anything
# MAGIC you disagree with; only your edited dict gets recorded, under your identity.

# COMMAND ----------

try:
    drafted = spark.sql(  # noqa: F821 — one LLM call per pending row, serverless-native
        """
        SELECT id, kind, term,
               ai_query('databricks-gpt-oss-120b', CONCAT(
                 'You triage data-governance escalations. Reply EXACTLY as ',
                 'approve|<max 8 word reason> or reject|<max 8 word reason>. Policy: ',
                 'below_gate_proposal: approve only if the term plausibly means that metric. ',
                 'poison_conflict: approve (clarify-first is the correct ruling). ',
                 'novel_term: approve ONLY genuine business vocabulary worth defining; ',
                 'reject greetings, sentence fragments, typos, and generic words. ',
                 'Item: kind=', kind, ' term=', term,
                 ' entity=', COALESCE(entity, 'n/a'),
                 ' evidence=', COALESCE(evidence, '{}')
               )) AS draft
        FROM workspace.retail.autopilot_escalations
        WHERE status = 'pending'
        ORDER BY id
        """
    ).collect()
    print("AI-drafted ruling sheet — copy into Step 4 and edit:\n")
    print("DECISIONS = {")
    for r in drafted:
        rec, _, why = (r.draft or "reject|no draft returned").partition("|")
        rec = "approve" if "approve" in rec.lower() else "reject"
        print(f'    {r.id}: "{rec}",  # [{r.kind}] {r.term!r} — {why.strip()[:70]}')
    print("}")
except Exception as exc:
    print(f"→ AI draft unavailable on this workspace ({str(exc)[:140]})")
    print("  Review the docket manually — the process works without the co-pilot.")

# COMMAND ----------

# MAGIC %md ## Step 4 — Record your rulings
# MAGIC **This is the only cell you edit.** Map queue `id` → `"approve"` or `"reject"`,
# MAGIC then run this cell and the next. You do not have to clear the queue in one
# MAGIC sitting — undecided items simply stay pending for the next session.
# MAGIC
# MAGIC Example: `DECISIONS = {12: "approve", 15: "reject"}`

# COMMAND ----------

DECISIONS: dict[int, str] = {
    # id: "approve" | "reject"     ← edit me, then run this cell and the next
}

invalid = {i: d for i, d in DECISIONS.items() if d not in ("approve", "reject")}
assert not invalid, f"decisions must be 'approve' or 'reject': {invalid}"
print(f"{len(DECISIONS)} ruling(s) staged: {DECISIONS or '(none — edit this cell to decide)'}")

# COMMAND ----------

# Apply the staged rulings. Only 'pending' rows can transition — a decision made in
# a previous session is never silently overwritten (optimistic concurrency for a
# multi-steward team) — and every transition is stamped with the attested identity
# and a timestamp. This UPDATE trail, plus Delta time travel, is the governance record.
applied_now = 0
for qid, ruling in DECISIONS.items():
    status = "approved" if ruling == "approve" else "rejected"
    spark.sql(  # noqa: F821
        f"""
        UPDATE workspace.retail.autopilot_escalations
        SET status = '{status}', decided_at = now(), decided_by = '{DECIDED_BY}'
        WHERE id = {int(qid)} AND status = 'pending'
        """
    )
    applied_now += 1
print(f"recorded {applied_now} ruling(s) as {DECIDED_BY}")
print("approved items are APPLIED by the next daily_ops run (03:30 MT, or trigger it manually)")

# COMMAND ----------

# MAGIC %md ## Step 5 — Verify

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT status, COUNT(*) AS n
# MAGIC FROM workspace.retail.autopilot_escalations GROUP BY status
# MAGIC UNION ALL
# MAGIC SELECT CONCAT('decided by ', decided_by), COUNT(*)
# MAGIC FROM workspace.retail.autopilot_escalations
# MAGIC WHERE decided_by IS NOT NULL GROUP BY decided_by

# COMMAND ----------

# MAGIC %md ## Scaling this process to a steward team
# MAGIC
# MAGIC This notebook is deliberately a **process**, not a personal tool:
# MAGIC
# MAGIC 1. **Adding a steward is one row:** `INSERT INTO workspace.retail.autopilot_roles
# MAGIC    VALUES ('teammate@corp.com', 'metric_steward', 'human:you@corp.com', now(), NULL)`.
# MAGIC    The role gate admits them immediately; every ruling they make is attested under
# MAGIC    their own identity — accountability scales with the team.
# MAGIC 2. **Dividing the docket:** stewards filter Step 3 by `kind` (a finance steward
# MAGIC    takes metric mappings; a governance steward takes poison terms) or by domain
# MAGIC    schema as more domains onboard. Only `pending` rows transition, so two
# MAGIC    stewards cannot double-decide the same item.
# MAGIC 3. **Cadence & SLA:** review at least weekly; the daily report alarms on
# MAGIC    `max pending age > 7 days` — the queue must drain.
# MAGIC 4. **On a paid workspace:** stewards become a real group (`metric_stewards`) with
# MAGIC    UC grants (`infra/rbac.md`); this notebook is shared CAN RUN, the roles table
# MAGIC    admin-writable only — same process, platform-enforced.
# MAGIC 5. **The definition-authoring loop:** approving a `novel_term` means "worth
# MAGIC    defining" — author the certified definition through the benchmark
# MAGIC    certification workflow (`make certify`), so new metrics arrive
# MAGIC    benchmark-tested exactly like everything else the system learns.
# MAGIC
# MAGIC ---
# MAGIC *Single source of truth: this notebook lives in GitHub
# MAGIC (`notebooks/80_steward_console.py`), deploys to the workspace via
# MAGIC `databricks bundle deploy`, and runs ad-hoc (Run All) or via the unscheduled
# MAGIC `steward_console` job. The queue is Delta with full time travel — every
# MAGIC historical decision is auditable via `DESCRIBE HISTORY`.*
