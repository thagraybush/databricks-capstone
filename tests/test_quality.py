"""Tests for the query-quality gate (quality.py) and the pure parts of lakebase.py."""

import json

import pytest

from genie_autopilot import lakebase
from genie_autopilot.quality import (
    QueryQualityModel,
    Route,
    evaluate,
    featurize,
    heuristic_route,
)

SCHEMA_TERMS = {
    "net_revenue",
    "country",
    "segment",
    "accounts",
    "branch",
    "balance",
    "transactions",
    "region",
    "channel",
    "churn_rate",
}

# 14 labeled examples covering both classes, for model round-trip tests.
TRAINING_SET = [
    ("total net revenue by country last month", "answerable"),
    ("average order value for the premium segment in Q2", "answerable"),
    ("count of new accounts opened last week", "answerable"),
    ("what was the churn rate in March?", "answerable"),
    ("total balance by branch for 2025", "answerable"),
    ("conversion rate by channel yesterday", "answerable"),
    ("sum of transaction volume by region this quarter", "answerable"),
    ("pull the Salesforce pipeline", "noise"),
    ("how are we doing?", "noise"),
    ("should we hire more analysts?", "noise"),
    ("sync the Jira board with our OKRs", "noise"),
    ("why is everyone unhappy?", "noise"),
    ("will things improve?", "noise"),
    ("thoughts?", "noise"),
]


# -- featurize -------------------------------------------------------------------


def test_featurize_time_references():
    assert featurize("total revenue last month", set())["has_time_reference"] == 1.0
    assert featurize("net revenue for Q3", set())["has_time_reference"] == 1.0
    assert featurize("balances as of 2025-06-30", set())["has_time_reference"] == 1.0
    assert featurize("list customers by segment", set())["has_time_reference"] == 0.0


def test_featurize_external_system_detection():
    assert featurize("pull the Salesforce pipeline", set())["references_external_system"] == 1.0
    assert featurize("sync it with HubSpot and Jira", set())["references_external_system"] == 1.0
    assert featurize("total revenue by region", set())["references_external_system"] == 0.0


def test_featurize_schema_overlap_math():
    # tokens: total, net, revenue, by, country (5); vocab expands net_revenue -> net, revenue
    # matches: net, revenue, country -> 3/5
    feats = featurize("total net revenue by country", {"net_revenue", "country"})
    assert feats["schema_term_overlap"] == pytest.approx(3 / 5)
    assert featurize("hello world", {"net_revenue"})["schema_term_overlap"] == 0.0
    assert featurize("", {"net_revenue"})["schema_term_overlap"] == 0.0


def test_featurize_misc_signals():
    feats = featurize("what is the total revenue?", set())
    assert feats["has_wh_word"] == 1.0
    assert feats["has_question_mark"] == 1.0
    assert feats["has_metric_term"] == 1.0
    assert feats["token_count"] == 5.0
    vague = featurize("how are we doing?", set())
    assert vague["vagueness"] >= 1.0


# -- heuristic_route ---------------------------------------------------------------


def test_heuristic_rejects_external_system():
    route = heuristic_route("pull the Salesforce pipeline", SCHEMA_TERMS)
    assert route.decision == "reject"


def test_heuristic_runs_grounded_question():
    route = heuristic_route("total net revenue by country last month", SCHEMA_TERMS)
    assert route.decision == "run"
    assert route.p_answerable >= 0.65


def test_heuristic_filters_vague_question():
    route = heuristic_route("how are we doing?", SCHEMA_TERMS)
    assert route.decision in ("reject", "human_review")


def test_heuristic_borderline_goes_to_human_review():
    # No metric term, no time reference, only schema overlap -> single weak signal.
    route = heuristic_route("list every branch", SCHEMA_TERMS)
    assert route.decision == "human_review"


def test_heuristic_rejects_empty_question():
    assert heuristic_route("", SCHEMA_TERMS).decision == "reject"


# -- model round-trip ----------------------------------------------------------------


@pytest.fixture(scope="module")
def trained_model():
    questions = [q for q, _ in TRAINING_SET]
    labels = [label for _, label in TRAINING_SET]
    return QueryQualityModel().fit(questions, labels, SCHEMA_TERMS)


def test_predict_route_returns_valid_route(trained_model):
    for question, _ in TRAINING_SET:
        route = trained_model.predict_route(question, SCHEMA_TERMS)
        assert isinstance(route, Route)
        assert route.decision in ("run", "reject", "human_review")
        assert 0.0 <= route.p_answerable <= 1.0


def test_evaluate_on_training_set(trained_model):
    questions = [q for q, _ in TRAINING_SET]
    labels = [label for _, label in TRAINING_SET]
    metrics = evaluate(trained_model, questions, labels, SCHEMA_TERMS)
    assert set(metrics) == {"accuracy", "precision", "recall"}
    assert metrics["accuracy"] >= 0.7
    assert 0.0 <= metrics["precision"] <= 1.0
    assert 0.0 <= metrics["recall"] <= 1.0


def test_save_load_round_trip(trained_model, tmp_path):
    path = tmp_path / "quality_model.joblib"
    trained_model.save(path)
    loaded = QueryQualityModel.load(path)
    probe = "total net revenue by country last month"
    assert loaded.p_answerable(probe, SCHEMA_TERMS) == pytest.approx(
        trained_model.p_answerable(probe, SCHEMA_TERMS)
    )
    assert loaded.predict_route(probe, SCHEMA_TERMS) == trained_model.predict_route(
        probe, SCHEMA_TERMS
    )


# -- lakebase pure parts --------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows=None):
        self.cur = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def test_hitl_ddl_contains_both_tables():
    assert "hitl_queue" in lakebase.HITL_DDL
    assert "healing_history" in lakebase.HITL_DDL
    assert "evidence jsonb" in lakebase.HITL_DDL
    assert "CHECK (status IN ('pending', 'approved', 'rejected'))" in lakebase.HITL_DDL
    assert "approver text" in lakebase.HITL_DDL


def test_ensure_schema_executes_each_statement():
    conn = FakeConn()
    lakebase.ensure_schema(conn)
    assert len(conn.cur.executed) == 2
    assert conn.commits == 1


def test_enqueue_parameterizes_and_returns_id():
    conn = FakeConn(rows=[(42,)])
    proposal = {
        "proposal_key": "net revenue→fact.net_revenue",
        "term": "net revenue",
        "entity": "fact.net_revenue",
        "confidence": 0.81,
        "distinct_users": 3,
        "kind": "synonym",
        "evidence": [{"user": "pm1"}],
    }
    assert lakebase.enqueue(conn, proposal) == 42
    sql, params = conn.cur.executed[0]
    assert "INSERT INTO hitl_queue" in sql
    assert "%s" in sql and "'net revenue" not in sql  # values bound, never inlined
    assert params[0] == "net revenue→fact.net_revenue"
    assert params[1] == "net revenue"
    assert params[3] == 0.81
    assert json.loads(params[6]) == [{"user": "pm1"}]
    assert conn.commits == 1


def test_pending_returns_dicts():
    row = (
        7, "aov→fact.avg_order_value", "aov", "fact.avg_order_value",
        0.9, 2, "synonym", "pending", "2026-07-08", [],
    )  # fmt: skip
    conn = FakeConn(rows=[row])
    result = lakebase.pending(conn)
    sql, params = conn.cur.executed[0]
    assert "WHERE status = %s" in sql
    assert params == ("pending",)
    assert result == [
        {
            "id": 7,
            "proposal_key": "aov→fact.avg_order_value",
            "term": "aov",
            "entity": "fact.avg_order_value",
            "confidence": 0.9,
            "distinct_users": 2,
            "kind": "synonym",
            "status": "pending",
            "created_at": "2026-07-08",
            "evidence": [],
        }
    ]


def test_decide_approved_and_rejected():
    conn = FakeConn()
    assert lakebase.decide(conn, 7, approved=True, decided_by="cfollmer@strataintel.ai") == (
        "approved"
    )
    sql, params = conn.cur.executed[0]
    assert "UPDATE hitl_queue" in sql
    assert params == ("approved", "cfollmer@strataintel.ai", 7)

    conn2 = FakeConn()
    assert lakebase.decide(conn2, 9, approved=False, decided_by="reviewer") == "rejected"
    assert conn2.cur.executed[0][1] == ("rejected", "reviewer", 9)
    assert conn2.commits == 1


def test_record_healing_parameterizes():
    conn = FakeConn(rows=[(3,)])
    record = {
        "ts": 1751932800.0,
        "action": "uc_comment",
        "target": "workspace.banking_gold.fact_transactions.net_revenue",
        "proposal_key": "net revenue→fact.net_revenue",
        "payload": "COMMENT ON COLUMN ...",
        "status": "applied",
        "approver": "auto",
    }
    assert lakebase.record_healing(conn, record) == 3
    sql, params = conn.cur.executed[0]
    assert "INSERT INTO healing_history" in sql
    assert params == (
        1751932800.0,
        "uc_comment",
        "workspace.banking_gold.fact_transactions.net_revenue",
        "net revenue→fact.net_revenue",
        "COMMENT ON COLUMN ...",
        "applied",
        "auto",
    )
    assert conn.commits == 1
