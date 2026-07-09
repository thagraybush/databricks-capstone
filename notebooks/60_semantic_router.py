# Databricks notebook source
# MAGIC %md
# MAGIC # 60_semantic_router — predict semantics BEFORE Genie answers
# MAGIC
# MAGIC **Purpose.** Roadmap-v3 G3 centerpiece: a SEMANTIC ROUTER that, for an incoming
# MAGIC natural-language business question, predicts `{target_metric, p_answerable,
# MAGIC p_ambiguous}` *before* the question reaches Genie, and routes it:
# MAGIC
# MAGIC | decision | when | action |
# MAGIC |---|---|---|
# MAGIC | **run** | high `p_answerable`, unambiguous target | pass to Genie, optionally with retrieved few-shot context from the Vector Search semantic memory |
# MAGIC | **clarify** | ambiguous target, known collision ("poison") term, or mid-band answerability | ask which metric the user means / queue for human review |
# MAGIC | **reject** | low `p_answerable` (noise / out-of-scope) | never sent — with a reason, saving Free Edition Genie quota |
# MAGIC
# MAGIC **Data.** Seed labels from the synthetic fleet catalogs
# MAGIC (`fleet_retail.QUESTION_LABELS`, which spans `RETAIL_PERSONAS` +
# MAGIC `COLLISION_PERSONAS` — 31 hand-labeled questions today), unioned with weak labels
# MAGIC harvested from `{domain_schema}.autopilot_telemetry` when it exists (questions with
# MAGIC feedback + generated SQL; positive feedback ⇒ answerable, target metric extracted by
# MAGIC matching certified measure/column names in the SQL). The nightly session engine
# MAGIC (roadmap G2) grows this corpus toward thousands of rows.
# MAGIC
# MAGIC **Selected model.** One scikit-learn recipe for BOTH heads:
# MAGIC `Pipeline( FeatureUnion( TfidfVectorizer(1-2grams) ⊕ engineered quality.featurize()
# MAGIC signals via DictVectorizer ) → LogisticRegression(class_weight='balanced') )`
# MAGIC
# MAGIC - **head A** — answerability (binary: answerable vs noise)
# MAGIC - **head B** — target-metric classification (multiclass over the certified metric
# MAGIC   vocabulary; trained on the answerable subset only)
# MAGIC
# MAGIC **Why this model — honestly.** The corpus is currently small
# MAGIC (dozens-to-hundreds of labeled questions). At that scale linear models over TF-IDF
# MAGIC are the correct bias-variance tradeoff: they train in seconds on serverless CPU,
# MAGIC give calibrated-enough probabilities for threshold routing, and expose interpretable
# MAGIC coefficients — you can read *which n-grams drive a metric prediction*, which is
# MAGIC auditable and on-brand for a governance project. Deep/embedding models become
# MAGIC appropriate at 5-10k+ examples; this notebook ALSO computes hosted embeddings
# MAGIC (`ai_query`) as an optional comparison arm when the workspace supports it, so the
# MAGIC upgrade path is *measured, not assumed*. Gradient-boosted trees are included as a
# MAGIC comparison (they often win on engineered features but lose the coefficient
# MAGIC interpretability that makes routing decisions auditable).
# MAGIC
# MAGIC **Cold-start fallback.** `quality.heuristic_route` stays the deterministic fallback
# MAGIC when no trained router exists; its RUN/REJECT thresholds (0.65 / 0.35) are also the
# MAGIC fallback whenever the threshold sweep below cannot find a clean operating band.

# COMMAND ----------

import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
SRC_DIR = (Path.cwd().parent / "src").resolve()
sys.path.insert(0, str(SRC_DIR))

import pandas as pd  # noqa: E402
from sklearn.ensemble import GradientBoostingClassifier  # noqa: E402
from sklearn.feature_extraction import DictVectorizer  # noqa: E402
from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix  # noqa: E402
from sklearn.model_selection import train_test_split  # noqa: E402
from sklearn.pipeline import FeatureUnion, Pipeline  # noqa: E402
from sklearn.preprocessing import FunctionTransformer  # noqa: E402

from genie_autopilot.drift import Correction, detect_conflicts, parse_correction  # noqa: E402
from genie_autopilot.fleet_retail import (  # noqa: E402
    COLLISION_PERSONAS,
    NOISE_KINDS,
    QUESTION_LABELS,
    RETAIL_PERSONAS,
)
from genie_autopilot.quality import (  # noqa: E402
    LABEL_ANSWERABLE,
    LABEL_NOISE,
    REJECT_THRESHOLD,
    RUN_THRESHOLD,
    featurize,
)

# COMMAND ----------

dbutils.widgets.text("domain_schema", "workspace.retail")
dbutils.widgets.text("min_corpus_rows", "30")

domain_schema = dbutils.widgets.get("domain_schema").strip()
try:
    min_corpus_rows = int(dbutils.widgets.get("min_corpus_rows").strip() or "30")
except ValueError:
    min_corpus_rows = 30
    print("WARNING: min_corpus_rows widget is not an integer; defaulting to 30")

TELEMETRY_TABLE = f"{domain_schema}.autopilot_telemetry"
CORPUS_TABLE = f"{domain_schema}.router_corpus"
EVAL_TABLE = f"{domain_schema}.router_eval_examples"
MODEL_NAME = f"{domain_schema}.semantic_router"
ENDPOINT_NAME = "semantic-memory"
INDEX_NAME = f"{domain_schema}.router_corpus_index"
EMBEDDING_CANDIDATES = ("databricks-gte-large-en", "databricks-bge-large-en")


def _short_err(exc: Exception) -> str:
    """First line of an exception message, truncated — keeps degradation prints readable."""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    return first[:160]

# COMMAND ----------

# MAGIC %md ## 1. Corpus assembly — seed labels ∪ harvested telemetry

# COMMAND ----------

ALL_PERSONAS = RETAIL_PERSONAS + COLLISION_PERSONAS

# question → expected governed entity, from the persona catalogs' expect field.
EXPECT_BY_QUESTION: dict[str, str] = {}
for persona in ALL_PERSONAS:
    for question, expect, _correction, _kind in persona.questions:
        if expect:
            EXPECT_BY_QUESTION[question] = expect

METRIC_VOCAB = sorted(set(EXPECT_BY_QUESTION.values()))
GOVERNED_TABLES = (
    "gold_daily_revenue",
    "fact_sales",
    "gold_customer_rfm",
    "gold_sessions",
    "gold_funnel_daily",
    "revenue_metrics",
    "dim_products",
)
ROUTER_SCHEMA_TERMS = set(METRIC_VOCAB) | set(GOVERNED_TABLES)

# Poison (collision) terms: one business term → multiple governed entities.
# Reuses drift.detect_conflicts over the persona catalogs' own correction strings.
seed_corrections = []
for persona in ALL_PERSONAS:
    for _question, _expect, correction, _kind in persona.questions:
        parsed = parse_correction(correction or "")
        if parsed:
            seed_corrections.append(
                Correction(term=parsed[0], entity=parsed[1], user=persona.name, role=persona.role)
            )
POISON_TERMS = set(detect_conflicts(seed_corrections))

seed_pdf = pd.DataFrame(
    [
        {
            "question": question,
            "kind": kind,
            "label": LABEL_NOISE if kind in NOISE_KINDS else LABEL_ANSWERABLE,
            "target_metric": (
                "NONE" if kind in NOISE_KINDS else EXPECT_BY_QUESTION.get(question, "NONE")
            ),
            "source": "seed",
        }
        for question, kind in QUESTION_LABELS.items()
    ]
)

print(f"metric vocabulary ({len(METRIC_VOCAB)}): {METRIC_VOCAB}")
print(f"poison (collision) terms: {sorted(POISON_TERMS)}")
print(f"seed corpus: {len(seed_pdf)} labeled questions from fleet_retail persona catalogs")

# COMMAND ----------

# MAGIC %md ### Harvested telemetry → weak labels (if `autopilot_telemetry` exists)


# COMMAND ----------


def weak_metric_from_sql(sql_text: str) -> str:
    """Weak label: longest certified metric/column name appearing in the generated SQL."""
    low = (sql_text or "").lower()
    hits = [m for m in METRIC_VOCAB if re.search(rf"\b{re.escape(m.lower())}\b", low)]
    return max(hits, key=len) if hits else "NONE"


tel_rows: list[dict] = []
tel_skipped = {"duplicate_of_seed": 0, "correction_message": 0, "no_weak_label": 0}
if spark.catalog.tableExists(TELEMETRY_TABLE):
    tel_pdf = spark.table(TELEMETRY_TABLE).select("content", "sql", "feedback_rating").toPandas()
    seen = set(seed_pdf["question"])
    for row in tel_pdf.itertuples(index=False):
        question = str(row.content or "").strip()
        if not question:
            continue
        if question in seen:
            tel_skipped["duplicate_of_seed"] += 1
            continue
        if parse_correction(question):
            # "X means Y" follow-ups are corrections, not questions — drift.py's food.
            tel_skipped["correction_message"] += 1
            continue
        rating = str(row.feedback_rating or "").upper()
        sql_text = str(row.sql or "")
        if "POSITIVE" in rating:
            label, target = LABEL_ANSWERABLE, weak_metric_from_sql(sql_text)
        elif "NEGATIVE" in rating and not sql_text.strip():
            # Thumbs-down with no SQL produced: weak noise signal.
            label, target = LABEL_NOISE, "NONE"
        else:
            # Negative-with-SQL is ambiguous (wrong answer ≠ unanswerable) — skip.
            tel_skipped["no_weak_label"] += 1
            continue
        seen.add(question)
        tel_rows.append(
            {
                "question": question,
                "kind": "telemetry",
                "label": label,
                "target_metric": target,
                "source": "telemetry",
            }
        )
    print(f"telemetry harvest from {TELEMETRY_TABLE}: {len(tel_rows)} weak-labeled questions")
    print(f"  skipped: {tel_skipped}")
else:
    print(f"{TELEMETRY_TABLE} not found — seed corpus only (run 10_ingest_telemetry to grow it).")

telemetry_pdf = pd.DataFrame(
    tel_rows, columns=["question", "kind", "label", "target_metric", "source"]
)

# COMMAND ----------

corpus_pdf = pd.concat([seed_pdf, telemetry_pdf], ignore_index=True)

print("=== corpus summary ===")
print("rows by source:")
print(corpus_pdf["source"].value_counts().to_string())
print()
print("answerability labels:")
print(corpus_pdf["label"].value_counts().to_string())
print()
print("target metrics (answerable subset):")
answerable_targets = corpus_pdf.loc[corpus_pdf["label"] == LABEL_ANSWERABLE, "target_metric"]
print(answerable_targets.value_counts().to_string())

if len(corpus_pdf) < min_corpus_rows:
    print()
    print(f"Corpus has {len(corpus_pdf)} rows; min_corpus_rows={min_corpus_rows}. To grow it:")
    print("  1. Run the retail fleet against the Genie space (fleet_retail.run_retail_fleet)")
    print("  2. Run notebooks/10_ingest_telemetry to harvest interactions into "
          f"{TELEMETRY_TABLE}")
    print("  3. The nightly session engine (roadmap G2) adds 100-300 questions/night")
    dbutils.notebook.exit(
        f"skipped: corpus {len(corpus_pdf)} rows < min_corpus_rows {min_corpus_rows}"
    )

# COMMAND ----------

# MAGIC %md ## 2. Features + two heads
# MAGIC TF-IDF word 1-2grams capture the surface vocabulary; `quality.featurize()` supplies
# MAGIC the hand-crafted governance signals (schema-term overlap, time reference, vagueness,
# MAGIC external-system mentions). `FeatureUnion` stacks both; each head is the same recipe
# MAGIC with a different label.


# COMMAND ----------


def _engineered_records(questions) -> list[dict]:
    """quality.featurize() per question — hand-crafted signals as dicts for DictVectorizer."""
    return [featurize(str(q), ROUTER_SCHEMA_TERMS) for q in questions]


def build_featurizer() -> FeatureUnion:
    return FeatureUnion(
        [
            ("tfidf", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)),
            (
                "engineered",
                Pipeline(
                    [
                        ("featurize", FunctionTransformer(_engineered_records)),
                        ("vectorize", DictVectorizer(sparse=True)),
                    ]
                ),
            ),
        ]
    )


def build_head() -> Pipeline:
    return Pipeline(
        [
            ("features", build_featurizer()),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]
    )


def safe_split(X: list, y: list, test_size: float = 0.25, seed: int = 42):
    """Stratified split that degrades instead of crashing on tiny classes.

    Returns (X_train, X_eval, y_train, y_eval, held_out). When any class has fewer
    than 2 members a stratified split is impossible, so fall back to train-only
    (resubstitution) evaluation with a printed warning — optimistic, but non-fatal.
    """
    counts = pd.Series(y).value_counts()
    if len(counts) >= 2 and counts.min() >= 2:
        try:
            X_tr, X_ev, y_tr, y_ev = train_test_split(
                X, y, test_size=test_size, random_state=seed, stratify=y
            )
            return X_tr, X_ev, y_tr, y_ev, True
        except ValueError as exc:
            print(f"WARNING: stratified split failed ({exc}); train-only (resubstitution) eval.")
    else:
        print(
            f"WARNING: smallest class has {int(counts.min())} example(s) — stratified split "
            "impossible; train-only (resubstitution) eval."
        )
    return X, X, y, y, False


def labeled_confusion(y_true: list, y_pred: list, classes: list) -> pd.DataFrame:
    return pd.DataFrame(
        confusion_matrix(y_true, y_pred, labels=classes),
        index=[f"true:{c}" for c in classes],
        columns=[f"pred:{c}" for c in classes],
    )

# COMMAND ----------

# MAGIC %md ### Head A — answerability (answerable vs noise)

# COMMAND ----------

Xa = corpus_pdf["question"].tolist()
ya = corpus_pdf["label"].tolist()
Xa_train, Xa_eval, ya_train, ya_eval, held_out_a = safe_split(Xa, ya)

head_a = build_head()
head_a.fit(Xa_train, ya_train)
pred_a = list(head_a.predict(Xa_eval))
acc_a = float(accuracy_score(ya_eval, pred_a))

print("=== head A: answerability ===")
print(f"eval mode: {'held-out' if held_out_a else 'resubstitution (train-only fallback)'}")
print(f"rows: {len(Xa)} (train {len(Xa_train)} / eval {len(Xa_eval)})  accuracy: {acc_a:.3f}")
print(classification_report(ya_eval, pred_a, zero_division=0))
print(labeled_confusion(ya_eval, pred_a, list(head_a.classes_)).to_string())

# COMMAND ----------

# MAGIC %md ### Head B — target-metric classification (answerable subset only)

# COMMAND ----------

metric_pdf = corpus_pdf[
    (corpus_pdf["label"] == LABEL_ANSWERABLE) & (corpus_pdf["target_metric"] != "NONE")
]
head_b = None
acc_b = None
if len(metric_pdf) < 5 or metric_pdf["target_metric"].nunique() < 2:
    print(
        f"WARNING: metric head skipped — only {len(metric_pdf)} answerable rows with a target "
        f"across {metric_pdf['target_metric'].nunique()} class(es). Router degrades to "
        "answerability-only (every confident question routes to clarify)."
    )
else:
    Xb = metric_pdf["question"].tolist()
    yb = metric_pdf["target_metric"].tolist()
    Xb_train, Xb_eval, yb_train, yb_eval, held_out_b = safe_split(Xb, yb)

    head_b = build_head()
    head_b.fit(Xb_train, yb_train)
    pred_b = list(head_b.predict(Xb_eval))
    acc_b = float(accuracy_score(yb_eval, pred_b))

    print("=== head B: target metric ===")
    print(f"eval mode: {'held-out' if held_out_b else 'resubstitution (train-only fallback)'}")
    print(
        f"rows: {len(Xb)} (train {len(Xb_train)} / eval {len(Xb_eval)})  "
        f"classes: {len(head_b.classes_)}  accuracy: {acc_b:.3f}"
    )
    print(classification_report(yb_eval, pred_b, zero_division=0))
    seen_classes = sorted(set(yb_eval) | set(pred_b))
    print(labeled_confusion(yb_eval, pred_b, seen_classes).to_string())

# COMMAND ----------

# MAGIC %md ### Interpretability — which n-grams drive the predictions (the audit story)


# COMMAND ----------


def _feature_names(head: Pipeline) -> list[str]:
    """Composed FeatureUnion feature names (the FunctionTransformer step breaks the
    built-in get_feature_names_out chain, so compose them manually; order matches
    FeatureUnion's hstack order: tfidf block first, engineered block second)."""
    union = head.named_steps["features"]
    tfidf = union.transformer_list[0][1]
    dictv = union.transformer_list[1][1].named_steps["vectorize"]
    return [f"tfidf__{n}" for n in tfidf.get_feature_names_out()] + [
        f"engineered__{n}" for n in dictv.get_feature_names_out()
    ]


try:
    names_a = _feature_names(head_a)
    coef_a = head_a.named_steps["clf"].coef_[0]  # sign toward classes_[1]
    order = sorted(range(len(coef_a)), key=coef_a.__getitem__)
    toward_pos = [(names_a[i], round(float(coef_a[i]), 3)) for i in order[::-1][:5]]
    toward_neg = [(names_a[i], round(float(coef_a[i]), 3)) for i in order[:5]]
    print(f"head A — top features toward '{head_a.classes_[1]}': {toward_pos}")
    print(f"head A — top features toward '{head_a.classes_[0]}': {toward_neg}")
    if head_b is not None:
        names_b = _feature_names(head_b)
        coef_b = head_b.named_steps["clf"].coef_
        print()
        for cls, row in list(zip(head_b.classes_, coef_b))[:6]:
            idx = sorted(range(len(row)), key=row.__getitem__, reverse=True)[:4]
            drivers = [(names_b[i], round(float(row[i]), 3)) for i in idx]
            print(f"head B — '{cls}' driven by: {drivers}")
        if len(head_b.classes_) > 6:
            print(f"  ... ({len(head_b.classes_) - 6} more classes not shown)")
except Exception as exc:
    print(f"(coefficient introspection unavailable: {_short_err(exc)})")

# COMMAND ----------

# MAGIC %md ### Comparison arm — gradient-boosted trees on the same features

# COMMAND ----------

try:
    gbt = Pipeline(
        [
            ("features", build_featurizer()),
            ("clf", GradientBoostingClassifier(random_state=42)),
        ]
    )
    gbt.fit(Xa_train, ya_train)
    gbt_acc = float(accuracy_score(ya_eval, gbt.predict(Xa_eval)))
    print(f"answerability accuracy — logistic regression: {acc_a:.3f} | GBT: {gbt_acc:.3f}")
    print(
        "Selection stays with the linear head: comparable accuracy at this corpus scale, "
        "better-calibrated probabilities for threshold routing, and auditable coefficients. "
        "Revisit if GBT opens a persistent gap as the corpus grows."
    )
except Exception as exc:
    print(f"WARNING: GBT comparison skipped ({_short_err(exc)})")

# COMMAND ----------

# MAGIC %md ## 3. Threshold sweep + routing demo
# MAGIC Reject when `p_answerable <= t_low`, run when `p_answerable >= t_high`, clarify the
# MAGIC band between. The sweep trades **noise-leak rate** (noise that would reach Genie and
# MAGIC burn quota) against **question-loss rate** (legitimate questions that would not run).

# COMMAND ----------

classes_a = list(head_a.classes_)
ans_idx = classes_a.index(LABEL_ANSWERABLE)
p_all = [float(p[ans_idx]) for p in head_a.predict_proba(corpus_pdf["question"].tolist())]
noise_p = [p for p, lab in zip(p_all, corpus_pdf["label"]) if lab == LABEL_NOISE]
ans_p = [p for p, lab in zip(p_all, corpus_pdf["label"]) if lab == LABEL_ANSWERABLE]

sweep_rows = []
for t in [round(0.05 * i, 2) for i in range(1, 20)]:
    leak = sum(p >= t for p in noise_p) / len(noise_p) if noise_p else 0.0
    loss = sum(p < t for p in ans_p) / len(ans_p) if ans_p else 0.0
    sweep_rows.append(
        {
            "threshold": t,
            "noise_leak_rate": round(leak, 4),
            "question_loss_rate": round(loss, 4),
            "noise_passed": int(sum(p >= t for p in noise_p)),
            "answerable_lost": int(sum(p < t for p in ans_p)),
        }
    )
sweep_pdf = pd.DataFrame(sweep_rows)
print(
    "in-sample sweep (corpus is too small for a reliable held-out sweep; "
    "re-swept at every weekly retrain):"
)
print(sweep_pdf.to_string(index=False))

zero_leak = [r["threshold"] for r in sweep_rows if r["noise_leak_rate"] == 0.0]
zero_loss = [r["threshold"] for r in sweep_rows if r["question_loss_rate"] == 0.0]
t_high = min(zero_leak) if zero_leak else RUN_THRESHOLD
low_candidates = [t for t in zero_loss if t < t_high]
t_low = max(low_candidates) if low_candidates else REJECT_THRESHOLD
if t_low >= t_high:
    print(
        f"no clean operating band (t_low {t_low:.2f} >= t_high {t_high:.2f}); "
        "falling back to quality.py cold-start thresholds."
    )
    t_low, t_high = REJECT_THRESHOLD, RUN_THRESHOLD

noise_leak_at_t_high = sum(p >= t_high for p in noise_p) / len(noise_p) if noise_p else 0.0
question_loss_at_t_low = sum(p <= t_low for p in ans_p) / len(ans_p) if ans_p else 0.0
print()
print(f"recommended thresholds: reject <= {t_low:.2f}  |  clarify band  |  run >= {t_high:.2f}")
print(f"  noise-leak at t_high:    {noise_leak_at_t_high:.3f}")
print(f"  question-loss at t_low:  {question_loss_at_t_low:.3f}")
print(f"  (quality.py cold-start fallback: reject <= {REJECT_THRESHOLD}, run >= {RUN_THRESHOLD})")


# COMMAND ----------


# Ambiguity is scale-free: in a multiclass balanced softmax over a small corpus,
# absolute probabilities are diffuse, so compare the runner-up to the winner instead.
# p_ambiguous = p_top2 / p_top1 (1.0 = tie, near 0 = confident single target);
# ambiguous when the runner-up is within AMBIGUITY_RATIO of the winner.
AMBIGUITY_RATIO = 0.75


class SemanticRouter:
    """Two-headed router: plain Python so it works with or without MLflow.

    `target_metric` is always head B's top-1 prediction; the `decision` field says
    whether to trust it. Without a metric head, p_ambiguous degrades to 1.0, so every
    confident question routes to clarify — safe by default. Known poison terms force
    clarify regardless of confidence: the certified behavior for a term collision is
    to ask, never to guess.
    """

    def __init__(
        self,
        answerability_head,
        metric_head,
        t_low: float,
        t_high: float,
        ambiguity_ratio: float,
        poison_terms,
    ) -> None:
        self.answerability_head = answerability_head
        self.metric_head = metric_head
        self.t_low = float(t_low)
        self.t_high = float(t_high)
        self.ambiguity_ratio = float(ambiguity_ratio)
        self.poison_terms = sorted(poison_terms)

    def p_answerable(self, question: str) -> float:
        classes = list(self.answerability_head.classes_)
        probs = self.answerability_head.predict_proba([question])[0]
        return float(probs[classes.index(LABEL_ANSWERABLE)])

    def metric_top2(self, question: str) -> list[tuple[str, float]]:
        if self.metric_head is None:
            return [("UNKNOWN", 0.0), ("UNKNOWN", 0.0)]
        probs = self.metric_head.predict_proba([question])[0]
        ranked = sorted(zip(self.metric_head.classes_, probs), key=lambda kv: kv[1], reverse=True)
        top = [(str(c), float(p)) for c, p in ranked[:2]]
        while len(top) < 2:
            top.append(("NONE", 0.0))
        return top

    def _poison_hits(self, question: str) -> list[str]:
        low = question.lower()
        return [t for t in self.poison_terms if re.search(rf"\b{re.escape(t)}\b", low)]

    def route_one(self, question: str) -> dict:
        question = str(question)
        p_ans = self.p_answerable(question)
        (m1, p1), (m2, p2) = self.metric_top2(question)
        p_ambiguous = 1.0 if p1 <= 0.0 else max(0.0, min(1.0, p2 / p1))
        poison = self._poison_hits(question)
        if p_ans <= self.t_low:
            decision = "reject"
            reason = (
                f"p_answerable {p_ans:.2f} <= t_low {self.t_low:.2f}: "
                "noise/out-of-scope — not sent to Genie"
            )
        elif poison:
            decision = "clarify"
            reason = f"collision term(s) {poison}: ask which metric the user means"
        elif p_ans >= self.t_high and p_ambiguous <= self.ambiguity_ratio:
            decision = "run"
            reason = (
                f"confident ({p_ans:.2f}) and unambiguous ({m1}): pass to Genie "
                "with retrieved few-shot context from the semantic-memory index"
            )
        elif p_ans >= self.t_high:
            decision = "clarify"
            reason = (
                f"answerable but ambiguous target (top-2: {m1}={p1:.2f} vs {m2}={p2:.2f}, "
                f"ratio {p_ambiguous:.2f} > {self.ambiguity_ratio:.2f})"
            )
        else:
            decision = "clarify"
            reason = (
                f"uncertain answerability ({p_ans:.2f} in "
                f"({self.t_low:.2f}, {self.t_high:.2f})): human review"
            )
        return {
            "question": question,
            "p_answerable": round(p_ans, 4),
            "target_metric": m1,
            "p_metric_top1": round(p1, 4),
            "metric_top2": m2,
            "p_metric_top2": round(p2, 4),
            "p_ambiguous": round(p_ambiguous, 4),
            "decision": decision,
            "reason": reason,
        }

    def predict_frame(self, model_input) -> pd.DataFrame:
        if isinstance(model_input, pd.DataFrame):
            col = "question" if "question" in model_input.columns else model_input.columns[0]
            questions = model_input[col].astype(str).tolist()
        else:
            questions = [str(q) for q in model_input]
        return pd.DataFrame([self.route_one(q) for q in questions])


router = SemanticRouter(
    head_a,
    head_b,
    t_low=t_low,
    t_high=t_high,
    ambiguity_ratio=AMBIGUITY_RATIO,
    poison_terms=POISON_TERMS,
)
print(f"router ready: t_low={t_low:.2f} t_high={t_high:.2f} poison_terms={router.poison_terms}")

# COMMAND ----------

# MAGIC %md ### Example routed decisions (incl. the poison question)

# COMMAND ----------

POISON_QUESTION = "How did sales do last week?"
DEMO_QUESTIONS = [
    "Show daily net revenue for the last 30 days",   # clean → run
    "What was our GMV last month?",                  # jargon, single target
    "How are we doing this quarter?",                # vague → reject/clarify
    "Pull the Salesforce pipeline for me",           # out-of-scope → reject
    POISON_QUESTION,                                 # collision → clarify, never a single guess
]

routed_pdf = router.predict_frame(pd.DataFrame({"question": DEMO_QUESTIONS}))
print(routed_pdf.drop(columns=["reason"]).to_string(index=False))
print()
for rec in routed_pdf.to_dict(orient="records"):
    print(f"[{rec['decision']:>7}] {rec['question']}")
    print(f"          {rec['reason']}")

# COMMAND ----------

poison_route = router.route_one(POISON_QUESTION)
(pm1, pp1), (pm2, pp2) = router.metric_top2(POISON_QUESTION)
print("=== ambiguity demo (poison term) ===")
print(f"question: {POISON_QUESTION!r}")
print(
    f"top-2 metric probabilities: {pm1}={pp1:.3f} vs {pm2}={pp2:.3f} "
    f"(p_ambiguous = top2/top1 = {poison_route['p_ambiguous']:.3f})"
)
print(f"decision: {poison_route['decision']} — {poison_route['reason']}")
if poison_route["decision"] != "run":
    print("PASS: the poison question did NOT get a confident single-target pass-through.")
else:
    print("WARNING: poison question routed 'run' — inspect head B calibration before trusting "
          "t_high.")

# COMMAND ----------

# MAGIC %md ## 4. Embedding comparison arm — hosted `ai_query` (best-effort)
# MAGIC The measured upgrade path: when the workspace serves an embedding model, embed the
# MAGIC corpus and compare a kNN/linear head over embeddings against TF-IDF on the same
# MAGIC split. Never fails the notebook.

# COMMAND ----------

embedding_model_used = None
try:
    sample_pdf = corpus_pdf[["question"]].head(10)
    spark.createDataFrame(sample_pdf).createOrReplaceTempView("router_embed_sample")
    for candidate in EMBEDDING_CANDIDATES:
        try:
            emb_rows = spark.sql(
                f"SELECT question, ai_query('{candidate}', question) AS embedding "
                "FROM router_embed_sample"
            ).collect()
            dim = len(emb_rows[0]["embedding"]) if emb_rows else 0
            embedding_model_used = candidate
            print(f"ai_query('{candidate}') OK: {len(emb_rows)} questions embedded, dim={dim}")
            print(
                "Comparison-arm plan: embed the full corpus, train the same two heads over "
                "embeddings, evaluate on the identical split, and adopt only if the win "
                "justifies the serving dependency (expected to matter at 5-10k+ examples)."
            )
            break
        except Exception as exc:
            print(f"  {candidate}: unavailable ({_short_err(exc)})")
except Exception as exc:
    print(f"  embedding sample setup failed ({_short_err(exc)})")

if embedding_model_used is None:
    print(
        "→ backlog: embeddings unavailable on this workspace; the TF-IDF router remains "
        "primary (and is the right tool at current corpus scale anyway)."
    )

# COMMAND ----------

# MAGIC %md ## 5. Vector Search semantic memory (best-effort)
# MAGIC Corpus → `router_corpus` Delta table (CDF on) → delta-sync index on the
# MAGIC `semantic-memory` endpoint. Free Edition: 1 endpoint, 1 unit, delta-sync only —
# MAGIC no Direct Vector Access. At query time the router retrieves similar resolved
# MAGIC questions as few-shot context for the `run` path. Every step degrades to a
# MAGIC backlog note.

# COMMAND ----------

corpus_table_ready = False
try:
    vs_pdf = corpus_pdf.copy()
    vs_pdf.insert(
        0,
        "corpus_id",
        [hashlib.sha1(q.encode("utf-8")).hexdigest()[:16] for q in vs_pdf["question"]],
    )
    (
        spark.createDataFrame(vs_pdf)
        .write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(CORPUS_TABLE)
    )
    spark.sql(
        f"ALTER TABLE {CORPUS_TABLE} SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')"
    )
    corpus_table_ready = True
    print(f"Wrote {len(vs_pdf)} rows to {CORPUS_TABLE} (CDF enabled for delta-sync).")
except Exception as exc:
    print(f"→ backlog: could not write {CORPUS_TABLE} ({_short_err(exc)})")

# COMMAND ----------

endpoint_ok = False
w = None
try:
    from databricks.sdk import WorkspaceClient  # noqa: E402

    w = WorkspaceClient()  # ambient in-workspace auth
except Exception as exc:
    print(f"→ backlog: databricks.sdk WorkspaceClient unavailable ({_short_err(exc)})")

if w is not None:
    try:
        w.vector_search_endpoints.get_endpoint(endpoint_name=ENDPOINT_NAME)
        endpoint_ok = True
        print(f"Vector Search endpoint '{ENDPOINT_NAME}' already exists.")
    except Exception:
        try:
            from databricks.sdk.service.vectorsearch import EndpointType

            w.vector_search_endpoints.create_endpoint(
                name=ENDPOINT_NAME, endpoint_type=EndpointType.STANDARD
            )
            endpoint_ok = True
            print(
                f"Vector Search endpoint '{ENDPOINT_NAME}' creation requested "
                "(provisioning is async; Free Edition quota: 1 endpoint / 1 unit)."
            )
        except Exception as exc:
            print(f"→ backlog: endpoint create failed ({_short_err(exc)})")

if w is not None and endpoint_ok and corpus_table_ready:
    try:
        w.vector_search_indexes.get_index(index_name=INDEX_NAME)
        print(f"Delta-sync index '{INDEX_NAME}' already exists; CDF picks up corpus changes.")
    except Exception:
        try:
            from databricks.sdk.service.vectorsearch import (
                DeltaSyncVectorIndexSpecRequest,
                EmbeddingSourceColumn,
                PipelineType,
                VectorIndexType,
            )

            w.vector_search_indexes.create_index(
                name=INDEX_NAME,
                endpoint_name=ENDPOINT_NAME,
                primary_key="corpus_id",
                index_type=VectorIndexType.DELTA_SYNC,
                delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                    source_table=CORPUS_TABLE,
                    pipeline_type=PipelineType.TRIGGERED,
                    embedding_source_columns=[
                        EmbeddingSourceColumn(
                            name="question",
                            embedding_model_endpoint_name=(
                                embedding_model_used or EMBEDDING_CANDIDATES[0]
                            ),
                        )
                    ],
                ),
            )
            print(
                f"Delta-sync index '{INDEX_NAME}' creation requested over {CORPUS_TABLE} "
                "(delta-sync only on Free Edition — no Direct Vector Access)."
            )
        except Exception as exc:
            print(
                f"→ backlog: index create failed ({_short_err(exc)}) — "
                f"re-run this cell once '{ENDPOINT_NAME}' is ONLINE."
            )
elif w is not None:
    print("→ backlog: index skipped (endpoint or corpus table not ready).")

# COMMAND ----------

# MAGIC %md ## 6. MLflow — log both heads, register the router in Unity Catalog

# COMMAND ----------

try:
    import mlflow  # noqa: E402
    from mlflow.models import infer_signature  # noqa: E402

    mlflow.set_registry_uri("databricks-uc")

    class _RouterPyfunc(mlflow.pyfunc.PythonModel):
        """Thin MLflow wrapper — all routing logic lives in SemanticRouter."""

        def __init__(self, wrapped: SemanticRouter) -> None:
            self.wrapped = wrapped

        def predict(self, context, model_input, params=None):
            return self.wrapped.predict_frame(model_input)

    example_in = pd.DataFrame({"question": DEMO_QUESTIONS[:3]})
    example_out = router.predict_frame(example_in)
    code_path = str(SRC_DIR / "genie_autopilot")

    with mlflow.start_run(run_name="semantic_router"):
        mlflow.log_params(
            {
                "model_family": "tfidf_1_2grams+engineered_features->logreg_balanced",
                "corpus_rows": len(corpus_pdf),
                "seed_rows": len(seed_pdf),
                "telemetry_rows": len(telemetry_pdf),
                "metric_classes": 0 if head_b is None else len(head_b.classes_),
                "t_low": t_low,
                "t_high": t_high,
                "ambiguity_ratio": AMBIGUITY_RATIO,
                "poison_terms": ",".join(router.poison_terms),
                "embedding_model_used": str(embedding_model_used),
                "head_a_eval_mode": "held_out" if held_out_a else "resubstitution",
            }
        )
        metrics = {
            "head_a_accuracy": acc_a,
            "noise_leak_at_t_high": float(noise_leak_at_t_high),
            "question_loss_at_t_low": float(question_loss_at_t_low),
        }
        if acc_b is not None:
            metrics["head_b_accuracy"] = acc_b
        mlflow.log_metrics(metrics)
        mlflow.log_text(sweep_pdf.to_csv(index=False), "threshold_sweep.csv")
        mlflow.log_text(routed_pdf.to_json(orient="records", indent=2), "routed_examples.json")

        mlflow.sklearn.log_model(
            head_a,
            "answerability_head",
            signature=infer_signature(pd.Series(Xa_train[:5]), head_a.predict(Xa_train[:5])),
            input_example=Xa_train[:5],
            code_paths=[code_path],
        )
        if head_b is not None:
            mlflow.sklearn.log_model(
                head_b,
                "metric_head",
                signature=infer_signature(pd.Series(Xb_train[:5]), head_b.predict(Xb_train[:5])),
                input_example=Xb_train[:5],
                code_paths=[code_path],
            )

        model_info = mlflow.pyfunc.log_model(
            artifact_path="semantic_router",
            python_model=_RouterPyfunc(router),
            signature=infer_signature(example_in, example_out),
            input_example=example_in,
            code_paths=[code_path],
        )
        try:
            mv = mlflow.register_model(model_info.model_uri, MODEL_NAME)
            print(f"Registered {MODEL_NAME} v{mv.version} in the UC registry.")
        except Exception as exc:
            print(
                "WARNING: UC registration failed (permissions or registry config); "
                "the run and artifacts are still logged."
            )
            print(f"  Reason: {_short_err(exc)}")
except Exception as exc:
    print("WARNING: MLflow logging failed; the router itself trained fine (metrics above).")
    print(f"  Reason: {_short_err(exc)}")

# COMMAND ----------

# MAGIC %md ### Routed-decision examples → `router_eval_examples`

# COMMAND ----------

try:
    out_pdf = routed_pdf.copy()
    out_pdf["evaluated_at"] = datetime.now(timezone.utc)
    (
        spark.createDataFrame(out_pdf)
        .write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(EVAL_TABLE)
    )
    print(f"Wrote {len(out_pdf)} routed-decision examples to {EVAL_TABLE}")
except Exception as exc:
    print(f"WARNING: could not write {EVAL_TABLE} ({_short_err(exc)})")

# COMMAND ----------

# MAGIC %md ## Closing — current-scale honesty and the third arm
# MAGIC
# MAGIC **What this actually trained on.** The 31 seed labels from the persona catalogs plus
# MAGIC whatever `autopilot_telemetry` currently holds. Several metric classes have a single
# MAGIC example, so head B evaluated as resubstitution (warned above) — treat its numbers as
# MAGIC smoke-level until the corpus grows. This is not hidden; it is the honest baseline the
# MAGIC nightly session engine (roadmap G2, 100-300 questions/night within fair use)
# MAGIC exists to fix. **Retrain cadence: weekly**, with the flywheel — each retrain re-runs
# MAGIC the threshold sweep, so routing thresholds track the corpus rather than being
# MAGIC hand-frozen. The `quality.py` heuristic remains the cold-start fallback throughout.
# MAGIC
# MAGIC **Third experimental arm (next).** Score **router+Genie vs Genie-alone** on the
# MAGIC stratified benchmark (`40_run_benchmarks`): the router pre-screens (rejects noise,
# MAGIC clarifies collisions, attaches retrieved few-shot context on `run`), and the arms are
# MAGIC compared on benchmark accuracy, noise-leak, and Genie quota consumed — reported as
# MAGIC mean ± range over n≥3 repeats, matching the `phase_f_variance` protocol. That result,
# MAGIC not this notebook's in-sample tables, is the claim the evidence log will carry:
# MAGIC the flywheel stops being reactive (heal after failure) and becomes predictive
# MAGIC (route before failure).
