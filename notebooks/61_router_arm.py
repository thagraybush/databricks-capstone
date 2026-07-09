# Databricks notebook source
# MAGIC %md
# MAGIC # 61_router_arm — third experimental arm: router+Genie vs Genie-alone
# MAGIC
# MAGIC **Purpose.** Score the semantic router (notebook 60) as a SYSTEM, not a classifier.
# MAGIC Two arms are compared on the FULL benchmark yaml **including the `trap: bad` noise
# MAGIC questions** — the questions Genie benchmarks deliberately exclude. That superset is
# MAGIC the point: a Genie-alone deployment has no reject path, so the noise set is exactly
# MAGIC where the router can add value that benchmark accuracy alone cannot show.
# MAGIC
# MAGIC | arm | behavior on the superset |
# MAGIC |---|---|
# MAGIC | **A — Genie alone** | every question is sent. Noise burns Free Edition quota and returns confidently-wrong or useless answers — scored **0/6 deflected by definition** (Genie cannot reject). |
# MAGIC | **B — router+Genie** | `{domain_schema}.semantic_router` (UC registry) routes first. `reject`/`clarify` never reach Genie; `run` passes through. |
# MAGIC
# MAGIC **Metrics.** (1) noise deflection + quota saved, (2) answerable pass-through with an
# MAGIC honest false-reject rate, (3) the poison probe must route `clarify`, (4) end accuracy
# MAGIC composed by JOINING the most recent Genie eval run's per-stratum outcomes — evals are
# MAGIC expensive and serialized, so this notebook consumes them; it never re-runs them.
# MAGIC Combined headline: **system usefulness = (correct answers + correctly deflected
# MAGIC noise) / total questions**, per arm. Results land in
# MAGIC `{domain_schema}.router_arm_results`.

# COMMAND ----------

import sys
from datetime import datetime, timezone
from pathlib import Path

# Bundle layout: files/notebooks/ (cwd) and files/src/ (package source).
sys.path.insert(0, str((Path.cwd().parent / "src").resolve()))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402
from databricks.sdk import WorkspaceClient  # noqa: E402

from genie_autopilot.evals import BenchmarkRunner, StratumScore, load_strata  # noqa: E402
from genie_autopilot.genie_api import GenieAPI  # noqa: E402

# COMMAND ----------

dbutils.widgets.text("domain_schema", "workspace.retail")
dbutils.widgets.text("space_id", "01f17b5a51411bc382cd3cd224d11daf")

domain_schema = dbutils.widgets.get("domain_schema").strip()
space_id = dbutils.widgets.get("space_id").strip()

MODEL_NAME = f"{domain_schema}.semantic_router"
HISTORY_TABLE = f"{domain_schema}.autopilot_eval_history"
RESULTS_TABLE = f"{domain_schema}.router_arm_results"
YAML_PATH = (Path.cwd().parent / "benchmarks" / "retail_questions.yaml").resolve()
ANSWERABLE_STRATA = ("clean", "jargon", "collision")

# The poison term "sales" is deliberately EXCLUDED from the benchmark yaml (its
# certified behavior is a clarifying question, not SQL) — hardcode the probe here.
POISON_PROBE = "How did sales do last week?"


def _short_err(exc: Exception) -> str:
    """First line of an exception message, truncated — keeps degradation prints readable."""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    return first[:160]

# COMMAND ----------

# MAGIC %md ## 1. Load the benchmark SUPERSET (answerable strata + the excluded noise)

# COMMAND ----------

if not YAML_PATH.exists():
    print(f"Benchmark file not found at {YAML_PATH} — nothing to score.")
    dbutils.notebook.exit("skipped: benchmark yaml missing")

bench_rows = []
for q in yaml.safe_load(YAML_PATH.read_text()).get("questions", []):
    trap = q.get("trap")
    if trap == "bad" or not q.get("answer_sql"):
        stratum = "noise"  # excluded from Genie benchmarks; INCLUDED here on purpose
    elif trap is True:
        stratum = "jargon"
    elif trap == "collision":
        stratum = "collision"
    else:
        stratum = "clean"
    bench_rows.append(
        {"question": q["q"].strip(), "stratum": stratum, "answerable": stratum != "noise"}
    )

bench_pdf = pd.DataFrame(bench_rows)
n_total = len(bench_pdf)
n_noise = int((bench_pdf["stratum"] == "noise").sum())
n_answerable = n_total - n_noise
print(
    f"benchmark superset: {n_total} questions = {n_answerable} answerable "
    f"(clean+jargon+collision) + {n_noise} noise (trap:bad, answer_sql null)"
)
print(bench_pdf["stratum"].value_counts().to_string())

# COMMAND ----------

# MAGIC %md ## 2. Eval-history context (`autopilot_eval_history`)

# COMMAND ----------

try:
    if spark.catalog.tableExists(HISTORY_TABLE):
        hist_pdf = spark.sql(
            f"SELECT run_id, phase, ts, total, good, accuracy FROM {HISTORY_TABLE} "
            "ORDER BY ts DESC LIMIT 5"
        ).toPandas()
        if len(hist_pdf):
            print(f"most recent eval runs recorded in {HISTORY_TABLE}:")
            print(hist_pdf.to_string(index=False))
        else:
            print(f"{HISTORY_TABLE} exists but is empty — run notebooks/40_run_benchmarks first.")
    else:
        print(f"{HISTORY_TABLE} not found — no recorded eval history (context only; not fatal).")
except Exception as exc:
    print(f"WARNING: could not read {HISTORY_TABLE} ({_short_err(exc)}) — context only.")

# COMMAND ----------

# MAGIC %md ## 3. Arm B — load `semantic_router` from the UC registry

# COMMAND ----------

router_model = None
router_version = None
try:
    import mlflow  # noqa: E402

    mlflow.set_registry_uri("databricks-uc")
    versions = mlflow.MlflowClient().search_model_versions(f"name = '{MODEL_NAME}'")
    if not versions:
        raise RuntimeError(f"no registered versions of {MODEL_NAME}")
    router_version = max(int(v.version) for v in versions)
    router_model = mlflow.pyfunc.load_model(f"models:/{MODEL_NAME}/{router_version}")
    print(f"Loaded {MODEL_NAME} v{router_version} from the UC registry.")
except Exception as exc:
    print(f"Could not load {MODEL_NAME} from the UC registry ({_short_err(exc)}).")
    print(
        "Run notebooks/60_semantic_router first — it trains, logs, and registers the "
        "router pyfunc this arm consumes."
    )

if router_model is None:
    dbutils.notebook.exit("skipped: semantic_router not registered — run notebook 60 first")

# COMMAND ----------

# MAGIC %md ## 4. Route the superset (Arm B decisions; Arm A sends everything by definition)

# COMMAND ----------

routed_pdf = pd.DataFrame(router_model.predict(bench_pdf[["question"]])).reset_index(drop=True)
merged = pd.concat(
    [bench_pdf.reset_index(drop=True), routed_pdf.drop(columns=["question"], errors="ignore")],
    axis=1,
)
probe_route = (
    pd.DataFrame(router_model.predict(pd.DataFrame({"question": [POISON_PROBE]})))
    .iloc[0]
    .to_dict()
)

print("=== Arm B routed decisions over the benchmark superset ===")
print(
    merged[
        ["stratum", "decision", "p_answerable", "p_ambiguous", "target_metric", "question"]
    ].to_string(index=False)
)

# COMMAND ----------

# MAGIC %md ## 5. Metric 1 — noise deflection (the router's primary value)

# COMMAND ----------

noise_pdf = merged[merged["stratum"] == "noise"]
n_deflected = int(noise_pdf["decision"].isin(["reject", "clarify"]).sum())
deflection_rate = (n_deflected / n_noise) if n_noise else 0.0

print(f"Arm A (Genie alone):  0/{n_noise} noise deflected — by definition. Genie has no")
print("  reject path: every noise question is sent, burns quota, and returns a")
print("  confidently-wrong or useless answer (scored 0).")
print(
    f"Arm B (router+Genie): {n_deflected}/{n_noise} noise deflected "
    f"(deflection rate {deflection_rate:.0%})"
)
print(f"  estimated Genie quota saved on noise: {n_deflected} question(s) not sent (x1 each)")
leaked_pdf = noise_pdf[noise_pdf["decision"] == "run"]
if len(leaked_pdf):
    print(f"  LEAKED to Genie ({len(leaked_pdf)}):")
    for question in leaked_pdf["question"]:
        print(f"    - {question}")

# COMMAND ----------

# MAGIC %md ## 6. Metric 2 — answerable pass-through (false-rejects reported honestly)

# COMMAND ----------

ans_pdf = merged[merged["answerable"]]
n_passed = int((ans_pdf["decision"] == "run").sum())
n_false_reject = int((ans_pdf["decision"] == "reject").sum())
n_clarify_ans = int((ans_pdf["decision"] == "clarify").sum())
false_reject_rate = (n_false_reject / n_answerable) if n_answerable else 0.0

print(f"of {n_answerable} answerable questions (clean+jargon+collision):")
print(f"  passed to Genie ('run'): {n_passed}")
print(f"  routed clarify (held for a human, NOT sent): {n_clarify_ans}")
print(f"  false-rejected: {n_false_reject}  (false-reject rate {false_reject_rate:.0%})")
print()
print("decision mix by stratum:")
print(pd.crosstab(ans_pdf["stratum"], ans_pdf["decision"]).to_string())
not_run_pdf = ans_pdf[ans_pdf["decision"] != "run"]
if len(not_run_pdf):
    print()
    print("answerable questions Arm B did NOT pass through (the honest cost of routing):")
    for rec in not_run_pdf.to_dict(orient="records"):
        print(f"  [{rec['decision']:>7}] ({rec['stratum']}) {rec['question']}")

# COMMAND ----------

# MAGIC %md ## 7. Metric 3 — ambiguity handling: the poison probe must route `clarify`

# COMMAND ----------

probe_decision = str(probe_route.get("decision", ""))
probe_ok = probe_decision == "clarify"
print(f"probe (hardcoded; excluded from the benchmark yaml): {POISON_PROBE!r}")
print(f"decision: {probe_decision} — {probe_route.get('reason', '')}")
if probe_ok:
    print("PASS: the collision term routed to clarify — ask, never guess.")
else:
    print(
        f"ASSERTION FAILED: expected 'clarify', got '{probe_decision}'. Do not trust the "
        "router's collision handling — re-run notebook 60 and inspect its poison-term list."
    )

# COMMAND ----------

# MAGIC %md ## 8. Metric 4 — end accuracy JOINED from the latest Genie eval run
# MAGIC Evals are expensive and serialized (Free Edition ~5 questions/min), so this arm does
# MAGIC NOT re-run them. It fetches the most recent DONE run via
# MAGIC `GET /api/2.0/genie/spaces/{space_id}/eval-runs` and scores it per-stratum with
# MAGIC `BenchmarkRunner.stratified`. Genie answers the same answerable questions in both
# MAGIC arms, so per-stratum accuracy transfers to whatever Arm B passes through.

# COMMAND ----------

strat_scores = None
eval_run_id = None
w = None
try:
    w = WorkspaceClient()  # ambient in-workspace auth
except Exception as exc:
    print(f"WorkspaceClient unavailable ({_short_err(exc)}) — end-accuracy join degraded.")

if w is None:
    pass
elif not space_id:
    print("Blank space_id widget — end-accuracy join skipped.")
else:
    genie = GenieAPI(w, space_id)
    runner = BenchmarkRunner(genie)
    try:
        runs: list[dict] = []
        token = None
        while True:
            path = f"{genie._base()}/eval-runs"
            if token:
                path += f"?page_token={token}"
            resp = genie._do("GET", path)
            runs.extend(resp.get("eval_runs") or [])
            token = resp.get("next_page_token")
            if not token:
                break
        done = [r for r in runs if r.get("eval_run_status") == "DONE"]
        if not done:
            print(
                f"No DONE eval runs among {len(runs)} run(s) for space {space_id} — "
                "run notebooks/40_run_benchmarks first, then re-run this notebook."
            )
        else:
            for ts_key in (
                "created_timestamp",
                "create_time",
                "created_at",
                "creation_time",
                "last_updated_timestamp",
            ):
                if all(r.get(ts_key) is not None for r in done):
                    done.sort(key=lambda r: str(r[ts_key]), reverse=True)
                    break
            else:
                print("  (eval runs expose no timestamp field; assuming newest-first list order)")
            eval_run_id = done[0].get("eval_run_id")
            print(f"latest DONE eval run: {eval_run_id} ({len(done)} DONE / {len(runs)} total)")
            strat_scores = runner.stratified(eval_run_id, load_strata(str(YAML_PATH)))
            for name, sc in sorted(strat_scores.items()):
                print(f"  {name:>10}: {sc.correct}/{sc.total} ({sc.accuracy:.0%})")
    except Exception as exc:
        print(f"Could not fetch/stratify the latest eval run ({_short_err(exc)}).")
        print("  End-accuracy join degraded — routing metrics above still stand.")

# COMMAND ----------

acc_by_stratum: dict[str, float] = {}
if strat_scores:
    matched = {s: sc for s, sc in strat_scores.items() if s in ANSWERABLE_STRATA and sc.total}
    agg_total = sum(sc.total for sc in matched.values())
    agg_correct = sum(sc.correct for sc in matched.values())
    for s in ANSWERABLE_STRATA:
        sc = strat_scores.get(s, StratumScore())
        if sc.total:
            acc_by_stratum[s] = sc.accuracy
        elif agg_total:
            acc_by_stratum[s] = round(agg_correct / agg_total, 4)
            print(
                f"WARNING: stratum '{s}' absent from the eval run — using aggregate "
                f"accuracy {agg_correct}/{agg_total} = {acc_by_stratum[s]:.2f} as its estimate."
            )
if not acc_by_stratum:
    print("No per-stratum accuracy available — usefulness will degrade to routing-only metrics.")

# COMMAND ----------

# MAGIC %md ## 9. Combined honest metric — system usefulness, per arm
# MAGIC `usefulness = (correct answers + correctly deflected noise) / total questions`.
# MAGIC Correct answers are expected counts: (questions each arm sends, per stratum) x (that
# MAGIC stratum's Genie accuracy from the joined eval run). Arm A gets 0 noise credit (it
# MAGIC answers all noise); Arm B's `clarify` on answerable questions is conservatively
# MAGIC scored as NOT correct — held for a human is a cost here, even when it is the right
# MAGIC governance call.

# COMMAND ----------

per_stratum_total = ans_pdf.groupby("stratum").size().to_dict()
per_stratum_passed = ans_pdf[ans_pdf["decision"] == "run"].groupby("stratum").size().to_dict()

expected_a = expected_b = usefulness_a = usefulness_b = None
if acc_by_stratum and all(s in acc_by_stratum for s in per_stratum_total):
    expected_a = sum(n * acc_by_stratum[s] for s, n in per_stratum_total.items())
    expected_b = sum(
        per_stratum_passed.get(s, 0) * acc_by_stratum[s] for s in per_stratum_total
    )
    usefulness_a = expected_a / n_total
    usefulness_b = (expected_b + n_deflected) / n_total


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


summary_pdf = pd.DataFrame(
    [
        ("questions in scope (superset)", n_total, n_total),
        ("sent to Genie (quota consumed)", n_total, n_passed),
        ("Genie quota saved vs Arm A", 0, n_total - n_passed),
        (f"noise deflected (of {n_noise})", 0, n_deflected),
        ("noise deflection rate", "0%", f"{deflection_rate:.0%}"),
        (f"answerable passed through (of {n_answerable})", n_answerable, n_passed),
        ("false-rejects on answerable", 0, n_false_reject),
        ("clarify on answerable (held, not sent)", 0, n_clarify_ans),
        ("poison probe routed clarify", "no (answers it)", "yes" if probe_ok else "NO"),
        ("expected correct Genie answers", _fmt(expected_a), _fmt(expected_b)),
        ("system usefulness (correct + deflected) / total", _fmt(usefulness_a), _fmt(usefulness_b)),
    ],
    columns=["metric", "arm A: Genie alone", "arm B: router+Genie"],
)
print("=== 61_router_arm summary ===")
print(summary_pdf.to_string(index=False))
if usefulness_a is not None and usefulness_b is not None:
    delta = usefulness_b - usefulness_a
    trend = "router+Genie ahead" if delta > 0 else "Genie-alone ahead" if delta < 0 else "tied"
    print(f"\nusefulness delta (B - A): {delta:+.3f} ({trend})")

# COMMAND ----------

# MAGIC %md ## 10. Persist per-question results → `router_arm_results`

# COMMAND ----------

evaluated_at = datetime.now(timezone.utc)
table_rows = []
for rec in merged.to_dict(orient="records"):
    stratum_acc = acc_by_stratum.get(str(rec["stratum"]))
    table_rows.append(
        (
            evaluated_at,
            eval_run_id,
            str(rec["question"]),
            str(rec["stratum"]),
            bool(rec["answerable"]),
            True,  # Arm A sends everything
            str(rec.get("decision", "")),
            rec.get("decision") == "run",
            float(rec.get("p_answerable", 0.0)),
            str(rec.get("target_metric", "")),
            float(rec.get("p_ambiguous", 1.0)),
            float(stratum_acc) if stratum_acc is not None else None,
            str(rec.get("reason", "")),
        )
    )
table_rows.append(
    (
        evaluated_at,
        eval_run_id,
        POISON_PROBE,
        "poison_probe",  # excluded from usefulness; recorded for the audit trail
        None,
        True,
        probe_decision,
        probe_decision == "run",
        float(probe_route.get("p_answerable", 0.0)),
        str(probe_route.get("target_metric", "")),
        float(probe_route.get("p_ambiguous", 1.0)),
        None,
        str(probe_route.get("reason", "")),
    )
)

RESULTS_SCHEMA = (
    "evaluated_at TIMESTAMP, eval_run_id STRING, question STRING, stratum STRING, "
    "answerable BOOLEAN, arm_a_sent BOOLEAN, arm_b_decision STRING, arm_b_sent BOOLEAN, "
    "p_answerable DOUBLE, target_metric STRING, p_ambiguous DOUBLE, "
    "stratum_accuracy DOUBLE, reason STRING"
)
try:
    (
        spark.createDataFrame(table_rows, RESULTS_SCHEMA)
        .write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(RESULTS_TABLE)
    )
    print(f"Wrote {len(table_rows)} rows to {RESULTS_TABLE} (router {MODEL_NAME} v{router_version}).")
except Exception as exc:
    print(f"WARNING: could not write {RESULTS_TABLE} ({_short_err(exc)}) — summary above stands.")

# COMMAND ----------

# MAGIC %md ## Closing — what this experiment does and does not show
# MAGIC
# MAGIC **Design, honestly.** The noise set is small (n=6), so the deflection rate is a
# MAGIC demonstration, not a tight estimate — one question flipping moves it by ~17 points.
# MAGIC Deflection (plus the quota it saves and the clarify-on-collision behavior) is the
# MAGIC router's PRIMARY value: Genie-alone deflects 0/6 by construction, so any deflection
# MAGIC at a low false-reject rate is real, structural lift. Accuracy on answerable questions
# MAGIC is **unchanged by design** — Genie still answers every question the router passes
# MAGIC through, so per-stratum accuracy transfers from the joined eval run; the router can
# MAGIC only *lose* answerable questions (false-rejects and clarifies, both reported and both
# MAGIC scored against Arm B). End accuracy is JOINED from the most recent eval run rather
# MAGIC than re-measured here, because eval runs are quota-expensive and serialized; the
# MAGIC routing pass itself is deterministic, so run-to-run variance (the `phase_f_variance`
# MAGIC n>=3 protocol) lives in the Genie eval history this notebook consumes, not in the
# MAGIC routing. Net: Arm B trades a measured false-reject risk on answerable questions for
# MAGIC structural noise deflection, quota savings, and ask-don't-guess collision handling —
# MAGIC and the usefulness metric prints that trade instead of hiding it.
