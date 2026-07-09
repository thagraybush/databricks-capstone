-- Admin monitoring queries (the OBSERVE layer of docs/admin-governance.md).
-- Free Edition facts: system.query.history IS readable on this workspace;
-- system.access.audit is NOT (see docs/backlog-free-edition-limits.md).
-- Safe column set used throughout: statement_text, executed_by, start_time,
-- total_duration_ms, execution_status, client_application. Richer columns
-- (read_bytes, produced_rows, spill metrics, compute struct) vary by release —
-- queries that use them say so inline.

-- ---------------------------------------------------------------------------
-- 1. Top-10 users by total query duration, last 7 days.
-- The top of this list gets a conversation, not a revocation.
-- ---------------------------------------------------------------------------
SELECT
  executed_by,
  COUNT(*) AS statements,
  SUM(total_duration_ms) AS total_duration_ms,
  ROUND(AVG(total_duration_ms), 0) AS avg_duration_ms
FROM system.query.history
WHERE start_time >= CURRENT_TIMESTAMP() - INTERVAL 7 DAYS
GROUP BY executed_by
ORDER BY total_duration_ms DESC
LIMIT 10;

-- ---------------------------------------------------------------------------
-- 2. Slowest individual statements, last 7 days.
-- ---------------------------------------------------------------------------
SELECT
  start_time,
  executed_by,
  client_application,
  execution_status,
  total_duration_ms,
  LEFT(statement_text, 200) AS statement_preview
FROM system.query.history
WHERE start_time >= CURRENT_TIMESTAMP() - INTERVAL 7 DAYS
ORDER BY total_duration_ms DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 3. Repeated failing statements, last 7 days.
-- Same fingerprint failing >= 2 times = someone stuck; feed the coaching loop
-- (a vocabulary or documentation gap, not misconduct).
-- ---------------------------------------------------------------------------
SELECT
  LEFT(statement_text, 120) AS statement_fingerprint,
  executed_by,
  COUNT(*) AS failures,
  MAX(start_time) AS last_seen
FROM system.query.history
WHERE execution_status = 'FAILED'
  AND start_time >= CURRENT_TIMESTAMP() - INTERVAL 7 DAYS
GROUP BY LEFT(statement_text, 120), executed_by
HAVING COUNT(*) >= 2
ORDER BY failures DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 4. Genie-generated vs direct SQL share, last 7 days.
-- Genie statements arrive with client_application = 'Databricks SQL Genie Space'.
-- Rising Genie share among business personas is the project's success metric;
-- a falling share after a bad-answer episode is the shadow-analytics early warning.
-- ---------------------------------------------------------------------------
SELECT
  CASE WHEN client_application = 'Databricks SQL Genie Space'
       THEN 'genie'
       ELSE COALESCE(client_application, 'unknown')
  END AS source,
  COUNT(*) AS statements,
  ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_statements,
  SUM(total_duration_ms) AS total_duration_ms
FROM system.query.history
WHERE start_time >= CURRENT_TIMESTAMP() - INTERVAL 7 DAYS
GROUP BY 1
ORDER BY statements DESC;

-- ---------------------------------------------------------------------------
-- 5. Statements scanning the most data, last 7 days.
-- read_bytes is present on current releases of system.query.history but is NOT
-- in the guaranteed-safe column set; if this errors on a future release, drop
-- read_bytes/read_gib and fall back to query 2 (duration as the cost proxy).
-- ---------------------------------------------------------------------------
SELECT
  start_time,
  executed_by,
  client_application,
  read_bytes,
  ROUND(read_bytes / POW(1024, 3), 2) AS read_gib,
  total_duration_ms,
  LEFT(statement_text, 200) AS statement_preview
FROM system.query.history
WHERE start_time >= CURRENT_TIMESTAMP() - INTERVAL 7 DAYS
ORDER BY read_bytes DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 6. Healing activity by approver lane, per week (audit-ledger review).
-- approver distinguishes the auto-approve lane from the human (HITL) lane.
-- Current posture per docs/eval-evidence.md: 6 healings applied, 0 auto,
-- 6 human-reviewed; poison terms must only ever appear as instructions.
-- ---------------------------------------------------------------------------
SELECT
  CAST(DATE_TRUNC('WEEK', ts) AS DATE) AS week,
  approver,
  action,
  status,
  COUNT(*) AS healings
FROM workspace.retail.autopilot_audit_ledger
GROUP BY CAST(DATE_TRUNC('WEEK', ts) AS DATE), approver, action, status
ORDER BY week DESC, healings DESC;

-- ---------------------------------------------------------------------------
-- 7. Audit-ledger status mix (quick health read of the governance gate).
-- ---------------------------------------------------------------------------
SELECT
  status,
  COUNT(*) AS actions,
  MIN(ts) AS first_seen,
  MAX(ts) AS last_seen
FROM workspace.retail.autopilot_audit_ledger
GROUP BY status
ORDER BY actions DESC;

-- ---------------------------------------------------------------------------
-- 8. HITL posture: proposals below the auto-approve gate.
-- The hitl_queue table lives in LAKEBASE (serverless Postgres), not in the
-- warehouse — the queries below are Postgres SQL; run them in the Lakebase SQL
-- editor (or psql against the Lakebase project), NOT on the Databricks warehouse.
--
--   -- Queue depth by status (an ageing 'pending' pile is admin debt):
--   SELECT status, COUNT(*) AS proposals, MIN(created_at) AS oldest
--   FROM hitl_queue
--   GROUP BY status
--   ORDER BY proposals DESC;
--
--   -- Oldest pending proposals to triage in the weekly review:
--   SELECT proposal_key, payload, created_at
--   FROM hitl_queue
--   WHERE status = 'pending'
--   ORDER BY created_at
--   LIMIT 20;
--
-- Decisions flow back as approvals; every APPLIED healing is mirrored into
-- workspace.retail.autopilot_audit_ledger (queries 6-7 above), so the warehouse
-- side always shows what actually changed even though the queue itself is
-- operational-store data.
-- ---------------------------------------------------------------------------
