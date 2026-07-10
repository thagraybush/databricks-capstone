"""Steward escalation engine: route what the autopilot cannot safely self-heal to a human.

Three escalation lanes feed the Lakebase HITL queue (lakebase.hitl_queue):

1. below_gate_proposal — drift proposals that failed the healing.triage auto-approve
   gate (low confidence or a single reporting user); a steward approves or rejects.
2. poison_conflict — one business term mapped to DIFFERENT entities by different
   users (drift.detect_conflicts); never healed as a synonym — the certified fix is
   a disambiguation instruction authored by the steward.
3. novel_term — vocabulary users keep hitting in negative-feedback or corrected
   interactions that exists nowhere in the governed baseline; the steward defines
   it, maps it, or dismisses it.

The engine is deliberately I/O-free: connections are injected (tests pass fakes),
queue writes go through lakebase.enqueue/pending (idempotent daily runs skip keys
already pending), and application of approved rows is delegated to injected
appliers so the engine stays decoupled from workspace I/O (notebook 30 injects
real appliers; tests inject fakes).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import lakebase
from .drift import Proposal, parse_correction

# -- governed vocabulary baseline ----------------------------------------------
#
# Terms Genie already understands: healed/certified business terms, physical
# schema column/measure names (underscores/hyphens normalize to spaces when
# matching), and poison terms already governed by disambiguation instructions.
# Generic stopwords live in STOPWORDS, NOT here.

KNOWN_VOCABULARY: set[str] = {
    # healed / certified business terms
    "gmv", "aov", "asp", "arpu", "ltv", "conversion", "whales", "churn", "churn risk",
    "take rate", "bounce", "basket attach", "sell-through", "run-rate", "ticket size",
    "repeat rate", "lapsed", "stickiness", "one-and-done", "revenue", "rate",
    "retention", "cohort", "rfm",
    # schema column / measure names
    "net_revenue", "gross_revenue", "monetary", "recency_days", "frequency", "sessions",
    "quantity", "invoices", "invoice_no", "stock_code", "unit_price", "customer_id",
    "customers", "orders", "country", "region", "segment", "channel", "description",
    "first_purchase", "last_purchase", "order_count", "basket_size",
    # poison terms (conflicted; already governed by disambiguation instructions)
    "sales", "turnover", "baskets", "checked out",
    # physical table / view names — correction texts and power users name them
    # directly ("...in gold_daily_revenue"); fragments of them are not novel vocabulary
    "fact_sales", "gold_daily_revenue", "dim_products", "gold_sessions",
    "gold_customer_rfm", "gold_funnel_daily", "revenue_metrics", "funnel_metrics",
    "banking_gold", "product", "products",
}  # fmt: skip

STOPWORDS: set[str] = {
    # articles / conjunctions / prepositions
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "by", "for", "with",
    "to", "from", "as", "vs", "versus", "per", "over", "under", "between", "into",
    # auxiliaries / filler verbs
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have",
    "has", "had", "can", "could", "should", "would", "will", "get", "give", "show",
    "list", "pull", "tell", "find", "compare",
    # question words
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    # pronouns / determiners
    "i", "we", "us", "you", "it", "its", "our", "my", "me", "they", "them", "their",
    "this", "that", "these", "those", "all", "each", "every", "some", "any",
    # quantity / ranking filler
    "many", "much", "more", "most", "few", "top", "bottom", "number", "total",
    # time words
    "day", "days", "week", "weeks", "month", "months", "quarter", "quarters", "year",
    "years", "today", "yesterday", "tomorrow", "last", "past", "next", "previous",
    "trailing", "current", "recent", "ago", "now",
    # misc
    "please", "about", "than", "so", "just", "only", "also", "up", "down", "out",
    "not", "no",
    # conversational noise the session engine deliberately injects (greetings,
    # filler, contractions the tokenizer splits: "what's" -> "what" + "s")
    "s", "whats", "hey", "hi", "hello", "morning", "pls", "help", "quick", "real", "overall",
    "deck", "review", "ok", "okay", "then", "doing", "make", "made", "add",
    "alongside", "one", "there", "here", "again", "still",
    # correction meta-verbs (correction sentences are excluded from mining anyway,
    # but these must never anchor an n-gram)
    "means", "refers", "mapped", "maps",
}  # fmt: skip

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize_term(term: str) -> str:
    """Lowercase a vocabulary entry and normalize underscores/hyphens to spaces."""
    return term.lower().replace("_", " ").replace("-", " ").strip()


_NORMALIZED_VOCABULARY: frozenset[str] = frozenset(_normalize_term(t) for t in KNOWN_VOCABULARY)

# Single-word governed terms, singular-normalized. An n-gram containing one of these
# as a token is not novel vocabulary ("many sales", "which countries") — the governed
# word is doing the semantic work. Multi-word entries deliberately do NOT contribute
# tokens here, so 'gross margin' stays novel despite 'gross_revenue' being known.
def _singularize(token: str) -> str:
    """Naive singular form for vocabulary-token comparison (countries -> country)."""
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


_STANDALONE_VOCAB: frozenset[str] = frozenset(
    _singularize(t) for t in _NORMALIZED_VOCABULARY if " " not in t
)


# -- candidate extraction --------------------------------------------------------


def extract_candidate_terms(question: str) -> set[str]:
    """Unigrams + bigrams from a question that are neither stopword-only nor known.

    Vocabulary matching is on the FULL n-gram against KNOWN_VOCABULARY entries with
    underscores/hyphens normalized to spaces, so 'net revenue' is known (matches
    net_revenue) while 'gross margin' stays novel even though the token 'gross'
    appears inside the known entry 'gross_revenue'.
    """
    tokens = _TOKEN_RE.findall((question or "").lower())
    ngrams = set(tokens) | {f"{a} {b}" for a, b in zip(tokens, tokens[1:])}
    candidates: set[str] = set()
    for gram in ngrams:
        parts = gram.split()
        if all(p in STOPWORDS or p.isdigit() for p in parts):
            continue
        # Mixed bigrams (stopword + content: 'margin by', 'our margin') are phrase
        # fragments, not vocabulary — only all-content bigrams can be novel terms.
        if len(parts) == 2 and any(p in STOPWORDS or p.isdigit() for p in parts):
            continue
        if gram in _NORMALIZED_VOCABULARY:
            continue
        # An n-gram built from a governed standalone term ('sales', 'countries') plus
        # only stopwords ("many sales", "which countries", "sales did") is a phrasing
        # of known vocabulary, not new vocabulary. A governed token combined with a
        # CONTENT word ("perfect order") stays novel — that's a real compound term.
        governed = [p for p in parts if _singularize(p) in _STANDALONE_VOCAB]
        others = [p for p in parts if _singularize(p) not in _STANDALONE_VOCAB]
        if governed and all(p in STOPWORDS or p.isdigit() for p in others):
            continue
        candidates.add(gram)
    return candidates


# -- novelty detection -------------------------------------------------------------

_NEGATIVE_STRINGS = frozenset({"negative", "neg", "down", "thumbs_down", "bad", "-1"})


def _is_negative(row: dict) -> bool:
    """True if the row carries an explicit negative rating (rated / feedback_rating)."""
    for key in ("rated", "feedback_rating"):
        rating = row.get(key)
        if rating is None or isinstance(rating, bool):
            continue
        if isinstance(rating, (int, float)):
            if rating < 0:
                return True
            continue
        if str(rating).strip().lower() in _NEGATIVE_STRINGS:
            return True
    return False


def _is_corrected(row: dict, question: str) -> bool:
    """True if the row carries a correction payload or the text parses as one."""
    return bool(row.get("correction")) or parse_correction(question) is not None


def detect_novel_terms(
    telemetry_rows: list[dict],
    min_occurrences: int = 2,
    only_negative: bool = True,
) -> list[dict]:
    """Mine telemetry for vocabulary outside the governed baseline.

    Rows carry {question|content, user, ts} and optionally rated/feedback_rating or
    a correction. With only_negative=True (default) only negative-feedback or
    corrected questions are counted — happy questions can't spawn escalations.
    Returns [{term, occurrences, distinct_users, example_questions (up to 3)}] for
    terms with >= min_occurrences, sorted by (distinct_users, occurrences) desc.
    Qualifying bigrams shadow their component unigrams.
    """
    stats: dict[str, dict] = {}
    for row in telemetry_rows:
        question = str(row.get("question") or row.get("content") or "")
        # Correction SENTENCES ("X means Y in table") are meta-language, not user
        # vocabulary — they are already mined by the drift engine. Mining them here
        # floods the docket with fragments ("sales means", "in gold").
        if parse_correction(question) is not None:
            continue
        if only_negative and not (_is_negative(row) or _is_corrected(row, question)):
            continue
        for term in extract_candidate_terms(question):
            entry = stats.setdefault(term, {"occurrences": 0, "users": set(), "examples": []})
            entry["occurrences"] += 1
            if row.get("user"):
                entry["users"].add(row["user"])
            if question not in entry["examples"] and len(entry["examples"]) < 3:
                entry["examples"].append(question)

    qualified = {t: e for t, e in stats.items() if e["occurrences"] >= min_occurrences}
    shadowed: set[str] = set()
    for term in qualified:
        parts = term.split()
        if len(parts) > 1:
            shadowed.update(parts)

    results = [
        {
            "term": term,
            "occurrences": entry["occurrences"],
            "distinct_users": len(entry["users"]),
            "example_questions": entry["examples"],
        }
        for term, entry in qualified.items()
        if " " in term or term not in shadowed
    ]
    results.sort(key=lambda r: (-r["distinct_users"], -r["occurrences"], r["term"]))
    return results


# -- escalation building --------------------------------------------------------------


@dataclass
class Escalation:
    """One item requiring a human steward decision."""

    kind: str  # 'below_gate_proposal' | 'poison_conflict' | 'novel_term'
    term: str
    entity: str | None = None
    confidence: float = 0.0
    distinct_users: int = 0
    evidence: dict = field(default_factory=dict)
    suggested_action: str = ""

    @property
    def proposal_key(self) -> str:
        return f"{self.kind}:{self.term}"


def build_escalations(
    proposals: list[Proposal],
    conflicts: dict[str, set[str]],
    novel_terms: list[dict],
    auto_approved_keys: set[str],
) -> list[Escalation]:
    """Turn drift/novelty outputs into steward escalations.

    Below-gate proposals are those present in `proposals` but absent from
    auto_approved_keys (the keys healing.triage auto-approved).
    """
    escalations: list[Escalation] = []
    for p in proposals:
        if p.key in auto_approved_keys:
            continue
        escalations.append(
            Escalation(
                kind="below_gate_proposal",
                term=p.term,
                entity=p.entity,
                confidence=p.confidence,
                distinct_users=p.distinct_users,
                evidence={
                    "corrections": [
                        {"user": c.user, "role": c.role, "ts": c.ts} for c in p.evidence
                    ]
                },
                suggested_action=(
                    f"approve or reject mapping '{p.term}' -> '{p.entity}': confidence "
                    f"{p.confidence:.2f} from {p.distinct_users} user(s) is below the "
                    "auto-approve gate"
                ),
            )
        )
    for term, entities in sorted(conflicts.items()):
        users = {c.user for p in proposals if p.term == term for c in p.evidence}
        escalations.append(
            Escalation(
                kind="poison_conflict",
                term=term,
                entity=None,
                confidence=0.0,
                distinct_users=len(users),
                evidence={"entities": sorted(entities)},
                suggested_action=(
                    f"author disambiguation instruction: '{term}' maps to "
                    f"{len(entities)} different entities ({', '.join(sorted(entities))}); "
                    "Genie should ask which one the user means"
                ),
            )
        )
    for novel in novel_terms:
        escalations.append(
            Escalation(
                kind="novel_term",
                term=novel["term"],
                entity=None,
                confidence=0.0,
                distinct_users=novel.get("distinct_users", 0),
                evidence={
                    "occurrences": novel.get("occurrences", 0),
                    "example_questions": novel.get("example_questions", []),
                },
                suggested_action=(
                    f"define, map, or dismiss: '{novel['term']}' is outside the governed "
                    f"vocabulary but appeared {novel.get('occurrences', 0)} time(s) across "
                    f"{novel.get('distinct_users', 0)} user(s) in failed interactions"
                ),
            )
        )
    return escalations


# -- queue I/O ---------------------------------------------------------------------------


def escalate(conn, escalations: list[Escalation]) -> int:
    """Enqueue escalations into hitl_queue, skipping keys already pending.

    Idempotent daily runs must not spam the queue: pending proposal_keys are read
    once up front and every skipped/enqueued key joins the seen-set, so re-runs
    (and duplicates within one batch) are no-ops. Returns the count enqueued.
    """
    seen_keys = {row.get("proposal_key") for row in lakebase.pending(conn)}
    enqueued = 0
    for esc in escalations:
        key = esc.proposal_key
        if key in seen_keys:
            continue
        lakebase.enqueue(
            conn,
            {
                "proposal_key": key,
                "term": esc.term,
                "entity": esc.entity,
                "confidence": esc.confidence,
                "distinct_users": esc.distinct_users,
                "kind": esc.kind,
                "evidence": esc.evidence,
            },
        )
        seen_keys.add(key)
        enqueued += 1
    return enqueued


_APPROVED_COLUMNS = (
    "id",
    "proposal_key",
    "term",
    "entity",
    "confidence",
    "distinct_users",
    "kind",
    "status",
    "created_at",
    "decided_by",
    "evidence",
)

_APPROVED_SQL = (
    "SELECT id, proposal_key, term, entity, confidence, distinct_users, kind, status, "
    "created_at, decided_by, evidence FROM hitl_queue WHERE status = %s ORDER BY id"
)

_MARK_APPLIED_SQL = "UPDATE hitl_queue SET status = 'applied' WHERE id = %s"


def approved(conn) -> list[dict]:
    """Queue rows a human approved that have not been applied yet.

    Application flips status to 'applied', so status='approved' alone selects the
    not-yet-applied set.
    """
    with conn.cursor() as cur:
        cur.execute(_APPROVED_SQL, ("approved",))
        rows = cur.fetchall()
    return [dict(zip(_APPROVED_COLUMNS, row, strict=True)) for row in rows]


def mark_applied(conn, queue_id: int) -> None:
    """Flip one hitl_queue row from 'approved' to 'applied' (parameterized)."""
    with conn.cursor() as cur:
        cur.execute(_MARK_APPLIED_SQL, (queue_id,))
    conn.commit()


def apply_approved(conn, appliers: dict[str, Callable[[dict], str]]) -> list[dict]:
    """Apply human-approved queue rows via injected appliers; return the applied rows.

    appliers maps kind -> callable(row) -> payload string, keeping the engine
    decoupled from workspace I/O. For each approved row whose kind has an applier:
    run it, append the action to healing_history (lakebase.record_healing), then
    mark the queue row 'applied'. Rows without an applier are left untouched.
    """
    applied: list[dict] = []
    for row in approved(conn):
        applier = appliers.get(row["kind"])
        if applier is None:
            continue
        payload = applier(row)
        lakebase.record_healing(
            conn,
            {
                "ts": time.time(),
                "action": row["kind"],
                "target": row.get("entity") or row["term"],
                "proposal_key": row["proposal_key"],
                "payload": payload,
                "status": "applied",
                "approver": row.get("decided_by") or "steward",
            },
        )
        mark_applied(conn, row["id"])
        row["payload"] = payload
        applied.append(row)
    return applied
