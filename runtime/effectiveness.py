"""Deterministic decision-effectiveness and research-value mechanics.

The LLM owns interpretation and causal hypotheses. This module only computes
reproducible metrics from explicitly separated ex-ante decisions and ex-post
outcomes. It never invents outcomes and never selects a trading action.
"""
from __future__ import annotations

from copy import deepcopy

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Iterable, Mapping, Sequence


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 identity for the ex-ante decision state."""
    import hashlib
    return hashlib.sha256(canonical_json(dict(snapshot)).encode("utf-8")).hexdigest()


def build_ex_ante_snapshot(*, portfolio: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]],
                           research_trace: Sequence[Mapping[str, Any]] = (),
                           system_version: str = "unknown") -> dict[str, Any]:
    """Freeze only information available at decision time.

    deepcopy, not dict(). A shallow copy shares every nested structure with
    the caller, so mutating the source portfolio AFTER the snapshot was
    built changed the snapshot and left its stored snapshot_hash no longer
    matching its own content.

    That is the anti-hindsight mechanism failing open. The whole point of
    an ex-ante snapshot is that it cannot acquire information that arrived
    later; a snapshot that keeps tracking its source can, and the decision
    then looks as if it were made on data it never saw.
    """
    body = {
        "portfolio": deepcopy(dict(portfolio)),
        "evidence": [deepcopy(dict(x)) for x in evidence],
        "research_trace": [deepcopy(dict(x)) for x in research_trace],
        "system_version": system_version,
    }
    body["snapshot_hash"] = snapshot_hash(body)
    return body


def verify_snapshot_integrity(snapshot: Mapping[str, Any]) -> list[str]:
    """Confirm a snapshot still hashes to the value it carries.

    Freezing is only meaningful if someone checks. Returns defects; an
    empty list means the snapshot is unchanged since it was built.
    """
    errors: list[str] = []
    stored = snapshot.get("snapshot_hash")
    if not stored:
        return ["missing_snapshot_hash"]
    body = {k: v for k, v in snapshot.items() if k != "snapshot_hash"}
    if snapshot_hash(body) != stored:
        errors.append("snapshot_hash_mismatch")
    return errors


def validate_ex_post_link(*, decision: Mapping[str, Any], outcome: Mapping[str, Any]) -> list[str]:
    """Reject hindsight leakage and broken decision/outcome linkage."""
    errors: list[str] = []
    if not decision.get("decision_id"):
        errors.append("missing_decision_id")
    if not outcome.get("outcome_id"):
        errors.append("missing_outcome_id")
    if outcome.get("decision_id") != decision.get("decision_id"):
        errors.append("outcome_points_to_different_decision")
    expected = decision.get("snapshot_hash")
    if expected and outcome.get("snapshot_hash") != expected:
        errors.append("outcome_snapshot_hash_mismatch")
    if decision.get("as_of") and outcome.get("observed_at"):
        if _parse_ts(str(outcome["observed_at"])) < _parse_ts(str(decision["as_of"])):
            errors.append("outcome_precedes_decision")
    if any(key in decision for key in ("realized_return", "experienced_risk", "outcome")):
        errors.append("decision_contains_ex_post_field")
    return errors


@dataclass(frozen=True)
class EffectivenessResult:
    status: str
    decision_id: str
    outcome_id: str
    attribution_complete: bool
    process_score: float | None
    realized_return: float | None
    realized_risk: float | None
    benchmark_return: float | None
    opportunity_regret: float | None
    feasible_alternative_count: int
    blockers: tuple[str, ...]


def evaluate_decision(*, decision: Mapping[str, Any], outcome: Mapping[str, Any],
                      alternatives: Sequence[Mapping[str, Any]] = ()) -> EffectivenessResult:
    """Evaluate one decision using only outcomes linked to its frozen state.

    Alternatives must explicitly say they were feasible at the decision
    timestamp. Their later realized value is allowed in ex-post evaluation;
    their feasibility and candidate identity must have existed ex ante.
    """
    errors = validate_ex_post_link(decision=decision, outcome=outcome)
    decision_id = str(decision.get("decision_id", ""))
    outcome_id = str(outcome.get("outcome_id", ""))
    feasible: list[Mapping[str, Any]] = []
    for alt in alternatives:
        if not alt.get("feasible_as_of", False):
            continue
        if alt.get("decision_id") != decision_id:
            errors.append("alternative_points_to_different_decision")
            continue
        if not alt.get("candidate_id"):
            errors.append("alternative_missing_candidate_id")
            continue
        if alt.get("as_of") and decision.get("as_of"):
            if _parse_ts(str(alt["as_of"])) > _parse_ts(str(decision["as_of"])):
                errors.append(f"alternative_future_candidate:{alt['candidate_id']}")
                continue
        if not isinstance(alt.get("realized_value"), (int, float)):
            errors.append(f"alternative_missing_realized_value:{alt['candidate_id']}")
            continue
        feasible.append(alt)

    realized_return = outcome.get("realized_return")
    realized_risk = outcome.get("experienced_risk")
    benchmark_return = outcome.get("benchmark_return")
    opportunity_regret = None
    if isinstance(outcome.get("realized_value"), (int, float)) and feasible:
        best_alt = max(float(x["realized_value"]) for x in feasible)
        opportunity_regret = best_alt - float(outcome["realized_value"])

    # Process score is descriptive, not an investment score. Each supplied
    # trace row may set process_ok to 0/1 after the LLM has judged the row.
    trace = outcome.get("process_trace", ())
    if trace:
        flags = [float(x["process_ok"]) for x in trace if isinstance(x, Mapping) and x.get("process_ok") in (0, 1, 0.0, 1.0)]
        process_score = sum(flags) / len(flags) if flags else None
    else:
        process_score = None

    # "measured" used to mean only that attribution held. An outcome
    # carrying nothing but ids reported measured with realized_return,
    # realized_risk, benchmark_return, opportunity_regret and process_score
    # all None -- a measurement record containing no measurement. Downstream
    # that reads as evidence, which is how insufficient data becomes a
    # confident-looking row.
    measurements = (realized_return, realized_risk, benchmark_return,
                    opportunity_regret, process_score)
    has_measurement = any(
        value is not None and isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in measurements
    )
    if errors:
        status = "blocked"
    elif has_measurement:
        status = "measured"
    else:
        # The link is sound and nothing was observed. Distinct from blocked,
        # which means the link itself does not hold.
        status = "attributed_unmeasured"
    return EffectivenessResult(
        status=status,
        decision_id=decision_id,
        outcome_id=outcome_id,
        attribution_complete=not errors,
        process_score=process_score,
        realized_return=float(realized_return) if isinstance(realized_return, (int, float)) else None,
        realized_risk=float(realized_risk) if isinstance(realized_risk, (int, float)) else None,
        benchmark_return=float(benchmark_return) if isinstance(benchmark_return, (int, float)) else None,
        opportunity_regret=opportunity_regret,
        feasible_alternative_count=len(feasible),
        blockers=tuple(errors),
    )


@dataclass(frozen=True)
class CalibrationSummary:
    n: int
    brier: float | None
    log_loss: float | None
    hit_rate: float | None
    reliability: tuple[dict[str, Any], ...]
    status: str


def calibration_ledger(observations: Iterable[Mapping[str, Any]], *, min_samples: int = 100,
                       bins: int = 10) -> CalibrationSummary:
    rows = [x for x in observations if isinstance(x.get("probability"), (int, float)) and isinstance(x.get("outcome"), (int, float))]
    valid = [x for x in rows if 0 <= float(x["probability"]) <= 1 and float(x["outcome"]) in (0, 1)]
    if not valid:
        return CalibrationSummary(0, None, None, None, (), "insufficient_data")
    brier = sum((float(x["probability"]) - float(x["outcome"])) ** 2 for x in valid) / len(valid)
    eps = 1e-12
    log_loss = -sum(
        float(x["outcome"]) * math.log(max(float(x["probability"]), eps))
        + (1 - float(x["outcome"])) * math.log(max(1 - float(x["probability"]), eps))
        for x in valid
    ) / len(valid)
    hit_rate = sum((float(x["probability"]) >= 0.5) == bool(float(x["outcome"])) for x in valid) / len(valid)
    reliability: list[dict[str, Any]] = []
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        group = [x for x in valid if lo <= float(x["probability"]) < hi or (i == bins - 1 and float(x["probability"]) == hi)]
        if group:
            reliability.append({
                "bin": i,
                "n": len(group),
                "mean_probability": sum(float(x["probability"]) for x in group) / len(group),
                "observed_rate": sum(float(x["outcome"]) for x in group) / len(group),
            })
    # Sample count says whether the ledger is large enough to assess. It does
    # not say whether calibration is good, whether a Brier score is acceptable,
    # or whether any weight should change. Calling this "learned" let code
    # turn quantity into a semantic/economic verdict without a quality rule.
    status = (
        "sample_gate_met"
        if len(valid) >= min_samples
        else "insufficient_data"
    )
    return CalibrationSummary(len(valid), brier, log_loss, hit_rate, tuple(reliability), status)


def research_value_ledger(traces: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate specialist/tool usefulness without ranking strategies."""
    stats: dict[str, dict[str, Any]] = {}
    for row in traces:
        key = str(row.get("role_id") or row.get("tool") or "unknown")
        item = stats.setdefault(key, {"key": key, "runs": 0, "decision_uses": 0, "delta_sum": 0.0, "delta_observations": 0, "skipped": 0})
        item["runs"] += 1
        item["decision_uses"] += int(bool(row.get("cited_by_decision", False)))
        if isinstance(row.get("decision_delta"), (int, float)):
            item["delta_sum"] += float(row["decision_delta"])
            item["delta_observations"] += 1
        if row.get("status") == "skipped":
            item["skipped"] += 1
    out = []
    for item in stats.values():
        item = dict(item)
        item["mean_decision_delta"] = (item["delta_sum"] / item["delta_observations"] if item["delta_observations"] else None)
        out.append(item)
    return sorted(out, key=lambda x: x["key"])


def aggregate_effectiveness(results: Iterable[EffectivenessResult]) -> dict[str, Any]:
    rows = list(results)
    measured = [x for x in rows if x.status == "measured" and x.attribution_complete]
    returns = [x.realized_return for x in measured if x.realized_return is not None]
    regrets = [x.opportunity_regret for x in measured if x.opportunity_regret is not None]
    return {
        "decision_count": len(rows),
        "measured_count": len(measured),
        "attribution_rate": len(measured) / len(rows) if rows else None,
        "mean_realized_return": sum(returns) / len(returns) if returns else None,
        "mean_opportunity_regret": sum(regrets) / len(regrets) if regrets else None,
        "learning_status": "adequate_sample" if len(measured) >= 30 else "insufficient_outcomes",
    }


def evaluate_calibration_requests(
    requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compute calibration for host-selected observations and sample gates."""
    results = []
    for index, request in enumerate(requests):
        try:
            summary = calibration_ledger(
                request["observations"],
                min_samples=int(request["min_samples"]),
                bins=int(request.get("bins", 10)),
            )
        except (KeyError, TypeError, ValueError) as error:
            results.append({
                "index": index,
                "valid": False,
                "problems": [f"{type(error).__name__}:{error}"],
            })
            continue
        results.append({
            "index": index,
            "valid": True,
            "summary": {
                "n": summary.n,
                "brier": summary.brier,
                "log_loss": summary.log_loss,
                "hit_rate": summary.hit_rate,
                "reliability": [dict(row) for row in summary.reliability],
                "status": summary.status,
            },
        })
    return results
