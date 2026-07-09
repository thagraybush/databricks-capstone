from collections import defaultdict

from genie_autopilot.drift import ROLE_AUTHORITY, parse_correction
from genie_autopilot.fleet_retail import (
    KINDS,
    QUESTION_LABELS,
    RETAIL_PERSONAS,
    RETAIL_ROLE_AUTHORITY,
    RETAIL_ROLES_BY_PERSONA,
    RetailPersona,
    run_retail_fleet,
)
from genie_autopilot.genie_api import GenieAnswer


# -- persona definitions -------------------------------------------------------

def test_personas_well_formed():
    assert RETAIL_PERSONAS
    for persona in RETAIL_PERSONAS:
        assert persona.name and persona.role
        assert persona.questions
        for question, expect, correction, kind in persona.questions:
            assert kind in KINDS, f"invalid kind {kind!r} for {question!r}"
            assert isinstance(question, str) and question
            if kind == "clean":
                assert expect, f"clean question needs an expected entity: {question!r}"
                assert correction is None
            elif kind == "jargon":
                assert expect, f"jargon question needs an expected entity: {question!r}"
                assert correction, f"jargon question needs a correction: {question!r}"
            else:  # vague / unanswerable noise
                assert expect is None and correction is None


def test_jargon_corrections_parseable_by_drift():
    for persona in RETAIL_PERSONAS:
        for _question, _expect, correction, kind in persona.questions:
            if kind != "jargon":
                continue
            parsed = parse_correction(correction)
            assert parsed is not None, f"unparseable correction: {correction!r}"
            term, entity = parsed
            assert term and entity and term != entity


def test_shared_dialects_can_clear_two_user_gate():
    """GMV / conversion / AOV must be reported by >=2 distinct personas (drift gate)."""
    reporters: dict[tuple[str, str], set[str]] = defaultdict(set)
    for persona in RETAIL_PERSONAS:
        for _question, _expect, correction, kind in persona.questions:
            if kind == "jargon":
                reporters[parse_correction(correction)].add(persona.name)
    multi = {pair for pair, names in reporters.items() if len(names) >= 2}
    assert ("gmv", "gross_revenue") in multi
    assert ("conversion", "session_conversion_rate") in multi
    assert ("aov", "revenue_metrics.aov") in multi


# -- role authority --------------------------------------------------------------

def test_role_authority_complete_for_all_persona_roles():
    for persona in RETAIL_PERSONAS:
        assert persona.role in RETAIL_ROLE_AUTHORITY, persona.role
    assert RETAIL_ROLES_BY_PERSONA.keys() == {p.name for p in RETAIL_PERSONAS}


def test_role_authority_merges_banking_roles_and_adds_retail_roles():
    for role, weight in ROLE_AUTHORITY.items():
        assert RETAIL_ROLE_AUTHORITY[role] == weight
    assert RETAIL_ROLE_AUTHORITY["pm"] == 0.9
    assert RETAIL_ROLE_AUTHORITY["marketing"] == 0.7
    assert RETAIL_ROLE_AUTHORITY["data_scientist"] == 1.1


# -- questions / labels -----------------------------------------------------------

def test_no_duplicate_questions_across_personas():
    all_questions = [q for p in RETAIL_PERSONAS for (q, _e, _c, _k) in p.questions]
    assert len(all_questions) == len(set(all_questions))


def test_question_labels_consistent_with_personas():
    expected: dict[str, str] = {}
    for persona in RETAIL_PERSONAS:
        for question, _expect, _correction, kind in persona.questions:
            expected[question] = kind
    assert QUESTION_LABELS == expected
    assert set(QUESTION_LABELS.values()) <= KINDS


# -- run_retail_fleet behavior (pure-python fake API) -----------------------------

class FakeAPI:
    """Minimal stand-in for GenieAPI: canned SQL per question, records all calls."""

    def __init__(self, sql_by_question: dict[str, str] | None = None):
        self.sql_by_question = sql_by_question or {}
        self.asked: list[tuple[str, str | None]] = []
        self.feedback: list[tuple[str, str, str]] = []
        self._n = 0

    def ask(self, question: str, conversation_id: str | None = None) -> GenieAnswer:
        self.asked.append((question, conversation_id))
        self._n += 1
        return GenieAnswer(
            conversation_id=conversation_id or f"conv-{self._n}",
            message_id=f"msg-{self._n}",
            status="COMPLETED",
            sql=self.sql_by_question.get(question, ""),
        )

    def send_feedback(self, conversation_id, message_id, rating, comment=None) -> dict:
        self.feedback.append((conversation_id, message_id, rating))
        return {}


def _mini_persona() -> RetailPersona:
    return RetailPersona(
        name="test_pm",
        role="pm",
        questions=[
            ("clean q", "net_revenue", None, "clean"),
            (
                "jargon q",
                "gross_revenue",
                "GMV means gross_revenue in gold_daily_revenue",
                "jargon",
            ),
            ("vague q", None, None, "vague"),
            ("unanswerable q", None, None, "unanswerable"),
        ],
    )


def test_run_fleet_rates_and_corrects():
    api = FakeAPI({"clean q": "SELECT SUM(net_revenue) FROM gold_daily_revenue"})
    results = run_retail_fleet(api, personas=[_mini_persona()])

    assert [r.rated for r in results] == ["POSITIVE", "NEGATIVE", "NEGATIVE", "NEGATIVE"]
    assert [r.kind for r in results] == ["clean", "jargon", "vague", "unanswerable"]

    # Exactly one follow-up was filed: the jargon correction, in its own conversation.
    follow_ups = [(q, conv) for q, conv in api.asked if conv is not None]
    jargon = next(r for r in results if r.kind == "jargon")
    assert follow_ups == [(jargon.correction, jargon.conversation_id)]
    assert jargon.correction == "GMV means gross_revenue in gold_daily_revenue"

    # Noise interactions never carry a correction.
    assert all(r.correction == "" for r in results if r.kind in {"vague", "unanswerable"})
    assert len(api.feedback) == 4


def test_run_fleet_include_noise_false_skips_noise():
    api = FakeAPI()
    results = run_retail_fleet(api, personas=[_mini_persona()], include_noise=False)
    assert {r.kind for r in results} == {"clean", "jargon"}
    assert all(q not in ("vague q", "unanswerable q") for q, _conv in api.asked)


def test_run_fleet_full_personas_file_all_jargon_corrections_on_miss():
    api = FakeAPI()  # no SQL for anything → every judged question misses
    results = run_retail_fleet(api, personas=RETAIL_PERSONAS)

    assert all(r.rated == "NEGATIVE" for r in results)
    filed = [q for q, conv in api.asked if conv is not None]
    expected = [
        correction
        for p in RETAIL_PERSONAS
        for (_q, _e, correction, kind) in p.questions
        if kind == "jargon"
    ]
    assert filed == expected
