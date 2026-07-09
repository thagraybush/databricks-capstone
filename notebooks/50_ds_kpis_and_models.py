# Databricks notebook source
# MAGIC %md
# MAGIC # 50_ds_kpis_and_models
# MAGIC Data-Science persona over the retail gold layer.
# MAGIC
# MAGIC Three sections:
# MAGIC 1. **KPIs** — monthly net revenue trend, top-10 products, return rate, RFM segments.
# MAGIC 2. **Demand forecast** — `AI_FORECAST` (Public Preview) over daily net revenue,
# MAGIC    materialized to `gold_revenue_forecast`; degrades to a warning if unavailable.
# MAGIC 3. **Purchase-propensity model** — sklearn LogisticRegression on RFM features,
# MAGIC    logged to MLflow with best-effort Unity Catalog registration.

# COMMAND ----------

from datetime import timedelta

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# COMMAND ----------

dbutils.widgets.text("domain_schema", "workspace.retail")
domain_schema = dbutils.widgets.get("domain_schema").strip()

REQUIRED_TABLES = ("gold_daily_revenue", "fact_sales", "gold_customer_rfm")
missing = [t for t in REQUIRED_TABLES if not spark.catalog.tableExists(f"{domain_schema}.{t}")]
if missing:
    print(
        f"Gold tables missing in {domain_schema}: {', '.join(missing)}. "
        "Run the retail_medallion pipeline first."
    )
    dbutils.notebook.exit("skipped: gold layer not materialized")

# COMMAND ----------

# MAGIC %md ## 1. KPIs from the gold layer

# COMMAND ----------

# MAGIC %md ### Monthly net revenue trend

# COMMAND ----------

monthly_revenue = spark.sql(
    f"""
    SELECT
      date_trunc('MONTH', sale_date)        AS month,
      ROUND(SUM(gross_revenue), 2)          AS gross_revenue,
      ROUND(SUM(returns_value), 2)          AS returns_value,
      ROUND(SUM(net_revenue), 2)            AS net_revenue,
      SUM(invoices)                         AS invoices
    FROM {domain_schema}.gold_daily_revenue
    GROUP BY 1
    ORDER BY 1
    """
)
display(monthly_revenue)

# COMMAND ----------

# MAGIC %md ### Top-10 products by net revenue

# COMMAND ----------

top_products = spark.sql(
    f"""
    SELECT
      f.stock_code,
      COALESCE(p.product_name, '(unknown)') AS product_name,
      ROUND(SUM(f.line_amount), 2)          AS net_revenue,
      SUM(f.quantity)                       AS units_sold,
      COUNT(DISTINCT f.invoice_id)          AS invoices
    FROM {domain_schema}.fact_sales f
    LEFT JOIN {domain_schema}.dim_products p ON f.stock_code = p.stock_code
    WHERE NOT f.is_return
    GROUP BY f.stock_code, p.product_name
    ORDER BY net_revenue DESC
    LIMIT 10
    """
)
display(top_products)

# COMMAND ----------

# MAGIC %md ### Return rate (line share and value share)

# COMMAND ----------

return_rate = spark.sql(
    f"""
    SELECT
      ROUND(AVG(CASE WHEN is_return THEN 1.0 ELSE 0.0 END), 4)                    AS line_return_rate,
      ROUND(SUM(CASE WHEN is_return THEN -line_amount ELSE 0 END)
            / NULLIF(SUM(CASE WHEN NOT is_return THEN line_amount ELSE 0 END), 0), 4)
                                                                                  AS value_return_rate
    FROM {domain_schema}.fact_sales
    """
)
display(return_rate)

# COMMAND ----------

# MAGIC %md ### RFM segment counts (quartile-scored)

# COMMAND ----------

rfm_segments = spark.sql(
    f"""
    WITH scored AS (
      SELECT
        customer_id,
        monetary,
        NTILE(4) OVER (ORDER BY recency_days DESC) AS r_score,  -- 4 = most recent
        NTILE(4) OVER (ORDER BY frequency ASC)     AS f_score,  -- 4 = most frequent
        NTILE(4) OVER (ORDER BY monetary ASC)      AS m_score   -- 4 = highest spend
      FROM {domain_schema}.gold_customer_rfm
    ),
    segmented AS (
      SELECT *,
        CASE
          WHEN r_score >= 3 AND f_score >= 3 AND m_score >= 3 THEN 'Champions'
          WHEN r_score >= 3 AND f_score >= 2                  THEN 'Loyal / Growing'
          WHEN r_score <= 2 AND f_score >= 3                  THEN 'At Risk (was loyal)'
          WHEN r_score <= 2 AND m_score >= 3                  THEN 'Big spender, lapsing'
          WHEN r_score = 1                                    THEN 'Hibernating'
          ELSE 'Casual'
        END AS segment
      FROM scored
    )
    SELECT segment, COUNT(*) AS customers, ROUND(AVG(monetary), 2) AS avg_monetary
    FROM segmented
    GROUP BY segment
    ORDER BY customers DESC
    """
)
display(rfm_segments)

# COMMAND ----------

# MAGIC %md ## 2. Demand forecast — `AI_FORECAST` (Public Preview, best-effort)

# COMMAND ----------

FORECAST_TABLE = f"{domain_schema}.gold_revenue_forecast"
try:
    max_date = spark.sql(
        f"SELECT MAX(sale_date) AS d FROM {domain_schema}.gold_daily_revenue"
    ).collect()[0]["d"]
    if max_date is None:
        raise ValueError("gold_daily_revenue is empty; nothing to forecast")
    horizon = (max_date + timedelta(days=28)).isoformat()
    spark.sql(
        f"""
        CREATE OR REPLACE TABLE {FORECAST_TABLE} AS
        SELECT * FROM AI_FORECAST(
          TABLE(
            SELECT sale_date AS ds, net_revenue AS revenue
            FROM {domain_schema}.gold_daily_revenue
          ),
          horizon => '{horizon}',
          time_col => 'ds',
          value_col => 'revenue'
        )
        """
    )
    forecast_df = spark.table(FORECAST_TABLE).orderBy("ds")
    print(f"Wrote {forecast_df.count()} forecast rows to {FORECAST_TABLE} (horizon {horizon})")
    display(forecast_df)
except Exception as exc:
    print("WARNING: AI_FORECAST unavailable (Public Preview; not enabled in every workspace).")
    print(f"  Reason: {exc}")
    print("  Skipping forecast; KPIs and the propensity model still run.")

# COMMAND ----------

# MAGIC %md ## 3. Purchase-propensity model (repeat-buyer probability)
# MAGIC Label: repeat buyer (`frequency > 1`). Because `frequency` *defines* the label, it is
# MAGIC excluded from the feature set (target leakage); the model learns from `recency_days`
# MAGIC and `monetary` only.

# COMMAND ----------

pdf = (
    spark.sql(
        f"""
        SELECT recency_days, frequency, monetary
        FROM {domain_schema}.gold_customer_rfm
        WHERE recency_days IS NOT NULL AND frequency IS NOT NULL AND monetary IS NOT NULL
        """
    )
    .toPandas()
    .astype({"recency_days": float, "frequency": float, "monetary": float})
)
pdf["repeat_buyer"] = (pdf["frequency"] > 1).astype(int)

FEATURES = ["recency_days", "monetary"]  # frequency excluded: it defines the label
model = None
auc = None

if len(pdf) < 20 or pdf["repeat_buyer"].nunique() < 2:
    print(
        f"WARNING: not enough data to train (rows={len(pdf)}, "
        f"classes={pdf['repeat_buyer'].nunique()}). Skipping model training."
    )
else:
    X_train, X_test, y_train, y_test = train_test_split(
        pdf[FEATURES],
        pdf["repeat_buyer"],
        test_size=0.25,
        random_state=42,
        stratify=pdf["repeat_buyer"],
    )
    model = Pipeline(
        [("scaler", StandardScaler()), ("clf", LogisticRegression(max_iter=1000))]
    )
    model.fit(X_train, y_train)
    auc = roc_auc_score(y_test, model.predict_proba(X_test)[:, 1])
    print("=== purchase_propensity training ===")
    print(f"rows:          {len(pdf)} (train {len(X_train)} / test {len(X_test)})")
    print(f"repeat buyers: {int(pdf['repeat_buyer'].sum())} ({pdf['repeat_buyer'].mean():.1%})")
    print(f"test AUC:      {auc:.4f}")

# COMMAND ----------

# MAGIC %md ### Log + register with MLflow (best-effort UC registration)

# COMMAND ----------

if model is None:
    print("No model trained; skipping MLflow logging.")
else:
    try:
        import mlflow  # noqa: E402

        mlflow.set_registry_uri("databricks-uc")
        model_name = f"{domain_schema}.purchase_propensity"
        with mlflow.start_run(run_name="purchase_propensity_logreg"):
            mlflow.log_param("features", ",".join(FEATURES))
            mlflow.log_param("label", "repeat_buyer(frequency>1)")
            mlflow.log_metric("test_auc", float(auc))
            mlflow.sklearn.log_model(
                model,
                "model",
                registered_model_name=model_name,
                input_example=pdf[FEATURES].head(5),
            )
        print(f"Logged and registered model as {model_name} (test AUC {auc:.4f})")
    except Exception as exc:
        print("WARNING: MLflow logging/registration failed (permissions or registry config).")
        print(f"  Reason: {exc}")
        print("  Model was still trained; AUC printed above.")
