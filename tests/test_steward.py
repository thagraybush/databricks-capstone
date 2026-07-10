"""Tests for the steward escalation engine (steward.py) — pure python, no network."""

import json

from genie_autopilot.drift import Correction, Proposal
from genie_autopilot.steward import (
    KNOWN_VOCABULARY,
    STOPWORDS,
    Escalation,
    apply_approved,
    build_escalations,
    detect_novel_terms,
    escalate,
    extract_candidate_terms,
)

# -- FakeConn/FakeCursor idiom (see test_quality.py): one cursor serving canned rows ---


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


# -- vocabulary baseline ---------------------------------------------------------


def test_vocabulary_and_stopwords_are_disjoint():
    assert "sales" in KNOWN_VOCABULARY  # poison term lives in the vocabulary
    assert "what" in STOPWORDS and "month" in STOPWORDS and "total" in STOPWORDS
    assert not (KNOWN_VOCABULARY & STOPWORDS)


# -- candidate extraction ----------------------------------------------------------


def test_extract_finds_novel_bigram():
    terms = extract_candidate_terms("What is our gross margin this month?")
    assert "gross margin" in terms


def test_extract_excludes_known_vocabulary_underscore_normalized():
    terms = extract_candidate_terms("show total net revenue and gmv by country")
    assert "net revenue" not in terms  # matches known net_revenue after normalization
    assert "gmv" not in terms
    assert "country" not in terms
    assert "sales" not in extract_candidate_terms("sales by region")  # poison term is known


def test_extract_token_overlap_with_vocab_does_not_suppress_novel_bigram():
    # 'gross' appears inside known 'gross_revenue', but matching is on the FULL n-gram.
    terms = extract_candidate_terms("gross margin by country")
    assert "gross margin" in terms


def test_extract_drops_stopword_only_ngrams():
    assert extract_candidate_terms("how many did we show last month?") == set()
    terms = extract_candidate_terms("what is our gross margin")
    assert "what" not in terms
    assert "what is" not in terms
    assert "our" not in terms


def test_extract_handles_empty_question():
    assert extract_candidate_terms("") == set()
    assert extract_candidate_terms(None) == set()


# -- novelty detection ----------------------------------------------------------------

TELEMETRY_ROWS = [
    {"question": "why is perfect order down", "user": "amy", "rated": "negative"},
    {"question": "show perfect order by region", "user": "bob", "feedback_rating": -1},
    {"question": "perfect order for march", "user": "amy", "rated": "negative"},
    {"question": "gross margin by country", "user": "cara", "rated": "positive"},
    {"question": "gross margin trend", "user": "eve", "rated": "positive"},
    {"question": "fill rate last month", "user": "dan", "rated": "negative"},
]


def test_detect_novel_terms_counts_and_shadows():
    found = detect_novel_terms(TELEMETRY_ROWS)
    assert found == [
        {
            "term": "perfect order",
            "occurrences": 3,
            "distinct_users": 2,
            "example_questions": [
                "why is perfect order down",
                "show perfect order by region",
                "perfect order for march",
            ],
        }
    ]
    terms = {r["term"] for r in found}
    assert "perfect" not in terms and "order" not in terms  # shadowed by the bigram
    assert "fill rate" not in terms  # only one occurrence, below min_occurrences
    assert "gross margin" not in terms  # positive-feedback rows are excluded by default


def test_detect_novel_terms_min_occurrences_gate():
    assert detect_novel_terms(TELEMETRY_ROWS, min_occurrences=4) == []


def test_detect_novel_terms_only_negative_false_counts_all_rows():
    found = detect_novel_terms(TELEMETRY_ROWS, only_negative=False)
    by_term = {r["term"]: r for r in found}
    assert "gross margin" in by_term
    assert by_term["gross margin"]["occurrences"] == 2
    assert by_term["gross margin"]["distinct_users"] == 2
    # sorted by (distinct_users, occurrences) desc: perfect order (2, 3) leads (2, 2)
    assert found[0]["term"] == "perfect order"


def test_detect_novel_terms_counts_corrected_rows_without_negative_rating():
    rows = [
        {"question": "show basket velocity by hour", "user": "amy",
         "correction": "basket velocity means gold.fact_orders.baskets_per_hour"},
        {"question": "basket velocity for last week", "user": "bob", "rated": "negative"},
    ]
    terms = {r["term"] for r in detect_novel_terms(rows)}
    assert "basket velocity" in terms


# -- escalation building -----------------------------------------------------------------


def test_build_escalations_kinds_and_actions():
    p_auto = Proposal(term="net rev", entity="fact.net_revenue", confidence=0.9, distinct_users=3)
    p_low = Proposal(
        term="wallet share",
        entity="fact.share_of_wallet",
        confidence=0.4,
        distinct_users=1,
        evidence=[Correction(term="wallet share", entity="fact.share_of_wallet", user="amy")],
    )
    conflicts = {"sales": {"fact.net_revenue", "fact.gross_revenue"}}
    novel = [
        {
            "term": "perfect order",
            "occurrences": 3,
            "distinct_users": 2,
            "example_questions": ["perfect order for march"],
        }
    ]
    escalations = build_escalations([p_auto, p_low], conflicts, novel, {p_auto.key})
    assert [e.kind for e in escalations].count("below_gate_proposal") == 1  # p_auto excluded

    below = next(e for e in escalations if e.kind == "below_gate_proposal")
    assert below.term == "wallet share"
    assert below.entity == "fact.share_of_wallet"
    assert below.confidence == 0.4
    assert below.evidence["corrections"][0]["user"] == "amy"

    conflict = next(e for e in escalations if e.kind == "poison_conflict")
    assert conflict.term == "sales"
    assert conflict.entity is None
    assert conflict.evidence["entities"] == ["fact.gross_revenue", "fact.net_revenue"]
    assert "author disambiguation instruction" in conflict.suggested_action

    novel_esc = next(e for e in escalations if e.kind == "novel_term")
    assert novel_esc.term == "perfect order"
    assert novel_esc.entity is None
    assert novel_esc.distinct_users == 2
    assert novel_esc.evidence["occurrences"] == 3
    assert "define, map, or dismiss" in novel_esc.suggested_action


# -- escalate (queue writes, idempotent) ----------------------------------------------------


def _pending_row(queue_id, key, term, kind):
    # matches lakebase._PENDING_COLUMNS order
    return (queue_id, key, term, None, 0.0, 2, kind, "pending", "2026-07-08", {})


def test_escalate_skips_already_pending_keys():
    conn = FakeConn(rows=[_pending_row(1, "novel_term:perfect order", "perfect order", "novel_term")])
    escalations = [
        Escalation(kind="novel_term", term="perfect order", distinct_users=2),
        Escalation(
            kind="poison_conflict",
            term="sales",
            distinct_users=3,
            evidence={"entities": ["a", "b"]},
            suggested_action="author disambiguation instruction",
        ),
    ]
    assert escalate(conn, escalations) == 1

    assert "WHERE status = %s" in conn.cur.executed[0][0]  # single pending() read up front
    inserts = [(sql, params) for sql, params in conn.cur.executed if "INSERT INTO hitl_queue" in sql]
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[0] == "poison_conflict:sales"
    assert params[1] == "sales"
    assert params[5] == "poison_conflict"
    assert json.loads(params[6]) == {"entities": ["a", "b"]}


def test_escalate_dedupes_within_one_batch():
    conn = FakeConn(rows=[_pending_row(9, "unrelated:key", "t", "novel_term")])
    duplicate = Escalation(kind="novel_term", term="perfect order", distinct_users=2)
    assert escalate(conn, [duplicate, duplicate]) == 1
    inserts = [sql for sql, _ in conn.cur.executed if "INSERT INTO hitl_queue" in sql]
    assert len(inserts) == 1


# -- apply_approved ---------------------------------------------------------------------


def _approved_row(queue_id, key, term, kind, decided_by):
    # matches steward._APPROVED_COLUMNS order
    return (queue_id, key, term, None, 0.0, 2, kind, "approved", "2026-07-08", decided_by, {})


def test_apply_approved_applies_records_and_marks():
    row = _approved_row(
        5, "novel_term:perfect order", "perfect order", "novel_term", "cfollmer@strataintel.ai"
    )
    conn = FakeConn(rows=[row])
    seen = []

    def applier(queue_row):
        seen.append(queue_row)
        return "instruction: define perfect order"

    applied = apply_approved(conn, {"novel_term": applier})
    assert len(applied) == 1
    assert applied[0]["id"] == 5
    assert applied[0]["payload"] == "instruction: define perfect order"
    assert seen[0]["term"] == "perfect order"

    select_sql, select_params = conn.cur.executed[0]
    assert "WHERE status = %s" in select_sql
    assert select_params == ("approved",)

    healing = next((s, p) for s, p in conn.cur.executed if "healing_history" in s)
    assert healing[1][1] == "novel_term"  # action
    assert healing[1][3] == "novel_term:perfect order"  # proposal_key
    assert healing[1][4] == "instruction: define perfect order"  # payload
    assert healing[1][5] == "applied"  # status
    assert healing[1][6] == "cfollmer@strataintel.ai"  # approver

    update = next((s, p) for s, p in conn.cur.executed if "UPDATE hitl_queue" in s)
    assert "status = 'applied'" in update[0]
    assert "%s" in update[0]  # parameterized, id never inlined
    assert update[1] == (5,)


def test_apply_approved_skips_kinds_without_applier():
    rows = [
        _approved_row(5, "novel_term:x", "x", "novel_term", "amy"),
        _approved_row(6, "poison_conflict:sales", "sales", "poison_conflict", "bob"),
    ]
    conn = FakeConn(rows=rows)
    applied = apply_approved(conn, {"poison_conflict": lambda row: "disambiguation instruction"})
    assert [r["id"] for r in applied] == [6]
    updates = [params for sql, params in conn.cur.executed if "UPDATE hitl_queue" in sql]
    assert updates == [(6,)]  # the unhandled kind stays 'approved' for a later run
