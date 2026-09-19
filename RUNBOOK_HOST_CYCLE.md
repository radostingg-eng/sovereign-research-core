# Running a host cycle

The host has IBKR access and can write to this repository but cannot execute
anything: its shell has no network. An executor with the repository can run
the runtime but has no IBKR. Neither closes the loop alone.

So the host commits what it observed and decided, and an executor runs the
cycle against that committed input and persists a real receipt.

## The host stages

`host_staging/<unique>.semantic.json`, using
`schemas/host_semantic_v1.example.json`. The host supplies real observations,
evidence, reasoning, stage outputs, and decisions. The deterministic builder
creates the canonical stage graph, call/provenance envelopes, evidence
projections, duplicated decision fields, and pretty JSON. It never invents
missing analysis. The host never writes canonical `host_input/` cycle files
directly.

- `source: "ibkr"`, an authoritative `as_of`, and `order_submission_used: false`
  stated explicitly. An absent key is refused: silence is not a denial.
- `snapshot`, `performance`, `open_orders`
- `research`, each entry carrying the actual `tool_calls` it made
- `findings` and a `decision`
- `host_input_schema_version: 4` and exactly one learning disposition for
  `learning_audit`, `meta_research`, and `self_improvement`
- a completed `market_scout` stage between `portfolio` and
  `research_director`

Market Scout commits a host-authored discovery scope and research budget,
then records concrete evidenced tool calls and zero or more candidates. Each
candidate uses the same immutable five-field identity as the opportunity
ledger. The runtime derives budget usage from selected specialist work, scout
tool-call kinds, and distinct opportunity updates. Overruns are allowed only
with an exact `budget_variance` disclosure. Candidate order is not a ranking.
Because the cycle is committed after cognition finishes, runtime validation
proves consistency between the declared budget and committed work, not the
wall-clock moment when the host chose the budget.

Research Director then allocates the specialist ceiling across four primary
accounting purposes: new opportunity, existing opportunity, portfolio risk,
and follow-up. Category usage is derived from explicit candidate links with
precedence follow-up, portfolio risk, existing opportunity, then new
opportunity. The host supplies qualitative novelty, portfolio-impact,
missing-information, and expected-information-gain reasons; the runtime never
turns them into scores or rankings. The allocation records the exact current
market-session overlap. Closed markets may justify broader discovery or more
parallel directions, but no clock or session state mechanically chooses the
mix. Portfolio-risk references resolve to current snapshot/account/position
facts. Follow-up references resolve to current instructions, known
opportunities, or prior accepted agenda candidates; invented references are
refused. Newly staged follow-ups must also have an `as_of` later than the
durable record they reference.
New staged candidates require this stage; historical schema-v2/v3 inputs
remain replay-compatible.

Forecast registration is optional. When current evidence supports a
falsifiable numeric view, `forecast_registrations` freezes the linked
opportunity, measurement source and field, baseline, future target, direction
or inclusive range, confidence probability, contextual benchmark, optional
unexecuted price context, risk assumptions, invalidation condition and
current-cycle evidence. Registration time, decision link, decision snapshot
hash and opportunity discovery time are derived from durable runtime records.
The append-only journal makes the forecast immutable. Revisions use a new ID
and `supersedes_forecast_id`; they never rewrite the earlier forecast. A
benchmark does not change the resolution rule, and invalidation does not
remove the forecast from later calibration.

Matured forecasts are measured through `forecast_outcomes`. The host supplies
only the forecast ID, a current-cycle connector tool-call reference, optional
invalidation reason and evidence. The runtime extracts the numeric value from
the frozen source field, requires the observation to fall inside the frozen
window, and computes the binary result. Outcomes are terminal and keyed by
forecast ID. Invalidation remains measured. A missed observation window
becomes overdue and remains in the matured denominator; new forecast
registration is blocked until the gap is addressed.

Instruction reconciliation is append-only and separate from the coarse
lifecycle state. `instruction_reconciliations` carries an explicit operator
quote plus current account-order and trade tool-call references. The runtime
recovers the durable proposal, uses conid-safe derivative matching, derives
`accepted_unchanged` versus `accepted_modified`, and preserves submitted and
executed as separate states. Saved-instruction deletion never proves live
order deletion. Connector/app disagreement is stored as unknown, and later
evidence supersedes rather than rewrites the earlier reconciliation. A
proposal recovered after the original create is labeled
`recovery_restatement`, not misrepresented as original create evidence.

Research-value feedback is a bounded descriptive census: specialist runs,
repeated questions, tool/source use, result-hash novelty, decision-status
changes, empirical calibration availability, implementation evidence and open
uncertainty counts. It does not rank tools, sources, strategies or investment
quality. Material adversarial disagreements may be persisted as immutable
`adversarial_dispute` records retaining both positions, disputed claims,
evidence, governance resolution and whether the final decision changed.
Current adversarial stages are roles in one host thread, not independently
sampled agents.

Historical canonical schema-v2, v3, and v4 cycles remain replay-compatible.
New scheduled candidates use semantic schema 1; intake emits canonical v4.
The exact semantic source and builder version are archived under
`host_staging/accepted_sources/`.

Each schema-v4 Market Scout or `research[].tool_calls[]` row carries:

```json
{
  "tool_call_id": "ibkr-price-one",
  "kind": "connector_lookup",
  "tool": "Interactive Brokers (IBKR)",
  "call": {
    "action": "get_price_snapshot",
    "arguments": {"contract_id": 123}
  },
  "result": {"last": 100.0},
  "provenance": {
    "result_origin": "connector_response",
    "observed_at": "2026-09-18T19:00:00Z",
    "source_refs": [
      {"kind": "response_id", "value": "connector-response-123"}
    ],
    "capture": {
      "schema_version": 1,
      "representation": "canonical_response",
      "redactions": []
    },
    "web_sources": []
  }
}
```

Connector responses are JSON values, never Python-repr strings. Host-authored
prose uses `host_summary` with `host_summary_no_response`. URL evidence records
title, nullable publication time, and retrieval time without credential-bearing
URLs. Redaction uses RFC 6901 JSON Pointers and the exact
`__SOVEREIGN_REDACTED__` marker. Credentials, account identifiers, and contact
PII may be declared; investment evidence such as instrument, price, quantity,
orders, executions, valuation, forecasts, and exposure may not be hidden.

After successful execution, inspect `tool_provenance` in `FEEDBACK.json` or
the `tool-provenance:<cycle_id>` journal record. Each row contains a canonical
SHA-256, normalized request hash, action, observation time, interpretation
reference, and private `profile://` artifact reference. Canonical connector
response bodies live content-addressed under `tool_artifacts/`; feedback never
contains bodies, request arguments, private filesystem paths, or URL queries.
The hash detects later edits to committed bytes; it does not prove connector
authenticity or detect undeclared omission. If receipt persistence succeeds but
artifact/index persistence is interrupted, a fingerprint-matched rerun repairs
the missing provenance exactly once.

Each v3/v4 `learning_stage_dispositions` row declares `artifact` or
`no_change`, a rationale of at most 600 characters, and 1-8 current-cycle
evidence refs using `stage:<stage_id>` or `finding:<finding_id>`. Artifact
rows also cite 1-8 same-cycle durable refs using the closed grammar documented
in the standing prompt. No-change rows omit `artifact_refs`. The executor
persists lessons, memory distillation, goal events, and the receipt first, then
reconciles artifact lineage before appending deterministic
`learning-disposition:<cycle_id>:<stage_id>` records.

New receipts declare finalization schema version 1. A cycle is complete only
after `cycle-finalization:<cycle_id>` binds the canonical input hash, receipt
hash, required disposition and provenance records, and private artifact
identities. A fingerprint-matched retry with a receipt but no finalization
record resumes the post-receipt writes. Exact prior writes are reused;
different payloads fail closed. Feedback does not treat a new receipt as
accepted until its finalization record exists.

`FEEDBACK.json.tool_inventory` is intentionally a bounded digest. The full
connector/action manifest remains in its immutable `tool_inventory` journal
record. The digest identifies that record, reports whether later accepted
cycles make it stale, and includes `full_inventory_command` for retrieving the
exact action inputs and return contracts. A stale digest is discovery context,
not proof that an action remains callable.

Invalid candidates move to `host_staging/rejected/` and the correction appears
in `host_staging/FEEDBACK.json`. Valid candidates move to `host_input/` with a
content-hash promotion marker. The executor ignores any new canonical file
without a matching marker.

The host does not wait for CI. It commits one candidate, reports a concise
staged-cycle summary with the decision, research focus, instruments,
conviction, and prior cycle verdict, then stops. Full paths, SHA, validator
state, and execution receipt remain available in debug details for failures or
explicit diagnostics. The next scheduled run reads the asynchronous staging
verdict before doing new work.

A refused candidate triggers a host-authored postmortem before new research.
When unclear or ineffective guidance contributed, the correction commit also
updates the standing prompt or schema example with a durable prevention change.
Repeated malformed JSON must be rebuilt from the schema as pretty-printed JSON,
never repaired in place. Syntax feedback includes the exact parser location and
an escaped context window. Duplicate keys, placeholder sentinels, and non-UTF-8
files are refused explicitly while their original bytes remain archived.

A candidate refusal is a handled workflow outcome: the validator archives the
bytes, publishes feedback, and exits successfully. Validator crashes,
publication failures, and unpromoted canonical inputs still fail the workflow.

## The executor runs

The runtime requires Python 3.12 or newer. The installer records the selected
interpreter's absolute, upgrade-stable path in the launchd configuration so
the validator and executor do not silently parse host evidence differently.

```
python3 -m runtime.run_host_cycle --input-dir host_input
```

Deliberately not a GitHub Action. Actions minutes are limited, and a loop
that depends on them stops when the budget does. This runs anywhere the
repository is checked out, including under `launchd` or `cron` on a laptop,
at no cost.

Re-running is safe: an input whose cycle is already persisted is skipped, so
the same push cannot append a cycle twice.

The macOS primary executor must use a dedicated clone. Running launchd from a
developer checkout couples autonomous execution to the current branch and
working-tree state. A feature branch with no upstream, or one uncommitted
file, makes `git pull --rebase` fail and pauses the loop.

Install or refresh the dedicated executor:

```bash
bash ops/install_hostcycle.sh
launchctl kickstart -k gui/$(id -u)/com.sovereign.hostcycle
tail -f ~/Library/Logs/sovereign-research-hostcycle.log
```

The default clone is `~/.local/share/sovereign-research-executor`. Override it
with `SOVEREIGN_EXECUTOR_REPO=/absolute/path` when installing.

## Inspect operational reliability

`FEEDBACK.json.reliability` is derived from immutable hash-chain order. It
counts receipts and cycle-candidate refusal records, exposes accepted-candidate
streaks and bounded refusal-to-next-receipt journal windows, and states which
stronger metrics remain unavailable instead of inferring retry or human
intervention lineage.

The cognitive streak is separate. Only schema-v3 receipts are scoreable. A
completed v3 receipt qualifies when all three learning stages completed and
all three persisted disposition records reconcile to the receipt. Refusals and
nonqualifying v3 receipts reset that streak. Schema-v2 and unversioned receipts
neither increment nor reset it.

Schema-v3 cycles may also submit `opportunity_updates`. Each event carries a
stable normalized identity, a legal lifecycle transition, and current-cycle
evidence. Accepted events are stored as receipt-linked `opportunity_event`
records and projected into `FEEDBACK.json.opportunity_ledger`. Exact duplicate
identities under different IDs are refused; softer same-instrument thesis
collisions are shown to the host without automatic merging or ranking.

```bash
python3 -m runtime.reliability --journal audit/2026-09-16-genesis.jsonl
```

The CLI scope is the named journal only. Archived journals are not silently
combined with it.

## What it does not do

It decides nothing. Every judgement -- what to research, what it means, what
to do -- came from the host. The runner supplies handlers that return the
host's own observations and lets the gates rule on them.

It also refuses rather than improvising. An input missing `as_of`, naming a
source other than IBKR, omitting the order declaration, or carrying no
research or decision is rejected before anything is appended, because an
append-only journal cannot take back a half-written cycle.
