-- Flywheel health dashboard queries (AI/BI dashboard datasets).
-- Each block is one dashboard dataset; wire into an AI/BI dashboard over the
-- Serverless Starter Warehouse.

-- 1. Benchmark accuracy trend (stratified) — the flywheel's heartbeat
SELECT phase, stratum, correct, total, ROUND(correct / total, 3) AS accuracy
FROM workspace.retail.autopilot_eval_history
ORDER BY recorded_at, stratum;

-- 2. Data-quality quarantine mix (retail sales)
SELECT reason, COUNT(*) AS rows
FROM (SELECT explode(quarantine_reasons) AS reason FROM workspace.retail.quarantine_sales)
GROUP BY reason ORDER BY rows DESC;

-- 3. Clickstream DQ health vs producer chaos
SELECT
  (SELECT COUNT(*) FROM workspace.retail.bronze_events)     AS bronze_events,
  (SELECT COUNT(*) FROM workspace.retail.silver_events)     AS silver_events,
  (SELECT COUNT(*) FROM workspace.retail.quarantine_events) AS quarantined,
  (SELECT COUNT(*) FROM workspace.retail.silver_events WHERE pii_detected) AS pii_caught,
  (SELECT COUNT(*) FROM workspace.retail.gold_sessions WHERE is_bot)       AS bot_sessions;

-- 4. Funnel health (non-bot)
SELECT event_date, sessions, views, add_to_carts, purchases,
       ROUND(session_conversion_rate, 4) AS conversion
FROM workspace.retail.gold_funnel_daily ORDER BY event_date;

-- 5. Revenue trend with returns
SELECT sale_date, gross_revenue, returns_value, net_revenue, invoices
FROM workspace.retail.gold_daily_revenue ORDER BY sale_date;

-- 6. Healing activity (mirror of the audit ledger, loaded by ops jobs)
SELECT ts, action, target, proposal_key, status, approver
FROM workspace.retail.autopilot_audit_ledger ORDER BY ts DESC LIMIT 100;
