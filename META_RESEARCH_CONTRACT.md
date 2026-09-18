# Meta-Research Contract

## Purpose

Meta-Research is an LLM-hosted layer above ordinary investment research. Its job is not to choose trades. Its job is to determine whether the system is making better decisions over time, why it succeeds or fails, and what evidence-gated changes should be tested.

## Inputs

Meta-Research receives:

- immutable historical decision records;
- ex-ante snapshots and hashes;
- linked ex-post outcomes, when observable;
- counterfactual candidates that were demonstrably feasible at decision time;
- research traces, including specialist/tool usage and skipped branches;
- calibration observations;
- experiment results and current active brain versions;
- integrity incidents and unresolved data-quality conflicts.

It must preserve the ex-ante/ex-post boundary. Later information may evaluate an earlier decision but may not be inserted into the earlier decision's evidence set.

## Core questions

For every sufficiently observed sample, ask:

1. Did the decision produce the intended result relative to its stated benchmark and risk?
2. Was the opportunity set complete enough to judge regret?
3. Were probabilities or confidence statements calibrated?
4. Which research branches or tools changed the decision, and which repeatedly added no information?
5. Did adversarial re-derivation find real weaknesses?
6. Were missed opportunities caused by research gaps, portfolio constraints, bad instrument choice, stale data, or an incorrect thesis?
7. Are proposed changes supported out of sample rather than by hindsight?

## Metrics

The deterministic runtime computes descriptive measures such as:

- attribution completeness;
- realized return and experienced risk;
- benchmark-relative result;
- opportunity regret against feasible ex-ante alternatives;
- calibration hit rate, Brier score and log loss;
- research/tool usage and decision-delta traces;
- measured-vs-unmeasured decision counts.

The LLM interprets these metrics and proposes hypotheses. It must not convert a small sample into a production conclusion.

## Research efficiency

Each research pass records whether it was cited by the final decision, whether it materially changed the candidate set or conclusion, what tools it used, and whether it was skipped. Meta-Research may propose retiring, combining or creating research capabilities, but only through the same experiment and promotion gates used for strategies and prompts.

The baseline specialist catalogue is not a ceiling. A new capability is valid when the current decision question exposes information that existing branches cannot adequately test.

## Decision regret

Regret is computed only from alternatives that were feasible and identified as candidates at the original decision timestamp. Their later realized value may be used for ex-post evaluation. An alternative discovered only after the decision is not permitted to become a hindsight counterfactual.

## Learning and experiments

Meta-Research may propose:

- a prompt variant;
- a strategy mutation or recombination;
- parameter changes;
- a new research capability or tool-routing policy;
- a different experiment design;
- a new goal or monitoring question.

Every proposal must state the expected effect, counter-metric, sample requirement, evaluation window and rollback condition. The deterministic gate decides only whether the mechanical promotion requirements are satisfied. The LLM supplies the hypothesis and interpretation.

## Promotion policy

No production change is promoted from a single successful decision. Candidate changes remain experimental until the applicable sample, counter-metric and out-of-sample requirements are met. Safety constraints, broker authority, historical audit records and human execution boundaries are immutable.

## Capacity and autonomy

Meta-Research is part of the same scheduled LLM task. It is not a second scheduler. When capacity is limited, the host prioritizes the highest decision-value analysis and records skipped work.

The human should constrain the system with a small constitution: mission, authority boundaries, safety and audit requirements. Research methods, specialist roles, tool selection, strategy families, hypotheses and experiments remain mutable within those boundaries.

## Required output

Each Meta-Research pass should return structured fields equivalent to:

`run_id`, `observed_decisions`, `measured_outcomes`, `attribution_status`, `calibration_status`, `research_value_findings`, `failure_patterns`, `missed_opportunities`, `hypotheses`, `experiments_proposed`, `promotion_blockers`, `brain_version`, `integrity_status`, `next_actions`.

The host must persist supported conclusions and proposals to GitHub with causal links. Meta-Research may never rewrite historical decisions or silently upgrade an unknown outcome into success or failure.
