import json
from datetime import date, timedelta
from pathlib import Path

from genie_autopilot import fleet_retail
from genie_autopilot.drift import Correction, detect_conflicts, parse_correction
from genie_autopilot.drift_pack import (
    FRESH_JARGON_SCRIPTS,
    V3_START,
    active_drift,
    drift_labels,
    v3_chaos_batch,
)
from genie_autopilot.session_engine import SCRIPTS

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = [f"1000{i}" for i in range(9)]


def _parsed_v3(lines):
    out = []
    for ln in lines:
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev.get("meta"), dict) and ev["meta"].get("sv") == 3:
            out.append((ln, ev))
    return out


def test_v3_events_nest_and_break_flat_normalization():
    lines, _ = v3_chaos_batch(PRODUCTS, sessions=60, seed=9)
    v3 = _parsed_v3(lines)
    assert v3, "expected v3 conversions at the default 0.3 fraction"
    for ln, ev in v3:
        # No flat snake_case event_id anywhere in the line: the pipeline's either()
        # coalescing cannot normalize this — it must land in quarantine_events.
        assert "event_id" not in ln
        assert set(ev) == {"meta", "actor", "action", "context"}
        assert ev["meta"]["sv"] == 3
        assert ev["meta"]["eventId"] and ev["meta"]["ts"]
        assert ev["actor"]["visitorId"] and ev["actor"]["sessionId"]
        assert ev["action"]["type"] in {"view", "add_to_cart", "purchase"}
        assert ev["context"]["channel"] in {"web", "ios", "android"}


def test_labels_cover_converted_events():
    lines, labels = v3_chaos_batch(PRODUCTS, sessions=60, seed=9)
    v3_ids = {ev["meta"]["eventId"] for _, ev in _parsed_v3(lines)}
    labeled = {g["event_id"] for g in labels if "schema_v3" in g["labels"]}
    assert v3_ids and v3_ids == labeled
    # producer chaos labels are merged in alongside the drift labels
    other = {label for g in labels for label in g["labels"] if label != "schema_v3"}
    assert other, "expected base producer chaos labels to be preserved"
    assert drift_labels("any-id") == ["schema_v3"]


def test_fresh_jargon_scripts_well_formed_and_parseable():
    assert len(FRESH_JARGON_SCRIPTS) == 3
    for s in FRESH_JARGON_SCRIPTS:
        assert s.turns and s.persona and s.role
        for t in s.turns:
            if t.correction:
                assert parse_correction(t.correction), t.correction
    # the CAC payback probe is unanswerable: judged None, files no correction
    cac = [t for s in FRESH_JARGON_SCRIPTS for t in s.turns if "CAC" in t.utterance]
    assert len(cac) == 1 and cac[0].expect is None and cac[0].correction is None


def test_jargon_terms_genuinely_novel():
    corpus = []
    for p in fleet_retail.RETAIL_PERSONAS + fleet_retail.COLLISION_PERSONAS:
        for question, _expect, correction, _kind in p.questions:
            corpus.append(question)
            corpus.append(correction or "")
    for s in SCRIPTS:
        for t in s.turns:
            corpus.append(t.utterance)
            corpus.append(t.correction or "")
    corpus.append((REPO_ROOT / "docs" / "eval-evidence.md").read_text())
    haystack = "\n".join(corpus).lower()
    for term in ("nrr", "perfect order", "perfect-order", "cac payback", "net_revenue retention"):
        assert term not in haystack, f"'{term}' is not novel — already seen by the system"


def test_volume_is_a_detectable_poison_term():
    mined = []
    for s in FRESH_JARGON_SCRIPTS:
        for t in s.turns:
            if t.correction:
                parsed = parse_correction(t.correction)
                assert parsed
                mined.append(Correction(term=parsed[0], entity=parsed[1],
                                        user=s.persona, role=s.role))
    conflicts = detect_conflicts(mined)
    assert "volume" in conflicts
    assert conflicts["volume"] == {"quantity", "invoices"}


def test_active_drift_staggering():
    assert active_drift(V3_START - timedelta(days=1)) == {
        "v3_schema": False, "fresh_jargon": False}
    assert active_drift(V3_START) == {"v3_schema": True, "fresh_jargon": False}
    assert active_drift(V3_START + timedelta(days=2)) == {
        "v3_schema": True, "fresh_jargon": False}
    assert active_drift(V3_START + timedelta(days=3)) == {
        "v3_schema": True, "fresh_jargon": True}
    assert V3_START == date(2026, 7, 14)


def test_deterministic_seeding():
    a = v3_chaos_batch(PRODUCTS, sessions=30, seed=4)
    b = v3_chaos_batch(PRODUCTS, sessions=30, seed=4)
    assert a == b
    c = v3_chaos_batch(PRODUCTS, sessions=30, seed=5)
    assert a != c


def test_v3_fraction_bounds():
    lines, labels = v3_chaos_batch(PRODUCTS, sessions=30, seed=4, v3_fraction=0.0)
    assert not _parsed_v3(lines)
    assert all("schema_v3" not in g["labels"] for g in labels)
    lines, _ = v3_chaos_batch(PRODUCTS, sessions=30, seed=4, v3_fraction=1.0)
    parseable = sum(1 for ln in lines if _loads_ok(ln))
    assert len(_parsed_v3(lines)) == parseable  # every parseable line converted


def _loads_ok(line):
    try:
        json.loads(line)
        return True
    except json.JSONDecodeError:
        return False
