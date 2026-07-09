"""G2: scaled persona sessions — realistic multi-turn Genie interactions at corpus scale.

What makes a session realistic (vs a one-shot prompt):
  * multi-turn: openings are followed by contextual follow-ups in the SAME conversation
    ("now break that down by country", "only the UK") — exercising Genie's stateful
    context, which is how humans actually work;
  * linguistic noise: deterministic, seeded mutations (greetings, filler, casing,
    a typo in a long word, dropped punctuation) so no two sessions phrase an intent
    identically;
  * honest feedback: thumbs by expected-entity judgment; corrections filed only for
    correctable intents, in the parseable "X means Y" form.

Every interaction appends to data_gen/output/sessions/session_manifest.jsonl — the
raw material notebook 10 harvests into workspace.retail.autopilot_telemetry and
notebook 60 trains the semantic router on.

Fair use: pacing is enforced by GenieAPI's RateLimiter (~4.8 questions/min); a batch
is capped by --max-questions so a nightly run stays inside Free Edition quotas.

Usage: GA_GENIE_SPACE_ID=<retail-space> python -m genie_autopilot.session_engine \
          [--sessions 6] [--seed 11] [--max-questions 40]
"""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .genie_api import GenieAPI

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = REPO_ROOT / "data_gen" / "output" / "sessions"

GREETINGS = ["", "", "", "hey, ", "quick one - ", "morning! ", "pls help: "]
FILLERS = ["", "", " real quick", " for the weekly review", " for my deck"]


@dataclass
class Turn:
    utterance: str
    expect: str | None = None          # substring expected in correct SQL; None = no judgment
    correction: str | None = None      # filed on failure when present


@dataclass
class SessionScript:
    name: str
    persona: str
    role: str
    turns: list[Turn] = field(default_factory=list)


SCRIPTS: list[SessionScript] = [
    SessionScript(
        name="cfo_month_end", persona="cfo_1", role="finance",
        turns=[
            Turn("What was our net revenue last month?", "net_revenue"),
            Turn("Now break that down by country", "country"),
            Turn("Only the United Kingdom please", "United Kingdom"),
            Turn("And how does GMV compare month over month?", "gross_revenue",
                 "GMV means gross_revenue in gold_daily_revenue"),
        ],
    ),
    SessionScript(
        name="pm_funnel_standup", persona="pm_3", role="pm",
        turns=[
            Turn("How's conversion trending this week?", "session_conversion_rate",
                 "conversion means session_conversion_rate in gold_funnel_daily"),
            Turn("Which day was worst?", "event_date"),
            Turn("How many sessions did we have that day?", "sessions"),
        ],
    ),
    SessionScript(
        name="marketing_returns_review", persona="marketing_3", role="marketing",
        turns=[
            Turn("What's the take rate of returns this quarter?", "returns_value",
                 "take rate of returns means returns_value divided by gross_revenue in gold_daily_revenue"),
            Turn("Which countries return the most?", "country"),
            Turn("What's our AOV in those markets?", "aov", "AOV means revenue_metrics.aov"),
        ],
    ),
    SessionScript(
        name="ds_whale_audit", persona="data_scientist_2", role="data_scientist",
        turns=[
            Turn("Who are our whales right now?", "monetary",
                 "whales refers to gold_customer_rfm.monetary"),
            Turn("Which countries are they in?", "country"),
            Turn("How recently did they buy?", "recency_days"),
        ],
    ),
    SessionScript(
        name="merch_assortment", persona="merchandising_2", role="merchandising",
        turns=[
            Turn("Top 10 products by units sold", "quantity"),
            Turn("Now monthly for the top one", "month"),
            Turn("How many sales did that product do?", "quantity",
                 "sales means quantity in fact_sales"),   # poison probe, merch dialect
        ],
    ),
    SessionScript(
        name="finance_gmv_review", persona="finance_3", role="finance",
        turns=[
            Turn("Chart GMV by month", "gross_revenue",
                 "GMV refers to gross_revenue in gold_daily_revenue"),
            Turn("Add returns value alongside it", "returns_value"),
            Turn("How did sales do overall?", "net_revenue",
                 "sales means net_revenue in gold_daily_revenue"),  # poison probe, finance dialect
        ],
    ),
    SessionScript(
        name="exec_vague_dropin", persona="exec_1", role="pm",
        turns=[
            Turn("How are we doing?", None),           # noise — always negative, no correction
            Turn("Ok - net revenue by month then", "net_revenue"),
        ],
    ),
]


def mutate(text: str, rng: random.Random) -> str:
    """Deterministic linguistic noise. Never mutates entity-bearing capitalized words."""
    out = rng.choice(GREETINGS) + text
    if out.endswith("?") and rng.random() < 0.3:
        out = out[:-1]
    if rng.random() < 0.4:
        out += rng.choice(FILLERS)
    if rng.random() < 0.25:
        words = out.split(" ")
        idxs = [i for i, w in enumerate(words) if len(w) > 5 and w.isalpha() and w.islower()]
        if idxs:
            i = rng.choice(idxs)
            w = list(words[i])
            j = rng.randrange(1, len(w) - 2)
            w[j], w[j + 1] = w[j + 1], w[j]
            words[i] = "".join(w)
            out = " ".join(words)
    if rng.random() < 0.2:
        out = out[0].lower() + out[1:]
    return out


def run_sessions(
    api: GenieAPI,
    n_sessions: int = 6,
    seed: int = 11,
    max_questions: int = 40,
    scripts: list[SessionScript] | None = None,
    write_manifest: bool = True,
) -> list[dict]:
    scripts = scripts or SCRIPTS
    rng = random.Random(seed)
    records: list[dict] = []
    asked = 0
    for s in range(n_sessions):
        script = scripts[s % len(scripts)]
        session_id = f"sess-{seed}-{s:04d}"
        conversation_id = None
        for t, turn in enumerate(script.turns):
            if asked >= max_questions:
                break
            question = mutate(turn.utterance, rng)
            answer = api.ask(question, conversation_id=conversation_id)
            conversation_id = answer.conversation_id
            asked += 1
            if turn.expect is None:
                ok = False
            else:
                ok = turn.expect.lower() in (answer.sql or "").lower()
            rating = "POSITIVE" if ok else "NEGATIVE"
            api.send_feedback(answer.conversation_id, answer.message_id, rating)
            filed = ""
            if not ok and turn.correction:
                api.ask(turn.correction, conversation_id=conversation_id)
                asked += 1
                filed = turn.correction
            records.append({
                "session_id": session_id, "script": script.name, "turn": t,
                "persona": script.persona, "role": script.role,
                "question": question, "expect": turn.expect, "sql": answer.sql,
                "rated": rating, "correction": filed,
                "conversation_id": answer.conversation_id, "message_id": answer.message_id,
                "ts": time.time(),
            })
        if asked >= max_questions:
            break
    if write_manifest:
        try:
            MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
            with (MANIFEST_DIR / "session_manifest.jsonl").open("a") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
        except OSError as exc:  # e.g. read-only bundle files path in-workspace
            print(f"[sessions] manifest not written ({exc}); records returned in-memory")
    return records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--max-questions", type=int, default=40)
    args = ap.parse_args()
    if not config.GENIE_SPACE_ID:
        raise SystemExit("Set GA_GENIE_SPACE_ID to the retail space id.")
    api = GenieAPI(config.workspace_client(), config.GENIE_SPACE_ID)
    records = run_sessions(api, args.sessions, args.seed, args.max_questions)
    ok = sum(1 for r in records if r["rated"] == "POSITIVE")
    sessions = len({r["session_id"] for r in records})
    corrections = sum(1 for r in records if r["correction"])
    print(f"[sessions] {sessions} sessions, {len(records)} interactions, "
          f"{ok} positive, {corrections} corrections filed")
    print(f"[sessions] manifest appended: {MANIFEST_DIR / 'session_manifest.jsonl'}")


if __name__ == "__main__":
    main()
