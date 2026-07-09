-- Genie Autopilot: banking gold schema (Cross-BU Retail Banking & Compliance)
-- Free Edition note: default catalog is `workspace`. CREATE CATALOG may be restricted;
-- if `CREATE CATALOG banking` fails, everything runs in workspace.banking_gold (the
-- default GA_CATALOG/GA_SCHEMA in src/genie_autopilot/config.py).

CREATE SCHEMA IF NOT EXISTS workspace.banking_gold
  COMMENT 'Gold layer for the Genie Autopilot capstone. Synthetic data only.';

-- Deliberately sparse comments below: the flywheel EARNS the good metadata.

CREATE TABLE IF NOT EXISTS workspace.banking_gold.dim_customers (
  customer_id STRING NOT NULL,
  customer_name STRING,
  segment STRING COMMENT 'Mass Affluent | High Net Worth | Retail',
  onboarded_date DATE,
  CONSTRAINT pk_customers PRIMARY KEY (customer_id)
);

CREATE TABLE IF NOT EXISTS workspace.banking_gold.fact_transactions (
  transaction_id STRING NOT NULL,
  customer_id STRING NOT NULL,
  account_type STRING COMMENT 'Checking | Savings',
  amount DOUBLE,
  available_balance DOUBLE,
  posted_date DATE,
  CONSTRAINT pk_transactions PRIMARY KEY (transaction_id),
  CONSTRAINT fk_txn_customer FOREIGN KEY (customer_id) REFERENCES workspace.banking_gold.dim_customers (customer_id)
);

CREATE TABLE IF NOT EXISTS workspace.banking_gold.fact_wealth_portfolios (
  portfolio_id STRING NOT NULL,
  customer_id STRING NOT NULL,
  liquid_cash_assets DOUBLE,
  invested_market_value DOUBLE,
  last_valuation_date DATE,
  CONSTRAINT pk_portfolios PRIMARY KEY (portfolio_id),
  CONSTRAINT fk_pf_customer FOREIGN KEY (customer_id) REFERENCES workspace.banking_gold.dim_customers (customer_id)
);

-- Flywheel state tables (proposals + audit ledger mirror)
CREATE TABLE IF NOT EXISTS workspace.banking_gold.autopilot_proposals (
  proposal_key STRING,
  term STRING,
  entity STRING,
  confidence DOUBLE,
  distinct_users INT,
  status STRING COMMENT 'proposed | approved | applied | rejected | rolled_back',
  created_ts TIMESTAMP,
  decided_by STRING
);

CREATE TABLE IF NOT EXISTS workspace.banking_gold.autopilot_audit_ledger (
  ts TIMESTAMP,
  action STRING,
  target STRING,
  proposal_key STRING,
  payload STRING,
  status STRING,
  approver STRING
);
