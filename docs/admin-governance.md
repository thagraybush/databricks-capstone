# Workspace Admin Playbook: Cleaning Up Behavior Without Chopping Access

The admin posture in this workspace is **coach, not cop**. Bad query behavior —
expensive scans, repeated failures, jargon misfires, noisy questions — is treated as
a *teaching signal*, not a permissions problem. Revoking access is explicitly off
the table (see "What we deliberately do NOT do"). The playbook has three escalating
layers: observe, coach, contain. Every mechanism below exists in this Free Edition
workspace today unless flagged `→ backlog`
([backlog-free-edition-limits.md](backlog-free-edition-limits.md)).

## A. OBSERVE — see the behavior before judging it

**system.query.history is readable on this workspace** (verified); it is the
observation plane. `system.access.audit` is NOT available on Free Edition
→ backlog. All monitoring queries are in
[sql/admin_monitoring.sql](../sql/admin_monitoring.sql); the weekly cadence:

1. **Expensive queries by user/app** — top-10 by `total_duration_ms` over 7 days,
   plus the slowest individual statements. A user at the top of this list gets a
   conversation, not a revocation.
2. **Repeated failing statements** — the same statement fingerprint failing ≥2
   times is someone *stuck*, which is a documentation or vocabulary gap, not
   misconduct. These feed the coaching loop directly.
3. **Genie share** — `client_application = 'Databricks SQL Genie Space'` vs direct
   SQL. Rising Genie share among business personas is the success metric of the
   whole project; falling share after a bad-answer episode is the early warning
   for shadow analytics.
4. **Audit-ledger review** — weekly read of `workspace.retail.autopilot_audit_ledger`
   (healing activity by approver lane): confirm nothing auto-approved that should
   have gone to a human, confirm poison terms were healed as clarify instructions
   and never as synonyms. Live posture: 6 healings applied, 0 auto, 6 human-reviewed.
5. **HITL queue triage** — proposals below the auto-approve gate and questions
   routed `human_review` sit in the Lakebase `hitl_queue`. Triage weekly; an ageing
   queue means users are waiting on definitions, which is admin debt.

## B. COACH — turn the signals into in-flow teaching

The system corrects users *in the flow of asking*, which is where vocabulary is
actually learned:

1. **Router reasons as nudges.** The query-quality router filters noise questions
   before they burn Genie quota, returning `reject` or `human_review` **with a
   reason** ("causal 'why' question — the warehouse holds outcomes, not causes";
   "no cost data exists — margin cannot be computed"). Measured: 5/6 noise
   questions caught, classifier accuracy 0.90 on the labeled set. The reason IS the
   coaching — the user learns what the platform can answer without filing a ticket
   or getting a confidently wrong number.
2. **Glossary and disambiguation instructions teach vocabulary in-flow.** The live
   example: the poison term **'sales'** (net_revenue to finance, units to
   merchandising) was detected from contradictory corrections and healed as a
   disambiguation instruction — Genie now *asks which 'sales' you mean*. Nobody was
   emailed a style guide; the correction happens at the moment of ambiguity, every
   time, for everyone. Certified definitions (take rate, whales, churn risk,
   bounce) work the same way: measured jargon accuracy 40% → 80% mean after
   definition healing.
3. **Benchmark-certified examples as the paved path.** The Genie space's sample
   questions and the `kpi_*` views ([sql/business_kpis.sql](../sql/business_kpis.sql),
   COMMENT-marked as certified-intent) show users what a *good* question looks like
   and what the certified formula is. Users who follow the paved path hit the 100%
   clean/collision strata; the benchmark suite regression-gates every healing so
   the paved path never silently degrades.

## C. CONTAIN — last resort, still no access chopping

When observation shows genuinely runaway usage, containment bounds the blast radius
without touching grants:

1. **Warehouse shape as a natural rate limit.** One Serverless Starter Warehouse
   (2X-Small) with auto-stop: there is no idle burn, and a single small warehouse
   caps concurrent damage by construction. This is the FE reality turned into
   policy.
2. **Statement timeouts.** `STATEMENT_TIMEOUT` at the warehouse/session level kills
   pathological statements instead of the user's access.
3. **Fair-use awareness.** Free Edition enforces its own fair-use kill switch; the
   paced Genie client (≤5 questions/min) and triggered-batch design keep the
   project inside it deliberately rather than accidentally.
4. **Tags for attribution.** UC tags on schemas/tables attribute workloads to
   personas/lanes so a usage conversation starts with facts. Governed-tag budget
   policies and ABAC would make this enforceable → backlog.

## What we deliberately do NOT do — and why

- **No revoking SELECT.** A user who loses access to the governed tables does not
  stop analyzing — they export a CSV once and build on it forever. Shadow analytics
  is strictly worse than expensive queries: it is ungoverned, unauditable, and
  permanently wrong after the next healing cycle.
- **No blocking users or apps.** A blocked user routes around the platform; a
  coached user feeds the flywheel. Every thumbs-down and correction is training
  signal — the "worst" users (most friction, most jargon) contributed the most to
  the 40% → 80% lift. Blocking them would starve the system that fixes them.
- **No silent query rewriting.** The router rejects with reasons and Genie asks
  clarifying questions; neither silently substitutes what the admin thinks the user
  meant. Trust in the answers is the product; silent intervention spends it.

The receipts requirement binds all three layers: every mutation the autopilot makes
lands in `autopilot_audit_ledger` (who/what/when/approver lane), and every healing
cycle is benchmark-gated with rollback. An admin action that leaves no receipt
doesn't happen here.
