"""Scheduled drift injection: chaos the system has NEVER seen, keyed to the calendar.

Everything the platform has healed so far (v2 camelCase drift, GMV/AOV/whale dialects)
was authored before the system was built — it has, in a sense, already "seen" it. This
module holds back two waves of genuinely novel drift and releases them by DATE, not by
human intent: drift activates when the calendar crosses ``V3_START`` so the system
encounters it "in the wild" on schedule, not when a human decides to run a script.

Wave 1 — producer schema v3 (active from V3_START):
    The product team "ships" a nested event envelope. The pipeline's flat-column
    ``either()`` coalescing finds no ``event_id``/``eventId`` top-level column, so v3
    events land in ``quarantine_events``. The operational drill: watch the quarantine
    trend spike, let the DE persona diagnose it from quarantine rows alone, and ship a
    v3 normalizer. Ground truth (``schema_v3`` labels) scores their fix objectively.

Wave 2 — fresh business jargon (active from V3_START + 3 days):
    Three personas arrive speaking dialect the glossary has never healed (NRR,
    perfect-order rate, CAC payback) plus one new poison term ('volume': quantity to
    merchandising, invoices to the PM org). The semantic drill: corrections flow into
    telemetry, the flywheel mines/heals them, and CAC payback stays honest — it is
    unanswerable (no spend data) and must NOT be healed into a wrong answer.

Upload path for the v3 wave (local files → Auto Loader):
    databricks fs cp data_gen/output/clickstream_v3/events_v3_batch_000.jsonl \\
        dbfs:/Volumes/workspace/retail/raw/clickstream/
    (repeat per batch file; keep ground_truth.jsonl local — it is the answer key).
    Batches are named ``events_v3_batch_*`` so they match the pipeline's
    ``events_*.jsonl`` glob without clobbering the base producer's ``events_batch_*``
    files already in the volume. The next pipeline update ingests them into
    bronze_events; the structural rules then route them to quarantine_events.

Usage:
    python -m genie_autopilot.drift_pack --wave status
    python -m genie_autopilot.drift_pack --wave v3 [--out data_gen/output/clickstream_v3]
    GA_GENIE_SPACE_ID=<space> python -m genie_autopilot.drift_pack --wave jargon
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .producer import ChaosConfig, generate, load_product_codes
from .session_engine import SessionScript, Turn, run_sessions

# The cadence keys off this date: wave 1 (schema v3) goes live on V3_START, wave 2
# (fresh jargon) three days later. Nothing before these dates emits novel drift.
V3_START = date(2026, 7, 14)

JARGON_LAG_DAYS = 3

V3_CHANNELS = ["web", "ios", "android"]

DEFAULT_V3_FRACTION = 0.3


def _field(ev: dict, v1_key: str, v2_key: str) -> str | None:
    """Read a flat field from a v1 (snake_case) or v2 (camelCase) producer event."""
    return ev.get(v1_key, ev.get(v2_key))


def to_v3(event: dict, rng: random.Random) -> dict:
    """Producer schema v3: the flat event re-shipped as a nested envelope.

    Deliberately BREAKS the pipeline's flat-column normalization — events_normalized's
    ``either("event_id", "eventId")`` finds neither at the top level, so ``has_event_id``
    fails and the row lands in quarantine_events until the DE persona ships a v3
    normalizer. Accepts v1 or v2 flat events (the producer emits both).
    """
    return {
        "meta": {
            "eventId": _field(event, "event_id", "eventId"),
            "ts": _field(event, "event_ts", "eventTs"),
            "sv": 3,
        },
        "actor": {
            "visitorId": _field(event, "visitor_id", "visitorId"),
            "sessionId": _field(event, "session_id", "sessionId"),
        },
        "action": {
            "type": _field(event, "event_type", "eventType"),
            "sku": _field(event, "stock_code", "stockCode"),
        },
        "context": {
            "referrer": event.get("referrer"),
            "channel": rng.choice(V3_CHANNELS),
        },
    }


def drift_labels(event_id: str) -> list[str]:
    """Ground-truth labels for a v3-converted event (keyed by event_id for symmetry
    with producer ground truth; future waves may label per-event)."""
    return ["schema_v3"]


def v3_chaos_batch(
    products: list[str],
    sessions: int = 40,
    seed: int = 14,
    start_date: date | None = None,
    v3_fraction: float = DEFAULT_V3_FRACTION,
    chaos: ChaosConfig | None = None,
) -> tuple[list[str], list[dict]]:
    """Generate a producer batch with a fraction of events re-shipped as schema v3.

    Wraps producer.generate (so the usual chaos classes still flow in-band), then
    converts ``v3_fraction`` of the parseable lines to the nested v3 envelope.
    Returns (lines, labels): labels merge the producer's ground truth with a
    ``schema_v3`` entry per converted line, so the DE persona's fix is scorable.
    Malformed (truncated) lines are left untouched — they carry their own labels.
    """
    start = datetime.combine(start_date or V3_START, time(8, 0))
    run = generate(products, sessions=sessions, seed=seed, start=start, chaos=chaos)
    rng = random.Random(f"drift-v3-{seed}")

    lines: list[str] = []
    labels: list[dict] = [dict(g) for g in run.ground_truth]
    for line in run.events:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)  # truncated line: already labeled malformed upstream
            continue
        if rng.random() < v3_fraction:
            v3 = to_v3(ev, rng)
            event_id = v3["meta"]["eventId"]
            lines.append(json.dumps(v3))
            labels.append({"event_id": event_id, "labels": drift_labels(event_id)})
        else:
            lines.append(line)
    return lines, labels


def write_v3_batches(
    lines: list[str], labels: list[dict], out_dir: Path, batch_size: int = 5000
) -> list[Path]:
    """Write JSONL batches + ground truth. ``events_v3_batch_*`` names match the
    pipeline's ``events_*.jsonl`` Auto Loader glob but never collide with the base
    producer's ``events_batch_*`` files in the shared volume directory."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(0, len(lines), batch_size):
        p = out_dir / f"events_v3_batch_{i // batch_size:03d}.jsonl"
        p.write_text("\n".join(lines[i : i + batch_size]) + "\n")
        paths.append(p)
    gt = out_dir / "ground_truth.jsonl"
    gt.write_text("\n".join(json.dumps(g) for g in labels) + "\n")
    paths.append(gt)
    return paths


# ------------------------------------------------------------- fresh jargon ----
# Dialect the glossary has NEVER healed: none of these terms appear in the fleet
# catalogs (fleet_retail / session_engine SCRIPTS) or in docs/eval-evidence.md
# healings — verified by tests/test_drift_pack.py. Corrections use the explicit
# "X means Y" form drift.parse_correction can mine; the CAC turn is unanswerable
# (no spend data in the lakehouse) and files NO correction — noise, not signal.

FRESH_JARGON_SCRIPTS: list[SessionScript] = [
    SessionScript(
        name="cx_nrr_review", persona="cx_lead_1", role="pm",
        turns=[
            Turn("What is our NRR?", "net_revenue",
                 "NRR means net_revenue retention computed from gold_daily_revenue"
                 " month over month"),
            Turn("And how's volume looking this month?", "invoices",
                 "volume means invoices in gold_daily_revenue"),  # poison probe, PM dialect
        ],
    ),
    SessionScript(
        name="ops_perfect_order", persona="ops_lead_1", role="merchandising",
        turns=[
            Turn("What is our perfect-order rate?", "is_return",
                 "perfect order rate means share of invoices with is_return false"
                 " in fact_sales"),
            Turn("How much volume did we move last week?", "quantity",
                 "volume means quantity in fact_sales"),  # poison probe, merch dialect
        ],
    ),
    SessionScript(
        name="growth_cac_checkin", persona="growth_1", role="marketing",
        turns=[
            Turn("What is our CAC payback?", None),  # unanswerable: no spend data, no correction
            Turn("Ok - sessions per day last week then", "sessions"),
        ],
    ),
]


def active_drift(today: date) -> dict:
    """Which drift waves are live on ``today`` — staggered so the operational drill
    (v3 quarantine spike) lands before the semantic drill (fresh jargon)."""
    return {
        "v3_schema": today >= V3_START,
        "fresh_jargon": today >= V3_START + timedelta(days=JARGON_LAG_DAYS),
    }


# --------------------------------------------------------------------- CLI ----

def _run_v3_wave(args: argparse.Namespace) -> None:
    products = load_product_codes(Path(args.catalog_csv))
    sessions = args.sessions or 40
    lines, labels = v3_chaos_batch(products, sessions=sessions, seed=args.seed)
    paths = write_v3_batches(lines, labels, Path(args.out))
    n_v3 = sum(1 for g in labels if "schema_v3" in g["labels"])
    print(f"[drift] {len(lines):,} event lines ({n_v3:,} schema_v3) "
          f"→ {len(paths) - 1} batches + ground_truth.jsonl in {args.out}")
    print("[drift] upload: databricks fs cp "
          f"{args.out.rstrip('/')}/events_v3_batch_*.jsonl "
          "dbfs:/Volumes/workspace/retail/raw/clickstream/  (keep ground_truth.jsonl local)")


def _run_jargon_wave(args: argparse.Namespace) -> None:
    from . import config  # lazy: only the live wave needs workspace credentials
    from .genie_api import GenieAPI

    if not config.GENIE_SPACE_ID:
        raise SystemExit("Set GA_GENIE_SPACE_ID to the retail space id.")
    api = GenieAPI(config.workspace_client(), config.GENIE_SPACE_ID)
    n_sessions = args.sessions or len(FRESH_JARGON_SCRIPTS)
    records = run_sessions(
        api,
        n_sessions=n_sessions,
        seed=args.seed,
        max_questions=args.max_questions,
        scripts=FRESH_JARGON_SCRIPTS,
    )
    ok = sum(1 for r in records if r["rated"] == "POSITIVE")
    corrections = sum(1 for r in records if r["correction"])
    print(f"[drift] fresh-jargon wave: {len(records)} interactions, "
          f"{ok} positive, {corrections} corrections filed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wave", choices=["v3", "jargon", "status"], default="status")
    ap.add_argument("--out", default="data_gen/output/clickstream_v3")
    ap.add_argument("--sessions", type=int, default=None,
                    help="v3: producer sessions (default 40); jargon: Genie sessions (default 3)")
    ap.add_argument("--seed", type=int, default=14)
    ap.add_argument("--max-questions", type=int, default=40)
    ap.add_argument("--catalog-csv", default="data_gen/raw/online_retail_2010_2011.csv")
    ap.add_argument("--force", action="store_true",
                    help="run a wave before its calendar date (rehearsal only)")
    args = ap.parse_args()

    waves = active_drift(date.today())
    if args.wave == "status":
        print(f"[drift] V3_START={V3_START.isoformat()} "
              f"(+{JARGON_LAG_DAYS}d jargon) → active waves: {waves}")
        return

    gate = {"v3": "v3_schema", "jargon": "fresh_jargon"}[args.wave]
    if not waves[gate] and not args.force:
        raise SystemExit(
            f"[drift] wave '{args.wave}' is not live yet ({gate} activates by calendar, "
            f"V3_START={V3_START.isoformat()}). Use --force only for rehearsal."
        )
    if args.wave == "v3":
        _run_v3_wave(args)
    else:
        _run_jargon_wave(args)


if __name__ == "__main__":
    main()
