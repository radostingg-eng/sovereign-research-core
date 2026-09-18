"""Outcome-linked experiment evaluation for prompts and strategy variants."""
from __future__ import annotations

from .profile_paths import code_root, profile_root
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

@dataclass(frozen=True)
class ExperimentDecision:
    status: str
    reason: str
    sample_size: int
    primary_delta: float
    counter_delta: float


def _finite(values: Sequence[float]) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def evaluate_variant(*, baseline: Sequence[float], variant: Sequence[float], baseline_counter: Sequence[float] | None = None, variant_counter: Sequence[float] | None = None, min_samples: int = 30, min_primary_delta: float = 0.0, max_counter_regression: float = 0.0, require_counter_metric: bool = True) -> ExperimentDecision:
    """Conservative promotion gate: adequate sample plus primary improvement and no counter-metric regression.

    This gate authorizes autonomous self-modification, so every branch
    below fails CLOSED. Four ways it previously returned "promote" on
    evidence that does not support promotion:

    * Non-finite measurements. ``NaN < 0.0`` is False in IEEE 754, so a
      NaN primary delta skipped the rejection branch and fell through to
      promote. A metric that is not a number is not evidence.
    * Unequal sample arrays were silently truncated to the shorter one
      with ``min(len(baseline), len(variant))``. Comparing 30 baseline
      observations against 5 variant observations is not a comparison.
    * Exactly zero gain passed, because the test was ``primary <
      min_primary_delta`` with a default of 0.0. A change measured to do
      nothing was promoted. Promotion now requires a strict improvement.
    * Absent counter-metrics defaulted to ``counter = 0.0``, so the
      regression gate passed trivially. Absence of evidence was being
      read as evidence of absence. Callers that genuinely have no counter
      metric must now opt out explicitly via require_counter_metric.
    """
    if len(baseline) != len(variant):
        return ExperimentDecision(
            "blocked", f"unequal_sample_sizes:{len(baseline)}v{len(variant)}",
            0, 0.0, 0.0)
    n = len(baseline)
    if n == 0:
        return ExperimentDecision("blocked", "no_outcomes", 0, 0.0, 0.0)
    if not _finite(baseline) or not _finite(variant):
        return ExperimentDecision("blocked", "non_finite_primary_measurements", n, 0.0, 0.0)

    primary = sum(variant) / n - sum(baseline) / n

    counter = 0.0
    have_counter = baseline_counter is not None and variant_counter is not None
    if have_counter:
        if len(baseline_counter) != len(variant_counter):
            return ExperimentDecision(
                "blocked",
                f"unequal_counter_sample_sizes:{len(baseline_counter)}v{len(variant_counter)}",
                n, primary, 0.0)
        m = len(baseline_counter)
        if m:
            if not _finite(baseline_counter) or not _finite(variant_counter):
                return ExperimentDecision("blocked", "non_finite_counter_measurements", n, primary, 0.0)
            counter = sum(variant_counter) / m - sum(baseline_counter) / m
        else:
            have_counter = False

    if n < min_samples:
        return ExperimentDecision("blocked", "insufficient_sample", n, primary, counter)
    if require_counter_metric and not have_counter:
        return ExperimentDecision("blocked", "missing_counter_metric", n, primary, counter)
    if not primary > min_primary_delta:
        return ExperimentDecision("rejected", "primary_metric_not_improved", n, primary, counter)
    if counter < -max_counter_regression:
        return ExperimentDecision("rejected", "counter_metric_regressed", n, primary, counter)
    return ExperimentDecision("promote", "gates_passed", n, primary, counter)


def calibration_update(*, seed: Mapping[str,float], observations: Sequence[Mapping[str,Any]], min_samples: int = 100, min_out_of_sample: int = 40) -> dict[str,Any]:
    """Return assessable proposed weights after total and OOS sample gates.

    The function intentionally does not invent a fitting algorithm. A host may
    supply proposed weights and OOS evidence; without it the seed is retained.
    Passing the sample gate does not mean the weights were learned or adopted.
    """
    proposed = observations[-1].get("proposed_weights") if observations else None
    oos = int(observations[-1].get("oos_samples",0)) if observations else 0
    if len(observations)<min_samples or oos<min_out_of_sample or not isinstance(proposed,dict):
        return {"status":"seed_locked","weights":dict(seed),"sample_size":len(observations),"oos_samples":oos}
    return {
        "status": "sample_gate_met",
        "seed_weights": dict(seed),
        "proposed_weights": {k: float(v) for k, v in proposed.items()},
        "sample_size": len(observations),
        "oos_samples": oos,
    }


_ARTIFACT_FIELDS = {
    "experiment_id", "created_at", "hypothesis",
    "strongest_counter_hypothesis", "data_required", "evidence",
    "evaluation_method", "evaluation_window", "success_criteria",
    "failure_criteria", "status", "caused_by",
}


def validate_experiment_artifact(value: Mapping[str, Any]) -> list[str]:
    """Validate a host-authored experiment without judging its hypothesis."""
    errors = [
        f"missing:{field}"
        for field in sorted(_ARTIFACT_FIELDS - set(value))
    ]
    for field in (
        "data_required", "evidence", "success_criteria",
        "failure_criteria", "caused_by",
    ):
        if field in value and (
            not isinstance(value[field], Sequence)
            or isinstance(value[field], (str, bytes))
        ):
            errors.append(f"invalid:{field}")
    return errors


def evaluate_experiment_requests(
    requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate host-supplied paired evidence without applying a promotion."""
    results = []
    for index, request in enumerate(requests):
        try:
            result = evaluate_variant(
                baseline=request["baseline"],
                variant=request["variant"],
                baseline_counter=request.get("baseline_counter"),
                variant_counter=request.get("variant_counter"),
                min_samples=int(request["min_samples"]),
                min_primary_delta=float(
                    request.get("min_primary_delta", 0.0)),
                max_counter_regression=float(
                    request.get("max_counter_regression", 0.0)),
                require_counter_metric=bool(
                    request.get("require_counter_metric", True)),
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
            "decision": {
                "status": result.status,
                "reason": result.reason,
                "sample_size": result.sample_size,
                "primary_delta": result.primary_delta,
                "counter_delta": result.counter_delta,
            },
        })
    return results


def main(argv: list[str] | None = None) -> int:
    """Validate every committed host-authored experiment."""
    argv = sys.argv[1:] if argv is None else argv
    root = (
        Path(argv[0]).resolve()
        if argv
        else profile_root() / "experiments"
    )
    errors: list[str] = []
    count = 0
    for path in sorted(root.glob("*.json")):
        count += 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{path.name}:unreadable:{error}")
            continue
        if not isinstance(value, Mapping):
            errors.append(f"{path.name}:not_an_object")
            continue
        errors.extend(
            f"{path.name}:{error}"
            for error in validate_experiment_artifact(value)
        )
    if errors:
        for error in errors:
            print(f"EXPERIMENT INVALID: {error}")
        return 1
    print(f"experiments: valid ({count} artifacts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
