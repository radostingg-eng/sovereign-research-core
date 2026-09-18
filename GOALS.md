# Agent-Authored Goals v3

Goals are generated from the current system state, not copied from a static checklist.

The append-only audit journal is the live authority for goals created by host
cycles. Files under `goals/` are historical seed/import records and grading
history; they do not override a journal-backed open goal.

## Goal contract

Each cycle may produce a small set of goals chosen by the agent. Each goal contains:

```json
{
  "goal_id": "string",
  "created_at": "timestamp",
  "category": "agent-invented category",
  "statement": "what the agent intends to improve",
  "deadline": "timestamp",
  "success_metric": "measurable quantity",
  "success_target": "agent-selected target",
  "partial_target": "agent-selected partial-success boundary",
  "evaluation_rubric": "brief grading method",
  "metric_type": "controllable|observable|mixed",
  "baseline": "known starting point or unknown",
  "direction": "higher_is_better|lower_is_better",
  "caused_by": ["record ids"]
}
```

## Goal quality

Goals must be grounded in the current portfolio and research state. The agent should reject goals that depend on impossible prerequisites, presume future market outcomes, or merely restate activity without a learning objective.

At least one goal should address financial or portfolio quality when the current state provides a measurable way to do so, but the system must not manufacture a numeric target solely to satisfy a quota.

Separate:

- **Controllable:** research completed, experiments evaluated, theses refreshed, evidence gaps closed, recommendations compared with alternatives.
- **Observable:** P&L, drawdown, exposure, realized volatility and market outcomes.
- **Mixed:** decisions whose outcome depends on both agent behavior and market response.

## Grading

At or after the deadline, a measured closure supplies fresh evidence and a
finite observed value. The runtime compares that value with the immutable
`partial_target` and `success_target`, using the declared direction, to compute
`met`, `partially_met`, or `missed`. Equality with the success boundary is
`met`; equality with the partial boundary is `partially_met`.

An `invalidated` closure is allowed before or after the deadline only when
current evidence shows that the premise became ungradable or irrelevant.
Poor progress is a measured result, not invalidation.

Every closure includes causal analysis of what worked, what failed, the
decisive cause, a counterfactual, and the next process change. The journal
stores lifecycle `status: closed` separately from the runtime-computed
`terminal_status`.

Ungraded expired goals are an integrity signal. They should reduce confidence in the system's self-assessment and trigger process repair rather than being silently discarded.

## Progress events

After creation, a later cycle may append one progress event for the open goal.
The host supplies the existing `goal_id`, cycle observation time, finite numeric
value, assessment, and current-cycle evidence. It cannot replace the goal
snapshot or claim a terminal status.

The runtime copies the immutable creation snapshot into the event and computes
the previous value, delta, and direction-aware distance remaining to the
target. Values may improve, remain flat, regress, or pass the target. Every
progress event remains `open`; only terminal grading may close a goal.

Expired open goals may still receive progress. This prevents a missed deadline
from making the journal unwriteable before the terminal grading slice exists,
while still exposing the goal as expired in feedback.

## Goal evolution

A goal generator can itself be evaluated. Track whether goals lead to useful experiments, better portfolio decisions, cleaner theses or measurable process improvements. Retire goal patterns that repeatedly create busywork or incentives to game metrics.

## Quality attribution

`FEEDBACK.json.goal_attribution` derives a census from the latest valid closed
state of each journal-backed goal. It keeps `met`, `partially_met`, `missed`,
and `invalidated` separate and groups those outcomes by:

- the immutable goal pattern: category, metric type, and direction;
- cause IDs recorded when the goal was created;
- evidence sources cited when the goal was closed.

Each cause or source counts at most once per goal. Missing creation provenance
reduces the reported coverage rather than being reconstructed from a copied
close snapshot. Malformed terminal states are reported as excluded.

These groups are descriptive associations, not source rankings or proof of
causation. Cause IDs mix recurring stage names with per-cycle finding and
decision-evidence IDs. Closure sources omit evidence cited only during
progress. Goals are sequential rather than independent samples, so the agent
must consider sample count and coverage before drawing a lesson.

## Incremental runtime rollout

The runtime supports one open, controllable, machine-gradable goal, append-only
progress observations, evidence-backed terminal closure, and derived
closed-goal attribution. No attribution block can prescribe a strategy or
portfolio action.
