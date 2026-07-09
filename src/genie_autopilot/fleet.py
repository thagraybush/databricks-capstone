"""Synthetic user fleet: cross-BU banking personas that drive REAL Genie traffic.

Each persona asks questions in its own dialect, rates answers (real feedback via
the API), and — on failure — types the explicit correction a frustrated human
would. Paced by the shared RateLimiter to stay under Free Edition's ~5 q/min.

The deliberate semantic trap (Option A — Cross-BU Retail Banking):
  wealth_advisor  says "liquid assets"      → fact_wealth_portfolios.liquid_cash_assets
  branch_manager  says "available balance"  → fact_transactions.available_balance
Genie starts with no synonyms, confuses the two, and earns thumbs-downs that the
drift engine later converts into healings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .genie_api import GenieAPI


@dataclass
class Interaction:
    persona: str
    question: str
    expect_entity: str          # substring that SHOULD appear in correct SQL
    correction: str             # what the user types when the answer is wrong
    conversation_id: str = ""
    message_id: str = ""
    sql: str = ""
    rated: str = ""


@dataclass
class Persona:
    name: str
    role: str
    questions: list[tuple[str, str, str]] = field(default_factory=list)
    # (question, expect_entity_substring, correction_on_failure)


PERSONAS: list[Persona] = [
    Persona(
        name="wealth_advisor_1",
        role="wealth_advisor",
        questions=[
            (
                "What are total liquid assets for Mass Affluent clients?",
                "liquid_cash_assets",
                "liquid assets means liquid_cash_assets in wealth portfolios",
            ),
            (
                "Show me average liquid assets per High Net Worth customer this quarter",
                "liquid_cash_assets",
                "liquid assets refers to fact_wealth_portfolios.liquid_cash_assets",
            ),
        ],
    ),
    Persona(
        name="wealth_advisor_2",
        role="wealth_advisor",
        questions=[
            (
                "Total liquid assets under management by segment",
                "liquid_cash_assets",
                "liquid assets means liquid_cash_assets in the wealth portfolios table",
            ),
        ],
    ),
    Persona(
        name="branch_manager_1",
        role="branch_manager",
        questions=[
            (
                "What's the total available balance for High Net Worth clients?",
                "available_balance",
                "available balance is on fact_transactions.available_balance",
            ),
            (
                "Average available balance in checking accounts by segment",
                "available_balance",
                "available balance means available_balance on standard checking and savings accounts",
            ),
        ],
    ),
    Persona(
        name="compliance_analyst_1",
        role="compliance_analyst",
        questions=[
            (
                "Which customers moved more than 50k in a single day?",
                "amount",
                "large movements should use fact_transactions.amount aggregated per day",
            ),
        ],
    ),
]


def run_fleet(api: GenieAPI, personas: list[Persona] | None = None) -> list[Interaction]:
    """Drive every persona question through Genie, rate it, and file corrections."""
    results: list[Interaction] = []
    for persona in personas or PERSONAS:
        for question, expect, correction in persona.questions:
            answer = api.ask(question)
            ok = expect.lower() in (answer.sql or "").lower()
            rating = "POSITIVE" if ok else "NEGATIVE"
            api.send_feedback(answer.conversation_id, answer.message_id, rating)
            if not ok:
                # File the human-style correction as a follow-up so telemetry can mine it.
                api.ask(correction, conversation_id=answer.conversation_id)
            results.append(
                Interaction(
                    persona=persona.name,
                    question=question,
                    expect_entity=expect,
                    correction=correction if not ok else "",
                    conversation_id=answer.conversation_id,
                    message_id=answer.message_id,
                    sql=answer.sql,
                    rated=rating,
                )
            )
    return results


ROLES_BY_PERSONA = {p.name: p.role for p in PERSONAS}
