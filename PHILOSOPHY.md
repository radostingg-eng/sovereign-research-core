# Philosophy: no ifs, AI decides

Adapted for the research system from the sibling trading engine's v4
vision (`ADR-0001` and `ADR-0007` in the engine repository). The engine
learned this the hard way over eighteen months of hardcoded thresholds
becoming silent ceilings on the brain's autonomy. This document brings
the same rule here before the research system re-learns the lesson.

## Rule

**Every strategic decision must be made by AI reasoning, not by a Python
`if`.** No hardcoded thresholds, no hardcoded rankings, no hardcoded
"safe defaults" that the model learns to stay inside. Ceilings on
autonomy become ceilings on quality.

Deterministic plumbing stays in code and is called mechanics. It is
allowed. Mechanics are things like: serializing an audit record, hashing
a chain link, refusing an envelope missing a required field, refusing a
second scheduled workflow, refusing a runtime module no caller can
reach. Mechanics constrain **outcomes**. Decisions constrain
**reasoning**, and decisions belong to the model.

## What this means, concretely

**In `runtime/`.** Code owns the shape of what the model produces
(envelope validation, chain hashing, adoption gates, scheduler
enforcement, receipt verification, memory-governance checks). Code
never picks a thesis, ranks a name, weighs a source against another,
or decides when to trade. If a review of a diff surfaces a Python `if`
that changes strategic behaviour based on prices, tickers, sentiment
scores, or portfolio state, that is the failure this document forbids.
File it as a bug or annotate it explicitly as mechanic.

**In `PARAMETERS.json` and `STATE.json`.** Numbers here are **seed
priors**, not enforced rules. `SCORING.md` calls this out already: the
weighted score is a ranking aid, not an automatic trigger. Parameters
can evolve through evidence-backed experiments (`experiments.py`); the
seed is where the system starts, not where it must stay.

**In the LLM host.** The host decides which specialists to run, which
sources to consult, when a source disagrees enough to block a
recommendation, when to defer, when to act. The scheduler wakes the
host; the host decides everything else.

**In the operator surface.** Kept intentionally narrow. A large
operator control surface produces a hardcoded menu by proxy, since the
host learns to serve it. The engine converged on three sliders and one
approval queue for exactly this reason. This repository will resist any
proposed control that would turn into a fifth slider or a
questionnaire.

## Anti-patterns, never reintroduce

- **Thesis wizards or "pick a strategy" screens.** The moment the
  operator has a menu, the model has a ceiling.
- **Hardcoded source weights.** `SOURCE_ROUTING.md` names sources by
  role, not by rank. The host arbitrates in prose, and the arbitration
  is auditable in the causal chain.
- **Hardcoded thresholds masquerading as "safe defaults."** RSI > 70,
  stop = -5%, max positions = 10. Each looks reasonable in isolation.
  Together they compose into the strategy the model is allowed to have.
- **Synthetic data fallback.** Missing data reports itself missing; the
  host decides skip / wait / degrade. Code never substitutes a stub.
  `SELF_INTEGRITY.md` "Research integrity" says the same in different
  words; this file names it a rule.
- **`if outcome == "rejected"` branches that decide the next action.**
  The runtime records the outcome; the host decides what it means.

## Anti-hallucination

Every finding, decision, outcome and lesson lives in the audit chain
with an explicit `caused_by`. The integrity gate refuses dangling
references. This is not a nicety. It is what stops the host from
inventing a source or a citation and shipping the result as fact.

The `envelope_hash` on coordination records serves the same purpose for
inter-agent messages. A helper agent's claim is only worth what its
envelope hash can prove.

## The escape valve

Deterministic mechanics that need to look like a decision get a comment:

```python
# philosophy-mechanic: <why this is plumbing, not judgment>
```

Reviewers reading a `philosophy-mechanic` annotation on a strategy call
should push back. Reviewers reading it on serialization, envelope
validation, or chain hashing should let it pass.

## Enforcement (what actually runs)

- `runtime/integrity.py` — chain, adoption, scheduler, referenced
  paths. Refuses new defects and refuses a baseline that lists something
  already fixed. Runs on every push.
- `runtime/coordination.py` — envelope validation and hash checking.
- `runtime/orchestrator.py` — dependency graph and fail-closed plan
  validation. No stage may be silently dropped.
- `runtime/cycle_receipt.py` — verifies the prior cycle's receipt before
  the next cycle runs, so the host cannot silently skip a stage.

None of these decide anything about what to trade. They constrain the
shape of the record the host must produce.

## Reversibility

Reversible only by contradicting the whole architecture. Every gate
above assumes the host is the decision owner. Reverting to
code-authored decisions would mean rewriting the runtime, not flipping
a flag.

When this document contradicts a gate, the gate wins.
