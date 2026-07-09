# Drift Cadence: Chaos the System Has Never Seen, On a Schedule

## Philosophy

Every defect the platform has healed so far — v2 camelCase drift, GMV/whale/take-rate
dialects — was authored before the system was built, so the demo proves recovery from
*known* chaos. A living system must also meet drift it wasn't built for. This cadence
(`src/genie_autopilot/drift_pack.py`) holds back two waves of genuinely novel drift and
releases them by **calendar date**, not by human intent: the waves key off
`V3_START = 2026-07-14`, so the system encounters them "in the wild" on schedule.
Running the script early requires an explicit `--force` and is a rehearsal, not a drill.

## The two waves

**Wave 1 — schema v3 (from `V3_START`): the operational drill.**
A fraction (~30%) of producer events re-ship as a nested envelope
(`meta`/`actor`/`action`/`context` — no flat `event_id` column). The pipeline's
`either()` v1/v2 coalescing cannot normalize them, so `has_event_id` fails and they
land in `quarantine_events`. Expected arc: **quarantine spike → DE persona diagnoses
from quarantine rows alone → ships a v3 normalizer** (flatten `meta.eventId` etc. into
`events_normalized`). The batch's `ground_truth.jsonl` (labels: `schema_v3`) stays
local as the answer key, so the fix is scored on precision/recall, not vibes.

**Wave 2 — fresh jargon (from `V3_START` + 3 days): the semantic drill.**
Three new personas speak dialect no glossary entry, fleet catalog, or healed
instruction covers: **NRR** (net revenue retention), **perfect-order rate**
(non-return invoice share), **CAC payback** (unanswerable — there is no spend data;
the honest outcome is *no* answer and *no* healed mapping), plus one new poison term:
**"volume"** means `quantity` to merchandising but `invoices` to the PM org. Expected
arc: **corrections hit telemetry → flywheel mines them → jargon heals, the collision
routes to HITL as a disambiguation, the unanswerable stays unanswered.**

Waves are staggered so each drill's signal is legible on its own dashboard: quarantine
trend first, semantic telemetry three days later.

## Operator cadence

Weekly (or per demo cycle):

1. `python -m genie_autopilot.drift_pack --wave status` — which waves are live.
2. Wave 1 live? Generate + upload once (`--wave v3`, then `databricks fs cp ...` per the
   module docstring), run the pipeline, and **check the quarantine trend** — the drill
   is *watching the system catch it*, not pre-briefing the DE persona.
3. Wave 2 live? Run `--wave jargon`, then the flywheel (mine → propose → HITL → heal →
   re-eval). Verify: new terms heal, `volume` is flagged as a conflict (never
   auto-healed), CAC payback is filtered as noise.
4. Score against ground truth / eval strata and append the evidence to
   `docs/eval-evidence.md`.

The drill passes when the *system's* signals (quarantine DQ scorecard, correction
mining, poison-term detection) surface the drift before any human explains it.

## Honest note on provenance

The wave content is **authored-but-unseen-by-the-system**: a human wrote `drift_pack.py`
in advance, so this is staged novelty, not cosmic surprise. The claim we can honestly
make is narrower and still valuable — the *system* has no access to this module's
future terms or schemas until they hit telemetry. No pipeline rule, glossary entry,
healed instruction, or benchmark question references v3 envelopes, NRR, perfect-order
rate, CAC payback, or the volume collision (enforced by `tests/test_drift_pack.py`
against the fleet catalogs and the eval-evidence log). What is being tested is the
detect → diagnose → heal → verify loop against inputs it could not have memorized.
