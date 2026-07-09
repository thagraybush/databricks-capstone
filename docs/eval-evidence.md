# Eval Evidence Log

## Baseline — retail space, eval run `01f17b5a77d6112e8e10ccb8cf6130f6` (2026-07-09)

**Aggregate: 10/18 = 56%** · clean: 6/8 = 75% · jargon: 4/10 = 40%

Failure reasons: {"RESULT_EXTRA_ROWS": 2, "LLM_JUDGE_INCORRECT_TABLE_OR_FIELD_USAGE": 2, "RESULT_MISSING_ROWS": 1, "LLM_JUDGE_INCOMPLETE_OR_PARTIAL_OUTPUT": 1, "RESULT_MISSING_COLUMNS": 3, "LLM_JUDGE_INCORRECT_METRIC_CALCULATION": 3, "LLM_JUDGE_MISSING_OR_INCORRECT_FILTER": 2, "SINGLE_CELL_DIFFERENCE": 2}

| stratum | verdict | question |
|---|---|---|
| clean | FAIL | Top 10 products by units sold |
| clean | FAIL | What is the total value of returns by country? |
| clean | PASS | How many invoices were there per day? |
| clean | PASS | How many known customers do we have per country? |
| clean | PASS | How many sessions and purchases were there per day? |
| clean | PASS | Show daily net revenue for December 2011 |
| clean | PASS | What is the average session duration in seconds, excluding bots? |
| clean | PASS | What is total net revenue by country? |
| jargon | FAIL | What share of sessions are bounces? |
| jargon | FAIL | What's our basket attach rate? |
| jargon | FAIL | What's the sell-through velocity of our top 10 products? |
| jargon | FAIL | What's the take rate of returns? |
| jargon | FAIL | Which customers are churn risks? |
| jargon | FAIL | Who are our whales? |
| jargon | PASS | How many daily shoppers do we get? |
| jargon | PASS | What is our GMV by month? |
| jargon | PASS | What's our conversion by day? |
| jargon | PASS | What's the average basket in the UK? |

## Post-healing — eval run `01f17ba60e001d47855597a64abad214`

Aggregate: 10/18 = 56%

```
jargon (bleeding-edge dialect): 4/10 → 4/10 (40% → 40%, lift +0%)
clean (control, no-regression):  6/8 → 6/8 (75% → 75%)
verdict: KEEP
```

Healings: 6 approved (0 auto, 6 human-reviewed), 1 metric views updated, 6 space instructions added.

## Post-glossary healing (HITL-enriched definitions) — eval run `01f17ba6fd231534b2200d3f90b556e6`

Aggregate: 16/18 = 89%

```
jargon (bleeding-edge dialect): 4/10 → 8/10 (40% → 80%, lift +40%)
clean (control, no-regression):  6/8 → 8/8 (75% → 100%)
verdict: KEEP
```

Key finding: pure synonym healing produced +0% — every benchmark failure was a metric-DEFINITION
or output-shape gap, not vocabulary. The healing unit for bleeding-edge dialect is the certified
definition (formula + threshold + units + shape), enriched by the human reviewer in the HITL queue.

## Phase E — collision stratum, poison terms, learning loops (runs `01f17bb4fe4a181488fdc86bbbde1030` → `01f17bb642a11dada5877a044b9b378c`)

```
collision: 4/4 → 4/4 (100% → 100%)
jargon   : 8/10 → 9/10 (80% → 90%)
clean    : 8/8 → 8/8 (100% → 100%)
```

Poison terms detected (healed as disambiguation instructions, never synonyms): {'sales': ['net_revenue', 'quantity']}

Query-quality classifier: training-set metrics: {'accuracy': 0.9032258064516129, 'precision': 0.7142857142857143, 'recall': 0.8333333333333334}; noise filtered (reject|human_review): 5/6

DQ scorecard vs producer ground truth:

```
PII detection: precision 19/19 = 100%, recall 19/19 = 100%
Bot detection (session level): precision 8/8 = 100%, recall 8/8 = 100%
Duplicates: 68 labeled dup emissions, 68 rows removed by dedup
Malformed: 14 labeled truncations, 14 rows quarantined (quarantine also catches unknown types/bad timestamps)
```

## Variance study — 3 repeated eval runs on the healed space

```
clean    : mean 100%  range [100%, 100%]  runs: 8/8, 8/8, 8/8
collision: mean 100%  range [100%, 100%]  runs: 4/4, 4/4, 4/4
jargon   : mean 80%  range [70%, 90%]  runs: 8/10, 7/10, 9/10
```

Genie is nondeterministic; stability is reported as mean ± range across repeats rather than a single-run point estimate.
