# Roadmap v3: From Validated Prototype to Living System

Honest calibration first: the v2 system's *components* are documented Databricks
patterns (medallion, expectations, Genie best practices). The *system* — a closed
healing loop with stratified benchmark gating, labeled-chaos DQ scoring, poison-term
governance, and a measured +0%-synonyms finding — has no public equivalent we found in
research (closest: Databricks' first-party "Analyze Space Usage" and the Genie
Ontology preview, both of which validate the direction without shipping the loop).
The credibility attack surface is therefore not novelty — it's *statistical rigor and
scale*. v3 closes exactly that.

## Anti-"vibe-coded" hardening (ordered by credibility-per-hour)

1. **Eval variance (n≥3 repeats per phase).** Genie is nondeterministic; single runs
   invite the "you got lucky" critique. Repeat evals, report mean ± range and exact
   counts. *(Shipped: `phase_f_variance.py` — see evidence log.)*
2. **Benchmark scale: 22 → 80+ questions** (500 allowed). Generate candidates with
   `ai_query` paraphrasing, hand-certify each golden — LLM drafts, human certifies,
   which is itself the HITL story.
3. **Scaled persona sessions (the interaction corpus).** N scripted sessions × M
   paraphrases per intent through the Conversation API on a nightly schedule
   (~5 q/min → 100-300 questions/night within fair use). Every interaction lands in
   `autopilot_telemetry` — real logs at training scale, not a 26-question manifest.
4. **The rollback drill, performed.** Inject a deliberately wrong certified definition,
   watch the benchmark gate catch the regression and restore the prior
   serialized_space + YAML. One run of this converts "rollback semantics" from a
   claim into evidence.
5. **Drift over time.** Producer v3 emits a new event schema and personas adopt new
   jargon mid-corpus on a schedule; the weekly flywheel catches it. A living system
   heals drift it has never seen; a demo heals the drift it was built for.
6. **Identity honesty.** Free Edition runs one PAT identity; persona attribution comes
   from the fleet manifest. Say so in the docs (done); on a paid workspace, personas
   become real workspace users and the limitation disappears.

## The semantic-prediction model (v3 centerpiece)

Train on the interaction corpus to predict incoming semantics BEFORE Genie answers:

- **Corpus:** `autopilot_telemetry` (question, generated SQL, feedback, corrections)
  from scaled persona sessions — thousands of rows, produced by the system itself.
- **Labels:** resolved target metric/entity (from successful SQL + certified
  corrections); answerability (from feedback); ambiguity (from poison-term conflicts).
- **Architecture (all Databricks-native):**
  1. Embed questions with a Databricks-hosted embedding model (`ai_query`).
  2. **Vector Search index** (Free Edition: 1 endpoint — currently unused capacity)
     over historical question→resolution pairs: retrieval-augmented semantic memory.
  3. Classifier head (MLflow-registered, UC model registry) predicting
     {target_metric, p_answerable, p_ambiguous} for each incoming question.
  4. Router: high-confidence → pass to Genie with retrieved few-shot context;
     ambiguous → clarify; low-answerability → reject with reason. The quality.py
     heuristic becomes the cold-start fallback.
- **Eval:** the same stratified benchmark, now scoring the router+Genie system vs
  Genie alone — a third experimental arm for the evidence log.
- **The narrative:** the flywheel stops being reactive (heal after failure) and
  becomes predictive (route before failure) — which is precisely the trajectory
  Databricks is on with Genie Ontology's authority-ranked, usage-refreshed context.

## Strategic alignment (why this delights Databricks)

Their announced direction: Genie One as the single NL front door, Ontology as
auto-learned context ranked by authority/usage/freshness, Agent Bricks for governed
agent construction, Lakebase as the agent-state store. v3 is a customer-side proof of
that exact thesis on public GA APIs — the kind of asset an RSA uses to (a) prove the
concept on a skeptical account today and (b) position the account's migration onto
Ontology as it GAs. The pitch to the panel: "this is your roadmap, de-risked from the
outside, with evidence."

## Sequencing

- **G1 (days):** variance runs · rollback drill · benchmark expansion to 80 · sample-question curation *(partially shipped)*
- **G2 (week):** scaled persona session engine + nightly schedule + corpus growth
- **G3 (week):** Vector Search semantic memory + router model + third experimental arm
- **G4 (ongoing):** drift injection cadence, weekly flywheel schedule, dashboard trend accumulation
