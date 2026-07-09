# Interview Pitch (Sr. Resident Solutions Architect)

## The 60-second version

"Human documentation is where semantic layers go to die. Every enterprise BI rollout
fails the same way: the warehouse is right, the vocabulary is wrong — a wealth advisor's
'liquid assets' and a branch manager's 'available balance' resolve to different physical
columns, and the AI/BI layer confidently answers the wrong question.

I built an autonomous context engine on Databricks that treats Genie thumbs-downs as a
real-time telemetry stream. It clusters failed interactions, extracts term-to-entity
mappings with LLM functions, scores them by authority, frequency, and freshness, and —
behind a governed approval gate with full audit lineage and benchmark-gated rollback —
programmatically heals Unity Catalog comments, Metric View synonyms, and the Genie space
itself. Baseline benchmark accuracy was X%; after one autonomous healing cycle it was Y%,
with zero manual metadata edits.

Databricks announced Genie Ontology while I was building this. I'd independently
converged on the same conviction — context should be learned from usage — and shipped it
on public GA APIs."

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
