# Investment System Runtime

This directory contains deterministic, dependency-free plumbing used by the adaptive research agent.

## Implemented primitives

- Append-only causal record construction, canonical hashing, hash-chain verification, and dangling-parent detection.
- Source arbitration with source tier, confidence, freshness, future-observation rejection, stale-data rejection, and numeric conflict detection.
- Strategy schema validation plus mutation, recombination, and direction-inversion operators. All derived strategies are marked experimental and carry parent-version references.
- Outcome calibration with hit rate and Brier score.
- Leakage-safe long-only historical evaluation with explicit fees/slippage and transition-based turnover.
- Walk-forward train/test split generation.
- Counterfactual ranking and risk-adjusted comparison helpers.
- Ex-ante decision snapshot hashing and ex-post linkage validation.
- Decision-effectiveness measurement, including attributable return/risk, benchmark-relative return, feasible-counterfactual regret and aggregate sample status.
- Calibration ledger with Brier score, log loss, hit rate, reliability bins and an explicit sample gate.
- Research/tool value ledger measuring decision citation, decision-delta observations and skipped work.
- Sequential orchestrator dependency contracts with dynamic specialist identifiers and a final Meta-Research stage.
- Memory distillation governance: append-only Research Memory, bounded Active
  Brain admission, evidence-gated retirement, provenance validation,
  contradiction preservation, reconstruction coverage and compression-ratio
  measurement.

## Memory boundary

The LLM host owns semantic memory work. The runtime does not summarize, rank or invent knowledge. It verifies that LLM-produced memory objects have stable provenance, remain inside the Active Brain budget, and pass reconstruction checks before activation.

The intended memory hierarchy is:

`Constitution -> Active Brain -> Research Memory -> Raw Archive`

Normal runs retrieve selectively rather than loading the entire historical archive into context. Raw history remains immutable and retrievable.

Validated Research Memory versions are persisted separately from Active Brain.
Failed and experimental versions remain auditable but cannot replace the last
validated version. A stale retirement can be reversed by later validated
evidence; an archived ID is terminal. Feedback bounds the working set and
reports omitted counts rather than silently growing the host context.

## Architectural boundary

The LLM host performs investment reasoning, discovery, tool selection, research, interpretation, adversarial analysis, decisions, Meta-Research and memory distillation. This runtime validates and measures those outputs but does not substitute hardcoded investment judgment for missing model reasoning.

The runtime does not contain a live brokerage submission path and does not decide position sizes by itself. Research agents provide the hypotheses, signals, data and portfolio context.

## Example

```python
from investment_system.runtime import (
    backtest_long_only,
    walk_forward_splits,
    build_ex_ante_snapshot,
    active_brain_admission_check,
)

result = backtest_long_only(bars, signal, as_of="2026-09-15T20:00:00Z")
splits = walk_forward_splits(len(bars), train_size=252, test_size=63)
snapshot = build_ex_ante_snapshot(
    portfolio=portfolio,
    evidence=evidence,
    research_trace=trace,
    system_version="brain-v1",
)
admission = active_brain_admission_check(
    candidate_entries=entries,
    required_claim_ids=required_claims,
    distilled_claim_ids=distilled_claims,
    source_claim_ids=source_claims,
)
```

A future-dated bar relative to `as_of` raises an error. This is intentional: contaminated historical data must fail closed rather than silently enter a backtest.

Economic self-improvement remains sample-gated. The runtime reports `insufficient_outcomes` rather than manufacturing evidence when attributable decision outcomes are not yet available.
