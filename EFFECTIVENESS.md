# Decision Effectiveness and System Learning

This is the operating specification for measuring whether the Sovereign Investment System is making better decisions, not merely producing more research.

## Measurement boundary

Every decision gets an immutable ex-ante snapshot and hash. Later outcomes are linked by decision ID and snapshot hash. Later facts can evaluate the decision, but cannot be inserted into the original evidence set.

An outcome is measurable only when the linkage and timestamps are valid. Unobservable engagement remains `unknown`.

## Effectiveness dimensions

### Decision outcome

Measure realized return, experienced risk and benchmark-relative result when those values are attributable to the decision.

### Opportunity quality

Measure opportunity regret against candidates that were both feasible and known at the original decision timestamp. A later-discovered winner is not a valid hindsight counterfactual.

### Calibration

Store probabilities with outcomes and compute hit rate, Brier score and log loss. Do not change weights from small samples. The runtime exposes a sample gate; the LLM determines what the measurements mean and what should be tested next.

### Research value

For every research pass record whether it was completed or skipped, whether it was cited by the decision, and whether it changed the candidate set or conclusion. Tool usefulness is measured by information added, not by call count.

### Process quality

Audit whether the workflow actually refreshed IBKR, preserved isolation, reconciled conflicts, compared hold/wait, performed adversarial re-derivation and kept the human execution boundary intact.

## Meta-Research loop

After Learning/Audit, Meta-Research asks why the system made the decision it made and whether it could have reasoned better. It looks for false positives, false negatives, missed opportunities, calibration errors, stale data, weak counterfactuals, poor tool routing, unnecessary research and recurring failure modes.

Meta-Research may propose new roles, tools, strategies, parameters, prompts, goals or experiment designs. It cannot promote those changes by itself. Promotion remains versioned and evidence-gated.

## Current evidence status

The existing portfolio runs contain decision traces and no attributable completed user outcome sufficient for economic self-improvement. Therefore the system may measure process effectiveness and create experiments, but it must report economic effectiveness as `insufficient_outcomes` until the sample supports comparison.

This is not a failure of implementation. It is an explicit empirical gate that prevents the system from teaching itself from hindsight or from a single anecdote.

## Runtime support

`runtime/effectiveness.py` provides the deterministic primitives. `META_RESEARCH_CONTRACT.md` defines the LLM-host behavior. `runtime/orchestrator.py` includes `meta_research` as the final stage in the cognitive graph.
