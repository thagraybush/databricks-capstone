# RBAC Model — roles, grants, and what Free Edition actually enforces

This is the role model for the Genie Autopilot workspace, written honestly for the
platform it runs on. **Free Edition has one identity**: a single user
(`cfollmer@strataintel.ai`, workspace admin) with a single PAT — no SCIM, no account
console, no groups, no service principals (see
[docs/backlog-free-edition-limits.md](../docs/backlog-free-edition-limits.md)).
So the design uses **two enforcement tiers**, and it never pretends one is the other.

## The two enforcement tiers

**PLATFORM tier — Unity Catalog grants.**
The full grant surface per role is written as exact `GRANT` statements below and in
[workspace_setup.sql](workspace_setup.sql). On a **paid workspace** with real users
and the six groups, these execute verbatim and UC enforces them at the data plane.
On **FE**, the grants are *expressed in code but inert*: the group principals do not
exist, and every capability resolves to the single admin user anyway. The group-grant
block in `workspace_setup.sql` is therefore commented out (a `GRANT` to a missing
principal is a hard error), with one live grant against the current user kept
executable to prove the mechanism works.

**APPLICATION tier — the `workspace.retail.autopilot_roles` registry.**
A plain Delta table (`principal`, `role`, `granted_by`, `granted_at`, `revoked_at`)
created and seeded by `workspace_setup.sql`. This tier **is enforceable today on
FE** because it is just a table read at notebook runtime — no identity
infrastructure required. The enforcement points are the code paths that mutate
governed state: the steward console (notebook 80) gates `approve`/`reject` on the
acting `decided_by` principal holding `metric_steward`, and the healing appliers
(notebook 30) gate non-dry-run application the same way. The registry check is one
query:

```sql
SELECT COUNT(*) > 0 AS allowed
FROM workspace.retail.autopilot_roles
WHERE principal = :who
  AND role = 'metric_steward'   -- or the role the code path requires
  AND revoked_at IS NULL;
```

On FE the single user holds both privileged roles, so the gate passes trivially —
the value today is the mechanism plus the audit trail (`granted_by`/`revoked_at`
make role changes receipts, matching the audit-ledger posture in
[docs/admin-governance.md](../docs/admin-governance.md)). On a paid workspace the
same check becomes a real boundary the moment principals differ.

## Current assignment (this workspace, today)

| Principal | Roles | How granted |
|---|---|---|
| `cfollmer@strataintel.ai` | `workspace_admin`, `metric_steward` | seeded by the idempotent `MERGE` in `workspace_setup.sql` (`granted_by = 'bootstrap'`) |

Every other role below is held by **logical principals** — personas in the fleet
manifests (`fleet_retail.py`), not identities. That mapping is documented here so a
paid workspace can create the groups and run the grant block on day 1.

## Role summary

| Role | Purpose | Reads | Writes | Privileged surfaces |
|---|---|---|---|---|
| `workspace_admin` | owns the workspace, grants, containment | everything | everything | grants, warehouse config, role registry |
| `data_producer` | ships the app that emits events | its own landings | `raw` volume (bronze source files) | none |
| `data_engineer` | owns the medallion pipeline | all of `retail` | silver/gold (via pipeline), `raw` volume | pipeline runs |
| `data_scientist` | KPIs, forecasts, propensity model | gold | model/forecast tables, UC models | MLflow/UC model registry |
| `business_consumer` | asks questions, consumes KPIs | gold KPI views + metric views, Genie | nothing | none |
| `metric_steward` | decides `hitl_queue`, approves glossary/definitions | all of `retail` + queue | decisions (Lakebase), tags | steward console, healing approvals |

## Role details

Object naming: grants target `workspace.retail` (the retail domain). The banking
domain (`workspace.banking_gold`) follows the same pattern with the same groups.
Backticked principals are the **paid-workspace groups**; on FE they resolve to
nothing and the statements live commented-out in `workspace_setup.sql`.

### workspace_admin

- **Purpose:** platform ownership — grants, warehouse shape, statement timeouts,
  the containment layer of the admin playbook. Explicitly *not* a data approver:
  semantic decisions belong to `metric_steward`.
- **UC grants (PLATFORM tier):**
  ```sql
  GRANT ALL PRIVILEGES ON SCHEMA workspace.retail       TO `workspace_admins`;
  GRANT ALL PRIVILEGES ON SCHEMA workspace.banking_gold TO `workspace_admins`;
  GRANT ALL PRIVILEGES ON VOLUME workspace.retail.raw   TO `workspace_admins`;
  ```
- **Genie/space permissions:** CAN MANAGE on both spaces (paid); on FE the single
  user owns the spaces outright.
- **Application-tier enforcement point:** only `workspace_admin` rows may write
  `autopilot_roles` (grant/revoke); bootstrap seeds with `granted_by='bootstrap'`.

### data_producer

- **Purpose:** the product-engineering persona (`producer.py`) — lands clickstream
  JSONL (with labeled chaos) into the raw volume. Writes files, never tables.
- **UC grants (PLATFORM tier):**
  ```sql
  GRANT USE CATALOG ON CATALOG workspace                  TO `data_producers`;
  GRANT USE SCHEMA ON SCHEMA workspace.retail             TO `data_producers`;
  GRANT READ VOLUME  ON VOLUME workspace.retail.raw       TO `data_producers`;
  GRANT WRITE VOLUME ON VOLUME workspace.retail.raw       TO `data_producers`;
  ```
- **Genie/space permissions:** none — producers do not consume Genie.
- **Application-tier enforcement point:** none needed; the volume boundary is the
  whole contract. The pipeline's quarantine layer scores what producers land.

### data_engineer

- **Purpose:** owns `retail_medallion` (bronze → silver → gold + quarantine);
  the only role that writes governed tables below gold-consumption level.
- **UC grants (PLATFORM tier):**
  ```sql
  GRANT USE CATALOG ON CATALOG workspace                  TO `data_engineers`;
  GRANT USE SCHEMA ON SCHEMA workspace.retail             TO `data_engineers`;
  GRANT CREATE TABLE ON SCHEMA workspace.retail           TO `data_engineers`;
  GRANT SELECT ON SCHEMA workspace.retail                 TO `data_engineers`;
  GRANT MODIFY ON SCHEMA workspace.retail                 TO `data_engineers`;
  GRANT READ VOLUME  ON VOLUME workspace.retail.raw       TO `data_engineers`;
  GRANT WRITE VOLUME ON VOLUME workspace.retail.raw       TO `data_engineers`;
  ```
- **Genie/space permissions:** CAN RUN (paid) — engineers verify answers but do not
  edit space context; the healing appliers own that surface.
- **Application-tier enforcement point:** pipeline job ownership (Plane A). On a
  paid workspace the pipeline runs as a service principal in this group
  (backlog item: SP-owned jobs).

### data_scientist

- **Purpose:** the DS persona (notebook 50) — reads gold, publishes KPI/forecast
  tables (`AI_FORECAST`) and registers the propensity model in UC.
- **UC grants (PLATFORM tier):**
  ```sql
  GRANT USE CATALOG ON CATALOG workspace                  TO `data_scientists`;
  GRANT USE SCHEMA ON SCHEMA workspace.retail             TO `data_scientists`;
  GRANT SELECT ON SCHEMA workspace.retail                 TO `data_scientists`;
  GRANT CREATE TABLE ON SCHEMA workspace.retail           TO `data_scientists`;
  GRANT CREATE MODEL ON SCHEMA workspace.retail           TO `data_scientists`;
  GRANT EXECUTE ON SCHEMA workspace.retail                TO `data_scientists`;
  ```
- **Genie/space permissions:** CAN RUN (paid); their questions feed telemetry like
  everyone else's (authority weight 1.1 in `RETAIL_ROLE_AUTHORITY`).
- **Application-tier enforcement point:** model/forecast writes are attributed in
  the audit trail by table naming + job identity; no registry gate required.

### business_consumer

- **Purpose:** CFO / PM / marketing / merchandising personas — the paved path:
  certified `kpi_*` views, metric views, and Genie. Deliberately *cannot* read
  bronze/silver/quarantine — per-view grants, not schema-wide `SELECT`.
- **UC grants (PLATFORM tier):**
  ```sql
  GRANT USE CATALOG ON CATALOG workspace                            TO `business_consumers`;
  GRANT USE SCHEMA ON SCHEMA workspace.retail                       TO `business_consumers`;
  GRANT SELECT ON VIEW workspace.retail.kpi_monthly_summary         TO `business_consumers`;
  GRANT SELECT ON VIEW workspace.retail.kpi_customer_health         TO `business_consumers`;
  GRANT SELECT ON VIEW workspace.retail.kpi_funnel_weekly           TO `business_consumers`;
  GRANT SELECT ON VIEW workspace.retail.kpi_country_performance     TO `business_consumers`;
  GRANT SELECT ON VIEW workspace.retail.revenue_metrics             TO `business_consumers`;
  GRANT SELECT ON VIEW workspace.retail.funnel_metrics              TO `business_consumers`;
  ```
- **Genie/space permissions:** CAN RUN on the retail space (paid) — this is their
  primary interface. Rising Genie share among this role is the project's success
  metric ([docs/admin-governance.md](../docs/admin-governance.md), OBSERVE #3).
- **Application-tier enforcement point:** the query-quality router (coach layer)
  shapes their traffic; per the admin playbook, access is never revoked — bad
  questions are teaching signal, not a permissions problem.

### metric_steward

- **Purpose:** the human in HITL — decides `hitl_queue` rows (Lakebase), approves
  or rejects glossary/definition/poison-term proposals. Decide ≠ deploy: approved
  items are *applied* by the next gated ops cycle, never directly by the steward.
- **UC grants (PLATFORM tier):**
  ```sql
  GRANT USE CATALOG ON CATALOG workspace                            TO `metric_stewards`;
  GRANT USE SCHEMA ON SCHEMA workspace.retail                       TO `metric_stewards`;
  GRANT SELECT ON SCHEMA workspace.retail                           TO `metric_stewards`;
  GRANT APPLY TAG ON SCHEMA workspace.retail                        TO `metric_stewards`;
  GRANT MODIFY ON TABLE workspace.retail.autopilot_audit_ledger     TO `metric_stewards`;
  ```
- **Genie/space permissions:** CAN EDIT on both spaces (paid) — stewards review
  instructions the appliers wrote; on FE the single user covers this.
- **Application-tier enforcement point:** **the primary one in the system.** The
  steward console (notebook 80) accepts decisions only from a `decided_by`
  principal holding an unrevoked `metric_steward` row; the healing appliers
  (notebook 30) require the same before flipping `dry_run` off. Every decision is
  recorded in `hitl_queue.decided_by` and every application in the audit ledger
  with `approver=human:<name>`.

## Personas → logical principals (paid-workspace day 1)

FE personas are code (fleet manifests), not identities. Day 1 on a paid workspace:
create the six groups, add real users per this table, uncomment the grant block in
`workspace_setup.sql`, and run it — nothing else changes
([backlog](../docs/backlog-free-edition-limits.md), "Paid workspace day-1 plan").

| Fleet persona / lane | Logical principal (group) | Authority weight |
|---|---|---|
| finance (CFO lane) | `business_consumers` | 1.2 |
| merchandising | `business_consumers` | 0.9 |
| pm | `business_consumers` | 0.9 |
| marketing | `business_consumers` | 0.7 |
| data_scientist persona (notebook 50) | `data_scientists` | 1.1 |
| chaos producer (`producer.py`) | `data_producers` | n/a |
| pipeline owner (`retail_medallion`) | `data_engineers` | n/a |
| certification human (`make certify`, notebook 80) | `metric_stewards` | decisions, not questions |
| workspace operator | `workspace_admins` | n/a |

## What FE actually enforces (the honesty box)

- **Enforced today:** the APPLICATION tier (`autopilot_roles` reads in the steward
  console and healing appliers), the audit ledger receipts, warehouse-shape
  containment, statement timeouts.
- **Expressed but inert today:** every group `GRANT` in this file. They are code
  review-able and day-1 executable, but on FE there is exactly one principal and it
  is workspace admin — no UC grant here restricts anyone.
- **Never claimed:** per-persona platform identity, SP-owned automation,
  `system.access.audit` attribution. Those are backlog items with a shipped
  workaround each, not silent assumptions.
