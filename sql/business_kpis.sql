-- Certified business KPI layer over the workspace.retail gold tables.
-- These are the run-the-business (RTB) metrics the personas consume daily/weekly:
--   kpi_monthly_summary     — CFO month-end close review
--   kpi_customer_health     — marketing retention review (whales / churn risks)
--   kpi_funnel_weekly       — PM funnel standup
--   kpi_country_performance — merchandising & marketing country review
-- Definitions mirror the certified answers in benchmarks/retail_questions.yaml
-- (bounce = n_views=1 & no carts/purchases; churn risk = recency_days > 90;
-- return rate = returns_value / gross_revenue; AOV = net revenue per distinct
-- invoice). line_amount in fact_sales is signed (negative for returns), so
-- SUM(line_amount) is net revenue; gross/returns split keys off is_return.

-- ---------------------------------------------------------------------------
-- 1. Monthly business summary (CFO month-end close)
-- known_customers is computed from fact_sales because daily distinct counts in
-- gold_daily_revenue cannot be summed into a monthly distinct.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW workspace.retail.kpi_monthly_summary AS
WITH monthly AS (
  SELECT
    CAST(DATE_TRUNC('MONTH', d.sale_date) AS DATE) AS month,
    SUM(d.gross_revenue)  AS gross_revenue,
    SUM(d.net_revenue)    AS net_revenue,
    SUM(d.returns_value)  AS returns_value,
    SUM(d.invoices)       AS invoices
  FROM workspace.retail.gold_daily_revenue d
  GROUP BY CAST(DATE_TRUNC('MONTH', d.sale_date) AS DATE)
),
monthly_customers AS (
  SELECT
    CAST(DATE_TRUNC('MONTH', f.sale_date) AS DATE) AS month,
    COUNT(DISTINCT f.customer_id) AS known_customers
  FROM workspace.retail.fact_sales f
  WHERE NOT f.is_anonymous
  GROUP BY CAST(DATE_TRUNC('MONTH', f.sale_date) AS DATE)
)
SELECT
  m.month,
  m.gross_revenue,
  m.net_revenue,
  m.returns_value,
  ROUND(100 * m.returns_value / NULLIF(m.gross_revenue, 0), 2) AS return_rate_pct,
  m.invoices,
  ROUND(m.net_revenue / NULLIF(m.invoices, 0), 2) AS aov,
  c.known_customers,
  ROUND(
    100 * (m.net_revenue - LAG(m.net_revenue) OVER (ORDER BY m.month))
        / NULLIF(LAG(m.net_revenue) OVER (ORDER BY m.month), 0), 2
  ) AS mom_net_revenue_growth_pct
FROM monthly m
LEFT JOIN monthly_customers c ON m.month = c.month;

-- ---------------------------------------------------------------------------
-- 2. Customer health by RFM monetary quartile (marketing retention review)
-- Q1 = top monetary quartile (whales live here); 'all' row is the book-level
-- rollup. repeat_purchase_rate_pct = share of customers with frequency > 1;
-- churn_risk_count = customers with recency_days > 90 (the certified threshold
-- behind the healed "churn risks" jargon question).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW workspace.retail.kpi_customer_health AS
WITH scored AS (
  SELECT
    customer_id,
    recency_days,
    frequency,
    monetary,
    NTILE(4) OVER (ORDER BY monetary DESC) AS monetary_quartile
  FROM workspace.retail.gold_customer_rfm
),
by_quartile AS (
  SELECT
    CONCAT('Q', CAST(monetary_quartile AS STRING)) AS segment,
    monetary_quartile AS sort_order,
    COUNT(*) AS customers,
    ROUND(AVG(monetary), 2) AS avg_monetary,
    ROUND(AVG(recency_days), 1) AS avg_recency_days,
    ROUND(100 * AVG(CASE WHEN frequency > 1 THEN 1.0 ELSE 0.0 END), 2)
      AS repeat_purchase_rate_pct,
    SUM(CASE WHEN recency_days > 90 THEN 1 ELSE 0 END) AS churn_risk_count
  FROM scored
  GROUP BY monetary_quartile
),
overall AS (
  SELECT
    'all' AS segment,
    0 AS sort_order,
    COUNT(*) AS customers,
    ROUND(AVG(monetary), 2) AS avg_monetary,
    ROUND(AVG(recency_days), 1) AS avg_recency_days,
    ROUND(100 * AVG(CASE WHEN frequency > 1 THEN 1.0 ELSE 0.0 END), 2)
      AS repeat_purchase_rate_pct,
    SUM(CASE WHEN recency_days > 90 THEN 1 ELSE 0 END) AS churn_risk_count
  FROM scored
)
SELECT segment, customers, avg_monetary, avg_recency_days,
       repeat_purchase_rate_pct, churn_risk_count
FROM (
  SELECT * FROM by_quartile
  UNION ALL
  SELECT * FROM overall
)
ORDER BY sort_order;

-- ---------------------------------------------------------------------------
-- 3. Weekly funnel health (PM funnel standup)
-- Bounce proxy matches the certified benchmark definition: non-bot sessions
-- with exactly one view and no carts or purchases. revenue_per_session joins
-- weekly net revenue from gold_daily_revenue; it is only meaningful for weeks
-- where the clickstream and sales windows overlap (LEFT JOIN keeps all
-- clickstream weeks, revenue columns are NULL outside the overlap).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW workspace.retail.kpi_funnel_weekly AS
WITH weekly_sessions AS (
  SELECT
    CAST(DATE_TRUNC('WEEK', started_at) AS DATE) AS week,
    COUNT(*) AS sessions,
    SUM(CASE WHEN converted THEN 1 ELSE 0 END) AS converted_sessions,
    SUM(CASE WHEN n_views = 1 AND n_carts = 0 AND n_purchases = 0
             THEN 1 ELSE 0 END) AS bounce_sessions
  FROM workspace.retail.gold_sessions
  WHERE NOT is_bot
  GROUP BY CAST(DATE_TRUNC('WEEK', started_at) AS DATE)
),
weekly_revenue AS (
  SELECT
    CAST(DATE_TRUNC('WEEK', sale_date) AS DATE) AS week,
    SUM(net_revenue) AS net_revenue
  FROM workspace.retail.gold_daily_revenue
  GROUP BY CAST(DATE_TRUNC('WEEK', sale_date) AS DATE)
)
SELECT
  s.week,
  s.sessions,
  s.converted_sessions,
  ROUND(s.converted_sessions / NULLIF(s.sessions, 0), 4) AS session_conversion_rate,
  s.bounce_sessions,
  ROUND(100 * s.bounce_sessions / NULLIF(s.sessions, 0), 2) AS bounce_rate_pct,
  r.net_revenue,
  ROUND(r.net_revenue / NULLIF(s.sessions, 0), 2) AS revenue_per_session
FROM weekly_sessions s
LEFT JOIN weekly_revenue r ON s.week = r.week
ORDER BY s.week;

-- ---------------------------------------------------------------------------
-- 4. Country performance (merchandising / marketing country review)
-- Built on fact_sales so return_rate and AOV share one grain; revenue_rank is
-- densest-first (rank 1 = highest net revenue).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW workspace.retail.kpi_country_performance AS
WITH by_country AS (
  SELECT
    country,
    SUM(line_amount) AS net_revenue,
    SUM(CASE WHEN NOT is_return THEN line_amount ELSE 0 END) AS gross_revenue,
    SUM(CASE WHEN is_return THEN -line_amount ELSE 0 END) AS returns_value,
    COUNT(DISTINCT invoice_id) AS invoices
  FROM workspace.retail.fact_sales
  GROUP BY country
)
SELECT
  country,
  net_revenue,
  ROUND(100 * returns_value / NULLIF(gross_revenue, 0), 2) AS return_rate_pct,
  ROUND(net_revenue / NULLIF(invoices, 0), 2) AS aov,
  RANK() OVER (ORDER BY net_revenue DESC) AS revenue_rank
FROM by_country;

-- ---------------------------------------------------------------------------
-- Certification intent: mark the KPI views so Catalog Explorer and Genie see
-- them as the paved path (formulas match the certified benchmark answers).
-- ---------------------------------------------------------------------------
COMMENT ON TABLE workspace.retail.kpi_monthly_summary IS
  'CERTIFIED (intent): monthly RTB summary for month-end close. Return rate = returns_value/gross_revenue; AOV = net revenue per distinct invoice; MoM growth via LAG. Definitions match benchmarks/retail_questions.yaml.';

COMMENT ON TABLE workspace.retail.kpi_customer_health IS
  'CERTIFIED (intent): customer health by RFM monetary quartile (Q1 = top). Repeat purchase = frequency > 1; churn risk = recency_days > 90 (the certified threshold behind the healed churn-risk jargon).';

COMMENT ON TABLE workspace.retail.kpi_funnel_weekly IS
  'CERTIFIED (intent): weekly non-bot funnel. Bounce = single view, no carts, no purchases (certified benchmark definition); revenue_per_session valid only where clickstream and sales windows overlap.';

COMMENT ON TABLE workspace.retail.kpi_country_performance IS
  'CERTIFIED (intent): country league table on fact_sales. Rank 1 = highest net revenue; return rate and AOV share the invoice-line grain.';
