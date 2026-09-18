# Evolution Runtime Contract

The Sovereign system is not considered self-evolving merely because evolution primitives exist.

A production evolution cycle must execute:

`observed outcome -> attributable lesson -> experiment -> statistical gate -> promotion/rollback -> versioned state -> next-run consumption -> audit`

Rules:

1. An unexecuted instruction is never treated as rejection.
2. Unknown thesis outcomes defer learning.
3. A single observation may create a lesson candidate but cannot change production weights.
4. Strategy/prompt changes require experiment gates and counter-metric protection.
5. Calibration weights remain seed-locked until total and out-of-sample sample gates pass.
6. Every promoted change must identify the outcome/lesson that caused it.
7. Every rejected or blocked experiment must remain recorded so the system can learn that the attempted change was not supported.
8. The scheduler may start the cycle, but trading logic belongs to the research/evolution runtime.
9. Safety constraints, including no automatic live-order submission, are immutable.

## Completion test

A run is an evolution success only when it either:

- promotes a statistically supported change and the next run consumes the new version; or
- correctly records why promotion is blocked/rejected and leaves production behavior unchanged.

Architecture without execution is not counted as evolution.
