# Outcome-Linked Calibration v1

Calibration measures whether the research system's predictions and process choices are useful. It does not optimize for prediction volume.

## Required dimensions

For each closed or otherwise measurable recommendation, record:
- original score and score bucket
- strategy id/version
- instrument / expression
- sector and region
- holding-period bucket
- source mix
- expected return and downside range
- realized result when observable
- counterfactual result
- maximum adverse excursion when available
- thesis validation state
- execution state

## Ex-ante registration contract

When current evidence supports a falsifiable numeric view, the host registers
it before outcomes are observable. The immutable record freezes:

- the durable opportunity and decision snapshot link
- the numeric metric, unit, baseline, exact observation source and field
- the future target timestamp
- either a strict up/down event (ties resolve false) or an inclusive range
- confidence probability
- optional benchmark and unexecuted entry context
- risk assumptions, portfolio context, invalidation condition and evidence

A benchmark is context for benchmark-relative analysis; it does not redefine
the forecast event. A revision is a new record with
`supersedes_forecast_id`. Invalidation is measured as a separate outcome
disposition and never removes the original forecast from calibration.

Each forecast freezes `observation_window_seconds`. At maturity the host
references a current connector tool call; the runtime extracts the frozen
field and computes the outcome. A forecast whose window closes without a
measurement is overdue and remains in the matured denominator. It does not
block a distinct future measurement event. Superseding an overdue forecast
neither resolves nor retires it; exact event duplicates remain forbidden.

## Metrics

Track by bucket:
- hit rate / directional accuracy
- calibration of score to outcome
- median and distribution of realized return
- downside / drawdown
- excess versus best counterfactual
- turnover and capital usage
- false-positive rate
- avoided-loss rate for rejected ideas
- source contribution

## Empirical dataset feedback

`FEEDBACK.json.empirical_calibration` joins immutable forecasts to computed
forecast outcomes and, only when cycle identity matches exactly, instruction
reconciliations. It exposes:

- persisted probability calibration as the sole Brier/log-loss source
- direction-only recommendation/acceptance/rejection forecast rates
- range forecasts separately through probability calibration
- unit/kind/direction-stratified observed deltas
- accepted unchanged/modified, submitted, executed and no-fill counts
- unmatched forecasts, unmatched reconciliations, open, overdue, invalidated
  and disputed records

These are descriptive claims. A forecast resolving true does not prove a
trade would fill or make money. Rejected forecast true rate is not a trade
counterfactual. Operator quality is not scored. Small or absent denominators
remain `insufficient_data`, and no metric changes prompts, strategies or
weights automatically.

## Rules

1. Do not change weights from a single result or short sample.
2. Require out-of-sample or forward evidence for material parameter changes.
3. Separate selection skill from execution luck.
4. Compare results with the opportunity actually available at the decision time.
5. Preserve failed and rejected ideas; they are calibration data.
6. Do not count unrealized mark-to-market as a completed outcome unless the evaluation explicitly uses mark-to-market.
7. Measure the exact source and field frozen at registration; do not choose a
   more favorable source after the target date.
8. Preserve superseded and invalidated forecasts in the denominator or a
   separately reported disposition bucket. Never silently drop them.

## Promotion threshold

A strategy or scoring change requires repeatable advantage over its benchmark/counterfactual, stability across reasonable parameter changes, and acceptable portfolio risk. Exact thresholds are agent-selected and must be documented for each experiment.

## Current calibration state

The persistent state correctly starts with `insufficient` calibration until a meaningful outcome-linked sample exists. Seed priors are not treated as learned weights.
