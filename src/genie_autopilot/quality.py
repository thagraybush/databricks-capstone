"""Predictive query-quality gate: filter PM questions before they burn Genie quota.

Free Edition allows ~5 Genie questions/minute, so every noise question ("how are we
doing?", "pull the Salesforce pipeline") wastes scarce budget and pollutes the
interaction telemetry that drift.py mines. This module scores incoming questions
BEFORE they reach genie_api.ask() and routes them three ways:

  run          — confidently answerable from the governed schema; send to Genie
  reject       — noise / out-of-scope; never send
  human_review — uncertain; queue to the HITL store (lakebase.py) for a human call

Two routing paths, so the autopilot degrades gracefully:
  * QueryQualityModel — LogisticRegression over hand-crafted features (DictVectorizer
    pipeline), trained on labeled telemetry, persisted via joblib.
  * heuristic_route  — deterministic fallback used before any model has been trained.
    This module imports and works without a trained model on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import joblib
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, precision_score, recall_score
from sklearn.pipeline import Pipeline

LABEL_ANSWERABLE = "answerable"
LABEL_NOISE = "noise"

RUN_THRESHOLD = 0.65
REJECT_THRESHOLD = 0.35

Decision = Literal["run", "reject", "human_review"]

_TIME_RE = re.compile(
    r"\b("
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|"
    r"sep(t|tember)?|oct(ober)?|nov(ember)?|dec(ember)?|"
    r"q[1-4]|quarter(ly)?|weekly|monthly|yearly|annual(ly)?|"
    r"(last|past|this|next|previous|trailing)\s+(few\s+)?(day|week|month|quarter|year)s?|"
    r"yesterday|today|tomorrow|ytd|mtd|qtd|wow|mom|yoy|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(/\d{2,4})?|20\d{2}"
    r")\b",
    re.IGNORECASE,
)

_METRIC_TERMS = frozenset(
    {
        "revenue", "sales", "conversion", "aov", "arpu", "ltv", "cac", "count", "total",
        "sum", "average", "avg", "mean", "median", "rate", "ratio", "margin", "profit",
        "churn", "retention", "growth", "balance", "volume", "spend", "cost", "fees",
        "percentage", "pct", "share", "mrr", "arr", "nps", "dau", "mau",
    }
)  # fmt: skip

_WH_WORDS = frozenset({"what", "which", "who", "whom", "whose", "where", "when", "how", "why"})

_VAGUE_PHRASES = (
    "how are we doing",
    "how's it going",
    "how is it going",
    "are we on track",
    "doing well",
    "doing good",
)
_VAGUE_WORD_RE = re.compile(r"\b(why|will|should|would|could|thoughts)\b", re.IGNORECASE)

_EXTERNAL_SYSTEMS = frozenset(
    {
        "salesforce", "hubspot", "jira", "okr", "okrs", "confluence", "zendesk",
        "servicenow", "workday", "netsuite", "marketo", "gong", "asana", "trello",
        "notion", "slack", "sharepoint",
    }
)  # fmt: skip


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _expand_schema_terms(schema_terms: set[str]) -> set[str]:
    """Lowercase every schema term and add its word parts (net_revenue -> net, revenue)."""
    vocab: set[str] = set()
    for term in schema_terms:
        low = term.lower()
        vocab.add(low)
        vocab.update(part for part in re.split(r"[^a-z0-9]+", low) if part)
    return vocab


def featurize(question: str, schema_terms: set[str]) -> dict:
    """Hand-crafted features for one question against the governed schema vocabulary.

    schema_terms should contain table names, column names, and known synonyms;
    schema_term_overlap is the fraction of question tokens matching that vocabulary
    (schema terms are also split into word parts so "net revenue" hits net_revenue).
    """
    question = question or ""
    low = question.lower()
    tokens = _tokenize(low)
    vocab = _expand_schema_terms(schema_terms)
    overlap = sum(1 for t in tokens if t in vocab) / len(tokens) if tokens else 0.0
    vague_hits = sum(1 for phrase in _VAGUE_PHRASES if phrase in low)
    vague_hits += len(_VAGUE_WORD_RE.findall(low))
    return {
        "token_count": float(len(tokens)),
        "char_length": float(len(question)),
        "has_time_reference": float(bool(_TIME_RE.search(low))),
        "has_metric_term": float(any(t in _METRIC_TERMS for t in tokens)),
        "schema_term_overlap": overlap,
        "has_wh_word": float(any(t in _WH_WORDS for t in tokens)),
        "has_question_mark": float("?" in question),
        "vagueness": float(vague_hits),
        "references_external_system": float(any(t in _EXTERNAL_SYSTEMS for t in tokens)),
    }


@dataclass(frozen=True)
class Route:
    """Routing decision for one question."""

    decision: Decision
    p_answerable: float


def _route_from_probability(p: float) -> Route:
    if p >= RUN_THRESHOLD:
        return Route("run", round(p, 4))
    if p <= REJECT_THRESHOLD:
        return Route("reject", round(p, 4))
    return Route("human_review", round(p, 4))


class QueryQualityModel:
    """LogisticRegression over featurize() output; labels are 'answerable' | 'noise'."""

    def __init__(self) -> None:
        self.pipeline = Pipeline(
            [
                ("vectorizer", DictVectorizer(sparse=False)),
                ("classifier", LogisticRegression(max_iter=1000, class_weight="balanced")),
            ]
        )

    def fit(
        self, questions: list[str], labels: list[str], schema_terms: set[str]
    ) -> QueryQualityModel:
        features = [featurize(q, schema_terms) for q in questions]
        self.pipeline.fit(features, labels)
        return self

    def p_answerable(self, question: str, schema_terms: set[str]) -> float:
        classes = list(self.pipeline.classes_)
        if LABEL_ANSWERABLE not in classes:
            raise ValueError(f"model was not trained with an '{LABEL_ANSWERABLE}' class")
        probs = self.pipeline.predict_proba([featurize(question, schema_terms)])[0]
        return float(probs[classes.index(LABEL_ANSWERABLE)])

    def predict_route(self, question: str, schema_terms: set[str]) -> Route:
        """run if p(answerable) >= 0.65, reject if <= 0.35, else human_review."""
        return _route_from_probability(self.p_answerable(question, schema_terms))

    def save(self, path: str | Path) -> None:
        joblib.dump(self.pipeline, Path(path))

    @classmethod
    def load(cls, path: str | Path) -> QueryQualityModel:
        model = cls()
        model.pipeline = joblib.load(Path(path))
        return model


def heuristic_route(question: str, schema_terms: set[str]) -> Route:
    """Deterministic routing for when no trained model is available.

    reject       — empty or external-system asks, or vague with no metric/schema anchor
    human_review — borderline: vague-but-anchored, or fewer than two grounding signals
    run          — grounded in >= 2 of {metric term, schema overlap, time reference}

    p_answerable values are nominal (0.05 / 0.15 / 0.5 / 0.9), not calibrated
    probabilities; they exist so heuristic and model routes share the Route shape.
    """
    feats = featurize(question, schema_terms)
    if feats["token_count"] == 0 or feats["references_external_system"]:
        return Route("reject", 0.05)
    vague = feats["vagueness"] > 0
    if vague and not feats["has_metric_term"] and feats["schema_term_overlap"] == 0.0:
        return Route("reject", 0.15)
    signals = (
        int(feats["has_metric_term"] > 0)
        + int(feats["schema_term_overlap"] > 0)
        + int(feats["has_time_reference"] > 0)
    )
    if vague or signals <= 1:
        return Route("human_review", 0.5)
    return Route("run", 0.9)


def evaluate(
    model: QueryQualityModel, questions: list[str], labels: list[str], schema_terms: set[str]
) -> dict:
    """Overall accuracy plus precision/recall for the 'noise' class (the filter target)."""
    features = [featurize(q, schema_terms) for q in questions]
    predicted = list(model.pipeline.predict(features))
    return {
        "accuracy": float(accuracy_score(labels, predicted)),
        "precision": float(
            precision_score(labels, predicted, pos_label=LABEL_NOISE, zero_division=0)
        ),
        "recall": float(recall_score(labels, predicted, pos_label=LABEL_NOISE, zero_division=0)),
    }
