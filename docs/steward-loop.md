# The Steward Loop: a Self-Healing Semantic Ecosystem

The certification session that produced taxonomy mode 9 was run by a human at a CLI.
This document specifies that process as a **system component**: the platform monitors
for ambiguity, novel metrics, and changing business definitions; elevates decisions to
a metric steward; and improves its own training data continuously — while **no user
session ever blocks**. Genie and AI/BI always answer from the current certified
context; the loop makes tomorrow's answers better than today's.

## The learning-modality map

| Modality | Component | What it learns | Databricks feature |
|---|---|---|---|
| **Supervised** | Semantic router (`semantic_router` in UC) | answerability, target metric, ambiguity — from labeled fleet outcomes + steward decisions | MLflow + UC model registry, serverless training |
| **Supervised** | Purchase propensity (`purchase_propensity`) | the DS persona's business model | MLflow, `AI_FORECAST` |
| **Unsupervised** | Drift clustering (`drift.score_proposals`) | term→entity mappings from correction telemetry, ranked authority × frequency × freshness | Genie Conversation API telemetry, Delta |
| **Unsupervised** | Novelty detection (`steward.detect_novel_terms`) | vocabulary in user questions with **no governed coverage** — new metrics and taxonomy candidates | telemetry corpus + vocabulary baseline |
| **Semantic memory** | Vector Search `semantic-memory` index | retrieval-augmented few-shot context per incoming question | Vector Search (delta-sync, CDF) |
| **Human-in-the-loop** | Steward queue (Lakebase) + console (notebook 80) | certified definitions, poison rulings, disclosure rules — the decisions machines must not make alone | Lakebase Postgres, Databricks Apps (paid-tier vision) |
| **Evaluation** | Stratified benchmark gate + variance protocol | whether any change actually helped, per failure mode | Genie Benchmarks eval-run API |

## The escalation policy (what reaches a human, and what never does)

Auto-handled (no human): mode-1 alias collisions with ≥2 distinct reporters and
confidence ≥ 0.75 — synonym healing, benchmark-gated, rollback-protected.

**Escalated to the steward queue** (`hitl_queue` on Lakebase):
1. **Below-gate proposals** — single-reporter or low-confidence mappings.
2. **Poison conflicts** — one term, contradictory targets across roles ('sales',
   'turnover', 'baskets'). Certified healing is a clarification menu, never a mapping.
3. **Novel terms** — recurring vocabulary with no governed coverage (the "New Metrics
   & Taxonomy Learned" pipeline). Steward defines, maps, or dismisses.
4. **Mode-9 candidates** — definitions whose population base is narrower than the
   question's plain reading (disclosure rulings).

**Decide ≠ deploy:** steward decisions (console, notebook 80) mark queue rows
approved/rejected; the next ops cycle *applies* approved items through the same
gated healing appliers as everything else — so even human-approved changes pass the
benchmark regression gate and land in the audit ledger with `approver=human:<name>`.

## The non-blocking guarantee

A pending escalation never degrades a session. Order of service: (1) Genie answers
from current certified context; (2) the router deflects noise and clarifies known
poison terms; (3) unknown ambiguity gets Genie's best-effort answer *plus* telemetry
capture; (4) the loop escalates; (5) the steward rules; (6) the next cycle heals;
(7) the benchmark gate verifies. Users experience continuous service and gradually
sharper answers — never a "definition pending" error.

## The system's own KPIs (daily report, notebook 90 → `autopilot_daily_report`)

- **Metric Definitions Evolved** — healings applied in the last day (by surface and approver lane)
- **New Metrics & Taxonomy Learned** — novel terms detected → certified
- **Steward Escalations** — opened / decided / max pending age (the queue must drain)
- **Accuracy trend** — latest stratified eval vs prior (the gate's longitudinal record)
- **Corpus growth** — interactions added by the nightly sessions
- **Router economics** — noise deflected, pass-through rate (relaxes as the corpus grows)
- **DQ posture** — quarantine trend vs labeled-chaos expectations

The ecosystem reports on its learning like a team member reports at standup.

## Feature coverage vs capability gaps (the roadmap-influence claim)

Databricks-native: telemetry (Conversation API), evaluation (Benchmarks), memory
(Vector Search), models (MLflow/UC), operational store (Lakebase), orchestration
(Jobs/DABs), semantics (Metric Views), governance surfaces (UC comments/tags).
**Gaps we plugged with notebook-glue that the platform could absorb:** a first-class
steward/review queue for semantic changes (today: our Lakebase schema + console
notebook); definition-lifecycle state on Metric View measures (proposed → certified →
deprecated); population-disclosure metadata (mode 9) surfaced in Genie answers;
escalation events as a native telemetry stream. Genie Ontology (preview) points the
same direction — this repo is the public-API existence proof with evidence.
