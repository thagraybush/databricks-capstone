# Interview Pitch (Sr. Resident Solutions Architect)

## The 60-second version

"Business users don't know the schema — they say GMV, whales, take rate, and assume the
semantic layer automagically understands. I measured what actually happens on Databricks:
Genie answered documented questions at 75% but bleeding-edge business dialect at **40%**.

So I built an autonomous context engine that treats Genie feedback as a real-time
telemetry stream: it mines corrections, scores term-to-entity proposals by authority,
frequency, and freshness, and — behind a governed gate with audit lineage and
benchmark-gated rollback — heals Unity Catalog metadata, Metric View synonyms, and the
Genie space itself.

The experiment that matters: synonym-only healing produced **+0% lift** — every failure
was a definition gap, not vocabulary. Whales… top how many? Take rate… ratio of what?
When the human reviewer in my Lakebase HITL queue enriched proposals into certified
definitions — formula, threshold, units, output shape — bleeding-edge accuracy went
**40% → 80% with the clean control at 100%** and zero manual metadata edits beyond the
approvals. And poison terms — 'sales' meaning revenue to finance but unit volume to
merchandising — are detected as contradictory corrections and healed as disambiguation
instructions, never as synonyms.

Databricks announced Genie Ontology while I was building this. I'd independently
converged on the same conviction — context should be learned from usage, ranked by
authority — and shipped it on public GA APIs, on Free Edition, in three weeks."

## Level mapping (Sr. RSA criteria)

- **Owns large-scale technical direction:** end-to-end system across UC, Metric Views,
  Genie APIs, AI Functions, Asset Bundles — designed and shipped solo in 3 weeks.
- **Quantifiable impact:** before/after benchmark scorecard; time-to-context measured in
  minutes instead of documentation sprints.
- **Influences senior decision-makers:** the governance gate is the CDO conversation —
  autonomous AI change management a bank's compliance office can sign off on.
- **Cross-BU complexity:** the demo scenario IS cross-BU semantic conflict, mirroring
  my Intuit experience unifying historically separate domains for a $500M business
  (no Intuit data, code, or IP involved — clean-room rebuild on synthetic data).

## Anticipated panel questions

1. *Why not just document the schema properly up front?* — Documentation decays the day
   an org reorganizes; the flywheel prices that decay in and repairs it continuously.
2. *What stops a bad healing?* — Three things: the ≥2-distinct-user threshold, the
   confidence gate with human review below it, and the benchmark regression gate with
   automatic rollback. Every change carries evidence lineage in the audit ledger.
3. *How does this relate to Genie Ontology?* — Same direction, complementary altitude:
   Ontology is platform-native and preview; this is a customer-side pattern on GA APIs
   that a solutions architect can deploy today and migrate onto Ontology as it matures.
4. *Free Edition limits?* — Designed within them deliberately: pacing, batching,
   serverless-only, PAT auth. Constraint-driven architecture is the RSA job.
