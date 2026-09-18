# Adaptive Experiment Protocol v1

The research system treats strategy discovery as an experiment loop, not as a fixed list of strategies.

## 1. Strategy genome
Every experiment describes a strategy as a composable object:

- thesis: what is mispriced and why
- signal: observable evidence that should create the edge
- mechanism: why the signal should predict the outcome
- universe: securities, markets or instruments
- direction: long, short, relative-value or hedge
- expression: shares, option, spread, ETF, future, FX or combination
- horizon: expected holding period
- regime: conditions in which the edge should work or fail
- entry/exit logic: model-described, not assumed from a hardcoded playbook
- risk: tail, liquidity, assignment, gap, model and operational risks
- capital use: cash, margin and opportunity cost
- benchmark: the best current alternative use of the same capital

A strategy may combine existing components or invent new ones.

## 2. Candidate generation
Each discovery cycle should search several transformations rather than repeatedly asking for stock ideas:

1. mutate: alter horizon, instrument, signal or regime assumption
2. recombine: join signals or strategy families that have not been tested together
3. invert: ask whether the opposite trade is better
4. substitute: express the same thesis with a different instrument
5. neutralize: remove an unwanted factor, beta or concentration
6. hedge: turn an existing risk into a research opportunity
7. cross-market: search relative-value relationships across assets, sectors, currencies or geographies
8. event-shift: test pre-event, post-event and delayed reactions
9. volatility: test whether implied/realized volatility, skew or term structure changes the best expression
10. source-arbitrage: test whether disagreement between data sources contains information

Do not force novelty. Stop generating when additional candidates are duplicates, weak variations or unsupported stories.

## 3. Experiment record
Every non-obvious candidate creates a compact experiment record containing:

- experiment_id
- created_at
- strategy_id
- hypothesis
- mechanism
- expected edge
- strongest counter-hypothesis
- data required
- benchmark/counterfactual
- evaluation method
- expected risks
- allowed risk state
- evaluation window
- success criteria
- failure criteria
- current status: proposed, testing, promising, inconclusive, promoted, retired
- caused_by record ids

## 4. Evaluation discipline
Use the strongest available evaluation method for the question: historical analysis, walk-forward test, simulation, paper observation or small-risk live evidence.

Where historical testing is used, check for look-ahead bias, survivorship bias, selection bias, missing-data effects, transaction costs, slippage, liquidity and realistic execution assumptions.

Always compare against the best alternative, not only against zero return. Portfolio-aware benchmarks are preferred over generic benchmarks when capital would otherwise be deployed elsewhere.

Evaluate return, downside, drawdown, volatility, tail behavior, turnover, liquidity, capital usage, correlation to the existing portfolio and stability under reasonable changes to parameters.

## 5. Promotion
Promotion from `experimental` to `active` requires evidence that the strategy has a repeatable advantage over the relevant counterfactual and that its risks remain acceptable in the actual portfolio.

The promotion record must state:

- evidence supporting adoption
- evidence against adoption
- why the result is not explained by a single lucky period
- portfolio impact
- implementation constraints
- what would cause retirement
- rollback path

The evidence standard should become stricter for strategies with higher tail risk, leverage, assignment exposure, poor liquidity or model uncertainty.

## 6. Retirement
Retire a strategy when evidence shows persistent underperformance, the mechanism is falsified, implementation is unreliable, the thesis is structurally obsolete, or a better expression dominates it after risk and capital costs.

Retirement is not deletion. Preserve the original hypothesis, evidence and reason for retirement so the strategy can later be reconsidered under a different regime.

## 7. Meta-experiments
The system may experiment on the research process itself:

- score weights
- candidate-generation methods
- source routing
- research depth
- benchmark selection
- thesis freshness logic
- portfolio-fit methods
- option-structure selection
- goal quality
- evaluation methods

A meta-experiment must be evaluated against the previous process, not merely judged on whether it produces more output.

## 8. Anti-overfitting rule
A result that depends on one ticker, one event, one parameter value or one short observation window is evidence for further research, not proof of a general strategy.
