# Sovereign E2E Test Protocol

## Purpose

Prove that the production Sovereign workflow is executable end to end as an LLM-hosted system, rather than merely described by documentation or deterministic runtime code.

## Canonical execution model

One enabled ChatGPT automation wakes one LLM host. The host executes the cognitive graph sequentially. Specialist passes are logically independent because they receive an immutable portfolio snapshot and their own research question, and sibling conclusions stay hidden until Evidence Arbitration.

Physical parallel model workers are optional. They are not a prerequisite for an E2E pass.

## Required stages

`GitHub read -> fresh IBKR -> immutable ex-ante snapshot -> Portfolio Agent -> Research Director -> selected isolated specialist passes -> Evidence Arbitration -> Portfolio Fit -> Counterfactual -> Adversarial Re-derivation -> Governance -> Decision -> Learning/Audit -> Meta-Research -> Self-Improvement -> GitHub write`

A stage counts as executed only when the LLM host actually performed it and recorded its output. A Python function, documentation reference or planned handler is not evidence that a cognitive stage ran.

## Ex-ante / ex-post proof

Before any outcome is observable, freeze and persist the decision snapshot identity. Later outcome records must point back to the decision ID and snapshot hash. Later facts may evaluate an earlier decision but must never be inserted into its original evidence set.

A counterfactual is admissible only when the alternative was explicitly feasible and known at the decision timestamp. Later-discovered candidates cannot become hindsight alternatives.

## Pass isolation proof

For each specialist pass, record:

- run id;
- role id;
- exact inputs received;
- tools actually consulted;
- observations and sources;
- candidate IDs;
- blockers and unknowns;
- execution order;
- next actions.

Before arbitration, a specialist pass must not receive another specialist's conclusion, ranking, thesis, probability or confidence.

## Tool proof

The E2E run should use IBKR directly for the live fields it exposes. Web search is a first-class open-ended research tool and should be invoked when the Research Director determines that current external evidence materially improves a research question. Other connected research capabilities may be selected when justified.

The run must never claim use of a capability that was not actually consulted.

## Multi-run test set

Run at least five cycles:

### Run A - smoke

Prove GitHub loading, live IBKR refresh, immutable snapshot construction, at least one isolated research pass, downstream decision stages and a durable result.

### Run B - dynamic research

Prove that the Research Director selects a non-fixed set of research capabilities and can add a newly invented research role when justified. Prove Web/other external research can be selected dynamically.

### Run C - memory and reconciliation

Prove the next cycle reads durable findings from the prior cycle and that fresh IBKR state overrides stale repository account state. Preserve any discrepancy rather than rewriting history.

### Run D - failure/learning

Prove a missing or failed specialist is recorded explicitly, downstream gates fail closed where required, and Learning/Audit records what happened without fabricating an output. Also prove Meta-Research marks economic effectiveness as `insufficient_outcomes` when no attributable outcome sample exists.

### Run E - self-improvement mutation loop

Deliberately inject at least three distinct, reproducible process defects across separate synthetic or shadow runs without touching production brokerage execution. Examples: stale research routing, a missing specialist capability, and a memory retrieval miss. Each defect must create its own FailureRecord and stable fingerprint. At least one defect must recur across multiple runs so Meta-Research can distinguish a pattern from a one-off event.

The host must then diagnose the recurring pattern, propose a falsifiable mutation, create a candidate branch from the exact production parent, apply only allowed targets, run sandbox/replay and adversarial checks, and evaluate the candidate out of sample. First prove the negative path: insufficient sample or counter-metric regression keeps the candidate in `testing`/`rejected` and leaves production unchanged. Then use a controlled replay dataset with enough independent observations to satisfy the gates and prove the positive path: the candidate becomes eligible, is versioned, and records parent/candidate commit identities. A promotion test may use a sandbox branch; it must never submit a live IBKR order.

Finally induce the candidate's documented rollback condition in a controlled environment. Verify that the active mutation is retired, the exact prior production version is identified for restore, the rollback reason is preserved, and no historical audit or ex-ante record is rewritten.

## Success criteria

An E2E run passes when:

1. the live IBKR connector is successfully queried before portfolio-sensitive reasoning;
2. the cognitive stages are actually executed by the LLM host;
3. selected specialist passes are sequential and isolated;
4. tool usage and skipped work are recorded honestly;
5. evidence arbitration sees the specialist outputs only after independent derivation;
6. counterfactual and adversarial stages run before a serious recommendation;
7. ex-ante/ex-post linkage is preserved and hindsight leakage is rejected;
8. Meta-Research measures process/economic effectiveness without inventing outcomes;
9. Self-Improvement converts material recurring defects into failure records and, when evidence is sufficient, versioned candidate mutations with negative and positive gate tests;
10. no live brokerage order is submitted;
11. durable GitHub state records the run result and its causal references;
12. the next run can consume the persisted result;
13. deterministic integrity checks do not contradict the recorded execution state;
14. a controlled rollback restores the prior effective version without rewriting history.

## Budget behavior

Latency is acceptable. If the host reaches a context, tool or execution limit, it must prioritize the highest decision-value passes, record skipped passes and reasons, and never claim skipped work executed.

## Interpretation of test results

A successful manual host run proves the connector/tool path and the sequential cognitive contract in this environment. It does not prove that the platform exposes persistent independent model processes or unconstrained compute. Those remain optional infrastructure optimizations.

Economic self-improvement is a separate claim. E2E execution can prove the learning pipeline runs, but strategy improvement requires attributable outcomes and evidence over time.
