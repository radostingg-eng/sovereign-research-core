# Hard-Have / XL Implementation Contract

This document converts the delivery ledger into executable acceptance criteria. Documentation alone does not satisfy an item.

## Portfolio risk and allocation

- Assignment exposure is calculated from every verified short put, by underlying, currency and expiry.
- Cash capacity, margin capacity, assignment capacity and economic risk capacity are separate metrics.
- Covered calls are evaluated for premium yield, upside cap, assignment value and opportunity cost.
- Legacy positions are triaged using thesis state, size, liquidity and evidence status; loss alone never determines action.
- Stress analysis includes correlated portfolio shocks and option assignment scenarios. Simple NAV shocks are a lower-bound diagnostic, not an option-risk model.
- Capital allocation compares hold, add, trim, replace, hedge, defined-risk options and wait using expected return, risk, capital use, correlation and liquidity.

## Research breadth

The discovery pass must consider, where supported by available data: equities, ETFs, equity options/spreads, volatility structures, FX, futures/futures options, hedges, relative value, event-driven, value, quality, GARP, special situations, buybacks, restructuring, insider/public disclosures, institutional positioning, short interest, sentiment/crowding, momentum, mean reversion and macro/factor exposures.

An instrument family is not considered evaluated merely because it appears in a list. A candidate needs evidence, portfolio fit and a counterfactual.

## Acceptance gates

1. Fresh IBKR state is present before account-sensitive decisions.
2. Independent market-data checks are used when available.
3. Stale/future/conflicting data are surfaced, not silently averaged.
4. Serious recommendations include a best counterfactual and hold/wait alternative.
5. Material changes have version, evidence, counter-evidence, evaluation window and rollback condition.
6. No code path submits a live brokerage order.
7. Tests cover deterministic calculations and fail-closed cases.
8. Current external-source limitations remain explicitly visible.

## Current limitations

- Live broker execution remains deliberately disabled.
- Longbridge account scope and some option quote entitlements may remain unavailable; Alpaca indicative option data is not treated as identical to executable quotes.
- Full futures/futures-option discovery cannot be marked complete unless an available source supplies the required contracts, prices, liquidity and margin fields.
