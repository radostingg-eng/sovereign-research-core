# Causal Audit Graph v1

The durable research record is a graph of compact records, not a transcript archive.

## Record types

- `finding`: observed fact, sourced market/fundamental/event data, calculation or research conclusion
- `decision`: a recommendation, rejection, experiment choice or system change
- `outcome`: what happened after a decision, including execution status, P&L, holding period and exit reason when known
- `goal`: agent-authored objective and later grading record
- `lesson`: distilled learning linked to supporting outcomes
- `system_change`: versioned modification to research methodology
- `incident`: tool failure, missing data, audit failure or other integrity issue

## Required fields

```json
{
  "record_id": "stable id",
  "record_type": "finding|decision|outcome|goal|lesson|system_change|incident",
  "created_at": "timestamp",
  "agent": "canonical role name",
  "payload": {},
  "caused_by": ["record ids"],
  "prev_hash": "previous record hash or null",
  "record_hash": "hash of canonical record representation"
}
```

Records are append-only. A changed conclusion creates a new record that cites the prior record rather than rewriting history.

## Causal discipline

Decisions should cite the findings that materially caused them. Outcomes should cite the decisions they evaluate. Lessons should cite the outcomes that justify them. System changes should cite the evidence that motivated them.

A record referencing a missing parent is a visible integrity defect, not an implicit null.

## Agent usefulness

Do not measure research-role value by call volume. Measure whether its findings are cited by decisions and whether those decisions subsequently perform well against their counterfactuals.

A role with many unused findings may be noisy. A role whose findings are repeatedly cited but lead to poor outcomes may be systematically misleading. Both should trigger review.

## Transcripts

LLM transcripts stay outside the compact graph. The graph stores conclusions, evidence references and causal links. A transcript may be stored separately and referenced by identifier when needed for audit.

## Adversarial re-derivation

The adversarial protocol is operational as an agent procedure and the deterministic runtime validates the resulting integrity fields. A host may execute the independent re-derivation step and append the result to the same causal journal. The system must not describe this as an always-running independent service.

## Persistence

`runtime/audit_store.py` implements the file-backed append-only journal, canonical record hashing, hash-chain verification and dangling-cause detection. The current daily journal is persisted under `audit/` and is updated additively.
