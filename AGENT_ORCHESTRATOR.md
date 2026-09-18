# Sovereign Agent Orchestrator

## Reasoning-effort requirement

Every orchestrated run must use the **highest available reasoning effort**. If the host exposes Think Harder or an equivalent high-effort mode, use it. If the scheduler cannot control reasoning effort, preserve the requirement in the task prompt and run the full workflow rather than shortening it.

## Purpose

This is the control plane for the single scheduled Sovereign Investment System task. The **LLM host is the cognitive orchestrator**. The scheduler wakes the host; the LLM decides what to research, which capabilities/tools to use, runs isolated research passes, arbitrates evidence and produces the decision. Deterministic runtime code validates structure and safety but never substitutes for model judgment.

## Execution model

One heartbeat invokes one LLM-hosted orchestrator. The canonical production execution model is **sequential isolated LLM passes**. Physical parallel worker processes are optional and must never be assumed.

The specialist passes are independent because each pass receives the same immutable portfolio snapshot and its own research question, while sibling specialist conclusions remain hidden until Evidence Arbitration. The runtime enforces this with dependency-scoped outputs.

Market Scout runs after the portfolio snapshot and before the Research
Director. The host chooses its discovery scope and research budget, records
concrete source-backed calls, and may return zero candidates with an explicit
rationale. Candidate order is not a ranking. Deterministic code validates
evidence, identity, graph edges, and budget accounting without choosing what
the host should investigate.

The Research Director dynamically chooses how many passes to run based on decision value, portfolio gaps, unresolved questions, source availability and host capacity. The baseline specialist catalogue is a capability map, not a mandatory list and not a ceiling. New research roles may be invented when justified.

The Research Director also commits a session-aware allocation plan across new
opportunity, existing opportunity, portfolio risk and follow-up work. The
runtime derives actual bucket usage from explicit links and validates only
accounting, references and variance. It does not score or rank candidates.
When markets are closed the host may use more capacity for broad discovery,
multiple directions or accumulated deep research; when markets are active it
may emphasize monitoring or follow-up. Current evidence, not a hardcoded
schedule, determines the mix.

**The selection is required input, not an optional argument.** `build_plan()` refuses to run a research cycle without an explicit `specialist_ids` selection. It previously defaulted to the entire catalogue with every pass required, producing a 23-stage all-or-nothing plan that does not fit in one host wake; an honest host could then only mark every stage blocked, which is why no complete cycle receipt existed. Do not restore that default. Code selecting specialists is code making a strategic decision, which `PHILOSOPHY.md` forbids. A cycle that genuinely needs no research pass sets `research_needed=False` rather than passing an empty selection.

## Fixed sequence

1. **Portfolio Agent** - refresh IBKR and build the exposure map, including assignment, covered-call caps, cash/margin, concentration and drawdown.
2. **Market Scout** - spend the host-chosen discovery budget on current source-backed scans, emit zero or more stable opportunity identities, and disclose any budget variance.
3. **Research Director Agent** - read Scout candidates plus GitHub state/audit, identify the highest-value questions and choose research capabilities/tools.
4. **Memory Retrieval / Context Builder** - retrieve only decision-relevant Active Brain and Research Memory. Descend to Raw Archive only when exact historical detail, original evidence, transaction records or audit reconstruction is needed. Never load the whole archive by default.
5. **Specialist Research Passes** - execute selected roles sequentially from the immutable snapshot. Consider value/FCF, quality/GARP, special situations/event-driven, options/volatility, futures/futures-options/macro, relative value/factor/hedging, insider/institutional/public disclosure, buybacks/restructuring, sentiment/momentum/mean reversion, cross-market and instrument substitution, plus any newly discovered role.
6. **Evidence Arbitration Agent** - reconcile findings against verified current evidence. IBKR is primary for fields it exposes; preserve unresolved conflicts and unknowns.
7. **Portfolio Fit Agent** - map candidates onto current exposure, assignment demand, correlation, capital usage, liquidity, holding period and tax context.
8. **Counterfactual Agent** - compare hold/wait and other credible uses of capital, including shares, options, defined-risk structures and hedges.
9. **Adversarial Agent** - independently rebuild serious recommendations from verified observations before seeing the original conclusion, then compare divergence.
10. **Governance Review Agent** - check integrity, constraints, experimental state, prompt/strategy changes and evidence for learning/promotion.
11. **Decision Agent** - output only `blocked`, `wait`, `researching`, `experiment` or `recommended` as supported by the evidence.
12. **Learning / Audit Agent** - process attributable outcomes, grade goals, evaluate experiments/calibration, run self-integrity checks, persist causal records and expose any promoted versions for the next run.
13. **Meta-Research Agent** - evaluate whether decisions and the research process are improving: enforce ex-ante/ex-post separation, measure attributable outcomes and opportunity regret, assess calibration and research/tool value, identify failure or missed-opportunity patterns, and propose evidence-gated changes to the system.
14. **Self-Improvement Agent** - convert material recurring failures into structured FailureRecords and MutationProposals, generate candidate prompt/strategy/parameter/memory/tool-routing/runtime changes, validate mutation boundaries, create candidate branches, run sandbox and adversarial evaluation, gather out-of-sample evidence, and promote or reject candidates using deterministic gates. A promoted runtime mutation becomes the next production version; rollback restores the exact prior version without rewriting history.
15. **Memory Distillation Agent** - during scheduled deep-learning windows, distill durable knowledge from source clusters into bounded Research Memory and propose Active Brain changes. Run reconstruction checks before activation.

The host must preserve cognitive separation even when all stages execute inside one model host. A stage may see only its permitted inherited inputs and declared dependency outputs.

## Helper-agent coordination

The canonical production cognitive owner remains `sovereign-host`, but external helper agents such as Codex, review agents, research agents or other LLM instances may assist through the durable coordination bus defined in `COORDINATION.md` and `coordination/`.

Helpers communicate through immutable per-message, per-claim and per-ack files. They may request work, claim bounded scope, publish findings/challenges/incidents, hand off evidence or code, and acknowledge receipt. The runtime contract is `runtime/coordination.py`.

Coordination messages are not sibling specialist conclusions and must not become an implicit bypass of the arbitration boundary. A helper can contribute evidence or challenge a claim; the receiving stage must independently evaluate it and preserve provenance. A helper claim is an advisory ownership lock, not execution authority.

Helpers must record exact file paths, commit SHAs, tests actually run, known gaps and requested follow-up in handoffs. They must never submit a live brokerage order or modify the constitution, historical audit/ex-ante evidence, audit semantics or human execution boundary.

## Effectiveness and self-improvement

Every decision should freeze an ex-ante snapshot identity before any outcome is known. Later outcomes link to that decision and may be used for evaluation only. A counterfactual is valid only when the alternative was feasible and known at the original decision timestamp.

When evidence supports a measurable claim, the host may also register an
immutable numeric forecast linked to a durable opportunity. The forecast
freezes its observation source, baseline, target horizon, deterministic
direction/range resolution rule, confidence, risks and invalidation condition.
Later evidence may supersede it through a new linked forecast but may never
rewrite it. Invalidation remains part of the outcome dataset rather than an
escape from calibration.

At maturity the host re-observes the exact frozen source through a connector
call. The runtime, not the host, extracts the numeric value and resolves the
event inside the forecast's frozen observation window. Unmeasured closed
windows remain overdue and count against measurement coverage.

Operator instruction decisions use a separate reconciliation record. The
runtime compares the durable proposal with current saved instructions, account
orders and trades. Acceptance terms and field-level edits are derived;
rejection/deletion require explicit operator observation; execution requires a
trade. Disagreement remains unknown. The reconciliation may be superseded by
later evidence, while every prior record remains immutable.

Research-value measurement remains multidimensional and descriptive. The
runtime reports usage, repetition, evidence provenance, novelty, decision
changes, implementation facts and uncertainty separately. Material
adversarial disagreements retain both positions in an append-only dispute
record. Role separation inside one host thread is not claimed as independent
multi-agent sampling.

The deterministic runtime computes reproducible measures including attribution completeness, realized result, experienced risk, benchmark-relative result, opportunity regret, probability calibration and research/tool usage. The Meta-Research Agent interprets those measurements and proposes hypotheses; it does not promote changes from small samples.

The self-improvement loop adds a second evidence layer for system changes: repeated process failures become causal fingerprints; the LLM designs a falsifiable mutation; deterministic checks enforce immutable boundaries and mechanical sample/OOS/counter-metric gates; successful candidates are versioned and promoted from a branch; production regressions can trigger rollback. Safety, audit semantics, the constitution and the human execution boundary remain immutable.

A production self-improvement change requires an explicit experiment, success metric, counter-metric, evaluation window, rollback condition and out-of-sample evidence. Historical decisions, safety constraints, broker authority and human execution boundaries are not mutable.

## Pass execution contract

Every specialist pass should record:

`run_id`, `stage_id`, `role_id`, `inputs_received`, `tools_used`, `observations`, `candidate_ids`, `evidence_status`, `blockers`, `confidence`, `next_actions`, `caused_by`, `execution_order`.

Before arbitration, a specialist pass must not read another specialist's conclusion, ranking, thesis, probability, confidence or recommendation.

When the host cannot safely complete the desired number of passes because of context, execution budget or tool limits, it must prioritize by decision value, record skipped passes and reasons, and never claim work that did not execute.

## Memory and context discipline

Working context is explicitly bounded. The normal cycle loads Constitution + Active Brain + fresh IBKR state + retrieved relevant Research Memory. Raw Archive is not loaded wholesale.

The Research Director first asks what information could change the current decision, then retrieves the minimum set of memory objects that can answer that question. Retrieval may escalate from Active Brain to Research Memory to Raw Archive.

Memory distillation is LLM-owned. The runtime only validates structure, provenance, contradiction preservation, Active Brain budgets and reconstruction coverage. A failed reconstruction keeps the distilled object out of Active Brain.

The raw archive is immutable. Distilled memory is a cache over source records, not a replacement for them. Later outcomes may invalidate a lesson, but may never rewrite the source decision or its ex-ante evidence.

## Capability and tool selection

The Research Director should first ask:

`What do I need to know to make or reject this decision?`

Then choose the smallest set of capabilities and tools that materially improves the answer. Web search is a first-class open-ended research capability. The host may use IBKR, Web, Longbridge, Alpaca, Next Stock, Stocktwits and GitHub according to their roles and actual availability.

The LLM should not optimize for using more tools. Tool usage is justified by decision value and evidence quality, and the exact tools consulted must be recorded.

## Evolution contract

The learning path is:

`recommendation -> user engagement -> execution/monitoring -> outcome -> lesson -> experiment -> statistical/OOS gate -> promotion or lock -> versioned strategy/parameter/prompt/goal -> effective next-run configuration -> meta-research -> self-improvement -> memory distillation -> audit`

A runtime mutation follows:

`failure -> fingerprint -> diagnosis -> proposal -> branch -> sandbox/replay -> adversarial validation -> OOS evidence -> deterministic gate -> production promotion -> monitoring -> rollback or retention`

An unexecuted IBKR instruction is not rejection. `unknown` engagement remains `unknown`. Without sufficient evidence for an A/B or out-of-sample comparison, record a proposed experiment rather than inferring a strategy change from a single observation.

## IBKR-first research rule

For every field exposed by the connected IBKR capability, use IBKR first. Current supported capabilities include live price snapshots, historical OHLCV, equity/options chain structure, option IV/OI/volume, underlying volatility, futures term structures, futures price/liquidity/open interest, account positions/orders/trades and account performance.

External sources are additive confirmation, gap-fill or independent evidence. Missing data remains unknown rather than being guessed.

## Agent contract

Every role returns structured data with:

`agent_id`, `run_id`, `as_of`, `inputs`, `observations`, `candidate_ids`, `evidence_status`, `blockers`, `confidence`, `next_actions`, `caused_by`.

Memory stages additionally return source IDs, claim IDs, contradiction groups, reconstruction status and promotion state. Self-improvement stages additionally return failure IDs, mutation IDs, parent/candidate commit IDs, gate results, test evidence, promotion state and rollback target. Candidates require evidence, portfolio capacity and counterfactual information before becoming reviewable. No role may submit a live brokerage order.

## LLM-first rule

The LLM owns discovery, interpretation, hypotheses, research direction, thesis construction/falsification, instrument selection, portfolio reasoning, counterfactual analysis, adversarial reasoning, goal design, experiment design, learning, meta-research, failure diagnosis, mutation design, memory distillation and prioritization.

Deterministic runtime code is restricted to serialization, validation, arithmetic requiring reproducibility, freshness checks, dependency checks, audit/hash-chain verification, capacity/stress guardrails, evaluation mechanics, memory structure/provenance/budget/reconstruction checks, mutation-envelope validation, sample/OOS/counter-metric gates, atomic promotion/rollback bookkeeping, coordination envelope validation and scheduler concurrency.

## Scheduling

There must be **one enabled production ChatGPT automation** for the Sovereign Investment System. Duplicate full-cycle scheduler tasks stay disabled because concurrent runs can race on GitHub's append-only state.

GitHub Actions are not the production investment scheduler. GitHub is durable storage/versioning. The ChatGPT automation is the cognitive scheduler and host.

Weekly learning and deep memory distillation are part of the same orchestrator rather than a separate investment scheduler.

## Stop conditions

Stop a branch when required authoritative data is missing, the question is already answered, expected decision value is low, the thesis is a duplicate, the candidate cannot beat hold/wait after portfolio fit, or evidence conflicts cannot be resolved enough for a decision. Blocked branches stay visible when they explain a material gap.

A self-improvement candidate stops at the first failed immutable-boundary, sandbox, test, OOS, sample, counter-metric or rollback gate. Rejection is a recorded outcome, not deletion.

## Governance and safety

- Adversarial review is independently derived before comparison.
- Goals require measurable contracts and grading.
- Prompt variants require baseline, hypothesis, success metric, counter-metric, testing state and rollback target.
- Integrity failures stop affected inference rather than increasing risk.
- No automatic live IBKR order submission.
- No fabricated observations or sources.
- No concealment of contradictions or losses.
- No treating an unexecuted instruction as executed or rejected.
- No increasing risk to improve recent performance.
- No bypassing portfolio or evidence gates.
- Fresh IBKR state wins over stale GitHub account snapshots.
- Memory promotion cannot bypass reconstruction, provenance or budget checks.
- Self-improvement cannot modify the constitution, historical evidence, audit-chain semantics or live brokerage execution boundary.
- Helper-agent coordination cannot grant authority that the production host and governance do not have.

## Host contract

`LLM_HOST_CONTRACT.md`, `META_RESEARCH_CONTRACT.md` and `SELF_IMPROVEMENT_CONTRACT.md` are authoritative for ChatGPT-host execution. `runtime/orchestrator.py` supplies dependency and isolation contracts. `runtime/effectiveness.py` supplies deterministic effectiveness mechanics. `runtime/self_improvement.py` supplies mutation gates and rollback state. `runtime/ibkr_backfill.py` supplies historical normalization. `runtime/memory.py` supplies deterministic memory-governance checks. `runtime/coordination.py` supplies helper-agent coordination envelope validation. Neither GitHub Actions nor Python runtime artifacts can claim an LLM stage ran unless the host actually executed and recorded it.
