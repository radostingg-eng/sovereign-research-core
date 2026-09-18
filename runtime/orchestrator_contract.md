# Agent Orchestration Contract

The scheduled Sovereign Investment Orchestrator is the control plane. GitHub owns strategy logic and durable state.

## Run graph

`IBKR refresh -> Portfolio Agent -> Market Scout -> Research Director -> specialist fan-out -> evidence arbitration -> portfolio fit -> counterfactual -> adversarial re-derivation -> decision -> learning/audit`

## Specialist branches

- value
- quality_garp
- options
- futures_macro
- relative_value
- event_special
- ownership
- sentiment_momentum
- hedging
- thesis_inversion

Market Scout records the host-chosen discovery scope, research budget, concrete
source-backed calls, and stable candidate identities. It may honestly return
zero candidates. The runtime validates this evidence and accounting but does
not choose, rank, or cap investment ideas.

The Research Director selects branches from Scout candidates, portfolio gaps, unresolved questions, expected decision value and source availability. Branches receive immutable run inputs and must return structured candidates; they must not submit orders.

## Candidate contract

Each candidate should contain: `candidate_id`, `thesis`, `instrument`, `expected_return`, `risk`, `capital_usage`, `evidence_status`, `evidence_ids`, `counter_hypothesis`, `portfolio_fit`, `best_counterfactual`, and `falsifier`.

Missing fields remain missing. No agent may invent market data, Greeks, FX, tax rules, probabilities or correlations.

## Fan-in gates

Only candidates with verified/cross-checked evidence and known assignment capacity can proceed. Ranking must include explicit hold/wait and alternative capital uses. Serious recommendations require adversarial re-derivation before becoming reviewable instructions.

## Safety boundary

The orchestrator can produce a reviewable IBKR instruction, but there is no automatic live-order submission path. Execution remains a user-reviewed step.

## Parallelism

When the host supports parallel agent execution, specialist branches may run concurrently. If it does not, the same branches run as isolated sequential passes over the same immutable inputs. Parallelism changes latency, not the decision contract.

## Learning mode

The same orchestrator owns weekly learning: outcome review, calibration, stale-thesis review, source usefulness, representative adversarial re-derivation and evidence-backed process changes. The scheduler does not contain this strategy logic.
