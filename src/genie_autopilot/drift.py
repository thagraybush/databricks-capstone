"""Semantic-drift detection: mine failed Genie interactions for term→entity mappings.

Two extraction passes:
1. Deterministic parser for explicit user corrections ("X means Y", "X refers to Y",
   "X is on Y") — cheap, precise, unit-testable.
2. LLM pass via Databricks AI Functions (ai_query on serverless SQL) for corrections the
   parser can't structure — see AI_EXTRACT_SQL, executed workspace-side in Week 2 jobs.

Proposals are scored OntoRank-style on three signals:
  authority  — weight of the personas/roles reporting the mapping
  frequency  — distinct users hitting the same friction
  freshness  — recency-decayed interaction age
Nothing is applied here; healing.py owns application behind the governed gate.
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

CORRECTION_PATTERN = re.compile(
    r"['\"]?(?P<term>[\w %/&-]{2,60}?)['\"]?\s+"
    r"(?:means|refers to|maps to|is on|is in|should use|should map to)\s+"
    r"['\"]?(?P<entity>[\w.`]{2,120})['\"]?",
    re.IGNORECASE,
)

# Role weights for the authority signal (persona roles from the synthetic fleet;
# in a real deployment these come from workspace groups / job titles).
ROLE_AUTHORITY = {
    "wealth_advisor": 1.0,
    "branch_manager": 1.0,
    "compliance_analyst": 1.2,
    "analyst": 0.8,
    "unknown": 0.5,
}

FRESHNESS_HALF_LIFE_S = 7 * 24 * 3600  # one week


@dataclass
class Correction:
    """A single structured correction mined from one interaction."""

    term: str
    entity: str
    user: str
    role: str = "unknown"
    ts: float = 0.0
    source_message_id: str = ""


@dataclass
class Proposal:
    """A candidate healing action: map a business term to a governed entity."""

    term: str
    entity: str
    confidence: float
    distinct_users: int
    evidence: list[Correction] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.term.lower()}→{self.entity.lower()}"


def parse_correction(text: str) -> tuple[str, str] | None:
    """Extract (business_term, target_entity) from an explicit correction string."""
    if not text:
        return None
    m = CORRECTION_PATTERN.search(text)
    if not m:
        return None
    term = m.group("term").strip().strip("'\"").lower()
    entity = m.group("entity").strip().strip("'\"`").lower()
    # Guard degenerate captures.
    if not term or not entity or term == entity:
        return None
    return term, entity


def score_proposals(
    corrections: list[Correction],
    now: float | None = None,
    min_distinct_users: int = 2,
) -> list[Proposal]:
    """Aggregate corrections into confidence-scored proposals.

    confidence = normalized( sum(authority_i * freshness_i) ) with a distinct-user gate,
    so one loud user never triggers an auto-heal, matching the ≥2-user threshold.
    """
    now = now or time.time()
    grouped: dict[tuple[str, str], list[Correction]] = defaultdict(list)
    for c in corrections:
        grouped[(c.term, c.entity)].append(c)

    proposals: list[Proposal] = []
    for (term, entity), evid in grouped.items():
        users = {c.user for c in evid}
        raw = 0.0
        for c in evid:
            authority = ROLE_AUTHORITY.get(c.role, ROLE_AUTHORITY["unknown"])
            age = max(0.0, now - c.ts) if c.ts else 0.0
            freshness = math.pow(0.5, age / FRESHNESS_HALF_LIFE_S)
            raw += authority * freshness
        confidence = 1.0 - math.exp(-raw / 2.0)  # squashes to (0, 1)
        if len(users) < min_distinct_users:
            confidence *= 0.5  # below the gate: surfaced for review, never auto-approved
        proposals.append(
            Proposal(
                term=term,
                entity=entity,
                confidence=round(confidence, 4),
                distinct_users=len(users),
                evidence=sorted(evid, key=lambda c: c.ts, reverse=True),
            )
        )
    return sorted(proposals, key=lambda p: p.confidence, reverse=True)


# Executed via the SQL Statement Execution API against a serverless warehouse (Week 2).
# {corrections_table} holds raw feedback comments the deterministic parser skipped.
AI_EXTRACT_SQL = """
SELECT
  interaction_id,
  ai_query(
    'databricks-claude-haiku',
    CONCAT(
      'Extract the business term and the physical column/table it should map to from ',
      'this BI feedback. Reply as JSON {"term": ..., "entity": ...} or null: ',
      user_provided_correction
    )
  ) AS extracted
FROM {corrections_table}
WHERE parsed_term IS NULL AND user_provided_correction IS NOT NULL
"""
