# tests — pure-python, zero-network

The unit suite runs anywhere `pytest` runs: no workspace, no credentials, no HTTP.
That is a design constraint on the package, not just on the tests — every module's
core logic takes injected clients/connections, so tests substitute **fakes rather
than mocks**: small real objects with real behavior (`FakeAPI` answers questions
with canned SQL, `FakeConn`/`FakeCursor` serve canned rows and record executed
statements) instead of assertion-laden mock choreography.

## Contents

| File | Covers |
|---|---|
| `test_drift.py` | Correction parser patterns ("means" / "refers to" / "is on") and proposal scoring |
| `test_drift_pack.py` | Calendar gating (`active_drift` vs `V3_START`), v3 chaos batches, drift labels, jargon novelty |
| `test_fleet_retail.py` | Persona/label integrity: roles, authorities, `QUESTION_LABELS` coverage, corrections parse back |
| `test_healing.py` | Triage confidence gate, UC comment SQL, metric-view YAML synonym regeneration, space synonym patches |
| `test_producer.py` | Determinism (same seed → same events + ground truth) and chaos-class emission |
| `test_quality.py` | Query-quality featurization, heuristic routing, model round-trip; pure parts of `lakebase.py` |
| `test_session_engine.py` | Session scripts, mutation determinism, honest feedback; the SQL-splitter regression; rollback-drill injection shape |
| `test_steward.py` | Novel-term extraction/detection gates, escalation building, idempotent enqueue, `apply_approved` lanes |

## Tests as encoded lessons

Two examples of the suite's real job — pinning lessons the project paid for:

- **`test_drift_pack.py::test_jargon_terms_genuinely_novel`** — the drift waves only
  prove anything if the system has never seen their vocabulary. This test asserts
  every fresh-jargon term is absent from the entire existing haystack (session
  scripts, fleet catalogs, glossaries), so nobody can accidentally "pre-train" the
  system on its own exam ([../docs/drift-cadence.md](../docs/drift-cadence.md)).
- **`test_session_engine.py::test_sql_splitter_handles_semicolons_in_strings`** —
  pins the three splitter lessons (comments, `$$` YAML blocks, quoted literals with
  `''` escapes may all contain semicolons) against `cli._run_sql_file`
  ([../sql/README.md](../sql/README.md)).

## How to run

```bash
make test     # pytest -q
make lint     # ruff over src, tests, data_gen
```

CI runs both on every push. Workspace-facing behavior (live Genie, eval runs,
Lakebase) is deliberately *not* unit-tested — it is exercised by the phase drivers
and recorded as evidence in [../docs/eval-evidence.md](../docs/eval-evidence.md).

Related: [package map](../src/genie_autopilot/README.md) ·
[evidence log](../docs/eval-evidence.md)
