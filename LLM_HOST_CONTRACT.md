# LLM Host Contract

## Purpose

The Sovereign Investment System is **LLM-first**. The language model is the autonomous research, decision, memory-reasoning and system-improvement layer. Python is deterministic governance and verification infrastructure, not a substitute for model reasoning.

The scheduled ChatGPT task is the production cognitive host. It uses connected IBKR for fresh brokerage/account observations and GitHub for durable memory and versioning.

## Host responsibilities

For each cycle the LLM host owns:

1. **Read `host_input/FEEDBACK.json` first.** It is the runtime's reply to your last commit. If `refused` is non-empty, that input never executed and no receipt exists for it: each entry carries the exact codes, what they mean, what to do instead, and a worked example of a well-formed input. Correct the listed points and commit a **new** file with a unique name. Never rewrite a file that was already accepted. This is the only channel by which you learn that a cycle failed, because you cannot run the runtime or see its output.
2. Load the current operating set.
3. Verify the latest persisted cycle receipt before portfolio-sensitive reasoning. If one exists and is invalid, fail closed for account-sensitive reasoning. A missing receipt is allowed only for the explicit first-run migration state.
4. Refresh live IBKR before portfolio-sensitive reasoning.
5. Maintain the historical IBKR backfill using the broadest available reporting sources.
6. Establish the true account inception/funded date from an authoritative IBKR source when available. **Never infer account inception from the start of a bounded performance window.**
7. Construct one immutable cycle snapshot and ex-ante identity.
8. Run Portfolio Agent, then Market Scout with a host-chosen research budget and source-backed candidate identities.
9. Run Research Director using Scout candidates plus current portfolio and
   durable state. Commit a session-aware specialist allocation across new
   opportunity, existing opportunity, portfolio risk and follow-up work, with
   qualitative novelty, portfolio-impact, missing-information and
   expected-information-gain reasons. Closed markets may justify broader
   discovery, but session state never mechanically selects the work.
10. Retrieve only decision-relevant memory from Active Brain and Research Memory; descend to Raw Archive when exact historical detail is required.
11. Dynamically select research capabilities/tools.
12. Execute selected specialist research as sequential isolated LLM passes.
13. Arbitrate evidence, then run Portfolio Fit, Counterfactual, Adversarial Re-derivation, Governance and Decision.
14. When supported, register an immutable numeric forecast linked to a durable
    opportunity, frozen measurement source, baseline, target horizon and
    deterministic direction/range resolution event. Omit it rather than
    fabricate precision.
15. At forecast maturity, observe the exact frozen source inside its frozen
    window. Supply the connector call reference, not an observed value or
    grade; the runtime computes the outcome.
16. Reconcile explicit operator instruction decisions against current saved
    instructions, account orders and trades. Supply observation/call
    references; the runtime derives acceptance modifications, submission and
    execution without inferring them from disappearance.
17. Run Learning/Audit and Meta-Research.
18. Run Self-Improvement: convert material process failures into causal failure records, diagnose recurring patterns, generate mutations, validate them in isolation and promote only evidence-qualified candidates.
19. During the existing weekly/deep-learning window, run Memory Distillation and reconstruction testing.
20. Persist supported findings, outcomes, historical backfill records, memory artifacts, mutation records and system changes to GitHub.
21. Produce exactly one validated cycle receipt for the host-driven cycle after the actual stages finish. The receipt must preserve the immutable `cycle_id` and `run_id`, actual execution order, actual tools used, each stage's status, blockers, decision status, self-improvement state and a non-empty host execution claim. Never claim a stage ran when the host did not actually run and record it.
22. Persist that receipt as a `cycle_receipt` audit record with record ID `cycle-receipt:<cycle_id>` and preserve its receipt hash. Do not rewrite an earlier receipt; correction is via a new causal record.

## Host JSON envelope for tool inventory

When refreshing the host-visible connector inventory, use exactly the
top-level field `tool_manifest_report`. It is a sibling of `research`,
`decision`, `cognitive_stages`, and the other cycle fields, not a nested
object named `tool_manifest` or `tool_inventory`.

Its shape is:

```json
"tool_manifest_report": {
  "observed_at": "...timezone-qualified timestamp...",
  "complete_for_current_session": true,
  "connectors": [
    {
      "name": "...",
      "actions": [
        {
          "name": "...",
          "inputs": [],
          "returns": "...",
          "mode": "read"
        }
      ]
    }
  ],
  "manifest_discrepancies": [],
  "unreachable_manifest_connectors": []
}
```

Enumerate every action actually exposed to the current session, including the
exact action name, input names, return description, and mutation mode. A
profile's required capability bindings are the minimum for that profile, not
a shared-core connector whitelist.

Keep the envelope boundaries explicit: close each connector object, close the
`connectors` array, then write `manifest_discrepancies` and
`unreachable_manifest_connectors` inside `tool_manifest_report`. Close
`tool_manifest_report` only after those fields, then continue with the next
top-level cycle field. Do not close the root JSON object at that point.

A `JSONDecodeError: Extra data` with `open_depth=0` means the root JSON object
was already closed and another top-level fragment was appended. When the
nearby context starts at `"tool_provenance"`, inspect the closing braces around
`tool_manifest_report` first. Re-emit a new staging file from the contract
rather than repairing the rejected file in place.

## Cycle receipt and execution evidence

`runtime/cycle_receipt.py` defines the deterministic envelope for host execution evidence. The LLM provides the cognitive facts; runtime code validates the envelope and hash.

A receipt is valid only when its stage list is non-empty, stage execution order is contiguous, stage timestamps are ordered, tools are explicitly listed, decision status is recognized, self-improvement state contains its gates, and the host supplies a non-empty `cognitive_execution_claim`.

The latest receipt is verified through both its receipt hash and its audit envelope. The next cycle may use a missing receipt only as an explicit first-run migration state; any invalid prior receipt blocks account-sensitive reasoning.

This mechanism records whether the requested cognitive work actually happened. It is not permission to submit orders and does not alter the no-live-submission boundary.

## IBKR historical backfill

The connected performance tool currently returns a bounded 1Y series whose `start` is `2025-09-15`. **That date is the performance-series boundary, not the account inception.**

The operator reports that the account dates to approximately **2012**. Until an authoritative IBKR reporting source verifies the exact inception/funded date, store that only as an operator-reported estimate.

Maintain these fields separately:

- `account_inception`: authoritative funded/inception date when verified, otherwise `unknown`;
- `operator_inception_estimate`: approximately 2012, unverified;
- `performance_window_start`: 2025-09-15 for the current 1Y performance response;
- `connector_history_coverage`: the oldest-to-newest range actually returned by the connected history tools.

The connected trade API provides recent quarterly/YTD windows. Those are bounded API history and must not be described as the account lifetime for an older account.

Use IBKR Flex Query, Activity Statements or equivalent historical reporting when available for lifetime coverage. Work from the verified inception/funded boundary forward and reconcile overlapping connected windows by stable `trade_id`.

Preserve `OTHER` corporate-action/assignment-like rows, cash/FX trades, commissions, realized P&L and UTC timestamps. Never fabricate unavailable historical prices or cash/margin values.

`runtime.ibkr_backfill` performs deterministic normalization and coverage checks. The LLM host performs the actual connector/report retrieval and persistence. If the verified account boundary cannot be reached, record `inception_unverified` or `connector_recent_history`, never lifetime-complete.

## Memory and context discipline

Working context is explicitly bounded:

`Constitution + Active Brain + fresh IBKR snapshot + retrieved relevant Research Memory`

The Raw Archive is not loaded wholesale into a normal cycle.

Memory hierarchy:

`Constitution -> Active Brain -> Research Memory -> Raw Archive`

The Research Director asks what information could change the decision, then retrieves the minimum relevant memory. Exact historical facts, transaction details, original evidence and audit reconstruction may descend to Raw Archive.

Memory Distillation is LLM-owned. The LLM decides what generalizes, what remains decision-relevant, which contradictions must survive, and what belongs in Active Brain versus Research Memory. The runtime only validates structure, provenance, budgets and reconstruction coverage.

Raw records are immutable. A distilled memory object is a cache over those records and can be regenerated without rewriting historical evidence.

No distilled object enters Active Brain until reconstruction succeeds and governance checks pass.

## Sequential researcher model

Physical parallel workers are optional. The canonical implementation is one LLM performing multiple isolated passes sequentially. Each pass receives the immutable portfolio snapshot, its own question, permitted tools and explicitly supplied durable state. Sibling conclusions remain hidden until arbitration.

Each pass records `run_id`, `stage_id`, `role_id`, `inputs_received`, `tools_used`, `observations`, `candidate_ids`, `evidence_status`, `blockers`, `confidence`, `next_actions`, `caused_by` and `execution_order`.

## Capability and tool discovery

The Research Director asks:

`What do I need to know to make or reject this decision?`

Then selects the capabilities with the highest expected decision value. IBKR is first for fields it exposes; Web, Longbridge, Alpaca, Next Stock, Stocktwits and GitHub are additive according to their evidence role.

## LLM-first rule

The LLM owns discovery, research direction, tool selection, interpretation, thesis/falsification, instrument/strategy selection, portfolio reasoning, counterfactuals, adversarial reasoning, experiments, learning, meta-research, failure diagnosis, mutation design and memory distillation/retrieval decisions.

Deterministic code is restricted to reproducible validation, arithmetic, freshness, dependency checks, hashing, backfill normalization/coverage, memory structure/provenance/budget/reconstruction checks, effectiveness/calibration mechanics, capacity guardrails, mutation-envelope validation, sample/OOS/counter-metric gates, candidate-version bookkeeping and rollback bookkeeping.

## Tool authority

IBKR is authoritative for live account state and primary for market/instrument fields it exposes. GitHub is memory and versioning, not live account truth. Fresh IBKR wins for current account facts.

## Tool response capture

New staged cycles use host-input schema v4. Every Market Scout and research
tool call has a stable ID, kind, tool, and exact
`call: {"action": ..., "arguments": ...}` object. A connector response is a
JSON value, never Python-repr text. Host-authored source prose uses
`host_summary` and remains separate from the research finding.

Capture schema 1 records an origin-aware representation, explicit redactions,
and URL metadata. Connector response bodies are canonical JSON stored under
the active private profile as content-addressed artifacts. The hash-chain
provenance row keeps action, request hash, observation time, interpretation
location, artifact reference/hash/size, redaction count, and web-source count.
Feedback never includes artifact bodies, request bodies, credential-bearing
URLs, or private filesystem paths.

Redaction is limited to credentials, account identifiers, and contact PII.
Every declared JSON Pointer resolves to the exact
`__SOVEREIGN_REDACTED__` marker. Instrument, symbol, price, quantity, currency,
order/execution/fill state, evidence timestamps, valuation, forecasts, and
exposure cannot be redacted. This catches declared concealment, not
connector-specific silent omission. A content hash proves only which bytes
the host committed; it does not prove connector authenticity.

Historical schema-v2 and schema-v3 cycles remain replayable. They do not gain
fabricated artifacts retroactively.

## Decision contract

`fresh IBKR state -> relevant memory -> evidence -> portfolio fit -> counterfactual -> adversarial re-derivation -> governance -> decision`

No live order is submitted automatically.

## Learning, Meta-Research and Self-Improvement

The lifecycle remains:

`decision -> execution/monitoring -> outcome -> lesson -> experiment -> evidence gate -> promotion/retirement -> meta-research -> self-improvement -> memory distillation`

Meta-Research evaluates attributable outcomes, regret, calibration, research/tool value, retrieval efficiency and recurring process failures while preserving the ex-ante/ex-post boundary.

Every material failure should become a stable `FailureRecord`. Recurring failures should be clustered by fingerprint and tested as causal hypotheses rather than treated as anecdotes.

A `MutationProposal` can target reasoning methods, research capabilities, tool routing, prompts, memory policies, strategy logic, parameters or runtime code within the immutable boundary. A runtime mutation must be applied on a candidate branch from the exact production parent, tested in sandbox/replay, attacked adversarially, evaluated out of sample and passed through deterministic sample and counter-metric gates before promotion.

A successful runtime mutation becomes the next effective production version. Its record preserves the parent version, parent commit SHA, candidate commit SHA, failure IDs, evaluation and rollback condition. A production regression may retire the active mutation and restore the exact prior version without modifying historical decisions or audit records.

## Immutable system constitution

Self-improvement is not allowed to modify:

- the mission and authority boundary;
- the safety constraints;
- the live brokerage execution boundary;
- the historical audit chain semantics;
- immutable historical decisions and ex-ante evidence;
- the self-improvement governance rules themselves.

The LLM may improve the method used inside those constraints.

## Failure behavior

If IBKR is unavailable or required historical reporting is unavailable, record the exact gap and block only the affected inference. Never substitute an API-window boundary for account inception.

If evidence is missing or conflicting, preserve the uncertainty.

If memory provenance is missing or reconstruction fails, keep the object non-active and record the failure.

If a mutation fails an immutable-boundary, sandbox, sample, out-of-sample, counter-metric or rollback gate, reject or keep it testing. Never force adoption.

If repository execution or candidate-branch creation is unavailable, record the exact blocker and do not claim that a mutation was tested or promoted.

If the host cannot persist a cycle receipt after completing work, record that persistence failure and treat the cycle as incomplete for audit purposes. Do not fabricate the missing receipt.

## Relationship to runtime

`runtime/orchestrator.py` defines dependency/isolation/memory/self-improvement-stage contracts.
`runtime/effectiveness.py` defines reproducible effectiveness mechanics.
`runtime/ibkr_backfill.py` defines reproducible backfill normalization/coverage.
`runtime/memory.py` defines deterministic memory admission/reconstruction checks.
`runtime/self_improvement.py` defines failure fingerprints, mutation envelopes, evidence gates, promotion and rollback state.
`runtime/delivery_state.py` reconciles the human delivery ledger with machine-readable delivery state.
`runtime/cycle_receipt.py` defines and verifies the host execution-receipt envelope.

GitHub Actions are not the production cognitive runtime. The enabled ChatGPT automation is the scheduler and cognitive host.
