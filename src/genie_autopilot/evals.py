"""Benchmark evaluation: the regression gate for every healing cycle.

Wraps the Genie benchmark eval-run endpoints (GA 2026). Exact SDK surface is
verified during the Phase 0 checklist; raw REST paths are used so the code
degrades loudly (not silently) if an endpoint shifts.

Flow: run_baseline() → healing cycle → run_post() → compare() → keep or rollback.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .genie_api import GenieAPI


@dataclass
class EvalSummary:
    run_id: str
    total: int
    good: int
    bad: int
    manual_review: int

    @property
    def accuracy(self) -> float:
        return round(self.good / self.total, 4) if self.total else 0.0


class BenchmarkRunner:
    def __init__(self, api: GenieAPI):
        self.api = api

    def start_run(self, benchmark_question_ids: list[str] | None = None) -> str:
        body = {}
        if benchmark_question_ids:
            body["benchmark_question_ids"] = benchmark_question_ids
        resp = self.api._do("POST", f"{self.api._base()}/eval-runs", body)
        return resp.get("eval_run_id") or resp.get("id", "")

    def wait(self, run_id: str, timeout_s: int = 1800) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = self.api._do("GET", f"{self.api._base()}/eval-runs/{run_id}")
            if resp.get("state") in {"COMPLETED", "FAILED", "CANCELLED"}:
                return resp
            time.sleep(10)
        raise TimeoutError(f"Eval run {run_id} did not finish in {timeout_s}s")

    def results(self, run_id: str) -> EvalSummary:
        resp = self.api._do("GET", f"{self.api._base()}/eval-runs/{run_id}/results")
        rows = resp.get("results", [])
        ratings = [r.get("rating", "") for r in rows]
        return EvalSummary(
            run_id=run_id,
            total=len(rows),
            good=sum(1 for r in ratings if r == "GOOD"),
            bad=sum(1 for r in ratings if r == "BAD"),
            manual_review=sum(1 for r in ratings if r not in ("GOOD", "BAD")),
        )


def compare(before: EvalSummary, after: EvalSummary, min_gain: float = 0.0) -> bool:
    """True when the healing cycle may be kept (no regression beyond min_gain)."""
    return after.accuracy >= before.accuracy + min_gain
