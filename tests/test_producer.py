import json

from genie_autopilot.producer import ChaosConfig, generate


def _parse_ok(lines):
    ok, bad = [], []
    for ln in lines:
        try:
            ok.append(json.loads(ln))
        except json.JSONDecodeError:
            bad.append(ln)
    return ok, bad


def test_deterministic():
    a = generate(["10001", "10002"], sessions=20, seed=1)
    b = generate(["10001", "10002"], sessions=20, seed=1)
    assert a.events == b.events
    assert a.ground_truth == b.ground_truth


def test_chaos_labels_cover_emitted_defects():
    run = generate([f"1000{i}" for i in range(9)], sessions=120, seed=3)
    parsed, malformed = _parse_ok(run.events)
    labels = {label for g in run.ground_truth for label in g["labels"]}
    assert {"duplicate", "schema_drift"} <= labels
    # every malformed line must be labeled
    n_malformed_labels = sum(1 for g in run.ground_truth if "malformed" in g["labels"])
    assert n_malformed_labels == len(malformed)


def test_schema_drift_shape():
    run = generate(["10001"], sessions=150, seed=5)
    parsed, _ = _parse_ok(run.events)
    v2 = [e for e in parsed if e.get("schemaVersion") == 2]
    v1 = [e for e in parsed if e.get("schema_version") == 1]
    assert v2 and v1
    assert all("user_agent" not in e and "currency" in e for e in v2)


def test_zero_chaos_is_clean():
    cfg = ChaosConfig(0, 0, 0, 0, 0, 0)
    run = generate(["10001", "10002"], sessions=30, seed=2, chaos=cfg)
    parsed, malformed = _parse_ok(run.events)
    assert not malformed
    assert run.ground_truth == []
    assert len({e["event_id"] for e in parsed}) == len(parsed)  # no dupes


def test_funnel_ordering_within_session():
    cfg = ChaosConfig(0, 0, 0, 0, 0, 0)
    run = generate([f"2000{i}" for i in range(5)], sessions=80, seed=11, chaos=cfg)
    parsed, _ = _parse_ok(run.events)
    purchases = [e for e in parsed if e["event_type"] == "purchase"]
    carts = {(e["session_id"], e["stock_code"]) for e in parsed if e["event_type"] == "add_to_cart"}
    assert purchases, "expected some purchases at these rates"
    assert all((p["session_id"], p["stock_code"]) in carts for p in purchases)
