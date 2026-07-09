# Operations Runbook — failure playbooks, idempotency inventory, health checks

This system runs scheduled workloads on Free Edition — `nightly_sessions` at 02:00
MT, `daily_ops` at 03:30 MT, the triggered `retail_medallion` pipeline, and the
manually triggered flywheel/router jobs ([../resources/autopilot_jobs.yml](../resources/autopilot_jobs.yml))
— on a platform with a fair-use kill switch and a single 2X-Small warehouse as
structural constraints. Every playbook follows the same skeleton: **symptom →
diagnosis → recovery → verification.** Three of the incidents below actually
happened (the notebook-85 OOM, the column-name pipeline errors, the Lakebase
endpoint-discovery failure); this runbook is written from that history, not from
imagination.

## Playbook 1 — Free Edition fair-use compute freeze

**Symptom.** Jobs fail to acquire serverless compute, runs sit queued far past their
schedule, or requests come back throttled/rejected with quota or fair-use messaging.

**Diagnosis.** Check recent run states (`databricks jobs list-runs --job-id <id>` or
the Jobs UI) — a cluster of same-day failures across unrelated jobs is quota, not
code. Confirm nothing new was scheduled: the design budget is ≤2 concurrent tasks
against FE's account-wide limit of 5 (the flywheel's 4 tasks run strictly
sequentially — see the header of
[../resources/autopilot_jobs.yml](../resources/autopilot_jobs.yml)), and Genie
traffic is paced to ~4.8 questions/min by the shared `RateLimiter`
([../src/genie_autopilot/genie_api.py](../src/genie_autopilot/genie_api.py)).

**Recovery.** There is no support ticket to file on FE — the reset is time-based:

1. Wait for the daily reset; do not retry-storm (retries spend the same quota).
2. Reduce the nightly spend: lower the `sessions` / `max_questions` widgets on
   `nightly_sessions` (e.g. `sessions=5 max_questions=30`) for a few nights.
3. Keep everything triggered-batch; never add an always-on or overlapping schedule.
   The 02:00 / 03:30 stagger exists so the two scheduled jobs never contend.

**Verify.** Manually run one small batch — `databricks bundle run nightly_sessions
-t dev --notebook-params sessions=2` — and confirm it completes; then let the
schedules resume untouched.

## Playbook 2 — Medallion pipeline failure (`retail_medallion`)

**Symptom.** `databricks bundle run retail_medallion -t dev` exits non-zero, or the
pipeline UI shows a FAILED update.

**Diagnosis.** Read the pipeline event log via the API:

```bash
databricks pipelines list-pipelines                       # get the pipeline id
databricks pipelines list-pipeline-events <pipeline-id>   # errors carry the stack
```

Causes this project has actually hit:

- **Column-name / normalization errors.** The UCI headers contain spaces and Delta
  forbids them — bronze normalizes everything to snake_case, and structural rules
  deliberately run on the *pre-rename* normalized columns
  ([../pipelines/retail_medallion.py](../pipelines/retail_medallion.py)). The v2
  camelCase drift (`eventId` vs `event_id`) is why the clickstream lane coalesces
  with `either("event_id", "eventId")`. New source-schema drift reproduces exactly
  this class of failure — look for `UNRESOLVED_COLUMN` / analysis errors in the
  event log.
- **Expectation changes.** DQ rules are tracked expectations (`@dp.expect_all`), so
  a rule regression usually does *not* fail the update — bad rows land in
  `quarantine_events` with machine-readable reasons. A quarantine **spike** is
  signal, not breakage (from 2026-07-14 the v3 drift wave produces one by design —
  [drift-cadence.md](drift-cadence.md)); a quarantine **flatline at zero** with
  rising bronze counts means a rule was accidentally weakened.

**Recovery.** Fix the transform in
[../pipelines/retail_medallion.py](../pipelines/retail_medallion.py), then:

```bash
databricks bundle deploy -t dev
databricks bundle run retail_medallion -t dev --full-refresh-all
```

Full refresh is safe: the pipeline owns bronze/silver/gold/quarantine outright, and
the metric/KPI views over gold are `CREATE OR REPLACE` (re-apply via
`python infra/bootstrap.py --skip-lakebase` if they ever need rebuilding — the
fresh-workspace ordering caveat in [../infra/README.md](../infra/README.md)).

**Verify.** Gold row counts are sane and quarantine reasons are explainable:

```sql
SELECT COUNT(*) FROM workspace.retail.fact_sales;
SELECT explode(quarantine_reasons) AS reason, COUNT(*)
FROM workspace.retail.quarantine_events GROUP BY 1 ORDER BY 2 DESC;
```

## Playbook 3 — Scheduled job failure: OOM on serverless (the notebook-85 incident)

**Symptom.** A `daily_ops`, `nightly_sessions`, or router job task fails with
**exit code 134** (SIGABRT — the serverless memory kill) or an "exited with unknown
state" notebook error. The real occurrence here: the `escalate_and_apply` task
(notebook 85) died with exit 134.

**Diagnosis.** Open the failed run's task output — 134 with no Python traceback is
memory, not logic. Notebook 85 is the heaviest of the ops notebooks (a `%pip
install psycopg` + `restartPython`, a full telemetry `collect()`, and a Postgres
connection in one serverless session).

**Recovery — first resort: just rerun.** The loop is idempotent end-to-end, so a
mid-flight kill loses nothing:

- telemetry mining is read-only over `autopilot_telemetry`;
- `steward.escalate` reads pending keys first and **skips anything already
  enqueued** ([../src/genie_autopilot/steward.py](../src/genie_autopilot/steward.py));
- `steward.apply_approved` only touches rows with `status='approved'` and flips
  each to `'applied'` as it lands, so a rerun resumes exactly where the kill
  stopped.

**Recovery — second resort: run the escalation locally.** Every engine takes an
injected connection, so the whole loop runs from a laptop on the Keychain PAT when
serverless memory keeps killing it:

```bash
.venv/bin/python - <<'PY'
from genie_autopilot import config, lakebase, steward
w = config.workspace_client()                       # PAT: env or Keychain
host, token = lakebase.get_credential(w, "genie-autopilot")
conn = lakebase.connect(host, token, w.current_user.me().user_name)
conn.autocommit = True
print(f"{len(lakebase.pending(conn))} pending escalations")
# decisions:  lakebase.decide(conn, <id>, approved=True, decided_by="human:<email>")
# mine+apply: mirror notebook 85 — telemetry via the Statement Execution API,
#             then steward.escalate(...) / steward.apply_approved(...)
conn.close()
PY
```

The phase drivers ([../src/genie_autopilot/phase_d.py](../src/genie_autopilot/phase_d.py)
and siblings) are the existing proof of this path — the entire flywheel ran from a
laptop before it was a job.

**Verify.** `hitl_queue` pending count reflects the new escalations, an
`autopilot_audit_ledger` row exists for anything applied, and the next notebook-90
run reports a non-NULL escalation KPI.

## Playbook 4 — Stale eval / benchmark drift

**Symptom.** Gate verdicts stop matching reality: the latest
`autopilot_eval_history` row predates the latest healing, or a single run dips and
someone calls it a regression.

**Diagnosis.** Compare the clocks, then check the variance band:

```sql
SELECT MAX(ts) FROM workspace.retail.autopilot_audit_ledger;         -- last mutation
SELECT phase, accuracy, ts FROM workspace.retail.autopilot_eval_history
ORDER BY ts DESC LIMIT 3;                                            -- last measurement
```

If the ledger is newer than the eval, the score is stale by definition. If the eval
is fresh, remember Genie is nondeterministic: jargon's measured band is **80% mean,
70–90% range** across repeated runs ([eval-evidence.md](eval-evidence.md)) — a
single run inside the band is weather, not climate.

**Recovery.** Re-measure with the variance protocol, never a single run:

```bash
GA_GENIE_SPACE_ID=<retail-space> .venv/bin/python -m genie_autopilot.phase_f_variance --runs 3
```

**Re-baseline** (record a new reference, not just re-measure) when the *suite*
changes — certification grew it 66 → 70 questions and the collision stratum shifted
denominator, the documented precedent — after a healing batch lands, and when a
drift wave activates (`V3_START = 2026-07-14`, [drift-cadence.md](drift-cadence.md)).
Never re-baseline to explain away a regression: the rollback drill exists precisely
because the gate is supposed to trip
([../src/genie_autopilot/phase_g_rollback.py](../src/genie_autopilot/phase_g_rollback.py)).

**Verify.** `phase_f_variance` appended its per-stratum mean ± range to
[eval-evidence.md](eval-evidence.md) and the fresh runs sit in
`autopilot_eval_history`.

## Playbook 5 — Lakebase unreachable (steward queue down)

**Symptom.** Notebook 85 prints `→ Lakebase unavailable (…)` and exits
`lakebase-unavailable`; notebook 80 exits `failed: no Lakebase credential`.

**Impact — by design, none that persists.** The notebooks degrade and skip rather
than fail the job (notebook 85's exit is clean, so the daily report still runs),
and **no data is lost**: telemetry lives in Delta and escalations are *re-derived*
from it on the next successful run; `steward.escalate` skips already-pending keys
so the backlog reconciles without duplicates; approved-but-unapplied decisions stay
`status='approved'` until the next run applies them. User-facing Genie sessions
never touch this queue at all ([steward-loop.md](steward-loop.md), the non-blocking
guarantee).

**Diagnosis.** Probe the project and endpoint:

```bash
databricks api get /api/2.0/postgres/projects/genie-autopilot
databricks api get /api/2.0/postgres/projects/genie-autopilot/branches/production/endpoints
```

Three failure shapes seen or designed for, from
[../src/genie_autopilot/lakebase.py](../src/genie_autopilot/lakebase.py):

1. **Scale-to-zero cold start** — first connection after idle can time out; retry.
2. **Endpoint-discovery shape drift** — the real incident: the read/write host
   moved under `status.hosts.host` and the endpoint id had to be recovered from the
   trailing segment of the endpoint *name*. `get_credential` now tries the project
   GET, the endpoint listing, and both host locations; the typed SDK
   `generate_database_credential` is preferred with a raw-REST credential fallback
   because the SDK surface varies by version. If discovery fails again, diff the
   API responses above against what `get_credential` expects.
3. **Token expiry** — credentials live 1 hour, enforced at login: an established
   connection outlives its token, but any reconnect must mint fresh. Never cache a
   Lakebase token.

**Recovery.** Re-provision is one idempotent command, then rerun the job:

```bash
.venv/bin/python infra/bootstrap.py --skip-workspace-sql --skip-domain-sql \
    --skip-verify --skip-checklist --skip-roles        # step 3 only: project + schema
databricks bundle run daily_ops -t dev
```

**Verify.** Notebook 85 prints `steward queue connected: <host>` and the pending
count matches expectations.

## Idempotency inventory

"Safe" means: re-running converges to the same governed state. External side
effects that merely *accrete* (new Genie traffic, new audit receipts, new eval
runs) are called out — receipts are append-only on purpose.

| Target | Safe to re-run? | On re-run | Notes / evidence |
|---|---|---|---|
| `infra/bootstrap.py` | **yes** | converges (skips/no-ops) | Every step `IF NOT EXISTS` / `CREATE OR REPLACE` / `MERGE`; Lakebase `ensure_project` is create-or-get, `ensure_schema` is `CREATE TABLE IF NOT EXISTS`; per-step `--skip-*` flags |
| `make bootstrap` (banking lane) | **conditional** | DDL + views converge; **`inserts.sql` re-inserts** | `sql/bootstrap.sql` is `IF NOT EXISTS` and the views are `CREATE OR REPLACE`, but the generated inserts are blind `INSERT INTO` — rerunning after data load duplicates the banking seed rows. Compensation: `TRUNCATE` the three banking tables first, or rerun with `data_gen/output/` absent |
| `make datagen` | **yes** | overwrites `data_gen/output/` | Deterministic seeded RNG; local files only |
| `make sessions` | **conditional** | new real Genie conversations + manifest append | Correct by design (each run is new traffic, joined by new `conversation_id`s) but spends fair-use quota — don't rerun to "fix" a partial batch, just run the next one |
| `make certify` | **yes** | resumes where it stopped | Decisions write back to the YAML immediately (crash-safe); already-decided questions are skipped ([../src/genie_autopilot/certify.py](../src/genie_autopilot/certify.py)) |
| `autopilot_flywheel` job (nb 10→20→30→40) | **yes** | see per-notebook rows below | The four tasks are individually idempotent, so the job is |
| — nb 10 `ingest_telemetry` | **yes** | `autopilot_telemetry` **overwrite** (Conversation API is source of truth); `autopilot_corrections` **MERGE** on `source_message_id` | Rows lacking a message id are skipped rather than break the MERGE key |
| — nb 20 `detect_drift` | **yes** | `autopilot_proposals` **merge-upsert**: scores refresh, **human decisions (`approved`/`applied`/`rejected`) are preserved** on match | The status column is deliberately untouched on `WHEN MATCHED` |
| — nb 30 `apply_healings` | **yes** | UC `COMMENT`/`TAG` are last-writer-wins; space synonym patch **dedupes** (`dict.fromkeys` merge in `healing.patch_space_column_synonyms`); audit ledger **appends** receipts | `dry_run=true` default; a dry run marks `approved`, the next live run picks up exactly that set |
| — nb 40 `run_benchmarks` | **yes** | **appends** one eval-history row per run | Appending is the point — it is the longitudinal record; costs one eval run of quota |
| `ds_persona` job (nb 50) | **yes** | `CREATE OR REPLACE` KPI/forecast tables; MLflow registers a **new model version** | Version accretion in UC is harmless and auditable |
| `nightly_sessions` job (nb 70 + 10) | **conditional** | nb 70 **appends** to `autopilot_session_manifest` and creates new conversations; nb 10 then rebuilds telemetry by overwrite | Same-night rerun doubles that night's traffic/quota (seed is date-keyed, so phrasing repeats but conversations are new). Telemetry never duplicates; manifest grows |
| `router_training` job (nb 60) | **yes** | corpus + router-eval tables **overwrite**; Vector Search endpoint/index **create-if-not-exists**; new UC model version | FE allows exactly 1 endpoint / 1 unit — the get-then-create pattern is what makes reruns safe |
| `router_arm` job (nb 61) | **yes** | `router_arm_results` **overwrite** | Spends real Genie traffic for the Genie-alone arm — rerun deliberately, not casually |
| `daily_ops` job (nb 85 + 90) | **yes** | nb 85: escalate **skips pending keys**, apply flips `approved`→`applied` row-by-row; nb 90: report rows **MERGE** keyed by report date | The rerun-after-OOM path in Playbook 3; nb 90 backfills via the `report_date` widget |
| `steward_console` job (nb 80) | **yes** | `list` is read-only; `decide` is `UPDATE … WHERE status='pending'` — re-deciding a decided row is a reported no-op | Role-gated on `metric_steward` in `autopilot_roles` |
| `phase_d` driver | **conditional** | re-drives the fleet (quota), re-applies healings (idempotent surfaces), appends audit receipts + new eval runs | Safe for state; each run is a new experiment, not a replay |
| `phase_e` driver | **conditional** | same as phase_d **plus appends sections to `docs/eval-evidence.md`** | Compensation for an unwanted rerun: `git checkout -- docs/eval-evidence.md` before commit |
| `phase_f_variance` driver | **conditional** | N fresh eval runs; **appends** the variance section to `docs/eval-evidence.md` | Built for repetition — that is its purpose; quota is the only cost |
| `phase_g_rollback` driver | **conditional** | snapshots → injects poison → evals → restores → appends evidence | **Crash window:** a death between inject and restore leaves the space poisoned. Compensation: the snapshot is the first thing taken — restore it via `api.update_space(snapshot, etag)`, or simply rerun; the gate re-trips and restores |

## Health checks — "is the system healthy?"

Run weekly, or after any incident above. Warehouse queries are in
[../sql/admin_monitoring.sql](../sql/admin_monitoring.sql); queue queries run on
Lakebase (Postgres), not the warehouse.

1. **Warehouse reachable** — `databricks warehouses list` shows `b9f4a06641eedd7b`;
   any `make eval`/CLI lane failing at `_warehouse_id` means auth or workspace, not
   code.
2. **Jobs green** — `databricks jobs list-runs --job-id <id>` (or the Jobs UI) for
   `nightly_sessions` and `daily_ops`: last runs SUCCESS, no exit-134 pattern
   (Playbook 3), no quota cluster (Playbook 1).
3. **Latest eval accuracy in band** —
   `SELECT phase, accuracy, ts FROM workspace.retail.autopilot_eval_history ORDER BY ts DESC LIMIT 5;`
   — jargon inside its 70–90% band, clean control at 100%; anything else →
   Playbook 4.
4. **Corpus growing** —
   `SELECT COUNT(*), MAX(harvested_at) FROM workspace.retail.autopilot_telemetry;`
   and manifest rows for last night in `autopilot_session_manifest`; a flat corpus
   means `nightly_sessions` is not landing.
5. **Queue draining** — on Lakebase:
   `SELECT status, COUNT(*), MIN(created_at) FROM hitl_queue GROUP BY status;` — an
   ageing `pending` pile is admin debt
   ([admin-governance.md](admin-governance.md), OBSERVE #5); `approved` rows older
   than a day mean `daily_ops` is not applying (Playbooks 3/5).
6. **Receipts present** — audit-ledger activity by approver lane (queries 6–7 in
   [../sql/admin_monitoring.sql](../sql/admin_monitoring.sql)): nothing
   auto-approved that should have been human, poison terms only ever as
   instructions.
7. **Quarantine trend explainable** — daily counts from `quarantine_events`
   (Playbook 2); expected spike from 2026-07-14 (v3 wave), otherwise steady-state.

Last-resort recovery — rebuilding the workspace from the repo — is the
reproducibility procedure in [../infra/README.md](../infra/README.md). Credential
posture and rotation live in [../SECURITY.md](../SECURITY.md).
