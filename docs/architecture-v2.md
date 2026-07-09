# Architecture v2: The Data-Organization Simulation

v1 (docs/architecture.md) proved the flywheel on a hand-built banking schema. v2 scales it
into a simulated data ORGANIZATION whose realistic behavior — production chaos, ELT
discipline, analytical consumption, and noisy business questioning — generates the
training signal for the autonomous semantic layer. Every persona is a runnable program;
nothing is always-on (Free Edition fair-use), everything is triggered batches.

## The personas and their systems

| Persona | Role in a real org | Implementation | Output |
|---|---|---|---|
| **Product Engineering** (producer) | Ships the app that emits events; breaks schemas on Fridays | `src/genie_autopilot/producer.py` — synthetic clickstream against the REAL UCI product catalog with **labeled chaos**: v1→v2 schema drift, duplicates, late events, bot traffic, truncated JSON, PII-in-referrer | JSONL batches → `/Volumes/workspace/retail/raw/clickstream/` + `ground_truth.jsonl` (defect labels for objective DQ scoring) |
| **Data Engineering** | Ingestion, ELT, data quality | `pipelines/retail_medallion.py` — Lakeflow Declarative Pipeline: Auto Loader bronze → expectation-tracked, quarantine-split silver → dimensional gold; UCI Online Retail II (1,067,371 rows, 9 verified DQ classes) + clickstream | `workspace.retail.*`: bronze_retail/bronze_events, silver_sales/silver_events (+quarantines), dim_products, fact_sales, gold_daily_revenue, gold_customer_rfm, gold_sessions, gold_funnel_daily |
| **Data Science** | Primary analytical consumer | `notebooks/50_ds_kpis_and_models.py` — KPIs from gold, `AI_FORECAST` demand forecast, sklearn purchase-propensity model logged to MLflow/UC registry | `gold_revenue_forecast`, registered model, certified metric definitions |
| **PM / Marketing** (business customers) | Ask questions; bring jargon, vagueness, and noise | `src/genie_autopilot/fleet_retail.py` — persona fleets driving the REAL retail Genie space: clean questions, jargon traps (GMV, AOV, whales, churn risk), vague noise ("how are we doing?"), unanswerable asks (no cost/store data) | Real Genie conversations + feedback + corrections = flywheel telemetry |
| **Governance / the Autopilot** | The semantic-layer team, automated | telemetry → drift detection → governed healing → benchmark regression gate (v1 modules, now domain-parametrized) | Healed UC comments/tags, metric-view synonyms, Genie space context; audit ledger |

## The learning loops (v2 additions)

1. **Predictive: query-quality filter** (`quality.py`). Trained on labeled fleet outcomes
   (kind ∈ clean/jargon/vague/unanswerable), it scores incoming PM questions BEFORE they
   burn Genie quota: `run` / `reject` / `human_review`. Bad-question filtering is itself a
   governance feature — the noise the org produces becomes the classifier's training set.
2. **Unsupervised: drift clustering** (drift scoring + `ai_query` extraction in
   notebook 20). Corrections and negative-feedback text cluster into term→entity
   proposals ranked by authority × frequency × freshness.
3. **Human-in-the-loop routing** (`lakebase.py`). Proposals below the auto-approve gate
   and questions routed `human_review` land in a **Lakebase** (serverless Postgres,
   Free Edition: 1 project) `hitl_queue` — the operational store pattern for agent
   systems. Decisions flow back as approvals; every applied healing is in
   `healing_history` + the Delta audit ledger.
4. **Regression gate** (notebook 40 + `evals.py`). Genie Benchmarks scored before/after
   each healing cycle; regressions auto-flag rollback. `autopilot_eval_history` is the
   longitudinal evidence table.

## Objective scoring (why labeled chaos matters)

The producer's `ground_truth.jsonl` labels every injected defect. That means the DQ layer
is scored with precision/recall (did quarantine catch exactly the malformed/bot/PII
events?), and the semantic loops are scored against `QUESTION_LABELS` (did the filter
reject the noise? did healing fix the jargon?). No vibes — confusion matrices.

## Databricks feature coverage (Free Edition verified)

Auto Loader + Declarative Pipelines w/ expectations · Unity Catalog (schemas, volumes,
comments, tags, constraints) · Metric Views (YAML 1.1 + synonyms) · Genie Spaces +
Conversation API + Space Management API + Benchmarks eval runs · AI Functions
(`ai_query`, `AI_FORECAST`) · MLflow + UC model registry · Asset Bundles · Lakebase
(project-based) · system.query.history telemetry. **Genie Ontology** (June 2026, Public
Preview) is account-team gated — unavailable on Free Edition; this project is the
public-API embodiment of the same conviction and the docs position it as an Ontology
on-ramp, not a competitor.

## Long-horizon roadmap

- [x] Phase A — banking flywheel v1 (schema, metric views, space via API, live smoke test)
- [x] Phase B — retail medallion: UCI ingest, DQ quarantine, gold marts, clickstream layer
- [ ] Phase C — retail Genie space + benchmarks loaded via API; baseline eval score
- [ ] Phase D — persona fleets run; telemetry → drift → HITL (Lakebase) → healing; post-heal eval
- [ ] Phase E — learning loops trained on labeled outcomes; DQ precision/recall scorecard
- [ ] Phase F — AI/BI health dashboard, demo recording, interview-pitch refresh (v2 story)
