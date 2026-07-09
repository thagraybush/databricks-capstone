# notebooks — the workspace-side flywheel

Databricks notebooks (source format) deployed by the Asset Bundle and orchestrated by
the jobs in [../resources/autopilot_jobs.yml](../resources/autopilot_jobs.yml). The
numbering is a pipeline order, not a chapter order: 10–40 are the flywheel, 50 is the
DS persona, 60s are the router, 70–90 are the living-system operations layer.

## Contents

| Notebook | Responsibility | Runs in job |
|---|---|---|
| `10_ingest_telemetry.py` | Harvest Genie conversations → `autopilot_telemetry` (overwrite; API is source of truth) + `autopilot_corrections` (MERGE, no dupes) | `autopilot_flywheel` task 1; also `nightly_sessions` task 2 |
| `20_detect_drift.py` | Score drift proposals (deterministic corrections + best-effort `ai_query` pass) → `autopilot_proposals`, preserving human decisions on upsert | `autopilot_flywheel` task 2 |
| `30_apply_healings.py` | Governed application: `healing.triage` gate → three surfaces → `autopilot_audit_ledger`; `dry_run=true` by default | `autopilot_flywheel` task 3 |
| `40_run_benchmarks.py` | Genie eval run labeled by `phase` widget → `autopilot_eval_history` (the longitudinal record) | `autopilot_flywheel` task 4 |
| `50_ds_kpis_and_models.py` | DS persona: KPIs, `AI_FORECAST` demand forecast, MLflow propensity model | `ds_persona` |
| `60_semantic_router.py` | Train the two-head semantic router (answerability + target metric), register to UC | `router_training` |
| `61_router_arm.py` | Third experimental arm: router+Genie vs Genie-alone on the noise-inclusive superset → `router_arm_results` | `router_arm` |
| `70_run_sessions.py` | Nightly paced multi-turn persona sessions (real traffic) + manifest persist | `nightly_sessions` task 1 (02:00 MT) |
| `80_steward_console.py` | HITL review surface over the Lakebase `hitl_queue`: list / approve / reject — decide ≠ deploy | `steward_console` (manual, param overrides) |
| `85_escalate_and_apply.py` | The steward loop's system half: mine below-gate proposals, poison conflicts, novel terms → enqueue (idempotent); apply steward-approved rows through the governed appliers | `daily_ops` task 1 (03:30 MT) |
| `90_daily_report.py` | The system's daily standup: healings, escalations, accuracy trend, router economics, DQ posture → `autopilot_daily_report` | `daily_ops` task 2 (after 85) |

## Widget conventions

Every notebook declares its own widgets with sensible defaults, so it runs standalone
in the workspace UI *and* under Jobs:

- `domain_schema` (default `workspace.retail`) — every table name is derived from it.
- `space_id` (default blank) — blank means "no Genie space yet": notebooks 10/30/40
  no-op gracefully instead of failing. Jobs pin only operator-tuned knobs via
  `base_parameters`; override at run time with
  `databricks bundle run autopilot_flywheel -t dev --notebook-params space_id=<id>`.
- Behavior knobs are strings-as-widgets: `min_distinct_users`, `auto_approve_confidence`,
  `dry_run` (default `"true"` — flip to `"false"` once the HITL queue looks sane),
  `phase` (labels eval runs `baseline` / `post_healing` / `adhoc`), `sessions`,
  `max_questions`, and the steward console's `action` / `ids` / `decided_by`.

## The bundle-files import pattern

The bundle deploys `files/notebooks/` (cwd) beside `files/src/`, so every notebook
imports the package with:

```python
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))
```

No wheel build, no cluster library — the notebooks always run the exact code that was
deployed with them. Auth is ambient (notebook/job context); no PAT handling here.

## Graceful-degradation policy

A missing prerequisite must never fail the flywheel job: blank `space_id` no-ops with
a message, a space without benchmarks warns and exits cleanly (notebook 40),
`ai_query` / `AI_FORECAST` degrade to warnings where unavailable, and notebook 90
writes each KPI as NULL with an honest `detail` note when its source table doesn't
exist yet. Pending steward escalations never block a user session — Genie keeps
answering from its current certified context ([../docs/steward-loop.md](../docs/steward-loop.md)).

Related: [package map](../src/genie_autopilot/README.md) ·
[architecture v2](../docs/architecture-v2.md) · [daily-report KPIs](../docs/steward-loop.md)
