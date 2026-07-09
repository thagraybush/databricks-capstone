-- Retail metric views (spec version 1.1) over the workspace.retail gold layer.
-- Synonyms are DELIBERATELY sparse/absent: the baseline Genie space must fail on
-- business jargon (GMV, AOV, take rate, conversion, ...) so the flywheel earns
-- them via healing (see healing.add_synonyms_to_yaml + ALTER VIEW ... AS $$yaml$$).

-- fact_sales → dim_products is many-to-one on stock_code (each sale line has one
-- product; a product appears on many lines). line_amount is signed: negative for
-- returns, so SUM(line_amount) is net and the gross/returns split keys off is_return.
CREATE OR REPLACE VIEW workspace.retail.revenue_metrics
WITH METRICS LANGUAGE YAML AS $$
version: 1.1
source: workspace.retail.fact_sales
comment: Retail sales revenue metrics (line_amount signed; negative for returns)
joins:
  - name: product
    source: workspace.retail.dim_products
    on: source.stock_code = product.stock_code
fields:
  - name: sale_date
    expr: source.sale_date
  - name: country
    expr: source.country
  - name: product_name
    expr: product.product_name
  - name: is_return
    expr: source.is_return
measures:
  - name: net_revenue
    expr: SUM(source.line_amount)
    display_name: Net Revenue
  - name: gross_revenue
    expr: SUM(CASE WHEN source.is_return THEN 0 ELSE source.line_amount END)
    display_name: Gross Revenue
  - name: returns_value
    expr: SUM(CASE WHEN source.is_return THEN -source.line_amount ELSE 0 END)
    display_name: Returns Value
  - name: aov
    expr: SUM(source.line_amount) / COUNT(DISTINCT source.invoice_id)
    display_name: Average Order Value
  - name: units
    expr: SUM(source.quantity)
    display_name: Units
$$;

CREATE OR REPLACE VIEW workspace.retail.funnel_metrics
WITH METRICS LANGUAGE YAML AS $$
version: 1.1
source: workspace.retail.gold_funnel_daily
comment: Daily clickstream funnel metrics (one row per event_date)
fields:
  - name: event_date
    expr: source.event_date
measures:
  - name: total_sessions
    expr: SUM(source.sessions)
    display_name: Total Sessions
  - name: total_purchases
    expr: SUM(source.purchases)
    display_name: Total Purchases
  - name: avg_view_to_cart_rate
    expr: AVG(source.view_to_cart_rate)
    display_name: Average View-to-Cart Rate
  - name: avg_cart_to_purchase_rate
    expr: AVG(source.cart_to_purchase_rate)
    display_name: Average Cart-to-Purchase Rate
  - name: avg_session_conversion_rate
    expr: AVG(source.session_conversion_rate)
    display_name: Average Session Conversion Rate
$$;
