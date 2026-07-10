# CLAUDE.md — agent operating instructions for this repo

## Single source of truth: GitHub `main`

All writes happen in this local clone and land on GitHub first. The Databricks
workspace holds **two downstream copies** that do NOT update themselves:

| Copy | Path | Consumed by | Updated by |
|---|---|---|---|
| Git folder | `/Users/cfollmer@strataintel.ai/databricks-capstone` | Craig, interactively | Repos API pull (below) |
| Bundle deployment | `.bundle/` dev target | Scheduled jobs (`nightly_sessions`, `daily_ops`, `steward_console`, …) | `databricks bundle deploy -t dev` |

Never edit code in the workspace Git folder — it is a read-and-run surface.
Merging a PR updates neither workspace copy; the ship cycle does.

## The ship cycle (run ALL four steps, every time)

```bash
export DATABRICKS_HOST="https://dbc-c00424a1-8d76.cloud.databricks.com" \
       DATABRICKS_TOKEN=$(security find-generic-password -s databricks-fe -w)

make test && make lint                       # gates first
git commit … && git push origin main         # 1+2  GitHub = source of truth
databricks bundle deploy -t dev              # 3    refresh the job copy
databricks api patch /api/2.0/repos/2581306377936810 --json '{"branch": "main"}'
                                             # 4    pull Craig's Git folder to HEAD
```

Skipping step 3 leaves jobs running stale code; skipping step 4 leaves Craig
interactively running stale code (this happened — he debugged a hang for a day
that was already fixed on `main`). If the repo id is stale, rediscover it:
`databricks api get "/api/2.0/repos?path_prefix=/Workspace/Users/cfollmer@strataintel.ai"`.

## Auth & credentials

- PAT lives ONLY in macOS Keychain: `security find-generic-password -s databricks-fe -w`.
  Never in files, env exports in committed scripts, or command history with the literal value.
- OAuth login fails on Free Edition — PAT is the only auth path.
- Never commit tokens, `.env`, or `.databrickscfg`.

## Platform constraints (Free Edition)

- Serverless only; one 2X-Small warehouse (`b9f4a06641eedd7b`); 5 concurrent tasks;
  fair-use kill switch (see `docs/runbook.md` Playbook 1).
- Genie ~5 questions/min. SQL via `POST /api/2.0/sql/statements` is stateless —
  fully qualify all names (`workspace.retail.…`); `USE` does not persist.
- No `%pip install psycopg[binary]` in notebooks — two real OOMs (runbook Playbook 3).
  The steward queue's in-workspace SoR is Delta; Lakebase is the laptop mirror.
- `databricks.sdk.WorkspaceClient()` hangs in INTERACTIVE serverless notebooks
  (auth env vars are injected only in jobs). In notebooks, get identity via
  `spark.sql("SELECT current_user()")`.

## Quality gates

- `make test` (pytest, pure-python modules) and `make lint` (ruff strict on
  `src tests data_gen infra`; `--ignore E501,F821,E402` on `notebooks pipelines`;
  py_compile on all notebooks/pipelines) — both must pass before any push. CI mirrors this.
- Notebook code style: inline comments explain WHY (business/platform constraints),
  `%md` cells narrate the business process. This repo is interview evidence —
  principal-engineer readability is a requirement, not a preference.

## Key workspace IDs

- Retail Genie space `01f17b5a51411bc382cd3cd224d11daf`; banking `01f17b53b7161737a57aec0195b92b45`
- Warehouse `b9f4a06641eedd7b`; Git folder repo id `2581306377936810`
- Steward queue: `workspace.retail.autopilot_escalations` (Delta, identity ids)

## Clean-room discipline

Synthetic data only. Zero Intuit or Strata Intelligence IP — code, data, or
naming. This repo is public interview material.
