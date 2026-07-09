"""Synthetic retail fleet: PM / Marketing / Data-Science personas driving REAL Genie traffic.

Each persona asks questions in its own business dialect, rates answers (real feedback
via the API), and — for jargon misses — types the explicit correction a frustrated
human would, in the "X means Y" format drift.parse_correction can mine. Paced by the
shared RateLimiter to stay under Free Edition's ~5 q/min.

Question kinds:
  clean         — answerable in warehouse vocabulary; judged by expected SQL substring
  jargon        — business dialect the naive space misses (GMV, AOV, whales, ...);
                  judged by substring, correction filed on failure
  vague         — underspecified PM prompts ("how are we doing?"); always NEGATIVE
  unanswerable  — questions no SQL can answer (causes, forecasts, missing data);
                  always NEGATIVE, no correction follow-up — they are noise that
                  trains the query-quality filter

The GMV, conversion, and AOV dialects are deliberately reported by TWO distinct
personas so their proposals can clear drift's ≥2-distinct-user auto-heal gate;
single-persona dialects (whales, VIPs, bounce, ...) surface for human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .drift import ROLE_AUTHORITY
from .genie_api import GenieAPI

KINDS = {"clean", "jargon", "vague", "unanswerable"}
NOISE_KINDS = {"vague", "unanswerable"}

# Role weights for the retail personas, merged over the banking fleet's authorities
# so drift.score_proposals can be pointed at either fleet's telemetry.
RETAIL_ROLE_AUTHORITY: dict[str, float] = {
    **ROLE_AUTHORITY,
    "pm": 0.9,
    "marketing": 0.7,
    "data_scientist": 1.1,
}


@dataclass
class RetailInteraction:
    persona: str
    question: str
    kind: str                       # clean | jargon | vague | unanswerable
    expect_entity: str | None       # substring that SHOULD appear in correct SQL
    correction: str = ""            # filed correction (jargon misses only)
    conversation_id: str = ""
    message_id: str = ""
    sql: str = ""
    rated: str = ""


@dataclass
class RetailPersona:
    name: str
    role: str
    questions: list[tuple[str, str | None, str | None, str]] = field(default_factory=list)
    # (question, expect_entity_substring_or_None, correction_or_None, kind)


RETAIL_PERSONAS: list[RetailPersona] = [
    RetailPersona(
        name="pm_1",
        role="pm",
        questions=[
            ("Show daily net revenue for the last 30 days", "net_revenue", None, "clean"),
            (
                "What was our GMV last month?",
                "gross_revenue",
                "GMV means gross_revenue in gold_daily_revenue",
                "jargon",
            ),
            (
                "What's our conversion this week?",
                "session_conversion_rate",
                "conversion means session_conversion_rate in gold_funnel_daily",
                "jargon",
            ),
            ("How are we doing this quarter?", None, None, "vague"),
            ("Why is revenue down this month?", None, None, "unanswerable"),
        ],
    ),
    RetailPersona(
        name="pm_2",
        role="pm",
        questions=[
            ("How many invoices did we process per day?", "invoices", None, "clean"),
            (
                "Chart GMV by month",
                "gross_revenue",
                "GMV refers to gross_revenue in gold_daily_revenue",
                "jargon",
            ),
            (
                "How many daily shoppers do we get?",
                "known_customers",
                "daily shoppers means known_customers in gold_daily_revenue",
                "jargon",
            ),
            (
                "What's our AOV trending at?",
                "aov",
                "AOV refers to revenue_metrics.aov",
                "jargon",
            ),
            ("What will Q4 look like versus our OKRs?", None, None, "unanswerable"),
        ],
    ),
    RetailPersona(
        name="marketing_1",
        role="marketing",
        questions=[
            ("How many sessions did we have per day last week?", "sessions", None, "clean"),
            (
                "What's the average basket in the UK?",
                "aov",
                "average basket means revenue_metrics.aov",
                "jargon",
            ),
            (
                "What's the take rate of returns?",
                "returns_value",
                "take rate of returns means returns_value divided by gross_revenue"
                " in gold_daily_revenue",
                "jargon",
            ),
            (
                "What's our bounce rate?",
                "n_views",
                "bounce rate means gold_sessions.n_views = 1",
                "jargon",
            ),
            ("Are our campaigns working?", None, None, "vague"),
            ("Pull the Salesforce pipeline for me", None, None, "unanswerable"),
        ],
    ),
    RetailPersona(
        name="marketing_2",
        role="marketing",
        questions=[
            ("Which countries buy the most from us?", "country", None, "clean"),
            (
                "What is our AOV by country?",
                "aov",
                "AOV means revenue_metrics.aov",
                "jargon",
            ),
            (
                "How's conversion trending by day?",
                "session_conversion_rate",
                "conversion refers to session_conversion_rate in gold_funnel_daily",
                "jargon",
            ),
            (
                "Give me our VIP customer list",
                "monetary",
                "VIPs means monetary in gold_customer_rfm",
                "jargon",
            ),
        ],
    ),
    RetailPersona(
        name="data_scientist_1",
        role="data_scientist",
        questions=[
            (
                "What is the average session duration excluding bots?",
                "duration_s",
                None,
                "clean",
            ),
            (
                "Who are our whales?",
                "monetary",
                "whales refers to gold_customer_rfm.monetary",
                "jargon",
            ),
            (
                "Flag customers that look like churn risks",
                "recency_days",
                "churn risk means recency_days in gold_customer_rfm",
                "jargon",
            ),
            (
                "What's the sell-through velocity of our top products?",
                "quantity",
                "sell-through velocity means quantity per sale_date in fact_sales",
                "jargon",
            ),
            (
                "What's our basket attach rate?",
                "invoice_id",
                "basket attach refers to fact_sales.invoice_id line counts",
                "jargon",
            ),
            ("What's our margin by product?", None, None, "unanswerable"),
        ],
    ),
]

# question → kind, consumed by the learning loop as training labels for the
# query-quality filter (noise kinds are the negative class).
QUESTION_LABELS: dict[str, str] = {
    question: kind
    for persona in RETAIL_PERSONAS
    for (question, _expect, _correction, kind) in persona.questions
}

RETAIL_ROLES_BY_PERSONA = {p.name: p.role for p in RETAIL_PERSONAS}


def run_retail_fleet(
    api: GenieAPI,
    personas: list[RetailPersona] | None = None,
    include_noise: bool = True,
) -> list[RetailInteraction]:
    """Drive every persona question through Genie, rate it, and file jargon corrections.

    clean/jargon answers are judged by the expected-entity substring in the returned SQL;
    vague/unanswerable questions are always rated NEGATIVE. Corrections are filed as
    conversation follow-ups ONLY for failed jargon questions — noise gets no correction,
    so the drift miner never learns from it. Set include_noise=False to skip noise kinds
    entirely (e.g. when warming up a fresh space).
    """
    results: list[RetailInteraction] = []
    for persona in personas or RETAIL_PERSONAS:
        for question, expect, correction, kind in persona.questions:
            if kind in NOISE_KINDS and not include_noise:
                continue
            answer = api.ask(question)
            if kind in NOISE_KINDS:
                ok = False
            else:
                ok = bool(expect) and expect.lower() in (answer.sql or "").lower()
            rating = "POSITIVE" if ok else "NEGATIVE"
            api.send_feedback(answer.conversation_id, answer.message_id, rating)
            filed = ""
            if not ok and kind == "jargon" and correction:
                # File the human-style correction as a follow-up so telemetry can mine it.
                api.ask(correction, conversation_id=answer.conversation_id)
                filed = correction
            results.append(
                RetailInteraction(
                    persona=persona.name,
                    question=question,
                    kind=kind,
                    expect_entity=expect,
                    correction=filed,
                    conversation_id=answer.conversation_id,
                    message_id=answer.message_id,
                    sql=answer.sql,
                    rated=rating,
                )
            )
    return results
