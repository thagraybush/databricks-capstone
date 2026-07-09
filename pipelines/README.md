# pipelines — the retail medallion

One Lakeflow Declarative Pipeline (`retail_medallion.py`, serverless, triggered)
carrying two lanes from raw files to Genie-ready gold: UCI Online Retail II sales
(1,067,371 rows) and the chaos producer's clickstream. Deployed by
[../resources/retail_pipeline.yml](../resources/retail_pipeline.yml) into
`workspace.retail`.

## Contents

| File | Responsibility |
|---|---|
| `retail_medallion.py` | Bronze (Auto Loader, as-landed, all strings) → silver (typed, deduped, quarantine-split) → gold marts (`dim_products`, `fact_sales`, `gold_daily_revenue`, `gold_customer_rfm`, `gold_sessions`, `gold_funnel_daily`) |

## DQ classes (verified against the real file — see the pipeline docstring)

| # | Class | Disposition |
|---|---|---|
| 1 | Missing Customer ID (22.77%) | KEPT in silver, flagged `is_anonymous` (valid revenue) |
| 2 | C-prefix invoices (returns, 1.83%) | KEPT, `is_return = true` (valid business events) |
| 3 | A-prefix invoices (bad-debt adjustments) | QUARANTINE `adjustment_invoice` |
| 4 | Negative qty NOT on a C-invoice | QUARANTINE `stock_adjustment` |
| 5 | Zero/negative price (non-return) | QUARANTINE `non_positive_price` |
| 6 | Non-product stock codes (POST, M, …) | QUARANTINE `non_product_code` |
| 7 | Exact duplicates + Dec-2010 two-sheet overlap | Deduplicated in silver |
| 8 | Unparseable qty/price/date | QUARANTINE `unparseable` |
| 9 | Description drift (1,232 codes) | Resolved in `dim_products` via modal description |

Clickstream gets its own structural rules (`has_event_id`, `known_event_type`,
`parseable_ts`) plus in-silver PII scrubbing (emails in referrers → `[REDACTED]`,
`pii_detected` flag) and downstream bot flagging in `gold_sessions`.

## Quarantine philosophy

Business-odd rows (returns, anonymous sales) are *kept and flagged* — quarantine is
reserved for structural violations, and every quarantined row carries a
machine-readable `quarantine_reasons` array so triage is a query, not an archaeology
dig. All rules are also tracked expectations (`@dp.expect_all`) so the pipeline event
log doubles as the DQ scorecard — and because the producer labels its chaos, the
quarantine is scored with precision/recall against ground truth (PII 100%/100%, bots
100%/100%, dupes 68/68, malformed 14/14 — [../docs/eval-evidence.md](../docs/eval-evidence.md)).

## How to run

```bash
databricks bundle deploy -t dev --profile free-edition   # from a laptop
databricks bundle run retail_medallion -t dev            # triggered update
databricks bundle run retail_medallion -t dev --full-refresh-all   # rebuild all tables
```

Raw inputs land in `/Volumes/workspace/retail/raw/` (CSV globs `online_retail_*.csv`,
JSONL glob `clickstream/events_*.jsonl`) — see [../data_gen/README.md](../data_gen/README.md).

## The v3 drift-wave expectation

From `V3_START` (2026-07-14), the producer "ships" a nested v3 event envelope
([../src/genie_autopilot/drift_pack.py](../src/genie_autopilot/drift_pack.py)). The
flat-column `either("event_id", "eventId")` normalization finds no top-level id, so
`has_event_id` fails and v3 events land in `quarantine_events` **by design**. The
operational drill: watch the quarantine trend spike, diagnose from quarantine rows
alone, ship a v3 normalizer, and score the fix against the `schema_v3` ground-truth
labels. Details: [../docs/drift-cadence.md](../docs/drift-cadence.md).

Related: [architecture v2](../docs/architecture-v2.md) ·
[data generators](../data_gen/README.md) · [evidence log](../docs/eval-evidence.md)
