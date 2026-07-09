-- Starter metric views (spec version 1.1). Synonyms are DELIBERATELY absent:
-- the baseline benchmark fails on cross-BU jargon, and the healing engine adds
-- learned synonyms via ALTER VIEW ... AS $$yaml$$ (see healing.add_synonyms_to_yaml).

CREATE OR REPLACE VIEW workspace.banking_gold.transactions_metrics
WITH METRICS LANGUAGE YAML AS $$
version: 1.1
source: workspace.banking_gold.fact_transactions
comment: Retail banking transaction metrics
joins:
  - name: customer
    source: workspace.banking_gold.dim_customers
    on: source.customer_id = customer.customer_id
fields:
  - name: account_type
    expr: source.account_type
  - name: posted_date
    expr: source.posted_date
  - name: segment
    expr: customer.segment
measures:
  - name: total_amount
    expr: SUM(source.amount)
    display_name: Total Transaction Amount
  - name: total_available_balance
    expr: SUM(source.available_balance)
    display_name: Total Available Balance
  - name: avg_available_balance
    expr: AVG(source.available_balance)
    display_name: Average Available Balance
$$;

CREATE OR REPLACE VIEW workspace.banking_gold.wealth_metrics
WITH METRICS LANGUAGE YAML AS $$
version: 1.1
source: workspace.banking_gold.fact_wealth_portfolios
comment: Wealth management portfolio metrics
joins:
  - name: customer
    source: workspace.banking_gold.dim_customers
    on: source.customer_id = customer.customer_id
fields:
  - name: segment
    expr: customer.segment
  - name: last_valuation_date
    expr: source.last_valuation_date
measures:
  - name: total_liquid_assets
    expr: SUM(source.liquid_cash_assets)
    display_name: Total Liquid Cash Assets
  - name: avg_liquid_assets
    expr: AVG(source.liquid_cash_assets)
    display_name: Average Liquid Cash Assets
  - name: total_invested_value
    expr: SUM(source.invested_market_value)
    display_name: Total Invested Market Value
$$;
