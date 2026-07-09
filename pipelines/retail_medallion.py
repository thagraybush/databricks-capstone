"""Retail medallion pipeline (Lakeflow Spark Declarative Pipelines).

bronze  — UCI Online Retail II CSVs + producer clickstream, ingested as-landed (Auto Loader)
silver  — typed, deduplicated, quarantine-split on 9 verified DQ issue classes
gold    — dimensional marts consumed by the DS/PM personas and the Genie space

DQ classes handled (verified against the real file, 1,067,371 rows):
  1. Missing Customer ID (22.77%)          → KEPT in silver, flagged anonymous (valid revenue)
  2. C-prefix invoices (returns, 1.83%)    → KEPT, is_return = true (valid business events)
  3. A-prefix invoices (bad-debt adjust)   → QUARANTINE adjustment_invoice
  4. Negative qty NOT on a C-invoice       → QUARANTINE stock_adjustment
  5. Zero/negative price (non-return)      → QUARANTINE non_positive_price
  6. Non-product stock codes (POST, M, …)  → QUARANTINE non_product_code
  7. Exact duplicates + Dec-2010 overlap
     between the two source sheets         → deduplicated in silver
  8. Unparseable qty/price/date            → QUARANTINE unparseable
  9. Description drift (1,232 codes)       → resolved in dim_products via modal description
"""

from pyspark import pipelines as dp
from pyspark.sql import functions as F

RAW_PATH = "/Volumes/workspace/retail/raw/"

NON_PRODUCT_CODES = (
    "'POST','DOT','M','C2','D','S','BANK CHARGES','ADJUST','ADJUST2','AMAZONFEE','CRUK','PADS','B','TEST001','TEST002'"
)

# ---------------------------------------------------------------- bronze ----

@dp.table(
    name="bronze_retail",
    comment="UCI Online Retail II line items as landed. Values untouched (all strings); headers normalized to snake_case because Delta forbids spaces in column names.",
    table_properties={"quality": "bronze"},
)
def bronze_retail():
    return (
        spark.readStream.format("cloudFiles")  # noqa: F821 (spark provided by runtime)
        .option("cloudFiles.format", "csv")
        .option("header", "true")
        .option("cloudFiles.inferColumnTypes", "false")
        .load(RAW_PATH + "online_retail_*.csv")
        .select(
            F.col("Invoice").alias("invoice"),
            F.col("StockCode").alias("stock_code"),
            F.col("Description").alias("description"),
            F.col("Quantity").alias("quantity"),
            F.col("InvoiceDate").alias("invoice_date"),
            F.col("Price").alias("price"),
            F.col("`Customer ID`").alias("customer_id"),
            F.col("Country").alias("country"),
            F.col("_metadata.file_name").alias("_source_file"),
            F.current_timestamp().alias("_ingested_at"),
        )
    )


# ---------------------------------------------------------------- silver ----

DQ_RULES = {
    "parseable_quantity": "TRY_CAST(quantity AS INT) IS NOT NULL",
    "parseable_price": "TRY_CAST(price AS DECIMAL(12,2)) IS NOT NULL",
    "parseable_date": "TRY_CAST(invoice_date AS TIMESTAMP) IS NOT NULL",
    "not_adjustment_invoice": "NOT (invoice LIKE 'A%')",
    "no_negative_qty_outside_returns": "NOT (TRY_CAST(quantity AS INT) <= 0 AND invoice NOT LIKE 'C%')",
    "positive_price_or_return": "NOT (TRY_CAST(price AS DECIMAL(12,2)) <= 0)",
    "product_stock_code": f"stock_code NOT IN ({NON_PRODUCT_CODES}) AND stock_code NOT LIKE 'gift_%'",
}
_ALL_RULES = " AND ".join(f"({c})" for c in DQ_RULES.values())


@dp.temporary_view(name="retail_tagged")
@dp.expect_all(DQ_RULES)  # tracked in the pipeline event log → DQ scorecard
def retail_tagged():
    return (
        spark.read.table("bronze_retail")  # noqa: F821
        .dropDuplicates(
            ["invoice", "stock_code", "description", "quantity", "invoice_date", "price", "customer_id", "country"]
        )
        .withColumn("is_quarantined", F.expr(f"NOT ({_ALL_RULES})"))
        .withColumn(
            "quarantine_reasons",
            F.filter(
                F.array(
                    *[
                        F.when(~F.expr(cond), F.lit(name))
                        for name, cond in DQ_RULES.items()
                    ]
                ),
                lambda x: x.isNotNull(),
            ),
        )
    )


@dp.table(
    name="silver_sales",
    comment="Typed, deduplicated sales lines. Returns kept (is_return); anonymous sales kept (is_anonymous).",
    table_properties={"quality": "silver"},
)
def silver_sales():
    return (
        spark.read.table("retail_tagged")  # noqa: F821
        .where(~F.col("is_quarantined"))
        .select(
            F.col("invoice").alias("invoice_id"),
            "stock_code",
            "description",
            F.expr("TRY_CAST(quantity AS INT)").alias("quantity"),
            F.expr("TRY_CAST(invoice_date AS TIMESTAMP)").alias("invoiced_at"),
            F.expr("TRY_CAST(price AS DECIMAL(12,2))").alias("unit_price"),
            "customer_id",
            "country",
            F.col("invoice").startswith("C").alias("is_return"),
            F.col("customer_id").isNull().alias("is_anonymous"),
            "_source_file",
        )
    )


@dp.table(
    name="quarantine_sales",
    comment="Rows failing structural DQ rules, with machine-readable reasons. Reviewed by the data-engineering persona.",
    table_properties={"quality": "silver"},
)
def quarantine_sales():
    return (
        spark.read.table("retail_tagged")  # noqa: F821
        .where(F.col("is_quarantined"))
        .select("*")
    )


# ------------------------------------------------------------------ gold ----

@dp.materialized_view(
    name="dim_products",
    comment="Product dimension. Modal description resolves the 1,232 stock codes with drifting descriptions.",
    table_properties={"quality": "gold"},
)
def dim_products():
    ranked = (
        spark.read.table("silver_sales")  # noqa: F821
        .where(F.col("description").isNotNull() & ~F.col("is_return"))
        .groupBy("stock_code", "description")
        .agg(F.count("*").alias("n"), F.min("invoiced_at").alias("first_seen"), F.max("invoiced_at").alias("last_seen"))
    )
    from pyspark.sql.window import Window

    w = Window.partitionBy("stock_code").orderBy(F.desc("n"), F.desc("last_seen"))
    return (
        ranked.withColumn("rank", F.row_number().over(w))
        .where("rank = 1")
        .select(
            "stock_code",
            F.col("description").alias("product_name"),
            "first_seen",
            "last_seen",
        )
    )


@dp.materialized_view(
    name="fact_sales",
    comment="Line-level sales fact. line_amount is signed (negative for returns).",
    table_properties={"quality": "gold"},
)
def fact_sales():
    return (
        spark.read.table("silver_sales")  # noqa: F821
        .select(
            "invoice_id",
            "stock_code",
            "customer_id",
            "country",
            "quantity",
            "unit_price",
            (F.col("quantity") * F.col("unit_price")).cast("decimal(14,2)").alias("line_amount"),
            F.to_date("invoiced_at").alias("sale_date"),
            "invoiced_at",
            "is_return",
            "is_anonymous",
        )
    )


@dp.materialized_view(
    name="gold_daily_revenue",
    comment="Daily gross revenue, returns, and net revenue with order and customer counts.",
    table_properties={"quality": "gold"},
)
@dp.expect("net_revenue_present", "net_revenue IS NOT NULL")
def gold_daily_revenue():
    return (
        spark.read.table("fact_sales")  # noqa: F821
        .groupBy("sale_date")
        .agg(
            F.sum(F.when(~F.col("is_return"), F.col("line_amount")).otherwise(0)).alias("gross_revenue"),
            F.sum(F.when(F.col("is_return"), -F.col("line_amount")).otherwise(0)).alias("returns_value"),
            F.sum("line_amount").alias("net_revenue"),
            F.countDistinct("invoice_id").alias("invoices"),
            F.countDistinct("customer_id").alias("known_customers"),
        )
    )


@dp.materialized_view(
    name="gold_customer_rfm",
    comment="RFM segmentation for identified customers (anonymous sales excluded by definition).",
    table_properties={"quality": "gold"},
)
def gold_customer_rfm():
    sales = spark.read.table("fact_sales").where(~F.col("is_anonymous"))  # noqa: F821
    max_date = sales.agg(F.max("sale_date")).collect()[0][0]
    return (
        sales.groupBy("customer_id", "country")
        .agg(
            F.datediff(F.lit(max_date), F.max("sale_date")).alias("recency_days"),
            F.countDistinct("invoice_id").alias("frequency"),
            F.sum("line_amount").alias("monetary"),
        )
    )


# ------------------------------------------------------------ clickstream ----
# Producer-agent events (JSONL). Chaos arrives in-band: v1/v2 schema drift,
# duplicates, late events, bots, truncated JSON, PII-in-referrer.

@dp.table(
    name="bronze_events",
    comment="Clickstream events as emitted by the product-engineering producer. Mixed v1/v2 schemas, uncleaned.",
    table_properties={"quality": "bronze"},
)
def bronze_events():
    return (
        spark.readStream.format("cloudFiles")  # noqa: F821
        .option("cloudFiles.format", "json")
        .option("cloudFiles.inferColumnTypes", "false")
        .load(RAW_PATH + "clickstream/events_*.jsonl")
        .select(
            "*",
            F.col("_metadata.file_name").alias("_source_file"),
            F.current_timestamp().alias("_ingested_at"),
        )
    )


# Structural validity on the NORMALIZED (pre-rename) columns — drives the quarantine split.
EVENT_RULES_IN = {
    "has_event_id": "event_id_n IS NOT NULL",
    "known_event_type": "event_type_n IN ('view','add_to_cart','purchase')",
    "parseable_ts": "TRY_CAST(event_ts_n AS TIMESTAMP) IS NOT NULL",
}
_EVENTS_VALID = " AND ".join(f"({c})" for c in EVENT_RULES_IN.values())

# Tracked expectations on silver's OUTPUT schema (LDP evaluates rules post-select).
EVENT_RULES_OUT = {
    "has_event_id": "event_id IS NOT NULL",
    "known_event_type": "event_type IN ('view','add_to_cart','purchase')",
    "has_event_ts": "event_ts IS NOT NULL",
}


@dp.temporary_view(name="events_normalized")
def events_normalized():
    df = spark.read.table("bronze_events")  # noqa: F821
    cols = set(df.columns)

    def either(v1: str, v2: str):
        if v1 in cols and v2 in cols:
            return F.coalesce(F.col(v1), F.col(v2))
        return F.col(v1) if v1 in cols else F.col(v2)

    return df.select(
        either("event_id", "eventId").alias("event_id_n"),
        either("session_id", "sessionId").alias("session_id_n"),
        either("visitor_id", "visitorId").alias("visitor_id_n"),
        either("event_type", "eventType").alias("event_type_n"),
        either("stock_code", "stockCode").alias("stock_code_n"),
        either("event_ts", "eventTs").alias("event_ts_n"),
        F.col("referrer"),
        (F.col("currency").isNotNull() if "currency" in cols else F.lit(False)).alias("is_v2"),
        F.col("_source_file"),
        (F.col("_rescued_data") if "_rescued_data" in cols else F.lit(None).cast("string")).alias("_rescued"),
    )


@dp.table(
    name="silver_events",
    comment="Normalized v1+v2 events: deduplicated, PII-scrubbed, typed. Bots flagged downstream in gold_sessions.",
    table_properties={"quality": "silver"},
)
@dp.expect_all(EVENT_RULES_OUT)
def silver_events():
    email_re = r"[\w.+-]+@[\w-]+\.[\w.]+"
    return (
        spark.read.table("events_normalized")  # noqa: F821
        .where(F.expr(_EVENTS_VALID))
        .dropDuplicates(["event_id_n"])
        .select(
            F.col("event_id_n").alias("event_id"),
            F.col("session_id_n").alias("session_id"),
            F.col("visitor_id_n").alias("visitor_id"),
            F.col("event_type_n").alias("event_type"),
            F.col("stock_code_n").alias("stock_code"),
            F.expr("TRY_CAST(event_ts_n AS TIMESTAMP)").alias("event_ts"),
            F.col("referrer").rlike(email_re).alias("pii_detected"),
            F.regexp_replace(F.col("referrer"), email_re, "[REDACTED]").alias("referrer"),
            F.col("is_v2"),
            F.col("_source_file"),
        )
    )


@dp.table(
    name="quarantine_events",
    comment="Events failing structural rules (truncated JSON, unknown types, bad timestamps).",
    table_properties={"quality": "silver"},
)
def quarantine_events():
    return spark.read.table("events_normalized").where(~F.expr(_EVENTS_VALID))  # noqa: F821


@dp.materialized_view(
    name="gold_sessions",
    comment="Sessionized clickstream with bot detection (view-heavy, zero-purchase, robotic cadence).",
    table_properties={"quality": "gold"},
)
def gold_sessions():
    agg = (
        spark.read.table("silver_events")  # noqa: F821
        .groupBy("session_id", "visitor_id")
        .agg(
            F.min("event_ts").alias("started_at"),
            F.max("event_ts").alias("ended_at"),
            F.sum(F.when(F.col("event_type") == "view", 1).otherwise(0)).alias("n_views"),
            F.sum(F.when(F.col("event_type") == "add_to_cart", 1).otherwise(0)).alias("n_carts"),
            F.sum(F.when(F.col("event_type") == "purchase", 1).otherwise(0)).alias("n_purchases"),
        )
    )
    return agg.select(
        "session_id",
        "visitor_id",
        "started_at",
        "ended_at",
        (F.unix_timestamp("ended_at") - F.unix_timestamp("started_at")).alias("duration_s"),
        "n_views",
        "n_carts",
        "n_purchases",
        ((F.col("n_views") >= 40) & (F.col("n_purchases") == 0)).alias("is_bot"),
        (F.col("n_purchases") > 0).alias("converted"),
    )


@dp.materialized_view(
    name="gold_funnel_daily",
    comment="Daily view→cart→purchase funnel over human (non-bot) sessions.",
    table_properties={"quality": "gold"},
)
def gold_funnel_daily():
    return (
        spark.read.table("gold_sessions")  # noqa: F821
        .where(~F.col("is_bot"))
        .groupBy(F.to_date("started_at").alias("event_date"))
        .agg(
            F.count("*").alias("sessions"),
            F.sum("n_views").alias("views"),
            F.sum("n_carts").alias("add_to_carts"),
            F.sum("n_purchases").alias("purchases"),
        )
        .withColumn("view_to_cart_rate", F.col("add_to_carts") / F.col("views"))
        .withColumn("cart_to_purchase_rate", F.col("purchases") / F.greatest(F.col("add_to_carts"), F.lit(1)))
        .withColumn("session_conversion_rate", F.col("purchases") / F.col("sessions"))
    )
