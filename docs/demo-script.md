# 5-Minute Demo Script

Audience: Databricks RSA panel / hiring manager. Everything below runs live on Free
Edition; every number is reproducible from this repo.

## 0:00 — The problem (30s)

"Business users don't know your schema. They say GMV, whales, take rate, bounce — and
they assume the semantic layer automagically knows. I measured what actually happens."

Show: `docs/eval-evidence.md` baseline table — clean 75%, bleeding-edge dialect **40%**.

## 0:30 — The system (60s)

Show: `docs/architecture-v2.md` flywheel diagram. Narrate the loop: real Genie traffic
from simulated personas → feedback + corrections mined as telemetry → drift scoring
(authority × frequency × freshness, ≥2-user gate) → governed healing across three
surfaces (UC metadata, Metric View synonyms, Genie space context) → benchmark
regression gate with rollback → Lakebase HITL queue for everything below the gate.

Mention: the medallion behind it — real UCI data (1.07M rows, 9 verified DQ classes),
chaos-labeled clickstream, quarantine with machine-readable reasons.

## 1:30 — The experiment that matters (90s)

"First healing pass: synonyms only. Lift: **zero**. Genie already knew the vocabulary —
it failed on definitions: whales... top how many? take rate... ratio of what? bounce...
fraction or percent?" Show: `docs/semantic-failure-taxonomy.md` table (8 failure modes,
only one is synonym-fixable).

"Second pass: the HITL reviewer enriches proposals into certified definitions —
formula, threshold, units, output shape. Result:"

```
jargon: 40% → 80%   clean control: 75% → 100%   aggregate: 56% → 89%
```

## 3:00 — Poison terms (60s)

"'Sales' means net revenue to finance and unit volume to merchandising. The system
detects contradictory corrections as a conflict signal — poison terms NEVER auto-heal;
they become a disambiguation instruction: Genie asks which metric you mean."

Show: Phase E section of the evidence log + the live Genie space instructions.

## 4:00 — Live proof (60s)

In the Genie space (Retail Analytics), ask live:
1. "What was our GMV last month?" — correct MEASURE on the metric view
2. "Who are our whales?" — top 20 by monetary (the certified threshold)
3. "How did sales do last week?" — Genie asks which 'sales' you mean

Close: "Everything is governed — audit ledger, benchmark gate, human queue on Lakebase.
This is Genie Ontology's conviction implemented on public GA APIs, today, by one person
in three weeks on Free Edition."

## Reproduce from scratch

```bash
make install datagen bootstrap          # schema + data + metric views
python -m genie_autopilot.phase_d       # fleet → heal → stratified eval
python -m genie_autopilot.phase_e       # collision stratum + poison terms + DQ scorecard
```
