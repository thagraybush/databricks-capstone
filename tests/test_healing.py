import json

from genie_autopilot.drift import Proposal
from genie_autopilot.healing import (
    add_synonyms_to_yaml,
    alter_metric_view_sql,
    patch_space_column_synonyms,
    triage,
    uc_comment_sql,
)

SAMPLE_YAML = """\
version: 1.1
source: workspace.banking_gold.fact_wealth_portfolios
measures:
  - name: total_liquid_assets
    expr: SUM(liquid_cash_assets)
    display_name: Total Liquid Assets
fields:
  - name: segment
    expr: segment
"""


def test_add_synonyms_merges_and_caps():
    out = add_synonyms_to_yaml(SAMPLE_YAML, {"total_liquid_assets": ["liquid assets", "LIQUID ASSETS", "aum cash"]})
    assert "liquid assets" in out
    assert out.count("liquid assets") >= 1
    # case-insensitive dedupe: uppercase duplicate must not appear as its own entry
    assert "LIQUID ASSETS" not in out


def test_alter_view_sql_wraps_yaml():
    sql = alter_metric_view_sql("workspace.banking_gold.wealth_metrics", "version: 1.1\n")
    assert sql.startswith("ALTER VIEW workspace.banking_gold.wealth_metrics AS $$")
    assert sql.endswith("$$")


def test_uc_comment_sql_shape():
    sql = uc_comment_sql("workspace.banking_gold.fact_wealth_portfolios", "liquid_cash_assets", "liquid assets")
    assert sql.startswith("COMMENT ON COLUMN")
    assert "liquid assets" in sql


def test_patch_space_adds_column_config():
    space = json.dumps({"data_sources": {"tables": [{"identifier": "workspace.banking_gold.fact_transactions"}]}})
    out = patch_space_column_synonyms(space, "workspace.banking_gold.fact_transactions", "available_balance", ["available balance"])
    parsed = json.loads(out)
    cfgs = parsed["data_sources"]["tables"][0]["column_configs"]
    assert cfgs == [{"name": "available_balance", "synonyms": ["available balance"]}]


def test_triage_gate():
    strong = Proposal(term="a", entity="b", confidence=0.9, distinct_users=3)
    weak = Proposal(term="c", entity="d", confidence=0.9, distinct_users=1)
    low = Proposal(term="e", entity="f", confidence=0.3, distinct_users=4)
    auto, review = triage([strong, weak, low])
    assert auto == [strong]
    assert weak in review and low in review


YAML_WITH_JOIN = """\
version: 1.1
source: workspace.retail.fact_sales
joins:
  - name: product
    source: workspace.retail.dim_products
    on: source.stock_code = product.stock_code
measures:
  - name: net_revenue
    expr: SUM(source.line_amount)
"""


def test_join_on_key_survives_roundtrip():
    out = add_synonyms_to_yaml(YAML_WITH_JOIN, {"net_revenue": ["gmv"]})
    assert "true:" not in out
    assert "on: source.stock_code = product.stock_code" in out or "'on':" in out
    assert "gmv" in out


def test_audit_ledger_mirrors_to_delta(tmp_path):
    from genie_autopilot.healing import AuditLedger, HealingRecord

    executed = []
    ledger = AuditLedger(tmp_path / "ledger.jsonl", delta_table="ws.retail.ledger",
                         sql_runner=executed.append)
    rec = HealingRecord(ts=1.0, action="uc_comment", target="t.c", proposal_key="k",
                        payload="term with 'quotes'", status="applied", approver="auto")
    ledger.append(rec)
    assert (tmp_path / "ledger.jsonl").read_text().count("\n") == 1
    assert executed[0].startswith("CREATE TABLE IF NOT EXISTS ws.retail.ledger")
    assert "INSERT INTO ws.retail.ledger" in executed[1]
    assert "term with ''quotes''" in executed[1]
    ledger.append(rec)  # DDL runs once
    assert sum(1 for s in executed if s.startswith("CREATE TABLE")) == 1


def test_audit_ledger_mirror_failure_keeps_local(tmp_path):
    from genie_autopilot.healing import AuditLedger, HealingRecord

    def boom(_):
        raise RuntimeError("warehouse down")

    ledger = AuditLedger(tmp_path / "ledger.jsonl", delta_table="ws.retail.ledger", sql_runner=boom)
    ledger.append(HealingRecord(ts=1.0, action="a", target="t", proposal_key="k",
                                payload="p", status="s", approver="x"))
    assert (tmp_path / "ledger.jsonl").read_text().count("\n") == 1  # no exception, record kept
