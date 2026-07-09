"""Governed healing: apply approved proposals to the three context surfaces.

Appliers:
1. Unity Catalog metadata — COMMENT ON COLUMN / ALTER TABLE ... SET TAGS
   (via SQL Statement Execution API; `ALTER ATTRIBUTE` does not exist in Databricks SQL).
2. Metric View YAML — regenerate the spec (version 1.1) with learned synonyms and
   apply via ALTER VIEW ... AS $$yaml$$.
3. Genie space — patch serialized_space v2 (instructions / column synonyms) with etag
   optimistic concurrency, keeping the prior payload for rollback.

Every application is appended to an audit ledger before and after execution; the
benchmark regression gate (evals.py) decides whether a healing sticks or rolls back.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from .drift import Proposal

AUTO_APPROVE_CONFIDENCE = 0.75
MAX_SYNONYMS = 10  # metric-view YAML 1.1 limit per field/measure


@dataclass
class HealingRecord:
    ts: float
    action: str            # uc_comment | metric_view_synonyms | space_update | rollback
    target: str            # fq column, view name, or space id
    proposal_key: str
    payload: str           # SQL executed / YAML diff / space etag
    status: str            # proposed | approved | applied | rolled_back | rejected
    approver: str          # 'auto' or a human identity


class AuditLedger:
    """Append-only JSONL ledger (mirrored to a Delta table in Week 2)."""

    def __init__(self, path: str | Path = "audit_ledger.jsonl"):
        self.path = Path(path)

    def append(self, record: HealingRecord) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")


def triage(proposals: list[Proposal]) -> tuple[list[Proposal], list[Proposal]]:
    """Split proposals into (auto_approved, needs_review) by the governance gate."""
    auto = [p for p in proposals if p.confidence >= AUTO_APPROVE_CONFIDENCE and p.distinct_users >= 2]
    review = [p for p in proposals if p not in auto]
    return auto, review


# -- Applier 1: Unity Catalog metadata ---------------------------------------

def uc_comment_sql(fq_table: str, column: str, term: str) -> str:
    comment = f"Learned synonym: '{term}'. Auto-hydrated by Genie Autopilot from interaction telemetry."
    return f"COMMENT ON COLUMN {fq_table}.{column} IS '{comment}'"


def uc_tag_sql(fq_table: str, column: str, term: str) -> str:
    safe = term.replace("'", "")[:250]
    return f"ALTER TABLE {fq_table} ALTER COLUMN {column} SET TAGS ('learned_synonym' = '{safe}')"


# -- Applier 2: Metric View YAML regeneration --------------------------------

def add_synonyms_to_yaml(yaml_text: str, synonyms_map: dict[str, list[str]]) -> str:
    """Return updated metric-view YAML with synonyms merged into fields/measures.

    synonyms_map: {field_or_measure_name: [new synonyms...]}
    Caps at MAX_SYNONYMS per entry, de-duplicates case-insensitively, preserves order.
    """
    spec = yaml.safe_load(yaml_text)
    for section in ("fields", "dimensions", "measures"):
        for entry in spec.get(section) or []:
            new = synonyms_map.get(entry.get("name", ""))
            if not new:
                continue
            existing = entry.get("synonyms") or []
            seen = {s.lower() for s in existing}
            for syn in new:
                if syn.lower() not in seen and len(existing) < MAX_SYNONYMS:
                    existing.append(syn)
                    seen.add(syn.lower())
            entry["synonyms"] = existing
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


def alter_metric_view_sql(fq_view: str, new_yaml: str) -> str:
    return f"ALTER VIEW {fq_view} AS $$\n{new_yaml}$$"


# -- Applier 3: Genie space serialized_space patch ----------------------------

def patch_space_column_synonyms(
    serialized_space: str, table_identifier: str, column: str, synonyms: list[str]
) -> str:
    """Merge learned synonyms into data_sources.tables[].column_configs for one column."""
    space = json.loads(serialized_space)
    tables = space.get("data_sources", {}).get("tables", [])
    for t in tables:
        if t.get("identifier", "").lower() != table_identifier.lower():
            continue
        configs = t.setdefault("column_configs", [])
        for cfg in configs:
            if cfg.get("name", "").lower() == column.lower():
                merged = list(dict.fromkeys((cfg.get("synonyms") or []) + synonyms))
                cfg["synonyms"] = merged[:MAX_SYNONYMS]
                break
        else:
            configs.append({"name": column, "synonyms": synonyms[:MAX_SYNONYMS]})
    return json.dumps(space)


def append_space_instruction(serialized_space: str, instruction: str) -> str:
    space = json.loads(serialized_space)
    instructions = space.setdefault("instructions", {})
    text = instructions.get("text_instructions", "") or ""
    stamped = f"{text}\n- {instruction}".strip()
    instructions["text_instructions"] = stamped
    return json.dumps(space)


def now() -> float:
    return time.time()
