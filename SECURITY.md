# Security — credentials, secrets, and data-handling posture

This project authenticates three distinct ways, and this file is the single auditable
statement of how each works (issue #20). The honest headline first: this is a
**Databricks Free Edition workspace with exactly one identity** — one user, one PAT,
no service principals, no secret scopes ([infra/rbac.md](infra/rbac.md),
[docs/backlog-free-edition-limits.md](docs/backlog-free-edition-limits.md)). The
posture below is designed around that reality rather than pretending it away.

## Credential inventory

| Secret | Where it lives | How code obtains it | Blast radius if leaked | Rotation |
|---|---|---|---|---|
| **Databricks PAT** (laptop lane: Makefile targets, CLI, phase drivers, `infra/bootstrap.py`) | macOS Keychain, service `databricks-fe` — never on disk in plaintext, never in the repo | `config.resolve_token()`: `DATABRICKS_TOKEN` env var first, else `security find-generic-password -s databricks-fe -w` ([src/genie_autopilot/config.py](src/genie_autopilot/config.py)) | **The whole workspace.** FE's single user is workspace admin; one PAT = every table, volume, job, and Genie space | Created with a 90-day lifetime. Revoke: workspace → Settings → Developer → Access tokens; re-add: `security add-generic-password -U -s databricks-fe -a <you> -w <new-token>` |
| **In-workspace ambient auth** (notebooks 10–90 under Jobs) | Nowhere our code can see — the platform injects the job/notebook identity | Bare `WorkspaceClient()` with no arguments (see any notebook, e.g. [notebooks/10_ingest_telemetry.py](notebooks/10_ingest_telemetry.py)); deliberately *not* `config.workspace_client()`, which is the PAT path | Same single identity, but **no secret is handled by our code**, so there is nothing for this repo to leak | Platform-managed; n/a |
| **Lakebase OAuth token** (HITL queue, notebooks 80/85 + `bootstrap.py` step 3) | Never persisted — minted per run, held in process memory as the Postgres password | `lakebase.get_credential()` → `w.postgres.generate_database_credential` (raw REST fallback); **1-hour expiry, enforced at login**; TLS required (`sslmode=require`) ([src/genie_autopilot/lakebase.py](src/genie_autopilot/lakebase.py)) | `hitl_queue` + `healing_history` in the operational store, for ≤1h; governed state changes still require the gated appliers | Automatic — every connection mints a fresh token |
| **GitHub auth** (issues, PRs, CI pushes) | `gh` CLI's system keyring + SSH key in `~/.ssh` — outside the repo entirely | `gh` and `git` resolve their own credentials; no project code touches them | Repo write access | `gh auth refresh` / rotate the SSH key |
| **Cloud storage keys** | **None exist.** FE uses managed Unity Catalog volumes (`workspace.retail.raw`); there is no S3/ADLS credential anywhere in this project | — | — | — |

Two non-secrets worth naming so the audit is complete: the workspace host URL is
deliberately committed in [databricks.yml](databricks.yml) (it identifies, it does not
authenticate), and the local `.env` holds only resource identifiers
(`GA_GENIE_SPACE_ID`, `GA_WAREHOUSE_ID`, Lakebase host) — no tokens — and is
gitignored anyway.

## What is never committed

PATs, OAuth tokens, `.databrickscfg`, and `.env` never enter git history:

- **Tokens** appear in no source file; the only credential-shaped strings in the repo
  are the *instructions* for storing one in the Keychain.
- **`.env` / `.envrc`** are covered by [.gitignore](.gitignore) (verified) and the
  working-tree `.env` is confirmed untracked.
- **`.databrickscfg`** (the `--profile free-edition` used by `databricks bundle
  deploy`) lives in the home directory, which git never sees. **Known gap,
  fix-forward:** `.gitignore` does not list `.databrickscfg` explicitly, so a
  repo-local copy would not be ignored. Until that line lands, keep the profile file
  in `~` only.
- `ground_truth.jsonl` (the DQ answer key) is also kept local — an eval-integrity
  measure rather than a secret, but part of the same "not in the workspace, not in
  the repo" discipline ([infra/README.md](infra/README.md), "not codified").

## Data handling

- **Synthetic data only.** Banking data is seeded-RNG output (`make datagen`); retail
  is the public UCI Online Retail II dataset plus a synthetic clickstream. No real
  person's data exists anywhere in this project.
- **The PII that does appear is planted, labeled, and scrubbed.** The chaos producer
  deliberately injects email addresses into clickstream referrers as a labeled DQ
  defect; the silver layer redacts them (`regexp_replace` → `[REDACTED]`) and flags
  the row `pii_detected` ([pipelines/retail_medallion.py](pipelines/retail_medallion.py)),
  scored at 100% precision / 100% recall against the producer's ground truth
  ([docs/eval-evidence.md](docs/eval-evidence.md)).
- **Free Edition privacy caveat.** FE workspaces carry a no-expectation-of-privacy
  caveat — Databricks personnel can access Free Edition workspace data. That is a
  second, independent reason the synthetic-only rule is absolute: nothing lands in
  this workspace that could not be public.

## Blast radius, honestly

One leaked PAT on Free Edition is not a contained incident — it is the whole
workspace, because there is only one identity and it is admin. The mitigations that
exist today, and what they actually buy:

| Mitigation (live today) | What it buys |
|---|---|
| 90-day PAT lifetime | A leaked-and-unnoticed token dies on its own |
| Keychain at rest, env-var only as an explicit override | No plaintext token on disk, in shell history, or in dotfiles |
| Ambient auth in all workspace jobs | The scheduled surface area handles zero secrets |
| Ephemeral 1h Lakebase tokens, minted per run | The operational store never has a long-lived credential |
| Warehouse shape + statement timeouts ([docs/admin-governance.md](docs/admin-governance.md), CONTAIN) | Bounds what a compromised session can burn |

The upgrade path is already written down: on a paid workspace, automation moves to
**service principals with scoped tokens**, secrets move to **Databricks Secrets**
scopes, and each job gets its own identity so the audit trail is platform-attested —
see the service-principal row and day-1 plan in
[docs/backlog-free-edition-limits.md](docs/backlog-free-edition-limits.md) and the
group model in [infra/rbac.md](infra/rbac.md). Nothing about the architecture
changes; the single-PAT posture is a Free Edition constraint, not a design choice.

## Responsible use

This is a personal educational capstone, not affiliated with or endorsed by
Databricks. If you find a security issue — a committed secret, an injection path in
the SQL runners, a hole in the role gates — open a GitHub issue on this repository
(or a private report if disclosure matters). Operational recovery procedures live in
[docs/runbook.md](docs/runbook.md).
