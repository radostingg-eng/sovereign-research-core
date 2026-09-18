# Strategy Genome v1

Strategies are represented as composable hypotheses rather than a hardcoded menu.

## Genome fields

```json
{
  "strategy_id": "string",
  "family": "agent-invented category",
  "thesis": "string",
  "signal": ["observable inputs"],
  "mechanism": "why the signal should create an edge",
  "universe": ["assets or markets"],
  "direction": "long|short|relative_value|hedge|mixed",
  "expression": ["shares|option|spread|etf|future|fx|combination"],
  "horizon": "agent-selected",
  "regime": ["conditions"],
  "catalyst": "string or null",
  "risk_sources": ["gap|liquidity|assignment|leverage|model|event|factor|other"],
  "capital_model": "string",
  "benchmark": "best competing use of capital",
  "invalidation": ["conditions"],
  "status": "experimental|active|retired"
}
```

## Mutation operators

The system may evolve a strategy by changing one or more genes:

- signal mutation
- signal recombination
- universe expansion or narrowing
- direction inversion
- instrument substitution
- horizon mutation
- regime segmentation
- catalyst timing shift
- hedge addition/removal
- factor neutralization
- capital-allocation change
- benchmark replacement

A mutation is a research hypothesis, not an assumption of improvement.

## Strategy graph

Strategies should be linked to the signals, instruments, regimes, findings and outcomes that support or contradict them. This creates a search space for discovering combinations that have not yet been tested.

The graph should make it possible to answer:

- which signals have never been combined?
- which strategies depend on the same hidden risk?
- which strategies work only in one regime?
- which strategies consistently outperform the same counterfactual?
- which strategies are repeatedly rediscovered and should be consolidated?
- which successful ideas have no active implementation?

## Promotion identity

A strategy keeps the same `strategy_id` while its version changes. Material changes create a new `strategy_version` and point back to the parent version. Historical versions are immutable.
