"""One-shot environment bootstrapper for the Genie Autopilot capstone (Free Edition).

Runs Plane B (SQL-as-code) and the Lakebase half of Plane C (API-as-code) from a
laptop, then verifies the workspace primitives and prints what remains manual.
See infra/README.md for the full IaC map and infra/rbac.md for the role model.

Usage (from the repo root, venv python):

    .venv/bin/python infra/bootstrap.py                 # all steps
    .venv/bin/python infra/bootstrap.py --skip-lakebase # skip any step by flag

Steps (each idempotent — safe to re-run any subset):

    1. workspace SQL   infra/workspace_setup.sql (schemas, volume, role registry, grants)
    2. domain SQL      sql/bootstrap.sql + sql/retail_metric_views.sql + sql/business_kpis.sql
    3. lakebase        ensure the 'genie-autopilot' project + HITL schema (hitl_queue, ...)
    4. verify          the raw volume and a SQL warehouse actually exist (list + assert)
    5. checklist       print the manual steps IaC cannot perform (PAT, data, spaces, ...)
    6. roles           print active role assignments from workspace.retail.autopilot_roles

Credentials: DATABRICKS_TOKEN env or the macOS Keychain item `databricks-fe`
(resolution in genie_autopilot.config). Nothing workspace-side happens at import
time — the WorkspaceClient is created lazily inside main(), only if a step needs it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo layout anchors. The package normally arrives via `make install`
# (pip install -e .); prepending src/ lets this script also run in a bare
# checkout where only the venv's dependencies are installed.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

# These imports are workspace-free: config's client is lru_cached and built on
# first call, lakebase is pure python, and the cli helpers only take an already
# constructed client. (E402: the sys.path bootstrap above must run first.)
from genie_autopilot import config, lakebase  # noqa: E402
from genie_autopilot.cli import _run_sql_file, _warehouse_id  # noqa: E402

# --- what this script applies -------------------------------------------------

# Step 1: workspace-level primitives (this directory owns the file).
WORKSPACE_SETUP_SQL = REPO_ROOT / "infra" / "workspace_setup.sql"

# Step 2: the retail-domain SQL, in dependency order. bootstrap.sql is the
# banking DDL (self-contained); the retail metric/KPI views reference gold
# tables OWNED BY the retail_medallion pipeline, so on a truly fresh workspace
# they fail until `databricks bundle run retail_medallion -t dev` has run once —
# that is reported as a warning, not a crash (see _step_domain_sql).
DOMAIN_SQL_FILES = [
    REPO_ROOT / "sql" / "bootstrap.sql",
    REPO_ROOT / "sql" / "retail_metric_views.sql",
    REPO_ROOT / "sql" / "business_kpis.sql",
]

# Step 4 expectations: the raw landing volume created by step 1, and the known
# warehouse (any warehouse passes — FE provisions one Serverless Starter
# Warehouse; b9f4a06641eedd7b is this workspace's).
EXPECTED_VOLUME = ("workspace", "retail", "raw")
KNOWN_WAREHOUSE_ID = "b9f4a06641eedd7b"

ROLES_TABLE = "workspace.retail.autopilot_roles"

MANUAL_CHECKLIST = """\
Manual steps this bootstrapper cannot perform (see infra/README.md, 'not codified'):

  1. Data upload
       make datagen                                  # banking inserts.sql (applied by `make bootstrap`)
       databricks fs cp <local-batches> dbfs:/Volumes/workspace/retail/raw/clickstream/
       (keep ground_truth.jsonl local — it is the DQ answer key, not input data)
  2. Deploy Plane A (jobs + retail_medallion pipeline + notebooks)
       databricks bundle deploy -t dev
       databricks bundle run retail_medallion -t dev  # then re-run this script's step 2
  3. Genie spaces
       via the phase drivers (python -m genie_autopilot.phase_d ...) or the documented
       API payloads (POST /api/2.0/genie/spaces + benchmarks/*.yaml); then re-wire the
       new ids into GA_GENIE_SPACE_ID / GA_RETAIL_SPACE_ID and the space_id parameters
       in resources/autopilot_jobs.yml
  4. Vector Search index
       run notebook 60 (60_semantic_router.py) or the router_training job — it creates
       the 'semantic-memory' endpoint + delta-sync index idempotently
  5. AI/BI dashboard
       built interactively via the Databricks Assistant from sql/dashboard_queries.sql
       (the query source of record); not declarable from the repo\
"""


def _banner(step: str, title: str) -> None:
    print(f"\n=== [{step}] {title} " + "=" * max(0, 60 - len(title)))


# --- steps ---------------------------------------------------------------------


def step_workspace_sql(w, warehouse_id: str) -> None:
    """Step 1: apply infra/workspace_setup.sql (idempotent DDL + MERGE + one grant)."""
    _banner("1/6", "workspace SQL — schemas, volume, role registry, grants")
    n = _run_sql_file(w, warehouse_id, WORKSPACE_SETUP_SQL)
    print(f"workspace_setup.sql: {n} statements OK (all IF NOT EXISTS / MERGE — idempotent)")


def step_domain_sql(w, warehouse_id: str) -> list[str]:
    """Step 2: apply the retail-domain SQL files; pipeline-dependent failures warn."""
    _banner("2/6", "domain SQL — banking DDL, retail metric views, KPI views")
    warnings: list[str] = []
    for path in DOMAIN_SQL_FILES:
        try:
            n = _run_sql_file(w, warehouse_id, path)
            print(f"{path.name}: {n} statements OK")
        except Exception as exc:  # keep going: view DDL legitimately fails pre-pipeline
            msg = (
                f"{path.name} FAILED: {exc}\n"
                "  (expected on a fresh workspace if the retail_medallion pipeline has "
                "not produced the gold tables yet — run "
                "`databricks bundle run retail_medallion -t dev`, then re-run: "
                "python infra/bootstrap.py --skip-workspace-sql --skip-lakebase)"
            )
            print(msg)
            warnings.append(msg)
    return warnings


def step_lakebase(w) -> None:
    """Step 3: ensure the Lakebase project and the HITL schema exist (Plane C)."""
    _banner("3/6", "Lakebase — project 'genie-autopilot' + HITL schema")
    # ensure_project is create-or-get: POST, and on already-exists falls back to GET.
    project = lakebase.ensure_project(w)
    print(f"project '{lakebase.DEFAULT_PROJECT_ID}' present "
          f"(keys: {sorted(project)[:6] if isinstance(project, dict) else type(project)})")
    # Postgres login: 1h OAuth token minted per connection; the current user's
    # email is the Postgres role (single-identity FE reality).
    host, token = lakebase.get_credential(w, lakebase.DEFAULT_PROJECT_ID)
    email = w.current_user.me().user_name
    conn = lakebase.connect(host, token, email)
    try:
        # CREATE TABLE IF NOT EXISTS for hitl_queue + healing_history — idempotent.
        lakebase.ensure_schema(conn)
    finally:
        conn.close()
    print(f"HITL schema ensured on {host} as {email} (hitl_queue, healing_history)")


def step_verify(w, warehouse_id: str) -> None:
    """Step 4: list-and-assert the primitives every later phase depends on."""
    _banner("4/6", "verify — raw volume + SQL warehouse")
    catalog, schema, volume = EXPECTED_VOLUME
    names = {v.name for v in w.volumes.list(catalog_name=catalog, schema_name=schema)}
    assert volume in names, (
        f"volume {catalog}.{schema}.{volume} missing (found: {sorted(names)}); "
        "re-run step 1 (workspace SQL)"
    )
    print(f"volume {catalog}.{schema}.{volume}: present")

    warehouse_ids = {wh.id for wh in w.warehouses.list()}
    assert warehouse_ids, "no SQL warehouse in the workspace — FE should provision one"
    assert warehouse_id in warehouse_ids, (
        f"resolved warehouse {warehouse_id} not in workspace list {sorted(warehouse_ids)}"
    )
    note = " (the known FE warehouse)" if warehouse_id == KNOWN_WAREHOUSE_ID else ""
    print(f"warehouse {warehouse_id}: present{note}")


def step_checklist() -> None:
    """Step 5: print what IaC cannot do — the honest manual-steps ledger."""
    _banner("5/6", "manual-steps checklist")
    print(MANUAL_CHECKLIST)


def step_roles(w, warehouse_id: str) -> None:
    """Step 6: print active role assignments from the APPLICATION-tier registry."""
    _banner("6/6", f"role assignments — {ROLES_TABLE}")
    resp = w.statement_execution.execute_statement(
        statement=(
            "SELECT principal, role, granted_by, CAST(granted_at AS STRING) "
            f"FROM {ROLES_TABLE} WHERE revoked_at IS NULL ORDER BY principal, role"
        ),
        warehouse_id=warehouse_id,
        wait_timeout="50s",
    )
    state = resp.status.state.value if resp.status and resp.status.state else "UNKNOWN"
    if state != "SUCCEEDED":
        raise RuntimeError(f"role query did not succeed (state={state}); did step 1 run?")
    rows = (resp.result.data_array or []) if resp.result else []
    if not rows:
        print("no active roles — step 1 seeds cfollmer@strataintel.ai; re-run it")
        return
    for principal, role, granted_by, granted_at in rows:
        print(f"  {principal:<30} {role:<18} granted_by={granted_by} at={granted_at}")
    print(f"({len(rows)} active grant(s); enforcement points documented in infra/rbac.md)")


# --- entrypoint ------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="infra/bootstrap.py",
        description="One-shot, idempotent environment bootstrap (Plane B + Lakebase).",
    )
    ap.add_argument("--skip-workspace-sql", action="store_true",
                    help="skip step 1 (infra/workspace_setup.sql)")
    ap.add_argument("--skip-domain-sql", action="store_true",
                    help="skip step 2 (sql/bootstrap.sql + retail metric/KPI views)")
    ap.add_argument("--skip-lakebase", action="store_true",
                    help="skip step 3 (Lakebase project + HITL schema)")
    ap.add_argument("--skip-verify", action="store_true",
                    help="skip step 4 (volume + warehouse assertions)")
    ap.add_argument("--skip-checklist", action="store_true",
                    help="skip step 5 (manual-steps checklist print)")
    ap.add_argument("--skip-roles", action="store_true",
                    help="skip step 6 (autopilot_roles printout)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Lazy client creation: only steps that talk to the workspace force auth.
    # `--skip-*` everything except the checklist and this runs fully offline.
    needs_workspace = not (
        args.skip_workspace_sql
        and args.skip_domain_sql
        and args.skip_lakebase
        and args.skip_verify
        and args.skip_roles
    )
    w = config.workspace_client() if needs_workspace else None
    # Warehouse resolution: GA_WAREHOUSE_ID env if set, else the first (on this
    # FE workspace: the only) warehouse — same logic every CLI lane uses.
    warehouse_id = _warehouse_id(w) if w is not None else ""
    if w is not None:
        print(f"workspace: {config.HOST}\nwarehouse: {warehouse_id}")

    warnings: list[str] = []
    if not args.skip_workspace_sql:
        step_workspace_sql(w, warehouse_id)
    if not args.skip_domain_sql:
        warnings += step_domain_sql(w, warehouse_id)
    if not args.skip_lakebase:
        step_lakebase(w)
    if not args.skip_verify:
        step_verify(w, warehouse_id)
    if not args.skip_checklist:
        step_checklist()
    if not args.skip_roles:
        step_roles(w, warehouse_id)

    print()
    if warnings:
        print(f"bootstrap finished with {len(warnings)} warning(s) — see step 2 output above.")
        return 1
    print("bootstrap finished cleanly. Next: `databricks bundle deploy -t dev`, "
          "then the phase drivers (see infra/README.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
