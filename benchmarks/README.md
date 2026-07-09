# benchmarks — curated ground truth

The benchmark suite is the highest-authority telemetry in the system: conversational
corrections are hints, but a human-certified golden question is *ground truth*. Every
healing cycle is gated on these suites (stratified lift + no-regression + rollback),
which is why nothing enters a certified file without a human decision.

## The stratified design

| Stratum (`trap` key) | Role | Example |
|---|---|---|
| `false` — **clean** | Control: documented questions that must never regress | "What is total net revenue by country?" |
| `true` — **jargon** | Treatment: bleeding-edge business dialect, the healing target | "Who are our whales?" |
| `collision` | Many names → one metric (taxonomy mode 1) | "gross sales" = "GNS" = `gross_revenue` |
| `bad` — **noise** | Unanswerable/vague questions incl. the poison-term probe; excluded from Genie benchmarks by design, scored by the router/quality gate instead | "How are we doing?" |

Stratification makes lift *attributable per failure mode* — it is how the +0%
synonym-only finding was measurable at all. Golden questions must fully specify
output shape, or they measure shape-guessing rather than semantics
([../docs/semantic-failure-taxonomy.md](../docs/semantic-failure-taxonomy.md)).

## Contents

| File | Responsibility |
|---|---|
| `questions.yaml` | Banking (v1) suite for the `workspace.banking_gold` space |
| `retail_questions.yaml` | **Certified** retail suite: the core clean/jargon/collision/noise questions loaded to the live space |
| `retail_questions_draft.yaml` | Expansion candidates (LLM-drafted paraphrases + new intents) carrying per-entry certification state: `certified` / `rejected` / `needs_edit` |

## The certification workflow

```bash
make certify        # python -m genie_autopilot.certify [--stratum clean|jargon|collision|bad]
```

For each uncertified draft entry the CLI shows question, stratum, and note, then
**executes the golden SQL live** and prints the first rows — so the human decision is
"does this result answer this question?", not "does this SQL look plausible?". Keys:
`y` certify · `n` reject · `e` certify+flag-edit · `s` skip · `q` quit. Every decision
writes back to the YAML immediately (crash-safe; stop and resume anytime). Noise
entries certify as "yes, this is realistically unanswerable." LLM drafts, human
certifies — the HITL story applied to the eval suite itself.

## Syncing to the space

Certified questions are synced into the Genie space by patching
`serialized_space.benchmarks.questions` (etag-guarded, idempotent — only missing
questions are added; see `ensure_benchmarks` in
[../src/genie_autopilot/phase_e.py](../src/genie_autopilot/phase_e.py)), then the
suite is re-baselined with an eval run
([../src/genie_autopilot/evals.py](../src/genie_autopilot/evals.py) /
`make eval`). `evals.load_strata` maps question text → stratum from these YAMLs when
scoring, so file and space stay the single source of truth. The certified-expansion
arc (66-question baseline 65% → 70-question post-healing 73%, jargon 56% → 79% with a
flat clean control) is logged in [../docs/eval-evidence.md](../docs/eval-evidence.md).

Related: [failure taxonomy](../docs/semantic-failure-taxonomy.md) ·
[evidence log](../docs/eval-evidence.md) · [steward loop](../docs/steward-loop.md)
