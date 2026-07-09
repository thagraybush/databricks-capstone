# Why Semantic Layers Fail: A Field Taxonomy

Business users do not know the schema, the normalization, or the hygiene rules — they
assume the system "automagically" knows what they mean. This document classifies the
failure modes observed **experimentally in this project** (live Genie benchmark runs,
2026-07-09) and maps each to its detection signal and its healing unit. The core
finding: *most semantic-layer failures are not vocabulary problems, and healing the
wrong layer produces exactly 0% lift.*

## The experimental evidence

Baseline (naive space): clean stratum 6/8 (75%), jargon stratum 4/10 (40%).
After **synonym-only healing** (UC comments/tags + metric-view synonyms + term→column
instructions): jargon 4/10 → 4/10, **lift +0%**. Genie was already choosing the right
tables and columns — every remaining failure was a definition, threshold, unit, or
shape gap. Healing with **certified definitions** (formula + threshold + units + output
shape, enriched by the human reviewer in the HITL queue) is what moves the number.

## The taxonomy

| # | Failure mode | Example (observed or injected) | Detection signal | Healing unit |
|---|---|---|---|---|
| 1 | **Alias collision** (many names → one metric) | "gross sales" = "sales" = "GNS" = "Gross Net Sales" = `gross_revenue` | Repeated corrections mapping different terms to one entity | Synonyms (metric-view `synonyms:`, space column_configs) — the ONLY mode synonyms fix |
| 2 | **Alias ambiguity / poison terms** (one name → many metrics) | "sales" = `net_revenue` to finance, `units` to merchandising | Contradictory corrections for the same term from different roles | Disambiguation instruction ("when the user says 'sales', ask whether they mean revenue or units") — never auto-resolve |
| 3 | **Definition drift** (term implies a formula nobody wrote down) | "take rate" = returns_value ÷ gross_revenue; Genie returned the numerator | `LLM_JUDGE_INCORRECT_METRIC_CALCULATION`, `SINGLE_CELL_DIFFERENCE` | Certified formula in the glossary; ideally a metric-view measure so the formula is governed code |
| 4 | **Threshold vagueness** (segment words without cutoffs) | "whales" (top how many?), "churn risks" (how stale?) | `RESULT_EXTRA_ROWS`, missing-filter judge reasons | Certified threshold ("top 20 by monetary", "recency_days > 90") via HITL |
| 5 | **Unit confusion** | bounce rate as 0–1 fraction vs ×100 percentage | `SINGLE_CELL_DIFFERENCE` with ~100x deltas | Unit convention in glossary ("rates are fractions unless % requested") |
| 6 | **Shape ambiguity** (right numbers, wrong columns/rows) | extra `country` grouping; missing `LIMIT 10` | `RESULT_EXTRA_ROWS`, `RESULT_MISSING_COLUMNS` | Output-shape instruction + benchmark questions that fully specify shape |
| 7 | **Business-rule blindness** (hygiene rules the business assumes) | Sales metrics must exclude returns (`NOT is_return`); returns valued as `-line_amount` | Systematic small deltas vs certified answers | Rule instruction + encode into gold tables so the rule is structural, not conversational |
| 8 | **Granularity/temporal fuzz** | "last month" (calendar? rolling 30d?), daily vs monthly grain | Disagreement on date filters across runs | Temporal convention instruction; date-spine documentation |

Modes 3–8 are invisible to synonym healing — they need **definitional context**, and the
richest corrections ("take rate means returns_value divided by gross_revenue") lose
their formula if the miner reduces them to term→column pairs (we hit exactly this bug:
`parse_correction` captured only the first entity token).

## Design consequences (implemented / planned)

1. **The healing unit is the certified definition**, not the synonym. The HITL queue is
   where a data steward turns a raw complaint into formula + threshold + units + shape.
   Synonym-only auto-healing stays for mode 1, gated at ≥2 distinct users.
2. **Poison terms must never auto-heal.** Contradictory corrections for one term are a
   conflict signal → route to HITL, heal as a disambiguation instruction (mode 2).
3. **Benchmarks are stratified** (clean control / jargon treatment / collision / noise)
   so lift is attributable per failure mode, and golden questions must fully specify
   output shape or they measure shape-guessing, not semantics.
4. **Two telemetry sources**: user thumbs-downs + corrections (conversational reality)
   and benchmark regression failures (curated ground truth = highest authority).
5. **Structural beats conversational**: whenever a definition stabilizes, promote it
   from an instruction into a metric-view measure (governed code) — instructions are
   the cache, metric views are the database.
6. **The endgame is Genie Ontology-shaped**: authority-ranked, usage-refreshed context.
   This project implements the public-API version of that conviction today.
