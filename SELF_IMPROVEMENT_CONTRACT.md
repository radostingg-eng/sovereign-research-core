# Autonomous Self-Improvement Contract

## Purpose

Convert recurring audit failures into versioned, testable system mutations without allowing the learning loop to rewrite its constitution, historical evidence, ex-ante records, or broker execution boundary.

## Closed loop

`audit -> failure ledger -> diagnosis -> mutation proposal -> candidate branch -> sandbox replay -> adversarial validation -> out-of-sample evaluation -> promotion -> production version -> outcome measurement -> rollback when triggered`

The LLM host owns diagnosis, hypothesis formation, patch generation, tool selection and interpretation. Deterministic runtime owns identity, mutation-envelope validation, mechanical thresholds, provenance, versioning and rollback state.

## Failure ledger

Every material defect becomes a causal `FailureRecord` with a stable fingerprint, run/stage identity, failure class, symptom, supporting evidence, severity and parent records. Repeated fingerprints are grouped as recurring failure patterns. A missing or contradictory failure identity blocks mutation promotion.

## Mutation envelope

Every `MutationProposal` must provide:

- a unique mutation ID and parent system version;
- explicit targets and mutation type;
- failure IDs that caused the proposal;
- the candidate patch or configuration delta;
- expected primary effect;
- counter-metrics;
- minimum sample requirement;
- evaluation window;
- rollback condition;
- creation timestamp.

The constitution is immutable. Runtime refuses mutations targeting constitutional contracts, historical audit data, ex-ante evidence, or live order submission. The proposal must remain inside an explicit path allowlist.

## Testing

A candidate is first tested in sandbox/replay mode against a known baseline. Promotion requires all of:

1. sandbox success;
2. the required sample size;
3. positive primary metric delta;
4. no counter-metric regression;
5. out-of-sample evidence;
6. no rollback trigger.

Any patch touching the standing prompt must pass the trusted prompt
invariants, size with a two-kilobyte review reserve, and refusal-contract
checks in the sandbox before its
requested command runs. Deployment repeats that sandbox preflight before
changing a live checkout. A passing compile command cannot authorize an
invalid prompt.
After the live verification command, the prompt must still pass those checks
and match the candidate commit exactly. Otherwise deployment rolls back the
candidate instead of reporting it as verified.
Only the exact standing-prompt target can enter opt-in candidate execution
(`--allow-candidate-execution`); a mixed prompt/runtime proposal is refused.
With execution disabled, a valid host proposal is durably recorded without
running its patch. Sandbox evaluation never activates or promotes a prompt
without later outcome evidence and the separate deployment gate.

The LLM may define and interpret metrics, but cannot waive deterministic gates.

## Promotion and rollback

A passed candidate is never written directly over production. The host creates a candidate branch from the exact production parent, applies the exact proposal, runs the required checks, and persists the resulting commit identity. Only after the deterministic evidence gate passes may the host promote the candidate into `main` (normally by merging the validated candidate branch or applying the exact validated commit through GitHub). The promotion record must contain the production parent SHA, candidate SHA, evaluation, tests and mutation ID.

Each promoted mutation receives a monotonically increasing version and retains its parent version, full proposal, failure causes and evaluation. Only one runtime mutation is active at a time. A subsequent promotion retires the prior mutation. A triggered rollback retires the active mutation and records the rollback reason and the exact prior production version to restore.

Promotion does not rewrite prior decisions or audit history. It changes only the effective system version used by future runs.

## Autonomous operation

The production LLM host should run this loop after Learning/Audit and Meta-Research. When a material recurring failure exists, it should create one or more candidate mutations, evaluate them in isolation, and persist the result. When evidence is insufficient it must leave the candidate in `testing`, not force adoption. When evidence passes, it should promote the validated candidate and make the new version the next runtime used by the scheduler.

The system is allowed to improve the method of reasoning, research routing, prompts, memory policies, strategy logic and runtime code within the immutable constitution. It is not allowed to change the mission, authority boundaries, safety constraints, audit integrity rules or live execution boundary.
