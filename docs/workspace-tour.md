# Workspace Tour: Discovery UX by Persona

Design principle: **nobody should be told "look here."** Each persona's natural first
action leads to a planted discovery hook; the aha moments are found by *doing*. This
doc scripts those paths — use it to dry-run the demo and to brief anyone exploring
the workspace cold.

## The discovery hooks (planted, not pointed at)

| Hook | Where it's planted | The aha it triggers |
|---|---|---|
| The +0% anomaly | Dashboard headline + Accuracy Trend (synonym_healing phase is flat) | "Why did a healing pass do NOTHING?" → pulls the viewer into the definitions-vs-vocabulary finding |
| The machine's fingerprints | Catalog Explorer: column comments reading *"Learned synonym: 'whales'. Auto-hydrated by Genie Autopilot from interaction telemetry"* | "Wait — who wrote this metadata?" → audit ledger answers with lineage |
| The clarifying question | Genie space sample question *"How did sales do last week?"* | Genie ASKS which 'sales' you mean → poison-term governance, discovered by asking |
| The quarantine with reasons | `quarantine_sales.quarantine_reasons` array column | "The pipeline explains WHY each row was rejected" |
| The perfect DQ scorecard | Dashboard counters + eval evidence | "How do you KNOW recall is 100%?" → labeled chaos / ground-truth design |

## Persona scripts — what they'd naturally ask, and what happens

### 1. Non-technical executive (CFO-type) — starts at the Genie space
- **"What was our net revenue last month?"** → clean answer off the metric view. Trust established.
- **"What was our GMV?"** → correct despite jargon (healed synonym). They don't know it's remarkable yet.
- **"How did sales do last week?"** → *Genie asks which 'sales' they mean.* The aha: the system knows the org disagrees about this word — most humans don't.
- **"Who are our whale customers?"** → top 20 by monetary, the certified threshold. Follow-up if curious: "why 20?" → the definition came through a governed review queue.

### 2. Product manager / analyst — starts at the dashboard
- Reads the headline (40%→90%, control 100%), notices the **flat synonym_healing phase** in Accuracy Trend → clicks into `docs/eval-evidence.md` (linked in widget description) → finds the stratified table and failure reasons.
- Natural follow-up in Genie: **"What's our conversion trending at?"** and **"which countries return the most product?"** — both work; the funnel and returns stories connect back to the dashboard charts they just saw.

### 3. Data engineer — starts at Catalog Explorer
- Browses `workspace.retail` → sees bronze/silver/gold + quarantine tables with **quality tags** and machine-written comments.
- Opens `quarantine_sales` → `quarantine_reasons` array → asks "what generates these?" → `pipelines/retail_medallion.py` in the Git folder: expectations tracked in the pipeline event log, the 9 documented DQ classes, the two-sheet dedup trap.
- Opens the pipeline run graph → sees Auto Loader streams + MV fan-out. The aha: **the DQ rules map 1:1 to documented, verified defects in a real public dataset.**

### 4. ML / platform engineer — starts at the repo
- `make test` (47 green) → `phase_d.py` → traces the loop: paced client → mining → scoring → triage → three appliers → benchmark gate.
- The aha lands in `evals.py`: **stratified lift with a no-regression verdict and rollback semantics** — this is an eval harness, not a demo script.
- Second aha: `producer.py` chaos is *labeled* → DQ precision/recall are measured, not asserted.

### 5. Security / governance reviewer — starts anywhere
- Every mutation traces: `autopilot_audit_ledger` (who/what/when/approver lane) → HITL queue on Lakebase → poison terms that were *refused* auto-healing. The aha: **the system's power is bounded by explicit gates, and the gates leave receipts.**

## Sample business prompts (curated in the space's sample questions)

Clean trust-builders: "What is the total transaction amount by account type?" ·
"Net revenue by country last month" · "How many sessions converted this week?"
Jargon (healed): "What was our GMV last month?" · "Who are our whales?" ·
"What's our AOV by country?" · "What's the take rate of returns?"
Poison (clarifies): "How did sales do last week?"
Honest limits (filtered or refused): "Why is revenue down?" · "What's our margin by product?"

## Dry-run checklist before any live audience

1. Warehouse warm (run one dashboard refresh).
2. Ask the poison question once yourself — confirm the clarify behavior.
3. Confirm the audit ledger widget renders (table is auto-mirrored by the flywheel).
4. Have `docs/eval-evidence.md` open in a tab — it's the receipts drawer.
