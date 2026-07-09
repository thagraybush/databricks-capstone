# data_gen — datasets, chaos, and ground truth

Everything the simulated data organization "produces": the real UCI retail dataset
(converted, never cleaned), deterministic synthetic banking data, labeled clickstream
chaos, and the session manifests that attribute Genie traffic to personas.

## Contents

| Path | Responsibility |
|---|---|
| `convert_uci.py` | UCI Online Retail II xlsx (two sheets) → raw CSVs for volume upload — **zero cleaning by design** |
| `generate_banking_data.py` | Deterministic synthetic banking data (seeded RNG, no real PII) → batched `output/inserts.sql` |
| `raw/` | UCI source files (xlsx/zip + converted CSVs) — gitignored bulk data |
| `output/clickstream/` | Producer event batches (`events_batch_*.jsonl`) + `ground_truth.jsonl` defect labels |
| `output/sessions/` | `session_manifest.jsonl` — per-interaction persona attribution from the session engine |
| `output/inserts.sql` | Banking INSERT batches consumed by `make bootstrap` |

## The UCI dataset (verified DQ inventory)

1,067,371 rows across two sheets, with every documented defect deliberately preserved
for bronze: 22.77% missing Customer ID, C-prefix return invoices (1.83%), A-prefix
bad-debt adjustments, negative quantities outside returns, zero/negative prices,
exact duplicates plus the Dec-2010 two-sheet overlap, non-product stock codes, and
description drift across 1,232 codes. Cleaning is the pipeline's job
([../pipelines/README.md](../pipelines/README.md)) — the converter's only
transformation is xlsx → CSV.

## The chaos producer and the labels philosophy

[../src/genie_autopilot/producer.py](../src/genie_autopilot/producer.py) emits
clickstream against the real UCI product catalog with configurable, **labeled** chaos:
duplicates, late events, v1→v2 schema drift, bot sessions, malformed lines, and
PII-in-referrer. Every injected defect is recorded in `ground_truth.jsonl`, which is
the whole point: **ground truth turns DQ from vibes into precision/recall.** The
quarantine layer is scored as a classifier against these labels (PII 100%/100%, bots
100%/100%, dupes 68/68, malformed 14/14 —
[../docs/eval-evidence.md](../docs/eval-evidence.md)). The calendar-staged v3 wave
([../src/genie_autopilot/drift_pack.py](../src/genie_autopilot/drift_pack.py)) writes
its batches to `output/clickstream_v3/`. Upload only the event batches to
`/Volumes/workspace/retail/raw/clickstream/`; **keep `ground_truth.jsonl` local — it
is the answer key.**

## Session manifests

The session engine
([../src/genie_autopilot/session_engine.py](../src/genie_autopilot/session_engine.py))
appends every multi-turn interaction (persona, question, conversation/message ids,
rating, correction) to `output/sessions/session_manifest.jsonl`. Free Edition runs
one PAT identity, so persona attribution lives in this manifest rather than in
workspace users — notebook 10 joins it during telemetry harvest and notebook 60
trains the semantic router on the resulting corpus.

## How to run

```bash
make datagen                                   # banking inserts.sql (seeded, idempotent)
.venv/bin/python data_gen/convert_uci.py       # UCI xlsx → raw CSVs (needs raw/online_retail_II.xlsx)
.venv/bin/python -m genie_autopilot.producer   # labeled clickstream batches
```

Related: [medallion pipeline](../pipelines/README.md) ·
[architecture v2](../docs/architecture-v2.md) · [drift cadence](../docs/drift-cadence.md)
