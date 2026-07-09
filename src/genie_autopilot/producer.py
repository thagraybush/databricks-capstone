"""Product-Engineering persona: the data producer.

Simulates the product team's app emitting clickstream events against the REAL
UCI product catalog, with configurable, LABELED chaos — the ground-truth file
records exactly which events carry which defect, so downstream DQ rules and the
learning loops can be scored objectively (precision/recall, not vibes).

Chaos classes (all rates configurable):
  duplicate        — exact re-emission of a prior event
  late             — timestamp hours/days in the past, emitted in a later batch
  schema_drift     — "v2" payloads: camelCase keys, added currency, dropped user_agent
  bot_session      — high-volume view-only sessions with robotic cadence
  malformed        — truncated / non-JSON lines
  pii_leak         — customer email embedded in the referrer query string

Output: JSONL batches + ground_truth.jsonl (event_id → labels) under
data_gen/output/clickstream/, ready for volume upload and Auto Loader ingestion.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

EVENT_TYPES = ["view", "add_to_cart", "purchase"]
FUNNEL_P = {"add_to_cart": 0.12, "purchase": 0.35}  # P(cart|view session), P(purchase|cart)
REFERRERS = ["https://google.com/search?q=gifts", "https://shop.example.com/home", "direct", "https://newsletter.example.com/campaign42"]
AGENTS_V1 = ["Mozilla/5.0 (Macintosh)", "Mozilla/5.0 (iPhone)", "Mozilla/5.0 (Windows NT 10.0)"]


@dataclass
class ChaosConfig:
    duplicate_rate: float = 0.03
    late_rate: float = 0.02
    schema_drift_rate: float = 0.10
    bot_session_rate: float = 0.04
    malformed_rate: float = 0.005
    pii_leak_rate: float = 0.01


@dataclass
class ProducerRun:
    events: list[str] = field(default_factory=list)        # serialized lines
    ground_truth: list[dict] = field(default_factory=list)  # {"event_id", "labels": [...]}

    def label(self, event_id: str, *labels: str) -> None:
        self.ground_truth.append({"event_id": event_id, "labels": list(labels)})


def load_product_codes(csv_path: Path, limit: int = 2000) -> list[str]:
    """Sample real stock codes from the UCI CSV (falls back to synthetic codes)."""
    if not csv_path.exists():
        return [f"SKU{i:05d}" for i in range(500)]
    codes: set[str] = set()
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            code = (row.get("StockCode") or "").strip()
            if code and code[0].isdigit():
                codes.add(code)
            if len(codes) >= limit:
                break
    return sorted(codes)


def _base_event(rng: random.Random, session_id: str, visitor_id: str, etype: str, code: str, ts: datetime) -> dict:
    return {
        "event_id": uuid.UUID(int=rng.getrandbits(128)).hex,
        "session_id": session_id,
        "visitor_id": visitor_id,
        "event_type": etype,
        "stock_code": code,
        "event_ts": ts.isoformat(timespec="seconds"),
        "referrer": rng.choice(REFERRERS),
        "user_agent": rng.choice(AGENTS_V1),
        "schema_version": 1,
    }


def _to_v2(ev: dict, rng: random.Random) -> dict:
    """Schema drift: the product team shipped camelCase + currency and dropped user_agent."""
    return {
        "eventId": ev["event_id"],
        "sessionId": ev["session_id"],
        "visitorId": ev["visitor_id"],
        "eventType": ev["event_type"],
        "stockCode": ev["stock_code"],
        "eventTs": ev["event_ts"],
        "referrer": ev["referrer"],
        "currency": rng.choice(["GBP", "EUR"]),
        "schemaVersion": 2,
    }


def generate(
    products: list[str],
    sessions: int = 400,
    seed: int = 7,
    start: datetime | None = None,
    chaos: ChaosConfig | None = None,
) -> ProducerRun:
    rng = random.Random(seed)
    chaos = chaos or ChaosConfig()
    start = start or datetime(2026, 6, 1, 8, 0, 0)
    run = ProducerRun()
    # Zipf-ish popularity: earlier codes are hotter.
    weights = [1.0 / (i + 1) ** 0.7 for i in range(len(products))]

    for s in range(sessions):
        session_id = f"s{seed}-{s:06d}"
        visitor_id = f"v{rng.randrange(10_000):05d}"
        is_bot = rng.random() < chaos.bot_session_rate
        t = start + timedelta(minutes=rng.randrange(0, 60 * 24 * 20))
        n_views = rng.randrange(40, 220) if is_bot else rng.randrange(1, 9)
        carted: list[str] = []

        for _ in range(n_views):
            code = rng.choices(products, weights=weights)[0]
            t += timedelta(seconds=2 if is_bot else rng.randrange(5, 240))
            ev = _base_event(rng, session_id, visitor_id, "view", code, t)
            labels = ["bot_session"] if is_bot else []
            _emit(run, ev, rng, chaos, labels)
            if not is_bot and rng.random() < FUNNEL_P["add_to_cart"]:
                carted.append(code)
                t += timedelta(seconds=rng.randrange(5, 90))
                _emit(run, _base_event(rng, session_id, visitor_id, "add_to_cart", code, t), rng, chaos, [])

        if carted and not is_bot and rng.random() < FUNNEL_P["purchase"]:
            for code in carted:
                t += timedelta(seconds=rng.randrange(10, 120))
                _emit(run, _base_event(rng, session_id, visitor_id, "purchase", code, t), rng, chaos, [])
    return run


def _emit(run: ProducerRun, ev: dict, rng: random.Random, chaos: ChaosConfig, labels: list[str]) -> None:
    event_id = ev["event_id"]

    if rng.random() < chaos.pii_leak_rate:
        ev["referrer"] = f"https://shop.example.com/track?email=user{rng.randrange(999)}@example.com"
        labels = [*labels, "pii_leak"]

    if rng.random() < chaos.late_rate:
        late_ts = datetime.fromisoformat(ev["event_ts"]) - timedelta(hours=rng.randrange(6, 72))
        ev["event_ts"] = late_ts.isoformat(timespec="seconds")
        labels = [*labels, "late"]

    drifted = rng.random() < chaos.schema_drift_rate
    payload = _to_v2(ev, rng) if drifted else ev
    if drifted:
        labels = [*labels, "schema_drift"]

    line = json.dumps(payload)
    if rng.random() < chaos.malformed_rate:
        line = line[: max(10, len(line) // 2)]  # truncated mid-JSON
        labels = [*labels, "malformed"]

    run.events.append(line)
    if labels:
        run.label(event_id, *labels)

    if rng.random() < chaos.duplicate_rate:
        run.events.append(line)
        run.label(event_id, "duplicate")


def write_batches(run: ProducerRun, out_dir: Path, batch_size: int = 5000) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(0, len(run.events), batch_size):
        p = out_dir / f"events_batch_{i // batch_size:03d}.jsonl"
        p.write_text("\n".join(run.events[i : i + batch_size]) + "\n")
        paths.append(p)
    gt = out_dir / "ground_truth.jsonl"
    gt.write_text("\n".join(json.dumps(g) for g in run.ground_truth) + "\n")
    paths.append(gt)
    return paths


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--catalog-csv", default="data_gen/raw/online_retail_2010_2011.csv")
    ap.add_argument("--out", default="data_gen/output/clickstream")
    args = ap.parse_args()
    products = load_product_codes(Path(args.catalog_csv))
    run = generate(products, sessions=args.sessions, seed=args.seed)
    paths = write_batches(run, Path(args.out))
    n_labeled = len({g["event_id"] for g in run.ground_truth})
    print(f"{len(run.events):,} event lines ({n_labeled:,} with chaos labels) → {len(paths) - 1} batches + ground_truth.jsonl")


if __name__ == "__main__":
    main()
