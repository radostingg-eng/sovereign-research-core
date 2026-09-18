# Sovereign Memory Distillation and Retrieval

## Purpose

The Sovereign Investment System must be able to accumulate very large historical archives without forcing the full archive into every LLM context. Memory therefore has separate storage and working layers.

The system never deletes raw evidence merely to reduce context. Distillation creates compact, versioned representations while retaining immutable source references and reconstruction tests.

## Memory layers

### 1. Constitution

Small, stable rules that define mission, authority, safety, audit integrity and human execution boundaries. Loaded on every full cycle.

### 2. Active Brain

Compact current working knowledge required for decisions: current portfolio model, active theses, promoted strategies, active experiments, current failure modes, validated reusable lessons, current goals, and unresolved high-value hypotheses.

The Active Brain is deliberately bounded. It is not a transcript and must not become a chronological dump.

### 3. Research Memory

Distilled findings, prior decision context, validated lessons, experiments, research/tool value observations and thesis histories. Retrieved selectively by the Research Director according to the decision question.

### 4. Raw Archive

Immutable source records including IBKR history, performance ledgers, raw research traces, source observations, historical decisions and outcome records. It is never injected wholesale into normal runs.

## Working-context rule

A normal cycle should load:

`Constitution + Active Brain + live IBKR snapshot + retrieved relevant Research Memory`

It should not load the Raw Archive wholesale.

The Research Director first determines:

`What information could change this decision?`

Only then does it retrieve relevant memory. Tool use and memory retrieval are justified by expected decision value, not by a requirement to inspect everything.

## Memory lifecycle

Every memory object has a lifecycle:

`raw -> distilled -> validated -> active -> stale -> archived`

A memory object must preserve:

- stable memory ID;
- layer;
- creation and update timestamps;
- source record IDs;
- source hashes where available;
- causal links to decisions/findings/outcomes;
- confidence/evidence status;
- supersession status;
- the brain version that adopted it;
- reconstruction-test status.

Raw records are append-only and never superseded by a summary.

## Distillation contract

Distillation is an LLM task, not a hardcoded summarization algorithm. The LLM decides what generalizes, what remains decision-relevant, what conflicts, and what should be retained verbatim.

The LLM must:

1. identify the source set and its time boundary;
2. extract candidate durable claims;
3. preserve contradictions rather than averaging them away;
4. distinguish observation, interpretation and hypothesis;
5. attach every durable claim to source IDs;
6. identify claims that are unsafe to generalize;
7. propose Active Brain updates separately from Research Memory summaries;
8. produce a reconstruction test specification.

Deterministic runtime code does not decide semantic relevance. It validates structure, provenance, limits, hashes and reconstruction coverage.

## Compression rule

Compression must reduce working-context size without reducing the system's ability to reproduce decision-relevant knowledge.

A distilled object may omit detail only when its source references allow the detail to be re-retrieved. The summary is therefore a cache, not a replacement for the archive.

## Reconstruction test

Before a distilled memory object becomes active, the host runs an independent reconstruction pass that receives the distilled representation and the original decision question, but not the original raw source set. It must answer what the distilled memory claims are sufficient to answer.

The host then compares the reconstruction against a source-grounded target prepared from the original records. Deterministic checks require:

- required claim IDs are covered;
- source references exist;
- no required source record is missing;
- no claim is marked validated without evidence;
- contradiction sets remain represented;
- the distillation budget is respected.

If required coverage fails, the memory object remains experimental and is not promoted to Active Brain.

## Retrieval discipline

Retrieval is hierarchical:

`Active Brain -> relevant Research Memory -> Raw Archive`

The host should stop once additional retrieval has low expected decision value. It may descend to the Raw Archive when a live decision requires an exact historical fact, original source, transaction detail, or audit reconstruction.

Retrieval is query-driven, not ticker-driven and not provider-driven. It may search by decision question, causal link, instrument, strategy, regime, failure mode, date range, source type, or exact claim.

## Distillation cadence

Full-cycle runs may perform lightweight memory hygiene when relevant. A deeper distillation pass runs periodically, preferably during the existing weekly learning window inside the same production scheduler.

The weekly pass should:

- identify oversized or redundant memory clusters;
- merge duplicated claims when provenance is preserved;
- separate stale facts from durable principles;
- retire invalidated summaries without deleting source history;
- promote validated reusable lessons into the Active Brain;
- demote stale or low-value material out of the Active Brain;
- run reconstruction tests on changed distilled objects;
- record compression ratios and failures;
- create follow-up experiments when repeated loss patterns appear.

## Active Brain budget

The host should treat the Active Brain as a bounded context budget, not an unlimited file. When full, admission becomes a selection problem. Retain information according to decision relevance, recency where appropriate, recurrence, validated predictive/process value, risk if forgotten, and uniqueness of information.

No item is removed solely because it is old. Old information can remain active when it encodes a durable structural lesson.

## Ex-ante protection

Distillation may summarize ex-post outcomes for learning, but it must never rewrite historical ex-ante evidence. A distilled lesson derived from later outcomes must carry its post-decision provenance and cannot be inserted into an old decision record.

## Failure behavior

If provenance is missing, reconstruction fails, or contradictions are silently collapsed, do not promote the distillation. Keep the source material retrievable and record the failed distillation attempt.

The system should prefer a larger but trustworthy memory over a smaller memory that has lost decision-relevant information.

## Completion target

The memory system is considered operational when:

1. raw and working layers are explicitly separated;
2. retrieval is query-driven;
3. distillation is versioned and provenance-preserving;
4. Active Brain admission is gated;
5. reconstruction testing exists;
6. stale material can leave Active Brain without deleting history;
7. the scheduler performs the lifecycle automatically;
8. failures are visible and cannot be silently promoted.
