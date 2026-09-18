# Adversarial Re-Derivation Protocol v1

The purpose is to detect reasoning drift and confirmation bias. The adversarial pass must not inherit the conclusion from the original recommendation.

## Trigger

Run for every serious recommendation, every material system change, and during weekly review for a sample of older decisions.

## Inputs allowed

- ledger findings cited by the decision
- current portfolio state and exposure map
- current market / options observations
- current source-routing rules
- user-stated constraints

Do not use the original recommendation prose except after the independent conclusion is recorded.

## Independent sequence

1. Rebuild the factual state from sources.
2. Identify the strongest bull thesis.
3. Identify the strongest bear thesis.
4. Identify the most damaging missing fact.
5. Re-test valuation and downside.
6. Re-test portfolio correlation and capital opportunity cost.
7. Re-test instrument choice against shares, options, defined-risk alternatives and waiting.
8. Re-test holding-period and tax implications without treating tax assumptions as law.
9. State what evidence would falsify the thesis.
10. Produce an independent action: `support`, `modify`, `reject`, or `insufficient_evidence`.

## Divergence classes

- `none`: independent result agrees with original conclusion and evidence.
- `evidence_drift`: new facts changed the answer.
- `reasoning_gap`: original conclusion relied on an unsupported inference.
- `portfolio_gap`: thesis may be sound but is inferior in the actual portfolio.
- `instrument_gap`: thesis is better expressed differently.
- `tax_or_cost_gap`: gross edge disappears after tax/fees/capital cost.
- `data_conflict`: sources disagree materially.

## Integrity scoring

Track:
- factual agreement
- thesis agreement
- action agreement
- evidence completeness
- counterfactual quality
- missing-risk count

Do not collapse these into a single score until enough outcome-linked observations exist.

## Record

Each run appends a compact audit record linking the original decision id, source finding ids, independent result, divergence class, and follow-up action.

This is operational as an agent procedure. It is not an independent always-running service, and the system must not describe it otherwise.
