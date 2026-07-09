# Blocked by Free Edition: Backlog + Vision

This project runs end-to-end on Databricks Free Edition — that constraint is a
feature (it forces public-API, serverless-only, triggered-batch design), but some
capabilities are genuinely gated. This doc is the honest ledger: what FE blocks,
what we shipped instead, and what each capability unlocks on a paid workspace.
Referenced inline from [admin-governance.md](admin-governance.md) as `→ backlog`.

| Capability | FE limitation | Workaround shipped | Paid-workspace vision |
|---|---|---|---|
| Real multi-user identities | Single user, single PAT — every persona executes as one identity | Persona fleets carry attribution in their own manifests; audit ledger records the logical persona/lane | Real users per persona; `system.query.history.executed_by` becomes true per-persona telemetry; friction attributed to actual roles (finance vs merchandising dialects separable by identity, not label) |
| Service principals | Not available | All automation (bootstrap, fleets, healing, eval runs) authenticates with the user PAT from the macOS Keychain | SP-owned jobs with scoped tokens and rotation; the autopilot gets its own identity so machine-written metadata is attributable to the machine in audit trails |
| `system.access.audit` | Absent on FE (verified; `system.query.history` IS readable) | Genie Conversation API telemetry + `system.query.history` monitoring shipped ([sql/admin_monitoring.sql](../sql/admin_monitoring.sql)) | Full audit joins: who read what, grant changes, Genie space edits — the OBSERVE layer stops inferring and starts knowing |
| Genie Ontology (Public Preview) | Account-team gated; unavailable on FE | The flywheel IS the public-API analog: authority-ranked, usage-refreshed semantic context via UC comments/tags, metric-view synonyms, space instructions | Migrate certified definitions into Ontology when GA; this project's healed glossary is the seed corpus, and the eval harness ports as the Ontology regression gate |
| Budget policies / cost APIs | Absent | Fair-use pacing (≤5 q/min Genie client), triggered batches, warehouse auto-stop, single 2X-Small as structural rate limit | Budget policies on governed tags; per-persona cost attribution; alerts on spend anomalies instead of shape-based containment |
| Model serving for the router | No GPU / provisioned throughput; endpoint limits | Query-quality router runs as batch scoring in a notebook (measured: 5/6 noise filtered, accuracy 0.90) | Real-time serving endpoint scores every question BEFORE it reaches Genie — the reject/human_review nudge becomes synchronous, not batch |
| Vector Search scale | 1 endpoint / 1 unit; no Direct Vector Access | Delta-sync index only, scoped to the certified-definition corpus | Scaled semantic retrieval over corrections and definitions; similarity-based drift clustering instead of term-frequency heuristics |
| Scheduled compute headroom | 5 concurrent tasks; fair-use kill switch | Nightly triggered batches; nothing always-on by design | Continuous/streaming triggers for telemetry ingest; healing cycles shrink from nightly to near-real-time |
| SCIM / groups | No group management for persona identities | Personas are code (fleet manifests), not principals | Group-per-persona with ABAC row/column policies; "finance sees revenue, merchandising sees units" becomes enforced policy instead of a disambiguation instruction |
| Clean rooms | Not available | N/A — single-org simulation | Cross-org semantic collaboration: share certified definitions (not data) with partners; the retailer's "GMV" and the marketplace's "GMV" reconciled in a clean room |
| Alerting | Basic; no rich SQL alerts wiring | Weekly manual cadence over the monitoring queries + dashboard | SQL alerts on eval regression (accuracy drop in `autopilot_eval_history`), failing-statement spikes, HITL queue age; the admin playbook's OBSERVE layer becomes push, not pull |

## Paid workspace day-1 plan

What we would flip on immediately, in order, and why it matters to the business
value story:

1. **Real identities + SCIM groups.** The flywheel's core signal is *who* said what
   — with one PAT, dialect attribution is simulated. Real identities make the
   poison-term detector honest (finance and merchandising as separate principals)
   and make the 40% → 80% lift measurable per team, which is how you prove value to
   each team's budget owner.
2. **`system.access.audit` + SQL alerts.** The coach-not-cop playbook currently
   reviews on a weekly pull cadence; audit tables plus alerts turn regressions and
   stuck-user signals into same-day pushes. Governance that reacts in hours retains
   trust; governance that reacts in weeks breeds shadow analytics.
3. **Service principal for the autopilot.** Machine-written metadata should carry a
   machine identity. This upgrades the audit ledger from "trust the payload column"
   to platform-attested provenance — the difference between a demo and a system a
   compliance reviewer signs off on.
4. **Model serving for the query-quality router.** Batch scoring means noise is
   filtered after the fact; a serving endpoint filters it before Genie spends a
   wrong answer. The router's reasons are the cheapest coaching channel we have —
   making them synchronous is the single biggest UX upgrade per unit of effort.
5. **Budget policies on governed tags.** Containment today is structural (one small
   warehouse). Tag-scoped budgets let the warehouse grow for real workloads while
   keeping per-persona accountability — removing the last reason an admin might be
   tempted to chop access.
6. **Genie Ontology migration.** When account access lands: port the certified
   glossary as the seed, keep the benchmark suite as the regression gate. Everything
   this project measured — synonym +0%, definition +40 points, poison-term clarify —
   is the evidence base for what the Ontology should be hydrated with first.

The through-line: nothing on this list changes the architecture. FE forced the
design into public APIs, triggered batches, and explicit gates — all of which
survive the upgrade intact; the paid features make the same loops faster, attributed,
and enforceable.
