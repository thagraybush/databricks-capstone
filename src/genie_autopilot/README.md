# genie_autopilot — package map

The Python package behind the flywheel: a paced Genie client, a simulated data
organization that generates *real* workspace traffic, learning loops that mine that
traffic, and a governed healing path that applies what it learns — always behind a
stratified benchmark gate with rollback.

## Contents

| Module | Responsibility |
|---|---|
| `config.py` | Host/catalog/schema env knobs; PAT resolution (env `DATABRICKS_TOKEN` → macOS Keychain `databricks-fe`) |
| `cli.py` | `python -m genie_autopilot.cli` bootstrap/simulate/detect/heal/eval; owns `_run_sql_file`, the quote/`$$`-aware SQL splitter |
| `genie_api.py` | Thin REST wrapper over the Genie Conversation + Space Management APIs; `RateLimiter` paces question POSTs to ~4.8/min (Free Edition fair use) |
| `producer.py` | Product-engineering persona: clickstream against the real UCI catalog with **labeled chaos** → `ground_truth.jsonl` |
| `fleet.py` | Banking persona fleet (v1 cross-BU trap: "liquid assets" vs "available balance") |
| `fleet_retail.py` | Retail PM/marketing/collision personas + `QUESTION_LABELS` (seed labels for the router and quality gate) |
| `session_engine.py` | Multi-turn persona sessions with seeded linguistic noise; appends `session_manifest.jsonl` |
| `drift_pack.py` | Calendar-keyed *novel* drift: v3 nested-envelope schema wave + fresh-jargon wave (activates at `V3_START`) |
| `telemetry.py` | Harvest Conversation-API history into structured correction records |
| `drift.py` | Deterministic correction parser; proposal scoring (authority × frequency × freshness); poison-conflict detection |
| `quality.py` | Query-quality gate: `run` / `reject` / `human_review` before a question burns Genie quota (trained model + heuristic fallback) |
| `steward.py` | Escalation engine: below-gate proposals, poison conflicts, novel terms → HITL queue; `apply_approved` bridges decisions to appliers |
| `healing.py` | Governed appliers (UC comments/tags · metric-view YAML synonyms · `serialized_space` patches), `triage` confidence gate, `AuditLedger` |
| `lakebase.py` | Lakebase Postgres HITL store: project provisioning, credential minting, `hitl_queue` / `healing_history` |
| `evals.py` | Stratified benchmark harness over the Genie eval-run API; lift report → KEEP / ROLLBACK |
| `phase_d.py` | Driver: full retail flywheel pass (fleet → mine → govern → heal → re-eval) |
| `phase_e.py` | Driver: collision stratum, poison terms, classifier training, DQ scorecard vs ground truth |
| `phase_f_variance.py` | Driver: repeat evals N× → per-stratum mean ± range (nondeterminism honesty) |
| `phase_g_rollback.py` | Driver: rollback drill — inject a bad definition, prove the gate trips and restores |
| `certify.py` | Steward CLI (`make certify`): guided human certification of draft benchmarks, golden SQL executed live |

## Dependency flow

```
                     config (auth · env · fq naming — imported by everything)

SIMULATION                                LEARNING + ESCALATION
producer ── JSONL files → volume          telemetry ─► drift ─► steward ─► lakebase
fleet / fleet_retail ──┐                  (harvest)   (score)  (escalate)  (HITL queue)
session_engine ────────┼─► genie_api ──►      ▲                                │ human
drift_pack (calendar) ─┘   (paced client)  Genie space                         ▼ decides
             quality ──┘   ▲  (real traffic, feedback)    GOVERNED APPLICATION
         (pre-Genie gate)  │                              healing ◄── approved rows
                           │                              (3 appliers + audit ledger)
EXPERIMENT HARNESS         │                                   │
phase_d/e/f/g · certify ───┴── evals ◄─────────────────────────┘
                               (stratified gate → KEEP / ROLLBACK)
```

## How to run

- v1 flywheel: `make simulate detect heal eval` (wraps `cli.py`; needs `GA_GENIE_SPACE_ID`).
- Phase drivers: `GA_GENIE_SPACE_ID=<retail-space> python -m genie_autopilot.phase_d|phase_e|phase_f_variance|phase_g_rollback`.
- Corpus + certification: `make sessions` · `make certify`.
- Everything workspace-facing needs a Free Edition PAT (env or Keychain); pure-python cores run without credentials — that's what [../../tests/README.md](../../tests/README.md) exercises.

## Key design decisions

- **Simulation produces real traffic.** Fleets and sessions hit the live Genie space through the paced client — the telemetry mined downstream is genuine Conversation-API history, not synthetic logs.
- **Poison terms never auto-heal.** Contradictory corrections are conflict signals routed to the steward; the certified fix is a disambiguation instruction, never a synonym.
- **Decide ≠ deploy.** Steward decisions only mark queue rows; application happens through the same gated appliers and audit ledger as automatic healings.
- **I/O-free cores, injected edges.** Scoring, escalation, and healing logic take injected connections/clients so tests pass fakes and never touch the network.

Related: [architecture v2](../../docs/architecture-v2.md) · [steward loop](../../docs/steward-loop.md) · [failure taxonomy](../../docs/semantic-failure-taxonomy.md) · [evidence log](../../docs/eval-evidence.md)
