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
