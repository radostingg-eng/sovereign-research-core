# Memory Distillation Contract

## Cognitive role

Memory distillation is an LLM-hosted reasoning task. The model decides which observations can be generalized, which must remain exact, which contradictions matter, and what information belongs in Active Brain versus Research Memory.

The runtime does not summarize semantic content. It validates the artifact produced by the LLM and prevents malformed or provenance-breaking promotion.

## Required inputs

A distillation pass receives:

- a source set and explicit time boundary;
- source record IDs and hashes when available;
- the current Active Brain;
- the decision/research question driving retrieval;
- relevant outcomes and experiments, clearly marked ex-post;
- active brain budget;
- integrity incidents affecting the source set.

## Required outputs

The LLM must return:

`distillation_id`, `source_ids`, `source_time_bounds`, `memory_objects`, `claim_ids`, `contradiction_groups`, `active_brain_proposals`, `retirements`, `reconstruction_spec`, `compression_metrics`, `blockers`, `ex_post_material`, `brain_version`.

Every memory object must have stable provenance and an explicit evidence status.

An Active Brain proposal uses:

```json
{
  "memory_id": "stable-id",
  "layer": "active_brain",
  "status": "active",
  "as_of": "timestamp-with-timezone",
  "claim": "source-grounded durable claim",
  "source_ids": ["source-record-id"],
  "evidence_status": "verified",
  "confidence": 0.0,
  "reconstruction_status": "passed",
  "claim_ids": ["stable-claim-id"]
}
```

`reconstruction_spec` contains `required_claim_ids`, `distilled_claim_ids`,
`source_claim_ids`, and `distilled_contradiction_groups`. The three claim-id
sets must support reconstruction without adding unsupported claims.

The container contract is exact:

- `source_ids`, `memory_objects`, `claim_ids`, `active_brain_proposals`,
  `retirements`, `blockers`, and `ex_post_material` are lists.
- `source_time_bounds`, `contradiction_groups`, `reconstruction_spec`, and
  `compression_metrics` are objects.
- `contradiction_groups` and
  `reconstruction_spec.distilled_contradiction_groups` map group ids to lists
  of claim ids. A contradiction-group value may instead be an object with a
  `claim_ids` list when the host needs to retain group metadata.
- `compression_metrics` has positive numeric `raw_units` and
  `distilled_units`.
- Every `memory_objects` item includes all base memory fields, including
  `reconstruction_status`. Use `validated` or `experimental` for a
  Research Memory candidate. `active_candidate` is not a valid status.
  `reconstruction_status` is separately one of `not_run`, `passed`, `failed`,
  or `blocked`; `validated` is a lifecycle status, not a reconstruction
  result.

Each `retirements` item is an object:

```json
{
  "memory_id": "stable-id",
  "status": "stale",
  "reason": "New source-backed evidence invalidated the prior claim.",
  "source_ids": ["source-record-id"]
}
```

`status` is `stale` or `archived`. `stale` suppresses existing versions but a
later validated version of the same `memory_id` may restore it. `archived` is
terminal for that `memory_id`; a genuinely new claim needs a new stable ID.
The target must already exist, the retirement sources must be included in the
distillation source set, and the pass must retrieve provenance for the target.
An ungrounded or unknown retirement is recorded as rejected and changes no
memory state.

Memory distillation is an additional top-level field in a normal schema-v3
host cycle. It does not replace `cognitive_stages` or
`learning_stage_dispositions`. Historical schema-v2 cycles remain
replay-compatible.

If any claim from an unresolved contradiction group is distilled, every claim
in that group must remain in the distilled claim set and in
`distilled_contradiction_groups`. The runtime publishes the latest admission
and reconstruction errors in `FEEDBACK.json.memory_distillation`.

## Separation rules

Observations, interpretations and hypotheses must be represented separately. Ex-post information may inform lessons but cannot be written into an earlier ex-ante evidence set.

Contradictions must remain visible unless the underlying source evidence itself is resolved. A summary must not turn disagreement into false consensus.

## Active Brain admission

An LLM-generated object becomes Active Brain only after:

1. structural validation;
2. source/provenance validation;
3. reconstruction testing;
4. contradiction preservation checks;
5. budget checks;
6. governance review.

Failed objects remain in Research Memory or experimental state.

Validated Research Memory objects are persisted separately from Active Brain.
The host retrieves in this order:

`Active Brain -> relevant validated Research Memory -> Raw Archive`

`FEEDBACK.json.research_memory` contains a bounded set of current validated
objects plus counts for inactive and retired objects. Experimental or failed
objects remain in the append-only journal but cannot influence a decision as
validated memory.

## Retrieval strategy

The Research Director should retrieve from Active Brain first, then relevant Research Memory, then Raw Archive. The Raw Archive is used when exact historical facts, original evidence, transaction details or audit reconstruction are required.

Retrieval should stop when additional information is unlikely to change the decision. This is an LLM judgment, not a fixed keyword threshold.

## Distillation loop

`retrieve -> reason -> distill -> reconstruct -> validate -> admit -> monitor`

A later outcome can invalidate a distilled lesson. Invalidation should mark the derived object stale or retired while preserving every source record and the original historical decision.

## Weekly deep pass

The existing single production scheduler performs a deeper memory pass during the weekly learning window. It should inspect oversized memory clusters, duplicate claims, stale Active Brain entries, invalidated lessons, failed reconstructions and retrieval patterns that repeatedly descend to raw data.

The deep pass may propose new memory schemas, retrieval capabilities or routing strategies through the existing experiment/promotion framework.
