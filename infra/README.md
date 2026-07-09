# Infrastructure as Code — the IaC map

Everything this capstone runs on is declared in the repo and applied through one of
**three management planes**. Each plane has a single owner-of-record for a class of
resources, a single apply command, and is idempotent — re-applying converges to the
same state instead of erroring. This document is the map; [rbac.md](rbac.md) is the
role model; [workspace_setup.sql](workspace_setup.sql) and [bootstrap.py](bootstrap.py)
are the executable pieces that live in this directory.

Live workspace facts (Free Edition, single user): host
`https://dbc-c00424a1-8d76.cloud.databricks.com`, user `cfollmer@strataintel.ai`
(workspace admin), catalog `workspace`, schemas `retail` + `banking_gold`, volume
`workspace.retail.raw`, warehouse `b9f4a06641eedd7b`, Lakebase project
`genie-autopilot`, Vector Search endpoint `semantic-memory`, Genie spaces created via
API (banking `01f17b53b7161737a57aec0195b92b45`, retail
`01f17b5a51411bc382cd3cd224d11daf`).

## The three planes

| Plane | Owns | Source of truth | Apply command |
|---|---|---|---|
| **A. Asset Bundles** (DABs) | Jobs, the Lakeflow pipeline, notebook sync | `databricks.yml` + `resources/*.yml` + `notebooks/` | `databricks bundle deploy -t dev` |
| **B. SQL-as-code** | Schemas, volume, tables, views, metric views, KPI views, the role registry, grants | `infra/workspace_setup.sql` + `sql/*.sql` | `infra/bootstrap.py` via the repo's SQL runner (`genie_autopilot.cli._run_sql_file` → Statement Execution API on the warehouse) |
| **C. API-as-code** | Genie spaces (incl. serialized benchmarks/instructions), Lakebase project + HITL schema, Vector Search endpoint/index | `src/genie_autopilot/*.py`, `benchmarks/*.yaml`, `notebooks/60_semantic_router.py` | python modules invoked by `infra/bootstrap.py` and the phase drivers |

### Plane A — Asset Bundles (`databricks bundle deploy`)

- **Jobs** (`resources/autopilot_jobs.yml`): `autopilot_flywheel`, `ds_persona`,
  `nightly_sessions` (02:00 cron), `router_training`, `router_arm`, `daily_ops`
  (03:30 cron), `steward_console` (manual trigger, parameter-overridden by the
  steward). All serverless, ≤2 concurrent tasks by design (FE limit is 5).
- **Pipeline** (`resources/retail_pipeline.yml`): `retail_medallion` — serverless,
  triggered, `workspace.retail`. The pipeline *owns* the bronze/silver/gold/quarantine
  tables; SQL-as-code deliberately does not re-declare them.
- **Notebooks** (`notebooks/*.py`): synced to the workspace as part of the bundle.

FE note: deploy from a laptop (`--profile free-edition`); FE blocks workspace-side
Terraform downloads.

### Plane B — SQL-as-code (via the repo's SQL runner)

Applied in this order by `infra/bootstrap.py` (each file is idempotent —
`IF NOT EXISTS` / `CREATE OR REPLACE` / `MERGE` throughout):

1. `infra/workspace_setup.sql` — workspace-level primitives: schemas `retail` and
   `banking_gold`, volume `workspace.retail.raw`, the
   `workspace.retail.autopilot_roles` role registry + its seed rows, and the
   PLATFORM-tier grant block (see [rbac.md](rbac.md) for why most of it is
   commented out on FE).
2. `sql/bootstrap.sql` — banking gold DDL (dims/facts + flywheel state tables).
3. `sql/retail_metric_views.sql` — Metric Views (YAML 1.1) over the retail gold layer.
4. `sql/business_kpis.sql` — the certified `kpi_*` views.

The runner is `genie_autopilot.cli._run_sql_file`: it splits statements with
awareness of `$$`-quoted metric-view YAML and quoted strings, and executes each via
the Statement Execution API against the SQL warehouse. `sql/metric_views.sql`
(banking) is applied by the existing `make bootstrap` lane; `sql/admin_monitoring.sql`
and `sql/dashboard_queries.sql` are query libraries, not DDL — they are read, not
applied.

Ordering caveat: files 3–4 reference gold tables that the `retail_medallion`
pipeline (Plane A) creates. On a truly fresh workspace those two files will fail
until the pipeline has run once; `bootstrap.py` treats that as a warning and tells
you to re-run after `databricks bundle run retail_medallion -t dev`.

### Plane C — API-as-code (python modules driven by `infra/bootstrap.py` and the phase drivers)

- **Genie spaces** — created and maintained through the Genie Space Management API
  (`src/genie_autopilot/genie_api.py`: `get_space(include_serialized_space=true)`,
  `update_space(serialized_space)`). The spaces' *content* is code: benchmark
  questions live in `benchmarks/questions.yaml` (banking) and
  `benchmarks/retail_questions.yaml` (retail, human-certified via `make certify`);
  instructions/synonyms are written by the governed healing appliers
  (`healing.append_space_instruction`, notebook 30) and every mutation lands in the
  audit ledger. Eval runs go through the Benchmarks API (`evals.BenchmarkRunner`,
  `POST /api/2.0/genie/spaces/{s}/eval-runs`).
- **Lakebase** — `src/genie_autopilot/lakebase.py`: `ensure_project` (project
  `genie-autopilot`, already-exists → GET), `ensure_schema` (HITL tables
  `hitl_queue` + `healing_history`, `CREATE TABLE IF NOT EXISTS`).
- **Vector Search** — `notebooks/60_semantic_router.py` creates the
  `semantic-memory` endpoint and its delta-sync index idempotently (get-then-create).
  FE allows exactly 1 endpoint / 1 unit, delta-sync only.

## Reproducibility guarantee

A fresh Free Edition workspace reaches full system state with:

```bash
make bootstrap-workspace        # = .venv/bin/python infra/bootstrap.py
databricks bundle deploy -t dev # Plane A: jobs + pipeline + notebooks
# then the phase drivers:
python -m genie_autopilot.phase_d   # fleet → drift → HITL → heal → post-heal eval
python -m genie_autopilot.phase_e   # learning loops + DQ precision/recall
python -m genie_autopilot.phase_f_variance
```

(`make bootstrap-workspace` is the intended Makefile alias for
`.venv/bin/python infra/bootstrap.py`; until the target lands in the Makefile,
invoke the script directly. `python infra/bootstrap.py --help` lists per-step
`--skip-*` flags.)

`bootstrap.py` runs Plane B, provisions Plane C's Lakebase pieces, verifies the
volume and warehouse exist, then prints the manual-steps checklist below and the
current role assignments from `autopilot_roles`.

## What is NOT codified (known manual steps)

Honesty ledger — these cannot be applied from the repo, and each has re-creation
instructions:

1. **The PAT itself.** Credentials are never in code. Re-create: workspace →
   Settings → Developer → Access tokens → generate; store it with
   `security add-generic-password -s databricks-fe -a <you> -w <token>`
   (resolution order in `src/genie_autopilot/config.py`: `DATABRICKS_TOKEN` env,
   then Keychain).
2. **Data upload.** `make datagen` produces `data_gen/output/inserts.sql`
   (applied by `make bootstrap`); retail clickstream batches are copied with
   `databricks fs cp <local> dbfs:/Volumes/workspace/retail/raw/clickstream/`
   (keep `ground_truth.jsonl` local — it is the answer key, not input data).
3. **Genie space *identity*.** Space content is code (Plane C) but the two space
   IDs are workspace-specific. After re-creating the spaces (phase drivers, or
   `POST /api/2.0/genie/spaces` with the warehouse ID + table identifiers, then
   load `benchmarks/*.yaml`), re-wire the new IDs: `GA_GENIE_SPACE_ID` /
   `GA_RETAIL_SPACE_ID` env vars and the `space_id` base parameters in
   `resources/autopilot_jobs.yml`.
4. **Vector Search index build.** Run notebook 60 (or the `router_training` job)
   once after the telemetry corpus exists — index creation is code, but the build
   is an in-workspace run.
5. **The AI/BI dashboard.** Built interactively via the Databricks Assistant, so it
   is not declared in the repo. Re-create: new dashboard in the workspace, add the
   queries from `sql/dashboard_queries.sql` (accuracy arc, DQ posture, healing
   activity) — that file is the dashboard's query source of record.
