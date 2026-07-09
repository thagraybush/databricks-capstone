# sql — SQL as code

Every semantic-layer object in this project is version-controlled SQL, applied
programmatically through the Statement Execution API — no console copy/paste. The
banking files bootstrap the v1 flywheel; the retail files define the v2 semantic and
KPI layers; the monitoring files are the admin/dashboard surface.

## Contents

| File | Responsibility |
|---|---|
| `bootstrap.sql` | Banking gold schema (`workspace.banking_gold`): dims/facts with **deliberately sparse comments** — the flywheel earns the good metadata |
| `metric_views.sql` | Banking metric views (YAML spec 1.1); synonyms deliberately absent so the baseline fails on cross-BU jargon |
| `retail_metric_views.sql` | Retail metric views over `workspace.retail` gold (signed `line_amount` convention documented inline); synonyms sparse for the same reason |
| `business_kpis.sql` | Certified RTB KPI views: `kpi_monthly_summary` (CFO close), `kpi_customer_health` (whales/churn), `kpi_funnel_weekly` (PM standup), `kpi_country_performance` |
| `dashboard_queries.sql` | AI/BI dashboard datasets: accuracy trend, quarantine mix, clickstream DQ health, healing activity |
| `admin_monitoring.sql` | The OBSERVE layer of the admin playbook: `system.query.history` queries using the FE-safe column set |

## The execution path (and the three splitter lessons)

Files are applied by `cli._run_sql_file`
([../src/genie_autopilot/cli.py](../src/genie_autopilot/cli.py)), which splits a file
into statements on `;` — but naive splitting breaks on exactly the SQL this project
writes. The splitter encodes three learned lessons:

1. **Comments can contain semicolons** — comment lines are stripped from the whole
   text *before* splitting, or a semicolon inside a comment splits a statement in half.
2. **Metric-view YAML lives in `$$…$$` blocks** whose YAML content legitimately
   contains semicolons — the splitter tracks dollar-quote state and never splits inside.
3. **`COMMENT ON` string literals contain semicolons too** (and `''` escapes) — the
   splitter tracks single-quote state with escape awareness.

All three are pinned by the regression test
`test_sql_splitter_handles_semicolons_in_strings` in
[../tests/test_session_engine.py](../tests/test_session_engine.py).

## Certification linkage

`business_kpis.sql` is the *governed-code* form of definitions certified by humans:
its formulas mirror the certified answers in
[../benchmarks/retail_questions.yaml](../benchmarks/retail_questions.yaml)
(bounce = single-view non-bot session with no carts/purchases; churn risk =
`recency_days > 90`; return rate = returns_value / gross_revenue; AOV = net revenue
per distinct invoice). This is taxonomy design consequence 5 — "structural beats
conversational": once a definition stabilizes in the HITL loop, it is promoted from a
space instruction into SQL ([../docs/semantic-failure-taxonomy.md](../docs/semantic-failure-taxonomy.md)).

## How to run

```bash
make bootstrap        # bootstrap.sql + data inserts + metric_views.sql via the splitter
```

Healing later rewrites metric views in place via `ALTER VIEW … AS $$yaml$$`
(`healing.add_synonyms_to_yaml`) — learned synonyms never get hand-edited into these files.

Related: [RTB scenarios](../docs/rtb-scenarios.md) ·
[admin playbook](../docs/admin-governance.md) · [package map](../src/genie_autopilot/README.md)
