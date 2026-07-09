import random

from genie_autopilot.session_engine import SCRIPTS, mutate, run_sessions


class FakeAnswer:
    def __init__(self, cid, mid, sql):
        self.conversation_id, self.message_id, self.sql = cid, mid, sql


class FakeAPI:
    """Answers every question with SQL containing the word 'net_revenue' only."""

    def __init__(self):
        self.asked, self.feedback = [], []
        self._n = 0

    def ask(self, question, conversation_id=None):
        self._n += 1
        self.asked.append((question, conversation_id))
        return FakeAnswer(conversation_id or f"c{self._n}", f"m{self._n}", "SELECT net_revenue FROM t")

    def send_feedback(self, cid, mid, rating, comment=None):
        self.feedback.append(rating)


def test_mutation_deterministic():
    a = mutate("What was our net revenue last month?", random.Random(5))
    b = mutate("What was our net revenue last month?", random.Random(5))
    assert a == b


def test_scripts_well_formed():
    assert len(SCRIPTS) >= 6
    for s in SCRIPTS:
        assert s.turns and s.persona and s.role
        for t in s.turns:
            if t.correction:
                from genie_autopilot.drift import parse_correction

                assert parse_correction(t.correction), t.correction


def test_multiturn_continuity_and_budget(tmp_path, monkeypatch):
    import genie_autopilot.session_engine as se

    monkeypatch.setattr(se, "MANIFEST_DIR", tmp_path)
    api = FakeAPI()
    records = run_sessions(api, n_sessions=2, seed=1, max_questions=5)
    assert len(records) <= 5
    # follow-up turns reuse the conversation id from turn 0 of their session
    first_session = [r for r in records if r["session_id"] == records[0]["session_id"]]
    assert len({r["conversation_id"] for r in first_session}) == 1
    assert (tmp_path / "session_manifest.jsonl").exists()
    # noise turn (expect None) must be rated NEGATIVE even though SQL came back
    noise = [r for r in records if r["expect"] is None]
    assert all(r["rated"] == "NEGATIVE" for r in noise)


def test_sql_splitter_handles_semicolons_in_strings(tmp_path):
    """cli._run_sql_file must not split inside quoted strings or $$ blocks."""
    from genie_autopilot import cli

    sql = tmp_path / "x.sql"
    sql.write_text(
        "-- a comment; with semicolon\n"
        "CREATE VIEW v AS SELECT 1;\n"
        "COMMENT ON TABLE v IS 'first; second; it''s fine';\n"
        "CREATE VIEW w WITH METRICS LANGUAGE YAML AS $$\ncomment: a; b\n$$;\n"
    )
    executed = []
    cli._run_sql = lambda w, wid, stmt: executed.append(stmt)  # monkeypatch module fn
    try:
        n = cli._run_sql_file(None, "wh", sql)
    finally:
        import importlib
        importlib.reload(cli)
    assert n == 3
    assert "first; second; it''s fine" in executed[1]
    assert "comment: a; b" in executed[2]


def test_rollback_drill_injection_shape():
    """Injection appends exactly one glossary line; restore returns the original."""
    import json

    from genie_autopilot import phase_g_rollback as gr

    class FakeSpaceAPI:
        def __init__(self):
            self.ser = json.dumps(
                {"instructions": {"text_instructions": [{"id": "a", "content": ["base"]}]}}
            )
            self.updates = []

        def get_space(self):
            return {"serialized_space": self.ser, "etag": "e1"}

        def update_space(self, serialized, etag=None):
            self.updates.append((serialized, etag))
            self.ser = serialized

    api = FakeSpaceAPI()
    snapshot, _ = gr.inject_bad_definition(api)
    injected = json.loads(api.ser)["instructions"]["text_instructions"][0]["content"]
    assert injected == ["base", gr.POISON_DEFINITION]
    assert json.loads(snapshot)["instructions"]["text_instructions"][0]["content"] == ["base"]
    gr.restore(api, snapshot)
    assert json.loads(api.ser)["instructions"]["text_instructions"][0]["content"] == ["base"]
