# Opportunity Scoring v3

Score only after portfolio context is known. The model should reason over the dimensions below, while the numerical weights and action bands are seed priors stored in `PARAMETERS.json` and may evolve through evidence-backed experiments.

## Dimensions

- valuation / margin of safety
- business quality / durability
- catalyst / time-to-thesis
- downside / loss asymmetry
- portfolio fit / diversification
- expected return
- capital efficiency
- liquidity / execution quality
- option pricing / structure edge
- sentiment / positioning confirmation

The weighted score is a ranking aid, not an automatic trading trigger. Do not assume that a single scalar score captures every trade; preserve the underlying evidence and uncertainty.

## Mandatory gates

1. **Portfolio gate:** quantify current exposure, correlated exposure, short-option assignment and covered-call opportunity cost.
2. **Thesis gate:** identify a specific mispricing, mechanism and invalidation condition.
3. **Instrument gate:** compare shares, options, spreads, ETFs, hedges and waiting when relevant.
4. **Evidence gate:** use independent evidence types where available. Sentiment alone cannot establish a high-conviction thesis.

## Penalties and uncertainty

Use explicit deductions for concentration, correlated risk, fragile balance sheet, poor liquidity, binary-event dependence, low-return locked capital, leverage/assignment mismatch and stale or broken theses when supported by evidence.

Record `confidence` separately from score: `low`, `medium`, `high`.
Record `evidence_agreement`: `aligned`, `mixed`, `conflicted`.
Record thesis freshness: `fresh`, `aging`, `stale`, `broken`.

## Counterfactual first

Every serious candidate must specify the best competing use of capital. The recommendation should explain why its chosen expression wins after risk, liquidity, taxes/fees where relevant, capital usage and portfolio correlation.

Examples include buying shares vs selling a put, holding shares vs covered call, single option vs defined-risk spread, adding to an existing thesis vs taking a new exposure, and trading now vs waiting.

## Option-specific evaluation

Compare strike, expiry, premium, implied vs realized volatility, skew, term structure when relevant, open interest, bid/ask quality, assignment economics, maximum loss, breakeven, capital requirement and opportunity cost.

## Calibration

Track predictions against later outcomes by score bucket, strategy family, instrument, sector, holding period, regime and source mix. Include avoided losses and rejected ideas where a defensible counterfactual exists.

Adjust scoring parameters only through a documented experiment with forward or out-of-sample evidence. Preserve prior versions for rollback.
