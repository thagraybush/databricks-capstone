-- ============================================================================
-- infra/workspace_setup.sql — workspace-level primitives for Genie Autopilot
-- ============================================================================
-- Plane B, file 1 of 4 (see infra/README.md). Applied by infra/bootstrap.py via
-- the repo's SQL runner (genie_autopilot.cli._run_sql_file → Statement Execution
-- API). Every statement is idempotent: re-running this file converges to the
-- same state instead of erroring.
--
-- Scope discipline: ONLY workspace-level primitives live here — schemas, the
-- landing volume, the role registry, and grants. Domain DDL stays in sql/
-- (bootstrap.sql, retail_metric_views.sql, business_kpis.sql); pipeline-owned
-- tables (bronze/silver/gold/quarantine) are declared by the Lakeflow pipeline
-- and are deliberately NOT re-declared here.
--
-- Runner note: the SQL runner strips full-line comments before splitting on
-- ';', so keep comments on their own lines (as done throughout this file).
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Domain schemas
-- ----------------------------------------------------------------------------
-- WHAT: the two domain schemas inside the FE default catalog `workspace`.
-- WHY: Free Edition restricts CREATE CATALOG, so both domains live as schemas
-- under `workspace` (mirrors the note at the top of sql/bootstrap.sql). Created
-- here — not in the domain DDL — because the schemas are the workspace-level
-- container everything else (volume, registry, grants) hangs off.

CREATE SCHEMA IF NOT EXISTS workspace.retail
  COMMENT 'Retail medallion domain for the Genie Autopilot capstone: raw volume, bronze/silver/gold (pipeline-owned), metric views, certified kpi_* views, flywheel state. Synthetic + UCI Online Retail II data only.';

CREATE SCHEMA IF NOT EXISTS workspace.banking_gold
  COMMENT 'Gold layer for the Genie Autopilot capstone (banking flywheel v1). Synthetic data only.';


-- ----------------------------------------------------------------------------
-- 2. Raw landing volume
-- ----------------------------------------------------------------------------
-- WHAT: the managed volume the chaos producer lands clickstream JSONL into and
-- Auto Loader (bronze) reads from (pipelines/retail_medallion.py RAW_PATH =
-- /Volumes/workspace/retail/raw/).
-- WHY: the volume is the producer/engineer boundary in the RBAC model — the
-- data_producer role touches ONLY this object (see infra/rbac.md). It must
-- exist before the pipeline first runs and before any `databricks fs cp`.

CREATE VOLUME IF NOT EXISTS workspace.retail.raw
  COMMENT 'Raw landing zone: producer-emitted clickstream JSONL batches (with labeled chaos) under /clickstream/. Read by the retail_medallion Auto Loader bronze layer. Write access = data_producer + data_engineer roles.';


-- ----------------------------------------------------------------------------
-- 3. Role registry (APPLICATION enforcement tier)
-- ----------------------------------------------------------------------------
-- WHAT: the workspace's application-level role table. One row per (principal,
-- role) grant; revocation is a soft-delete via revoked_at so history is never
-- lost (receipts posture — every governance action leaves an audit trail).
-- WHY: Free Edition has no groups/SCIM, so UC grants cannot separate personas.
-- This table CAN — it is a plain Delta read at notebook runtime. The steward
-- console (notebook 80) and the healing appliers (notebook 30) gate privileged
-- actions on membership here (role = 'metric_steward', revoked_at IS NULL).
-- Full role model: infra/rbac.md.

CREATE TABLE IF NOT EXISTS workspace.retail.autopilot_roles (
  principal STRING COMMENT 'User email (FE) or group name (paid workspace) holding the role',
  role STRING COMMENT 'workspace_admin | data_producer | data_engineer | data_scientist | business_consumer | metric_steward',
  granted_by STRING COMMENT 'Who granted it: bootstrap, or a workspace_admin principal',
  granted_at TIMESTAMP COMMENT 'When the role was granted',
  revoked_at TIMESTAMP COMMENT 'NULL while active; set (never deleted) on revocation so the grant history is auditable'
)
COMMENT 'APPLICATION-tier role registry for the autopilot. Enforced by notebook-level checks (steward console, healing appliers) — the FE-workable half of the two-tier RBAC design in infra/rbac.md.';

-- WHAT: idempotent seeding of the two roles the single FE user actually holds.
-- WHY: MERGE (not INSERT) so re-running bootstrap never duplicates rows; the
-- match key includes role and the active-row predicate, so a revoked role could
-- later be legitimately re-granted as a new row. granted_by='bootstrap' marks
-- these as machine-seeded, distinguishing them from human grants.

MERGE INTO workspace.retail.autopilot_roles AS t
USING (
  SELECT 'cfollmer@strataintel.ai' AS principal, 'workspace_admin' AS role, 'bootstrap' AS granted_by
  UNION ALL
  SELECT 'cfollmer@strataintel.ai' AS principal, 'metric_steward' AS role, 'bootstrap' AS granted_by
) AS s
ON t.principal = s.principal AND t.role = s.role AND t.revoked_at IS NULL
WHEN NOT MATCHED THEN
  INSERT (principal, role, granted_by, granted_at, revoked_at)
  VALUES (s.principal, s.role, s.granted_by, current_timestamp(), NULL);


-- ============================================================================
-- 4. PLATFORM-tier grants — paid-workspace block (COMMENTED OUT ON PURPOSE)
-- ============================================================================
-- Free Edition has no SCIM, no account console, no groups, no service
-- principals: the group principals below (`workspace_admins`, `data_producers`,
-- ...) do not exist here, and a GRANT to a missing principal is a hard error —
-- so leaving this block executable would break the idempotent bootstrap.
-- It is kept as the exact, reviewed grant surface to run VERBATIM on a paid
-- workspace on day 1, immediately after creating the six groups (persona →
-- group mapping in infra/rbac.md). On this FE workspace every capability below
-- already resolves to the single admin user, so the grants would restrict
-- no one even if they could execute. Do NOT uncomment on Free Edition.
--
-- -- workspace_admin: platform ownership of both domain schemas + the volume.
-- GRANT ALL PRIVILEGES ON SCHEMA workspace.retail       TO `workspace_admins`;
-- GRANT ALL PRIVILEGES ON SCHEMA workspace.banking_gold TO `workspace_admins`;
-- GRANT ALL PRIVILEGES ON VOLUME workspace.retail.raw   TO `workspace_admins`;
--
-- -- data_producer: lands files in the raw volume; never touches tables.
-- GRANT USE CATALOG ON CATALOG workspace            TO `data_producers`;
-- GRANT USE SCHEMA ON SCHEMA workspace.retail       TO `data_producers`;
-- GRANT READ VOLUME  ON VOLUME workspace.retail.raw TO `data_producers`;
-- GRANT WRITE VOLUME ON VOLUME workspace.retail.raw TO `data_producers`;
--
-- -- data_engineer: owns the medallion pipeline; full read/write in retail.
-- GRANT USE CATALOG ON CATALOG workspace            TO `data_engineers`;
-- GRANT USE SCHEMA ON SCHEMA workspace.retail       TO `data_engineers`;
-- GRANT CREATE TABLE ON SCHEMA workspace.retail     TO `data_engineers`;
-- GRANT SELECT ON SCHEMA workspace.retail           TO `data_engineers`;
-- GRANT MODIFY ON SCHEMA workspace.retail           TO `data_engineers`;
-- GRANT READ VOLUME  ON VOLUME workspace.retail.raw TO `data_engineers`;
-- GRANT WRITE VOLUME ON VOLUME workspace.retail.raw TO `data_engineers`;
--
-- -- data_scientist: reads gold, writes model/forecast tables, registers models.
-- GRANT USE CATALOG ON CATALOG workspace            TO `data_scientists`;
-- GRANT USE SCHEMA ON SCHEMA workspace.retail       TO `data_scientists`;
-- GRANT SELECT ON SCHEMA workspace.retail           TO `data_scientists`;
-- GRANT CREATE TABLE ON SCHEMA workspace.retail     TO `data_scientists`;
-- GRANT CREATE MODEL ON SCHEMA workspace.retail     TO `data_scientists`;
-- GRANT EXECUTE ON SCHEMA workspace.retail          TO `data_scientists`;
--
-- -- business_consumer: the paved path ONLY — per-view grants on the certified
-- -- KPI views and metric views, deliberately NOT schema-wide SELECT, so
-- -- bronze/silver/quarantine stay invisible to this role.
-- GRANT USE CATALOG ON CATALOG workspace                        TO `business_consumers`;
-- GRANT USE SCHEMA ON SCHEMA workspace.retail                   TO `business_consumers`;
-- GRANT SELECT ON VIEW workspace.retail.kpi_monthly_summary     TO `business_consumers`;
-- GRANT SELECT ON VIEW workspace.retail.kpi_customer_health     TO `business_consumers`;
-- GRANT SELECT ON VIEW workspace.retail.kpi_funnel_weekly       TO `business_consumers`;
-- GRANT SELECT ON VIEW workspace.retail.kpi_country_performance TO `business_consumers`;
-- GRANT SELECT ON VIEW workspace.retail.revenue_metrics         TO `business_consumers`;
-- GRANT SELECT ON VIEW workspace.retail.funnel_metrics          TO `business_consumers`;
--
-- -- metric_steward: sees everything in the domain to rule on definitions;
-- -- writes tags and audit-ledger rows; decisions themselves live in Lakebase.
-- GRANT USE CATALOG ON CATALOG workspace                          TO `metric_stewards`;
-- GRANT USE SCHEMA ON SCHEMA workspace.retail                     TO `metric_stewards`;
-- GRANT SELECT ON SCHEMA workspace.retail                         TO `metric_stewards`;
-- GRANT APPLY TAG ON SCHEMA workspace.retail                      TO `metric_stewards`;
-- GRANT MODIFY ON TABLE workspace.retail.autopilot_audit_ledger   TO `metric_stewards`;
--
-- ============================ end paid-workspace block ======================


-- ----------------------------------------------------------------------------
-- 5. Live mechanism proof (EXECUTES on Free Edition)
-- ----------------------------------------------------------------------------
-- WHAT: exactly one real GRANT, against the real current user and an object
-- created above, so this file demonstrably exercises the UC grant mechanism on
-- FE — mechanically identical to the group grants; only the principal differs.
-- WHY this object: every role in the model is allowed to read the role
-- registry (transparency is part of the receipts posture), so the grant is
-- also meaningful in itself, and the table is guaranteed to exist because
-- section 3 just created it. Idempotent: re-granting an existing privilege is
-- a no-op in Unity Catalog.

GRANT SELECT ON TABLE workspace.retail.autopilot_roles TO `cfollmer@strataintel.ai`;
