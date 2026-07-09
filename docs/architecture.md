# Architecture: the Autonomous Context Flywheel

Traditional data engineering assumes business context is static and well-documented.
It isn't. This system treats Genie user interactions as a real-time telemetry stream
and converts human friction into governed, benchmark-gated metadata healing.

```
[Synthetic user fleet] --(Conversation API, paced ≤5 q/min, cross-BU personas)--> [Genie Space]
      |  real thumbs up/down + typed corrections (feedback endpoint)
      v
[Telemetry ingest] <-- conversation/message list API (primary source)
                   <-- system.access.audit  service_name='aibiGenie'          (if readable)
                   <-- system.query.history client_application='...Genie Space' (if readable)
      v
[Drift-detection engine]
      deterministic correction parser ("X means Y")  +  ai_query LLM pass (serverless SQL)
      scoring: authority × frequency × freshness  ("OntoRank-inspired")
      v
[Proposals] --> [GOVERNED HEALING GATE]
      auto-approve: confidence ≥ 0.75 AND ≥2 distinct users; else human review queue
      every action appended to the audit ledger (JSONL + Delta)
      v (three appliers)
      1. Unity Catalog:  COMMENT ON COLUMN / ALTER TABLE ... SET TAGS
      2. Metric Views:   regenerate YAML 1.1 with learned synonyms → ALTER VIEW ... AS $$yaml$$
      3. Genie space:    serialized_space v2 patch (column_configs synonyms, instructions) + etag
      v
[Benchmark regression gate]
      Genie Benchmarks eval-run API, before vs after; regression → automatic rollback
      (prior serialized_space + YAML kept as rollback artifacts)
      v
[AI/BI health dashboard]  accuracy trend · healings applied · open proposals · telemetry volume
```

## Design decisions

- **Real loop, not simulation.** The fleet drives actual Genie conversations and files
  actual feedback through GA 2026 APIs; nothing about the telemetry is mocked.
- **Governance is a feature, not overhead.** Autonomous metadata mutation in a bank is a
  compliance event. The gate (thresholds, human queue, audit lineage, rollback) is the
  part an RSA sells to a regulated buyer.
- **`ALTER ATTRIBUTE` does not exist.** UC mutation surface is `COMMENT ON`,
  `ALTER TABLE … ALTER COLUMN … COMMENT`, and `SET TAGS` — a fact-check this project
  encodes in code and tests.
- **Free Edition constraints shaped the runtime**: serverless-only, one 2X-Small
  warehouse, ≤5 concurrent job tasks, ~5 Genie questions/min (hence the RateLimiter),
  fair-use quotas (batched runs), PAT-only auth, everything re-creatable from this repo.
- **Positioning vs Genie Ontology** (announced 2026-06-16, Public Preview): this project
  is the public-API embodiment of the same conviction — context should be learned from
  usage, ranked by authority/frequency/freshness, and continuously applied. Ontology has
  no public API yet; this flywheel runs on GA endpoints today.

## Module map

| Module | Responsibility |
|---|---|
| `genie_autopilot.config` | host, catalog/schema, PAT via env or macOS Keychain |
| `genie_autopilot.genie_api` | paced Conversation + Space Management REST wrapper |
| `genie_autopilot.fleet` | cross-BU personas driving real Genie traffic + feedback |
| `genie_autopilot.telemetry` | conversation-history harvesting → structured corrections |
| `genie_autopilot.drift` | correction parsing, authority/frequency/freshness scoring |
| `genie_autopilot.healing` | governed gate, three appliers, audit ledger, YAML regen |
| `genie_autopilot.evals` | benchmark eval runs, accuracy scorecard, regression compare |
