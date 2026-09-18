# Source Routing and Arbitration v2

## Active source stack

| Domain | Primary | Secondary / cross-check | Rule |
|---|---|---|---|
| Account, positions, orders, fills, margin, buying power | IBKR | Persistent last verified snapshot | IBKR is the sole authoritative account source. Never substitute another broker for current account state. |
| U.S./HK/European price data | IBKR where available | Longbridge, Alpaca | Prefer fresh IBKR observations for instruments the account can access; use external feeds to identify feed/venue discrepancies. |
| Equity/options chains, Greeks, IV, OI and liquidity | IBKR where available | Longbridge, Alpaca | Prefer IBKR contract identity and executable-market fields. Missing fields remain unknown and block expression selection when material. |
| Futures / futures options | IBKR | External cross-check when available | IBKR is primary for contract discovery, quotes, liquidity and account-relevant margin. FOP ranking remains conditional on required fields. |
| Fundamentals / statements / filings | IBKR research capabilities where available | Longbridge, public authoritative filings/news | Use the strongest available primary evidence; external structured research is additive, not a portfolio-state dependency. |
| Analyst consensus / ratings | IBKR research capabilities where available | Longbridge, Next Stock | Treat as evidence, not truth. |
| Institutional / insider holders | IBKR research capabilities where available | Longbridge, public filings | Distinguish holdings from actual transaction evidence. |
| Short interest / short sales | IBKR research capabilities where available | Longbridge, public exchange/regulatory data | Use only when timestamp and source are clear. |
| News / catalysts | IBKR research capabilities where available | Longbridge, public web/news | Validate event date and primary-source claims. |
| Model/ranking signal | Next Stock | Fundamental evidence | Never allow a model score to override contradictory fundamentals without explanation. |
| Retail sentiment / crowding | Stocktwits | None required | Context only; never sufficient for high-conviction thesis. |
| Tax treatment | Current authoritative Czech sources | Professional advice when needed | Verify current rules; never hardcode tax law into strategy logic. |
| Durable memory / versions | GitHub | None | Persistent records are additive; history is never silently overwritten. |

## IBKR-first rule

IBKR is the system's account and execution-state authority and should also be the first research venue whenever the required research field is available through the connected IBKR capabilities. The system must not require Longbridge or another research provider merely to perform portfolio-aware research.

External sources are used for independent validation, alternative models, sentiment, or information that IBKR does not expose. A failed external source must not block an otherwise executable IBKR-first research run.

## Freshness policy

Every material finding stores `observed_at`, `source`, and a freshness class: `live`, `same_session`, `recent`, `stale`, or `unknown`.

Account-sensitive decisions require fresh IBKR state. If IBKR is unavailable, use the latest verified snapshot only for contextual analysis and explicitly block decisions that depend on current margin, orders, assignments or exact position quantities.

## Conflict policy

Do not average conflicting observations blindly. First classify the conflict:

1. timing mismatch
2. corporate-action / adjustment mismatch
3. venue/feed difference
4. definition mismatch
5. stale data
6. genuine disagreement

Then preserve both observations, select the better-provenanced value for the decision, and record why.

For price or options conflicts, compare timestamp, session, venue/feed, bid/ask and contract identity before interpreting the difference as a signal.

For fundamental conflicts, prefer primary regulatory/company disclosures over secondary summaries, then explain restatement, period, currency or definition differences.

## Source-arbitrage research

Disagreement between credible sources is itself a candidate signal. The agent may create an experiment when the disagreement has a plausible mechanism and can be evaluated out of sample. A raw discrepancy is not evidence of alpha.

## Removed / unavailable providers

The current active stack does not depend on previously considered paid or unavailable providers such as Financial Datasets, Massive or Quartr. The research process must not claim to have consulted them when they are unavailable.

## Failure behavior

A source outage creates an `incident` record. The agent should continue with unaffected parts of the workflow rather than fabricating a value or silently skipping a required gate.
