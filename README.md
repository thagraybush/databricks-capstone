# Genie Autopilot — Autonomous Context Engine

Autonomous Context Engine: an autonomous learning system that treats user interaction
with Genie as a real-time telemetry stream. By mining feedback and query logs, the
system dynamically hydrates Metric Views, Unity Catalog metadata, and Genie space
context — converting raw human behavior into automated metadata & governance actions.

**The demo artifact:** a quantified before/after — Genie benchmark accuracy at baseline
vs. after one governed, fully autonomous healing cycle. *(Scores land here in Week 3.)*

```
user friction (thumbs-down + corrections)
   → telemetry ingest (Conversation API / system tables)
   → drift detection (deterministic parser + ai_query, scored by authority·frequency·freshness)
   → governed healing gate (auto-approve threshold, human queue, audit ledger)
   → three appliers: UC comments/tags · Metric View YAML synonyms · Genie serialized_space
   → benchmark regression gate (eval-run API) with automatic rollback
```

Full design: [docs/architecture.md](docs/architecture.md) ·
Pitch: [docs/interview-pitch.md](docs/interview-pitch.md) ·
Study track: [docs/study-plan.md](docs/study-plan.md)

## Scenario

Cross-BU Retail Banking & Compliance (synthetic data, seeded RNG, zero real PII):
a wealth advisor's *"liquid assets"* (`fact_wealth_portfolios.liquid_cash_assets`) and a
branch manager's *"available balance"* (`fact_transactions.available_balance`) collide.
Genie starts with deliberately sparse metadata, fails the jargon questions, and earns
its improved semantic layer from user friction alone.

## Quickstart

```bash
make install          # venv + package
make test lint        # pure-python units (parser, scorer, YAML regen, gate)
make datagen          # deterministic synthetic banking data → data_gen/output/inserts.sql

# Workspace targets need a Databricks Free Edition PAT in the macOS Keychain:
#   security add-generic-password -s databricks-fe -a <you> -w <token>
make bootstrap        # schema + data + metric views + Genie space
make eval             # baseline benchmark scorecard
make simulate         # persona fleet drives REAL Genie traffic + feedback (paced ≤5 q/min)
make detect           # scored drift proposals from harvested telemetry
make heal             # governed application + audit ledger
make eval             # post-healing scorecard → the before/after
```

Runs entirely on [Databricks Free Edition](https://docs.databricks.com/aws/en/getting-started/free-edition)
(serverless-only, PAT auth). Built with Metric Views (YAML spec 1.1), the Genie
Conversation & Space Management APIs (GA 2026), Genie Benchmarks eval runs, AI
Functions on serverless SQL, and Databricks Asset Bundles.

## Status

- [x] Package skeleton: paced Genie client, drift scorer, healing appliers, eval runner
- [x] Banking schema, metric views, synthetic data generator, seed benchmark suite
- [ ] Week 1: workspace bootstrap, Genie space via API, baseline benchmark score
- [ ] Week 2: telemetry ingest job, ai_query extraction pass, end-to-end healing cycle
- [ ] Week 3: before/after scorecard, rollback drill, health dashboard, demo recording

---

*Personal educational project on Databricks Free Edition. Synthetic data only.
Not affiliated with or endorsed by Databricks.*
