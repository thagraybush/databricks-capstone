# Run-the-Business Scenarios: From → To

The flywheel's value shows up in *recurring* work, not demos. This doc walks five
personas through their weekly/monthly run-the-business (RTB) loops on this retail
business, before and after the flywheel accumulated their interaction data. Every
"after" claim is tied to a measured number in [eval-evidence.md](eval-evidence.md);
the "before" friction (ticket queues, multi-day turnarounds) is the standard
pre-self-serve pattern, and the *accuracy* half of it is measured: the naive space
answered business dialect at **40%** (jargon stratum, baseline run
`01f17b5a77d6112e8e10ccb8cf6130f6`). The certified formulas behind every prompt
below live in [sql/business_kpis.sql](../sql/business_kpis.sql).

No dollar figures are invented anywhere in this doc — the honest currency here is
time (self-serve vs ticket queue) and accuracy (measured eval lift).

## 1. CFO — month-end close review (monthly)

**The RTB loop.** First business day after close: reconcile the month — net revenue,
returns drag, AOV, growth vs prior month — before the numbers go into the board pack.

**From.** The close pack was an analyst ticket: request on day 1, draft on day 3,
one revision cycle because "sales" meant net revenue to finance and units to the
analyst who pulled it. When the CFO tried Genie directly, the finance dialect
misfired: at baseline, "What's the take rate of returns?" returned the numerator
instead of the ratio (`LLM_JUDGE_INCORRECT_METRIC_CALCULATION`) — part of the 40%
jargon baseline.

**To.** The close review is a five-minute Genie session in the CFO's own vocabulary:

- "What was our net revenue last month?"
- "What was our GMV by month?"
- "What's the take rate of returns?" — now the certified ratio
  (returns_value / gross_revenue, the `kpi_monthly_summary.return_rate_pct` formula)
- "What's our AOV by country?"
- "How did sales do last week?" — Genie **asks which 'sales' they mean** instead of
  guessing; the poison term was healed as a disambiguation instruction, never a synonym

**Evidence.** Take rate: baseline FAIL → healed PASS; jargon stratum 40% → 80% mean
(70–90% across 3 repeat runs); clean control 100% stable. Poison-term handling:
`{'sales': ['net_revenue', 'quantity']}` detected and healed as a clarify instruction
(Phase E runs).

## 2. PM / analyst — weekly funnel standup

**The RTB loop.** Monday standup: conversion trend, bounce share, revenue per
session — decide where the week's funnel work goes.

**From.** The standup deck was assembled by hand from ad-hoc SQL; "what share of
sessions are bounces?" asked in Genie at baseline simply failed (no certified bounce
definition existed — is it single-view? no-cart? bot-inclusive?). Wrong numbers in a
standup are worse than no numbers: they redirect a week of work.

**To.** The standup runs live off Genie against the same definitions as
`kpi_funnel_weekly`:

- "What's our conversion by day?"
- "What share of sessions are bounces?" — certified as non-bot sessions with one
  view, no carts, no purchases
- "How many sessions and purchases were there per day?"
- "What is the average session duration in seconds, excluding bots?"

The PM's noisy asks ("how are we doing?", "why is revenue down?") no longer burn a
wrong answer — the query-quality router rejects or routes them to human review
*with a reason* the PM can act on.

**Evidence.** Bounce question: baseline FAIL → healed (jargon stratum 40% → 80%
mean). Clean funnel questions: 100% post-heal, stable across repeats. Noise
filtering: 5/6 noise questions caught (classifier accuracy 0.90 on the labeled
training set).

## 3. Marketing lead — monthly retention & returns review (monthly)

**The RTB loop.** Segment the book for the month's campaigns: who are the high-value
customers, who is drifting away, and which markets are quietly bleeding value to
returns.

**From.** "Whales" and "churn risks" were tribal vocabulary with no written
thresholds — at baseline both questions failed (`RESULT_EXTRA_ROWS`,
missing-filter judge reasons): Genie returned *something*, with no cutoff, which is
how a campaign gets targeted at the wrong list. Even the clean "total value of
returns by country" failed at baseline.

**To.** Self-serve segmentation in the marketing lead's own dialect:

- "Who are our whales?" — top 20 by RFM monetary, the certified threshold that came
  through the HITL review queue
- "Which customers are churn risks?" — recency_days > 90, the same threshold
  `kpi_customer_health.churn_risk_count` encodes
- "What is the total value of returns by country?"
- "What is total net revenue by country?" — cross-checks `kpi_country_performance`

**Evidence.** Whales and churn-risk: baseline FAIL → healed PASS (jargon 40% → 80%
mean, 70–90% range). Returns-by-country: baseline FAIL → clean stratum 100% after
definition healing. Both thresholds are auditable: 6 healings approved via the human
lane (0 auto) in `autopilot_audit_ledger`.

## 4. Data engineer — daily DQ triage

**The RTB loop.** Morning check: what did last night's pipeline quarantine, and why?
Triage before consumers notice.

**From.** DQ triage was reactive — "the numbers look wrong" tickets arriving days
after the fact, with no way to distinguish a pipeline defect from a semantic misfire
(a business user's jargon question answered with the wrong metric looks identical to
bad data from the outside). At the 40% jargon baseline, a large share of "data
quality" complaints were actually vocabulary failures landing in the DE's queue.

**To.** Triage is a scan of `quarantine_sales.quarantine_reasons` (every rejected
row explains itself) plus the dashboard DQ counters — and the semantic-misfire
tickets largely stopped arriving because the flywheel heals them upstream. What
remains in the queue is real: the quarantine catches exactly the labeled defects.

- Genie sanity-check prompts: "How many invoices were there per day?" ·
  "Show daily net revenue for December 2011"
- Dashboard: quarantine mix + clickstream DQ health
  ([sql/dashboard_queries.sql](../sql/dashboard_queries.sql), blocks 2–3)

**Evidence.** DQ scorecard vs producer ground truth: PII 100%/100% and bots
100%/100% (precision/recall), duplicates 68/68 removed, malformed 14/14 quarantined
— measured against labeled chaos, not vibes.

## 5. Merchandising / ops — weekly assortment review

**The RTB loop.** Weekly: which products are moving, how fast, and whether baskets
are getting deeper — feed the reorder and range decisions.

**From.** Merchandising dialect was the worst-served at baseline: "sell-through
velocity" and "basket attach rate" both failed (definition gaps — units per selling
day, lines per invoice — that no synonym could fix; synonym-only healing measured
exactly +0%). And "sales" meant *units* to this team while finance meant revenue —
the same word, two metrics, silently wrong for one of them.

**To.** The weekly review is self-serve in merchandising vocabulary:

- "What's the sell-through velocity of our top 10 products?" — units per distinct
  selling day, certified formula
- "What's our basket attach rate?" — line items per invoice
- "Top 10 products by units sold"
- "How did sales do last week?" — Genie clarifies revenue vs units before answering;
  merchandising picks units and gets *their* number

**Evidence.** Sell-through and attach rate: baseline FAIL → healed (within the
40% → 80% jargon lift); synonym-only pass produced +0% lift, proving the healing
unit is the certified definition (run `01f17ba60e001d47855597a64abad214` vs
`01f17ba6fd231534b2200d3f90b556e6`). Poison term 'sales' → clarify, logged in the
audit ledger as a disambiguation instruction.

## Summary: from → to

| Persona | RTB task | Before | After | Evidence |
|---|---|---|---|---|
| CFO | Month-end close review | Analyst ticket, multi-day turnaround; take-rate misfire | 5-min self-serve close in finance dialect; 'sales' clarifies | Jargon 40% → 80% mean (70–90%); poison-term instruction live |
| PM / analyst | Weekly funnel standup | Hand-built deck; bounce question failed outright | Live Genie standup on certified funnel definitions; noise filtered with reasons | Bounce FAIL → healed; clean 100% stable; 5/6 noise caught |
| Marketing lead | Retention & returns review | Unwritten whale/churn thresholds → wrong campaign lists | Certified thresholds (top-20 monetary, recency > 90) on first ask | Whales/churn FAIL → PASS; 6 human-approved healings in audit ledger |
| Data engineer | Daily DQ triage | Reactive "numbers look wrong" tickets, semantics mixed with DQ | Self-explaining quarantine + upstream semantic healing | DQ precision/recall 100%/100% (PII, bots); 68/68 dupes, 14/14 malformed |
| Merchandising / ops | Weekly assortment review | Sell-through/attach-rate misfires; 'sales' silently wrong | Self-serve in merch dialect; 'sales' disambiguates to units | Synonym-only +0% vs definition healing +40 pts; clarify behavior |

The pattern across all five: the flywheel didn't make people learn the warehouse's
vocabulary — it made the warehouse learn *theirs*, with every learned definition
gated, audited, and regression-tested.
