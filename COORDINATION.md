# Multi-Agent Coordination Protocol

The Sovereign system is single-host for production cognition, but other agents may assist with research, implementation, validation, debugging, or tooling. Coordination is durable and auditable through `coordination/`.

## Roles

An agent identifies itself with a stable `agent_id`, for example `sovereign-host`, `codex-cli`, `review-agent`, or `research-agent`.

The production host remains the authority for the cognitive decision flow. Helper agents may investigate, implement, validate, or challenge work, but they do not gain permission to submit live brokerage orders or bypass governance.

## Message types

Use immutable, single-purpose files under `coordination/messages/`:

- `request` - asks another agent to investigate or perform bounded work.
- `finding` - reports evidence or a reproducible observation.
- `challenge` - disputes a claim or asks for re-derivation.
- `handoff` - transfers a bounded work product to another agent.
- `status` - reports progress, blocked work, or completion.
- `incident` - reports an operational or integrity failure.

Each message must include a unique `message_id`, `agent_id`, `created_at`, `type`, `subject`, `body`, `caused_by`, and `priority`. References to files, commits, test runs, source IDs and evidence IDs should be explicit.

## Claims and leases

A task may be claimed with an immutable file under `coordination/claims/`. Claims contain a unique `claim_id`, task identifier, owner, created/expiry timestamps, scope, parent request and status. Claims are advisory locks for coordination, not authority to override the production host.

Another agent must not silently take over an active claim. Use a new claim with `supersedes` when ownership needs to change, preserving both records.

## Acknowledgements

Acknowledgements are immutable files under `coordination/acks/` and point to the exact message or handoff being acknowledged. An acknowledgement records `accepted`, `rejected`, or `needs-more-info`; it does not rewrite the original message.

## Concurrency model

Do not use one shared append-only coordination JSONL file. GitHub file updates are whole-blob operations and concurrent writers can race. One-file-per-message/claim/ack gives each write a separate immutable path and lets Git history provide the durable write record.

Agents must read the relevant coordination directory before starting overlapping work. The runtime validator rejects malformed envelopes, duplicate IDs inside a supplied set, invalid timestamps, invalid message types, invalid priorities, expired/closed claim transitions, and references to missing parent IDs when validating a local coordination snapshot.

## Authority and safety

Coordination never overrides:

- the system constitution;
- fresh IBKR authority for current account state;
- evidence and portfolio gates;
- audit-chain semantics;
- human approval for live brokerage execution.

Helper-agent output is evidence, not truth. The receiving agent must independently assess source quality and conflicts. A handoff cannot make an unverified claim authoritative.

## Recommended flow

`request -> claim -> work -> finding/challenge -> handoff -> ack -> receiving-agent integration`

A blocked task should publish a `status` or `incident` with the exact blocker rather than silently disappearing. A completed implementation should include commit SHA, tests actually run, known test gaps, and rollback considerations where applicable.
