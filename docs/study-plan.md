# 3-Week Study Track (runs alongside the build)

## Week 1 — Semantic layer fundamentals
- Metric Views: concepts, YAML 1.1 reference (`version`, `source`, `joins`, `fields`,
  `measures`, `synonyms`, `display_name`, `format`), SQL create/alter paths, feature
  availability by DBR. docs: /metric-views/ and /metric-views/yaml-ref
- Genie: best practices (≤5 tables to start, example SQLs, instructions), tune-quality,
  trusted assets vs example queries distinction.
- Watch: DAIS 2026 keynote segments on Genie One, Genie Ontology, Genie Agents.
- Hands-on: walk the bootstrapped schema; hand-write one metric view before reading ours.

## Week 2 — APIs and Spark-on-serverless
- Genie Conversation API + Space Management API (`serialized_space` v2, etag) — drive
  five questions by hand with curl/SDK before the fleet does it.
- Benchmarks: create 5 questions in the UI, run one eval, read the scoring rules
  (Good = exact result-set semantics, 4-sig-digit numeric tolerance).
- Serverless constraints: Spark Connect only, no RDD/cache, UDF limits — know why.
- AI Functions: ai_query vs task-specific functions; run ai_extract on sample text.

## Week 3 — Narrative and depth
- Rehearse docs/interview-pitch.md cold, twice.
- Leveling stories: map 3 Intuit-scale anecdotes (cross-BU data unification, ML infra
  automation, compliance engineering) to Sr-RSA criteria WITHOUT proprietary details.
- Prepare the "what would you do with a paid workspace" answer: Lakebase operational
  store, scheduled flywheel, Ontology migration path, multi-space federation.
- Share the repo in the private Databricks Slack channel; fold veteran feedback in.
