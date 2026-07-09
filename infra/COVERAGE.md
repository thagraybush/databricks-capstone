# IaC coverage audit — every workspace object, mapped to its creator

Verification pass for issue #19 over the [infra/README.md](README.md) three-plane
model. One row per workspace object this project creates. **Re-creatable?** legend:

- **yes** — running the committed artifact recreates the object on a fresh workspace
- **partial** — the object's *content* is code but part of its identity/state is not
- **manual** — a human step the repo can only document, not execute
- **n/a** — pre-provisioned by Free Edition; the repo verifies, never creates

Statuses are honest: manual seams are marked, not hidden.

## 1. Schemas, volume, RBAC (Plane B — `infra/bootstrap.py` step 1)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| `workspace.retail` | UC schema | `infra/workspace_setup.sql` §1 via `.venv/bin/python infra/bootstrap.py` | yes | `CREATE SCHEMA IF NOT EXISTS` |
| `workspace.banking_gold` | UC schema | `infra/workspace_setup.sql` §1 (also `sql/bootstrap.sql` via `make bootstrap` — both idempotent) | yes | declared in both lanes on purpose; converges either way |
| `workspace.retail.raw` | UC managed volume | `infra/workspace_setup.sql` §2 | yes | producer/engineer RBAC boundary (see `infra/rbac.md`) |
| `workspace.retail.autopilot_roles` (+ seed rows) | Delta table + data | `infra/workspace_setup.sql` §3 (`CREATE IF NOT EXISTS` + `MERGE` seed) | yes | APPLICATION-tier role registry; MERGE keys prevent duplicate seeds |
| `GRANT SELECT ON ... autopilot_roles TO cfollmer@strataintel.ai` | UC grant | `infra/workspace_setup.sql` §5 | yes | the one live FE grant; the full paid-workspace grant block is committed but commented out (FE has no groups — deliberate, see file header) |

## 2. Raw data (the data plane)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| Banking rows in `dim_customers` / `fact_transactions` / `fact_wealth_portfolios` | data load | `make datagen` → `data_gen/output/inserts.sql`, applied by `make bootstrap` (`genie_autopilot.cli cmd_bootstrap`) | yes | deterministic seeded RNG — regenerates identically. **Caveat:** `inserts.sql` is plain `INSERT`; re-running `make bootstrap` duplicates rows (see Gaps §G8) |
| UCI CSVs at `/Volumes/workspace/retail/raw/online_retail_*.csv` | volume files | **MANUAL** — download UCI *Online Retail II* xlsx → `data_gen/raw/online_retail_II.xlsx`, then:<br>`.venv/bin/python data_gen/convert_uci.py`<br>`databricks fs cp data_gen/raw/online_retail_2009_2010.csv dbfs:/Volumes/workspace/retail/raw/`<br>`databricks fs cp data_gen/raw/online_retail_2010_2011.csv dbfs:/Volumes/workspace/retail/raw/` | manual | xlsx is gitignored bulk data (source: UCI ML repository, "Online Retail II"). Conversion is code (zero cleaning by design); the download + upload are hands |
| Clickstream JSONL at `/Volumes/workspace/retail/raw/clickstream/events_batch_*.jsonl` | volume files | generation is code: `.venv/bin/python -m genie_autopilot.producer` → `data_gen/output/clickstream/`; upload is **MANUAL**:<br>`for f in data_gen/output/clickstream/events_batch_*.jsonl; do databricks fs cp "$f" dbfs:/Volumes/workspace/retail/raw/clickstream/; done` | manual | deterministic (seeded) chaos batches. **Never upload `ground_truth.jsonl`** — it is the DQ answer key, not input data (which is why the upload is per-batch, not `-r`) |

## 3. Banking gold DDL + metric views (Plane B)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| `workspace.banking_gold.dim_customers` | Delta table | `sql/bootstrap.sql` via `make bootstrap` or `infra/bootstrap.py` step 2 | yes | `IF NOT EXISTS`; PK constraint |
| `workspace.banking_gold.fact_transactions` | Delta table | `sql/bootstrap.sql` (same lanes) | yes | PK + FK constraints |
| `workspace.banking_gold.fact_wealth_portfolios` | Delta table | `sql/bootstrap.sql` (same lanes) | yes | |
| `workspace.banking_gold.autopilot_proposals` | Delta table | `sql/bootstrap.sql` (same lanes) | yes | banking flywheel state |
| `workspace.banking_gold.autopilot_audit_ledger` | Delta table | `sql/bootstrap.sql` (same lanes) | yes | banking audit mirror |
| `workspace.banking_gold.transactions_metrics` | metric view (YAML 1.1) | `sql/metric_views.sql` via `make bootstrap` only | yes | not in `infra/bootstrap.py`'s `DOMAIN_SQL_FILES` (see Gaps §G7) |
| `workspace.banking_gold.wealth_metrics` | metric view (YAML 1.1) | `sql/metric_views.sql` via `make bootstrap` only | yes | same |

## 4. Retail medallion — pipeline-owned (Plane A)

All created by `pipelines/retail_medallion.py`, deployed by
`resources/retail_pipeline.yml`: `databricks bundle deploy -t dev` then
`databricks bundle run retail_medallion -t dev`. SQL-as-code deliberately does
not re-declare these. (`retail_tagged` and `events_normalized` are
`@dp.temporary_view`s — pipeline-internal, never persisted, not counted.)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| `workspace.retail.bronze_retail` | streaming table | `pipelines/retail_medallion.py` (Auto Loader over `raw/online_retail_*.csv`) | yes | needs the manual CSV upload first (§2) |
| `workspace.retail.bronze_events` | streaming table | `pipelines/retail_medallion.py` (Auto Loader over `raw/clickstream/events_*.jsonl`) | yes | needs the manual JSONL upload first (§2) |
| `workspace.retail.silver_sales` | table | `pipelines/retail_medallion.py` | yes | typed/deduped; returns + anonymous kept |
| `workspace.retail.quarantine_sales` | table | `pipelines/retail_medallion.py` | yes | DQ-failed rows with machine-readable reasons |
| `workspace.retail.silver_events` | table | `pipelines/retail_medallion.py` | yes | v1/v2 normalized, PII-scrubbed |
| `workspace.retail.quarantine_events` | table | `pipelines/retail_medallion.py` | yes | structural-failure events |
| `workspace.retail.dim_products` | materialized view | `pipelines/retail_medallion.py` | yes | modal-description resolution |
| `workspace.retail.fact_sales` | materialized view | `pipelines/retail_medallion.py` | yes | |
| `workspace.retail.gold_daily_revenue` | materialized view | `pipelines/retail_medallion.py` | yes | |
| `workspace.retail.gold_customer_rfm` | materialized view | `pipelines/retail_medallion.py` | yes | |
| `workspace.retail.gold_sessions` | materialized view | `pipelines/retail_medallion.py` | yes | bot detection |
| `workspace.retail.gold_funnel_daily` | materialized view | `pipelines/retail_medallion.py` | yes | |

## 5. Retail metric views + certified KPI views (Plane B — `infra/bootstrap.py` step 2)

These reference pipeline-owned gold tables, so on a truly fresh workspace they
fail (as a warning) until `retail_medallion` has run once — re-run
`infra/bootstrap.py --skip-workspace-sql --skip-lakebase` afterwards.

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| `workspace.retail.revenue_metrics` | metric view (YAML 1.1) | `sql/retail_metric_views.sql` via `infra/bootstrap.py` | yes | `CREATE OR REPLACE` |
| `workspace.retail.funnel_metrics` | metric view (YAML 1.1) | `sql/retail_metric_views.sql` via `infra/bootstrap.py` | yes | |
| `workspace.retail.kpi_monthly_summary` | view | `sql/business_kpis.sql` via `infra/bootstrap.py` | yes | certified KPI surface |
| `workspace.retail.kpi_customer_health` | view | `sql/business_kpis.sql` | yes | |
| `workspace.retail.kpi_funnel_weekly` | view | `sql/business_kpis.sql` | yes | |
| `workspace.retail.kpi_country_performance` | view | `sql/business_kpis.sql` | yes | |

## 6. Autopilot operational tables (created lazily by notebooks on first job run)

All DDL is `CREATE TABLE IF NOT EXISTS` (or an idempotent `saveAsTable` /
`CREATE OR REPLACE`), so any job run on a fresh workspace materializes its own
tables. Jobs are wired in `resources/autopilot_jobs.yml` (Plane A).

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| `workspace.retail.autopilot_telemetry` | Delta table | `notebooks/10_ingest_telemetry.py` (`saveAsTable` overwrite) — `autopilot_flywheel` / `nightly_sessions` jobs | yes | full re-harvest from the live space on every run |
| `workspace.retail.autopilot_corrections` | Delta table | `notebooks/10` + `notebooks/20_detect_drift.py` (`IF NOT EXISTS` + MERGE) | yes | |
| `workspace.retail.autopilot_proposals` | Delta table | `notebooks/20_detect_drift.py` | yes | upserted on (term, entity) |
| `workspace.retail.autopilot_audit_ledger` | Delta table | `notebooks/30_apply_healings.py` (also mirrored by `src/genie_autopilot/healing.py` `AuditLedger.DDL` from the phase drivers) | yes | local `audit_ledger.jsonl` is source of truth; Delta mirror is best-effort |
| `workspace.retail.autopilot_eval_history` | Delta table | `notebooks/40_run_benchmarks.py` — `autopilot_flywheel` job | yes | |
| `workspace.retail.autopilot_session_manifest` | Delta table | `notebooks/70_run_sessions.py` (append, mergeSchema) — `nightly_sessions` job | yes | |
| `workspace.retail.autopilot_daily_report` | Delta table | `notebooks/90_daily_report.py` — `daily_ops` job | yes | |
| `workspace.retail.router_corpus` | Delta table | `notebooks/60_semantic_router.py` — `router_training` job | yes | CDF-enabled source of the delta-sync index |
| `workspace.retail.router_eval_examples` | Delta table | `notebooks/60_semantic_router.py` | yes | |
| `workspace.retail.router_arm_results` | Delta table | `notebooks/61_router_arm.py` — `router_arm` job | yes | |
| `workspace.retail.gold_revenue_forecast` | Delta table | `notebooks/50_ds_kpis_and_models.py` (`CREATE OR REPLACE TABLE`) — `ds_persona` job | yes | |

## 7. Genie spaces (Plane C — the one real manual seam)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| Banking Genie space `01f17b53b7161737a57aec0195b92b45` | Genie space | space *creation* is not scripted — `make bootstrap` ends by instructing "create the Genie space (UI or API)"; content sources: `sql/metric_views.sql` views + `benchmarks/questions.yaml` | **partial** | identity (the ID) is workspace-specific and captured by hand into `GA_GENIE_SPACE_ID` |
| Retail Genie space `01f17b5a51411bc382cd3cd224d11daf` | Genie space | same seam — created via UI/`POST /api/2.0/genie/spaces` (warehouse ID + retail gold/metric-view identifiers); content then converges from code (rows below) | **partial** | new ID must be re-wired: `GA_RETAIL_SPACE_ID`/`GA_GENIE_SPACE_ID` env + `space_id` base parameters in `resources/autopilot_jobs.yml` (currently hardcoded — Gaps §G1) |
| Retail benchmark suite (in-space, serialized) | serialized space content | `src/genie_autopilot/phase_e.py::ensure_benchmarks` syncs `benchmarks/retail_questions.yaml` into `serialized_space.benchmarks.questions`; human certification via `make certify` (`src/genie_autopilot/certify.py`) | yes (given a space ID) | YAML in repo is the source of truth; sync is idempotent |
| Space instructions / synonyms (healed content) | serialized space content | `healing.append_space_instruction` applied by `notebooks/30_apply_healings.py` and the phase drivers (`phase_d`, `phase_e`); every mutation logged to the audit ledger | **partial** | the *process* is fully code and replayable; the accumulated serialized state has no checked-in snapshot (Gaps §G2) |

## 8. Lakebase (Plane C — `infra/bootstrap.py` step 3)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| Lakebase project `genie-autopilot` | Lakebase project | `src/genie_autopilot/lakebase.py::ensure_project` via `infra/bootstrap.py` | yes | create-or-get (POST, falls back to GET on already-exists) |
| `hitl_queue` | Postgres table | `lakebase.py::ensure_schema` (`CREATE TABLE IF NOT EXISTS`) via `infra/bootstrap.py` | yes | HITL approve/reject queue |
| `healing_history` | Postgres table | `lakebase.py::ensure_schema` via `infra/bootstrap.py` | yes | applied/rolled-back healing actions |

## 9. Vector Search + registered models (Plane C)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| `semantic-memory` | Vector Search endpoint | `notebooks/60_semantic_router.py` §5 (get-then-create) — run notebook 60 or the `router_training` job | yes | creation is code; the *build* is an in-workspace run. FE: exactly 1 endpoint / 1 unit |
| `workspace.retail.router_corpus_index` | delta-sync index | `notebooks/60_semantic_router.py` §5 | yes | needs `router_corpus` populated and the endpoint ONLINE; re-run the cell if created before the endpoint settles |
| `workspace.retail.semantic_router` | UC registered model | `notebooks/60_semantic_router.py` §6 (`mlflow.register_model`, registry `databricks-uc`) | yes | new version per training run |
| `workspace.retail.purchase_propensity` | UC registered model | `notebooks/50_ds_kpis_and_models.py` (`registered_model_name=`) — `ds_persona` job | yes | best-effort UC registration |
| MLflow runs/experiments (router + propensity) | MLflow experiment data | side effect of notebooks 50/60 | yes | evidence artifacts regenerate on each run; exact metrics are data-dependent, not pinned |

## 10. Jobs, pipeline, workspace files (Plane A — `databricks bundle deploy -t dev --profile free-edition`)

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| `autopilot_flywheel` | job (4 sequential tasks) | `resources/autopilot_jobs.yml` | yes | `space_id` blank in dev — notebooks no-op gracefully until supplied |
| `ds_persona` | job | `resources/autopilot_jobs.yml` | yes | |
| `nightly_sessions` | job (02:00 cron) | `resources/autopilot_jobs.yml` | yes | **hardcodes retail space ID** in `base_parameters` (Gaps §G1) |
| `router_training` | job | `resources/autopilot_jobs.yml` | yes | |
| `router_arm` | job | `resources/autopilot_jobs.yml` | yes | **hardcodes retail space ID** (Gaps §G1) |
| `daily_ops` | job (03:30 cron) | `resources/autopilot_jobs.yml` | yes | |
| `steward_console` | job (manual trigger) | `resources/autopilot_jobs.yml` | yes | steward overrides parameters at run time |
| `retail_medallion` | Lakeflow declarative pipeline | `resources/retail_pipeline.yml` | yes | serverless, triggered; owns all §4 tables |
| Synced workspace files (`notebooks/`, `src/`, …) | workspace files | `databricks.yml` bundle sync on deploy | yes | FE note: deploy from a laptop; workspace-side Terraform downloads are blocked |

## 11. Dashboard, warehouse, credentials

| object | type | created by (repo path / command) | re-creatable from repo? | notes |
|---|---|---|---|---|
| Flywheel health AI/BI dashboard | AI/BI (Lakeview) dashboard | **MANUAL** — built interactively via the Databricks Assistant. Query source of record: `sql/dashboard_queries.sql` (6 datasets: accuracy trend, quarantine mix, clickstream DQ vs chaos, funnel health, revenue trend, healing activity). Rebuild: new dashboard → add each query block as a dataset over the Serverless Starter Warehouse → one widget per dataset | manual | not exported as Lakeview JSON (Gaps §G3) |
| SQL warehouse `b9f4a06641eedd7b` (Serverless Starter) | SQL warehouse | n/a — provisioned by Free Edition itself; `infra/bootstrap.py` step 4 verifies it exists, `GA_WAREHOUSE_ID` overrides resolution | n/a | the repo never creates warehouses; any warehouse passes verification |
| PAT (`databricks-fe` Keychain item) | credential | **MANUAL** — workspace → Settings → Developer → Access tokens, then `security add-generic-password -s databricks-fe -a <you> -w <token>` | manual | by design: credentials never in code (resolution order in `src/genie_autopilot/config.py`) |

**Coverage summary: 67 objects. 59 yes · 3 partial (Genie space identity ×2, healed serialized-space state) · 4 manual (UCI upload, clickstream upload, dashboard, PAT) · 1 n/a (FE-provisioned warehouse).**

## Gaps → fix-forward

Everything not fully re-creatable, with the concrete step that closes it:

- **G1 — Genie space IDs hardcoded.** `01f17b5a…` appears in
  `resources/autopilot_jobs.yml` (`nightly_sessions`, `router_arm` base
  parameters) and both IDs in `infra/README.md`/`.env`. Fix: declare
  `retail_space_id` / `banking_space_id` as bundle variables in
  `databricks.yml` and reference `${var.retail_space_id}` in the job YAML;
  after re-creating spaces, capture new IDs once (bundle variable +
  `GA_GENIE_SPACE_ID`/`GA_RETAIL_SPACE_ID`) instead of editing three files.
- **G2 — Space creation + serialized snapshot not in the repo.**
  `genie_api.py` has `get_space`/`update_space` but no `create_space`; the
  healed instruction state exists only in the live space (audited, but not
  snapshotted). Fix: add `create_space()` (`POST /api/2.0/genie/spaces` with
  warehouse ID + table identifiers) plus an export lane (e.g. `make
  space-export`) that writes `get_space(include_serialized_space=true)` JSON to
  `infra/spaces/<domain>.json` — creation then becomes "create from snapshot,
  sync benchmarks", and the snapshot diffs in review like any other code.
- **G3 — Dashboard is Assistant-built, not exported.** Fix: export the Lakeview
  JSON (dashboard kebab menu → export, or the Lakeview API) and check it in
  under `resources/`; if Free Edition blocks the export path, the
  documented-manual rebuild from `sql/dashboard_queries.sql` (§11) stays the
  honest fallback, recorded in `docs/backlog-free-edition-limits.md` style.
- **G4 — Raw uploads are hand-run `databricks fs cp`.** Fix: add a
  `make upload-raw` target wrapping the §2 commands (CSVs + per-batch JSONL
  loop) with a guard that refuses to copy `ground_truth.jsonl`.
- **G5 — UCI xlsx is an external download.** Gitignored as bulk data. Fix:
  record the source URL + a checksum in `data_gen/README.md` (or add a small
  fetch script) so "download the xlsx" is verifiable, not tribal.
- **G6 — `make bootstrap-workspace` promised but missing.** `infra/README.md`
  names it as the intended alias for `.venv/bin/python infra/bootstrap.py`; the
  Makefile target doesn't exist yet. Fix: add the one-line target.
- **G7 — Banking metric views only in the `make bootstrap` lane.**
  `sql/metric_views.sql` is not in `infra/bootstrap.py`'s `DOMAIN_SQL_FILES`,
  so the single entrypoint doesn't fully cover Plane B. Fix: append it to the
  list (it is already `CREATE OR REPLACE` — idempotent).
- **G8 — Banking `inserts.sql` is not idempotent.** Plain `INSERT` batches:
  re-running `make bootstrap` against a loaded workspace duplicates rows. Fix:
  have `generate_banking_data.py` emit `TRUNCATE TABLE`-then-`INSERT` (or
  `MERGE` on the PKs) so the data load converges like the DDL does.
- **G9 — PAT.** Inherently manual and intentionally so — documented in §11;
  no code fix wanted.

## Reproduce a fresh workspace (the golden path)

Ordered command sequence; manual seams marked. Every step is idempotent except
where noted (G8).

```bash
# 0. credentials (MANUAL): workspace → Settings → Developer → Access tokens
security add-generic-password -s databricks-fe -a <you> -w <PAT>

# 1. local environment
make install

# 2. banking lane: schema + tables + synthetic data + metric views
make datagen
make bootstrap                     # re-run caveat: inserts.sql duplicates rows (G8)

# 3. workspace primitives + retail Plane B + Lakebase
#    (retail metric/KPI views WARN here — gold tables don't exist yet; expected)
.venv/bin/python infra/bootstrap.py

# 4. raw data (MANUAL seam — download, then repo-scripted conversion/generation)
#    download UCI "Online Retail II" xlsx → data_gen/raw/online_retail_II.xlsx
.venv/bin/python data_gen/convert_uci.py
databricks fs cp data_gen/raw/online_retail_2009_2010.csv dbfs:/Volumes/workspace/retail/raw/
databricks fs cp data_gen/raw/online_retail_2010_2011.csv dbfs:/Volumes/workspace/retail/raw/
.venv/bin/python -m genie_autopilot.producer
for f in data_gen/output/clickstream/events_batch_*.jsonl; do
  databricks fs cp "$f" dbfs:/Volumes/workspace/retail/raw/clickstream/
done                               # ground_truth.jsonl stays local — it is the answer key

# 5. Plane A: jobs + pipeline + notebook sync, then first pipeline run
databricks bundle deploy -t dev --profile free-edition
databricks bundle run retail_medallion -t dev

# 6. retail metric/KPI views — now the gold tables exist
.venv/bin/python infra/bootstrap.py --skip-workspace-sql --skip-lakebase

# 7. Genie spaces (MANUAL seam — the one identity gap, G1/G2):
#    create via UI (New → Genie space over the retail gold + metric views; ditto banking)
#    or POST /api/2.0/genie/spaces, then capture the new IDs:
#      - GA_GENIE_SPACE_ID / GA_RETAIL_SPACE_ID in .env
#      - space_id base_parameters in resources/autopilot_jobs.yml
databricks bundle deploy -t dev --profile free-edition   # re-deploy with the wired IDs

# 8. benchmarks + flywheel state (phase drivers; ensure_benchmarks syncs benchmarks/*.yaml)
GA_GENIE_SPACE_ID=<retail-id> .venv/bin/python -m genie_autopilot.phase_d
GA_GENIE_SPACE_ID=<retail-id> .venv/bin/python -m genie_autopilot.phase_e
GA_GENIE_SPACE_ID=<retail-id> .venv/bin/python -m genie_autopilot.phase_f_variance

# 9. models + vector search (in-workspace runs; all creation code committed)
databricks bundle run router_training -t dev   # notebook 60: endpoint + index + semantic_router
databricks bundle run ds_persona -t dev        # notebook 50: forecast table + purchase_propensity

# 10. dashboard (MANUAL, G3): rebuild from sql/dashboard_queries.sql, §11 steps
```

Re-run safety: all SQL is `IF NOT EXISTS` / `CREATE OR REPLACE` / `MERGE`, the
bundle deploy converges, `ensure_project`/`ensure_schema` are create-or-get, and
notebook DDL is `IF NOT EXISTS` — the full sequence is safe to re-apply to an
already-provisioned workspace **except** step 2's data load (G8).
