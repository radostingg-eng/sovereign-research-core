# Self-Integrity Monitor v1

Self-integrity is a process-health layer. Poor integrity increases review and reduces confidence; it is never a reason to trade more.

## Running the checks

Part of this document is executable. Run it before you finish a session:

```
python3 -m runtime.integrity
```

It rebuilds the audit journal into a single hash chain, verifies it, and
exits non-zero on a defect. It reads only; it never rewrites the journal.

Checks marked `[gate]` below are enforced by that command and by CI.
Everything else is your judgment and stays your judgment: the gate cannot
tell whether a source claim was fabricated, only whether the record
citing it still hashes to what was written.

Known defects are grandfathered in `runtime/integrity_baseline.json` with
a reason. That list is shrink-only. Adding to it is a deliberate act;
when a defect is fixed, the gate fails until the line is deleted, so the
file cannot keep granting permission for something already repaired.

## Checks

### State integrity
- required state files readable `[gate]`
- no broken JSON `[gate]`
- version references agree `[gate]`
- every repo path a state file names actually exists `[gate]`
- no unexplained overwrite of historical records `[gate]`

### Causal integrity
- every decision cites findings
- every outcome cites its decision
- every lesson cites outcomes
- every system change cites evidence
- no dangling `caused_by` references `[gate]`
- hash chain intact, no orphaned, forked or silently dropped records `[gate]`

### Research integrity
- no fabricated source claims
- source timestamps recorded
- conflicts surfaced
- stale inputs marked
- tiny samples not presented as proof

### Portfolio integrity
- current exposure used before proposing a new position
- assignment and covered-call exposure considered
- hold/wait counterfactual considered
- long-horizon preference respected unless an explicit exception is justified

### Outcome integrity
- execution state is never inferred from an instruction
- unknown follow-through is preserved as `unknown`
- closed outcomes contain result and counterfactual when observable
- expired goals are graded or flagged

## Signals

Flag:
- repeated stale data
- repeated ungraded goals
- repeated recommendations with no new evidence
- source roles generating many unused findings
- adversarial re-derivation disagreement
- score calibration deterioration
- recurring thesis refutation ignored by new recommendations

## Response ladder

1. log incident
2. stop affected inference
3. re-check sources
4. repair the process or data contract
5. only then resume recommendations

Never respond to an integrity warning by increasing risk or research activity solely to improve a metric.
