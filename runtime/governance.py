"""Deterministic governance primitives for adversarial review, goals, prompts and integrity."""
from __future__ import annotations
from .profile_paths import code_root, profile_root
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ACTIONS = {"support", "modify", "reject", "insufficient_evidence"}
DIVERGENCES = {"none", "evidence_drift", "reasoning_gap", "portfolio_gap", "instrument_gap", "tax_or_cost_gap", "data_conflict"}

@dataclass(frozen=True)
class AdversarialResult:
    action: str
    divergence: str
    factual_agreement: float
    thesis_agreement: float
    action_agreement: float
    evidence_completeness: float
    counterfactual_quality: float
    missing_risk_count: int
    follow_up: str

def adversarial_review(original: Mapping[str, Any], independent: Mapping[str, Any]) -> AdversarialResult:
    """Compare an independently derived case only after its conclusion is supplied."""
    action = str(independent.get("action", "insufficient_evidence"))
    if action not in ACTIONS:
        raise ValueError("invalid adversarial action")
    fa = float(independent.get("factual_agreement", 0.0)); ta = float(independent.get("thesis_agreement", 0.0))
    aa = 1.0 if action == str(original.get("action")) else 0.0
    ec = float(independent.get("evidence_completeness", 0.0)); cq = float(independent.get("counterfactual_quality", 0.0))
    missing = int(independent.get("missing_risk_count", 0))
    if not all(0.0 <= x <= 1.0 for x in (fa, ta, aa, ec, cq)) or missing < 0:
        raise ValueError("invalid adversarial metrics")
    if action == "insufficient_evidence": divergence = "data_conflict" if independent.get("data_conflict") else "reasoning_gap"
    elif action != str(original.get("action")): divergence = str(independent.get("divergence", "reasoning_gap"))
    else: divergence = "none"
    if divergence not in DIVERGENCES: raise ValueError("invalid divergence")
    return AdversarialResult(action, divergence, fa, ta, aa, ec, cq, missing, str(independent.get("follow_up", "recheck evidence")))

@dataclass(frozen=True)
class Goal:
    goal_id: str; category: str; statement: str; deadline: str; success_metric: str; success_target: Any
    metric_type: str; baseline: Any; caused_by: tuple[str, ...]

def validate_goal(goal: Mapping[str, Any]) -> list[str]:
    required = {"goal_id","category","statement","deadline","success_metric","success_target","evaluation_rubric","metric_type","baseline","caused_by"}
    errors = [f"missing:{x}" for x in sorted(required - set(goal))]
    if goal.get("metric_type") not in {"controllable","observable","mixed"}: errors.append("invalid:metric_type")
    if not isinstance(goal.get("caused_by"), list): errors.append("invalid:caused_by")
    return errors


GOAL_DIRECTIONS = frozenset({"higher_is_better", "lower_is_better"})
GOAL_CREATION_STRING_FIELDS = (
    "goal_id",
    "created_at",
    "category",
    "statement",
    "deadline",
    "success_metric",
    "evaluation_rubric",
    "direction",
)
GOAL_PROGRESS_FIELDS = frozenset({
    "mode",
    "goal_id",
    "observed_at",
    "observed_value",
    "assessment",
    "evidence",
    "caused_by",
})
GOAL_CLOSE_FIELDS = frozenset({
    "mode",
    "goal_id",
    "observed_at",
    "closure_basis",
    "observed_value",
    "evidence",
    "caused_by",
    "invalidation_reason",
    "analysis",
})
GOAL_ANALYSIS_FIELDS = frozenset({
    "causal_summary",
    "worked",
    "failed",
    "counterfactual",
    "next_change",
})
GOAL_TERMINAL_STATUSES = frozenset({
    "met",
    "partially_met",
    "missed",
    "invalidated",
})


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_goal_evidence(
    request: Mapping[str, Any],
    allowed_causes: Sequence[str],
) -> list[str]:
    problems = []
    evidence = request.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        problems.append("evidence_empty")
    elif len(evidence) > 5:
        problems.append("evidence_too_many")
    else:
        allowed = set(map(str, allowed_causes))
        expected = {"evidence_id", "source", "finding"}
        for index, row in enumerate(evidence):
            if not isinstance(row, Mapping):
                problems.append(f"evidence_{index}_not_object")
                continue
            if set(row) - expected:
                problems.append(f"evidence_{index}_unexpected_fields")
            for field in ("evidence_id", "source", "finding"):
                text = str(row.get(field, "")).strip()
                if not text:
                    problems.append(f"evidence_{index}_{field}_missing")
                elif len(text) > 1000:
                    problems.append(f"evidence_{index}_{field}_too_long")
            evidence_id = str(row.get("evidence_id", "")).strip()
            if evidence_id and evidence_id not in allowed:
                problems.append(f"evidence_{index}_not_in_current_cycle")
    causes = request.get("caused_by")
    if not isinstance(causes, list) or not causes:
        problems.append("caused_by_empty")
    elif not set(map(str, causes)).issubset(map(str, allowed_causes)):
        problems.append("caused_by_not_in_current_cycle")
    return problems


def _goal_event_time(payload: Mapping[str, Any]) -> datetime | None:
    goal_value = payload.get("goal")
    goal = goal_value if isinstance(goal_value, Mapping) else {}
    value = (
        payload.get("observed_at")
        or payload.get("opened_at")
        or goal.get("created_at")
    )
    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def validate_goal_creation(
    request: Mapping[str, Any],
    *,
    cycle_as_of: str,
    allowed_causes: Sequence[str],
) -> list[str]:
    """Validate one bounded, machine-gradable open-goal request."""
    if request.get("mode") != "create":
        return ["mode_unknown"]
    goal = request.get("goal")
    if not isinstance(goal, Mapping):
        return ["goal_not_an_object"]
    problems = validate_goal(goal)
    if "created_at" not in goal:
        problems.append("missing_created_at")
    if "direction" not in goal:
        problems.append("missing_direction")
    for field in GOAL_CREATION_STRING_FIELDS:
        if field in goal and not str(goal.get(field, "")).strip():
            problems.append(f"empty_{field}")
    if goal.get("metric_type") != "controllable":
        problems.append("metric_type_not_controllable")
    for field in ("baseline", "partial_target", "success_target"):
        value = goal.get(field)
        if not _finite_number(value):
            problems.append(f"{field}_not_numeric")
    if goal.get("direction") not in GOAL_DIRECTIONS:
        problems.append("direction_invalid")
    baseline = goal.get("baseline")
    partial = goal.get("partial_target")
    target = goal.get("success_target")
    if all(_finite_number(value) for value in (
        baseline, partial, target,
    )):
        if goal.get("direction") == "higher_is_better" and not (
            float(baseline) < float(partial) < float(target)
        ):
            problems.append("partial_target_not_between_baseline_and_target")
        if goal.get("direction") == "lower_is_better" and not (
            float(baseline) > float(partial) > float(target)
        ):
            problems.append("partial_target_not_between_baseline_and_target")
    try:
        created = datetime.fromisoformat(
            str(goal.get("created_at", "")).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(
            str(cycle_as_of).replace("Z", "+00:00"))
    except ValueError:
        problems.append("created_at_invalid")
    else:
        if created.tzinfo is None or created.utcoffset() is None:
            problems.append("created_at_timezone_required")
        elif observed.tzinfo is None or observed.utcoffset() is None:
            problems.append("cycle_as_of_timezone_required")
        elif created != observed:
            problems.append("created_at_must_equal_cycle_as_of")
    try:
        deadline = datetime.fromisoformat(
            str(goal.get("deadline", "")).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(
            str(cycle_as_of).replace("Z", "+00:00"))
    except ValueError:
        problems.append("deadline_invalid")
    else:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            problems.append("deadline_timezone_required")
        elif deadline <= observed:
            problems.append("deadline_not_future")
    causes = goal.get("caused_by")
    if not isinstance(causes, list) or not causes:
        problems.append("caused_by_empty")
    elif not set(map(str, causes)).issubset(map(str, allowed_causes)):
        problems.append("caused_by_not_in_current_cycle")
    return sorted(set(problems))


def validate_goal_progress(
    request: Mapping[str, Any],
    *,
    cycle_as_of: str,
    allowed_causes: Sequence[str],
    open_goal: Mapping[str, Any] | None,
) -> list[str]:
    """Validate one observation without grading or mutating its goal."""
    problems = []
    unexpected = sorted(set(request) - GOAL_PROGRESS_FIELDS)
    problems.extend(f"unexpected_field_{field}" for field in unexpected)
    if request.get("mode") != "progress":
        problems.append("mode_unknown")
    goal_id = str(request.get("goal_id", "")).strip()
    if not goal_id:
        problems.append("goal_id_missing")
    if open_goal is None or open_goal.get("status") != "open":
        problems.append("goal_not_open")
    elif str(open_goal.get("goal_id", "")) != goal_id:
        problems.append("goal_id_not_open")
    for field in ("observed_at", "assessment"):
        if not str(request.get(field, "")).strip():
            problems.append(f"{field}_missing")
    if len(str(request.get("assessment", ""))) > 2000:
        problems.append("assessment_too_long")
    value = request.get("observed_value")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        problems.append("observed_value_not_finite_number")
    try:
        observed = datetime.fromisoformat(
            str(request.get("observed_at", "")).replace("Z", "+00:00"))
        cycle_time = datetime.fromisoformat(
            str(cycle_as_of).replace("Z", "+00:00"))
    except ValueError:
        problems.append("observed_at_invalid")
    else:
        observed_has_timezone = (
            observed.tzinfo is not None
            and observed.utcoffset() is not None
        )
        if not observed_has_timezone:
            problems.append("observed_at_timezone_required")
        elif cycle_time.tzinfo is None or cycle_time.utcoffset() is None:
            problems.append("cycle_as_of_timezone_required")
        elif observed != cycle_time:
            problems.append("observed_at_must_equal_cycle_as_of")
        if open_goal is not None:
            goal_value = open_goal.get("goal")
            goal = goal_value if isinstance(goal_value, Mapping) else {}
            previous_at = (
                open_goal.get("observed_at")
                or open_goal.get("opened_at")
                or goal.get("created_at")
            )
            try:
                previous_time = datetime.fromisoformat(
                    str(previous_at).replace("Z", "+00:00"))
            except ValueError:
                problems.append("previous_goal_time_invalid")
            else:
                previous_has_timezone = (
                    previous_time.tzinfo is not None
                    and previous_time.utcoffset() is not None
                )
                if not previous_has_timezone:
                    problems.append("previous_goal_time_invalid")
                elif observed_has_timezone and observed <= previous_time:
                    problems.append("observed_at_not_after_previous")
    problems.extend(_validate_goal_evidence(request, allowed_causes))
    return sorted(set(problems))


def validate_goal_close(
    request: Mapping[str, Any],
    *,
    cycle_as_of: str,
    allowed_causes: Sequence[str],
    open_goal: Mapping[str, Any] | None,
    created_goal: Mapping[str, Any] | None,
) -> list[str]:
    """Validate terminal evidence while keeping the grade runtime-owned."""
    problems = [
        f"unexpected_field_{field}"
        for field in sorted(set(request) - GOAL_CLOSE_FIELDS)
    ]
    if request.get("mode") != "close":
        problems.append("mode_unknown")
    goal_id = str(request.get("goal_id", "")).strip()
    if not goal_id:
        problems.append("goal_id_missing")
    if open_goal is None or open_goal.get("status") != "open":
        problems.append("goal_not_open")
    if created_goal is None:
        problems.append("goal_creation_event_missing")
        goal: Mapping[str, Any] = {}
    else:
        goal_value = created_goal.get("goal")
        goal = goal_value if isinstance(goal_value, Mapping) else {}
        if str(created_goal.get("goal_id", "")) != goal_id:
            problems.append("goal_id_not_open")

    basis = request.get("closure_basis")
    if basis not in {"measurement", "invalidated"}:
        problems.append("closure_basis_invalid")
    for field in ("observed_at",):
        if not str(request.get(field, "")).strip():
            problems.append(f"{field}_missing")
    try:
        observed = datetime.fromisoformat(
            str(request.get("observed_at", "")).replace("Z", "+00:00"))
        cycle_time = datetime.fromisoformat(
            str(cycle_as_of).replace("Z", "+00:00"))
    except ValueError:
        problems.append("observed_at_invalid")
        observed = None
    else:
        observed_has_timezone = (
            observed.tzinfo is not None
            and observed.utcoffset() is not None
        )
        if not observed_has_timezone:
            problems.append("observed_at_timezone_required")
        elif cycle_time.tzinfo is None or cycle_time.utcoffset() is None:
            problems.append("cycle_as_of_timezone_required")
        elif observed != cycle_time:
            problems.append("observed_at_must_equal_cycle_as_of")
        previous = (
            _goal_event_time(open_goal)
            if open_goal is not None
            else None
        )
        if previous is None:
            problems.append("previous_goal_time_invalid")
        elif observed_has_timezone and observed <= previous:
            problems.append("observed_at_not_after_previous")

    value = request.get("observed_value")
    invalidation_reason = str(
        request.get("invalidation_reason", "")).strip()
    if basis == "measurement":
        if not _finite_number(value):
            problems.append("observed_value_not_finite_number")
        if invalidation_reason:
            problems.append("invalidation_reason_forbidden")
        try:
            deadline = datetime.fromisoformat(
                str(goal.get("deadline", "")).replace("Z", "+00:00"))
        except ValueError:
            problems.append("goal_deadline_invalid")
        else:
            if (
                deadline.tzinfo is None
                or deadline.utcoffset() is None
            ):
                problems.append("goal_deadline_invalid")
            elif (
                observed is not None
                and observed.tzinfo is not None
                and observed.utcoffset() is not None
                and observed < deadline
            ):
                problems.append("measurement_before_deadline")
        if goal_terminal_status(goal, value) is None:
            problems.append("goal_ungradable")
    elif basis == "invalidated":
        if "observed_value" in request:
            problems.append("observed_value_forbidden_for_invalidation")
        if not invalidation_reason:
            problems.append("invalidation_reason_missing")
        elif len(invalidation_reason) > 2000:
            problems.append("invalidation_reason_too_long")

    problems.extend(_validate_goal_evidence(request, allowed_causes))
    analysis = request.get("analysis")
    if not isinstance(analysis, Mapping):
        problems.append("analysis_not_object")
    else:
        for field in sorted(set(analysis) - GOAL_ANALYSIS_FIELDS):
            problems.append(f"analysis_unexpected_field_{field}")
        for field in (
            "causal_summary",
            "counterfactual",
            "next_change",
        ):
            text = str(analysis.get(field, "")).strip()
            if not text:
                problems.append(f"analysis_{field}_missing")
            elif len(text) > 2000:
                problems.append(f"analysis_{field}_too_long")
        total_points = 0
        for field in ("worked", "failed"):
            values = analysis.get(field)
            if not isinstance(values, list):
                problems.append(f"analysis_{field}_not_list")
                continue
            if len(values) > 5:
                problems.append(f"analysis_{field}_too_many")
            for index, item in enumerate(values):
                text = str(item).strip()
                if not text:
                    problems.append(
                        f"analysis_{field}_{index}_empty")
                elif len(text) > 1000:
                    problems.append(
                        f"analysis_{field}_{index}_too_long")
                else:
                    total_points += 1
        if total_points == 0:
            problems.append("analysis_worked_or_failed_required")
    return sorted(set(problems))


def make_goal(goal: Mapping[str, Any]) -> Goal:
    errors = validate_goal(goal)
    if errors: raise ValueError("invalid_goal:" + ",".join(errors))
    return Goal(str(goal["goal_id"]), str(goal["category"]), str(goal["statement"]), str(goal["deadline"]), str(goal["success_metric"]), goal["success_target"], str(goal["metric_type"]), goal["baseline"], tuple(goal["caused_by"]))

def goal_direction(goal: Mapping[str, Any]) -> str | None:
    """Which way is better for this goal, or None when nothing says.

    Grading assumed higher_is_better for every goal, so a reduction goal was
    graded upside down: "at most 1 integrity error" scored 10 errors as met
    and 0 errors as missed. The best possible result was the failing one.

    An explicit direction wins. Otherwise it is inferred from the goal's own
    declared baseline and target, which is arithmetic on stated values rather
    than a judgement about what the goal means. When the two are equal there
    is no basis and this returns None.
    """
    declared = goal.get("direction")
    if declared in {"higher_is_better", "lower_is_better"}:
        return str(declared)
    baseline = goal.get("baseline")
    target = goal.get("success_target")
    if (isinstance(baseline, (int, float)) and isinstance(target, (int, float))
            and not isinstance(baseline, bool) and not isinstance(target, bool)
            and float(baseline) != float(target)):
        return "lower_is_better" if float(target) < float(baseline) else "higher_is_better"
    return None


def goal_terminal_status(
    goal: Mapping[str, Any],
    observed_value: Any,
) -> str | None:
    """Grade against declared target boundaries without invented tolerance."""
    target = goal.get("success_target")
    if not _finite_number(target) or not _finite_number(observed_value):
        return None
    direction = goal_direction(goal)
    if direction is None:
        return None
    observed = float(observed_value)
    success = float(target)
    partial_value = goal.get("partial_target")
    partial = (
        float(partial_value)
        if _finite_number(partial_value)
        else None
    )
    if direction == "higher_is_better":
        if observed >= success:
            return "met"
        if partial is not None and observed >= partial:
            return "partially_met"
        return "missed"
    if observed <= success:
        return "met"
    if partial is not None and observed <= partial:
        return "partially_met"
    return "missed"


def _grade_numeric(goal: Mapping[str, Any], target: float, observed: float) -> str:
    direction = goal_direction(goal)
    if direction is None:
        # Guessing a direction here is how 10 errors became "met". An
        # ungradable goal is a reportable state, not a passing one.
        return "ungraded_direction_unknown"
    # The partial band is a quarter of the target's magnitude, which keeps
    # the previous 0.75x behaviour for positive higher-is-better targets and
    # stays on the correct side of zero for negative ones.
    tolerance = abs(float(target)) * 0.25
    if direction == "lower_is_better":
        if observed <= target: return "met"
        return "partially_met" if observed <= target + tolerance else "missed"
    if observed >= target: return "met"
    return "partially_met" if observed >= target - tolerance else "missed"


def grade_goal(goal: Mapping[str, Any], observed_value: Any, *, now: str, invalidated: bool = False) -> dict[str, Any]:
    """Grade a goal using its declared rubric; expired ungraded goals are flagged."""
    deadline = datetime.fromisoformat(str(goal["deadline"]).replace("Z", "+00:00")); current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if invalidated: status = "invalidated"
    else:
        target = goal["success_target"]
        if isinstance(target, (int,float)) and isinstance(observed_value, (int,float)) and not isinstance(target, bool):
            status = _grade_numeric(goal, target, observed_value)
        else: status = "met" if observed_value == target else "partially_met"
    return {"goal_id": goal["goal_id"], "status": status, "observed_value": observed_value, "expired": current > deadline, "graded_at": now, "direction": goal_direction(goal), "caused_by": [goal["goal_id"]]}

@dataclass(frozen=True)
class PromptVariant:
    prompt_id: str; version: int; body: str; state: str; baseline: str | None; hypothesis: str; success_metric: str; counter_metric: str; rollback_to: str | None

def validate_prompt_variant(p: Mapping[str, Any]) -> list[str]:
    required = {"prompt_id","version","body","state","hypothesis","success_metric","counter_metric","rollback_to"}
    errors = [f"missing:{x}" for x in sorted(required - set(p))]
    if p.get("state") not in {"candidate","testing","adopted","retired"}: errors.append("invalid:state")
    if p.get("state") in {"testing","adopted"} and not p.get("baseline"): errors.append("missing:baseline")
    return errors

def prompt_variant(p: Mapping[str, Any]) -> PromptVariant:
    errors = validate_prompt_variant(p)
    if errors: raise ValueError("invalid_prompt:" + ",".join(errors))
    return PromptVariant(str(p["prompt_id"]), int(p["version"]), str(p["body"]), str(p["state"]), p.get("baseline"), str(p["hypothesis"]), str(p["success_metric"]), str(p["counter_metric"]), p.get("rollback_to"))

def promote_prompt(p: Mapping[str, Any], *, success: bool, counter_metric_ok: bool) -> dict[str, Any]:
    """Adopt only after both the primary metric and anti-gaming counter-metric pass."""
    row = dict(p)
    if row.get("state") != "testing": raise ValueError("prompt must be testing before promotion")
    row["state"] = "adopted" if success and counter_metric_ok else "retired"
    return row

def rollback_prompt(p: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(p)
    if not row.get("rollback_to"): raise ValueError("missing rollback target")
    row["state"] = "retired"; row["rollback_applied"] = row["rollback_to"]
    return row

@dataclass(frozen=True)
class IntegrityReport:
    ok: bool; incidents: tuple[str, ...]; confidence_modifier: float

def integrity_check(*, required_files_ok: bool, chain_errors: Sequence[str], dangling: Sequence[str], stale_count: int = 0, ungraded_expired_goals: int = 0, repeated_no_evidence: int = 0, adversarial_disagreements: int = 0) -> IntegrityReport:
    incidents = list(chain_errors) + list(dangling)
    if not required_files_ok: incidents.append("state_files_invalid")
    if stale_count: incidents.append(f"stale_inputs:{stale_count}")
    if ungraded_expired_goals: incidents.append(f"ungraded_expired_goals:{ungraded_expired_goals}")
    if repeated_no_evidence: incidents.append(f"repeated_no_evidence:{repeated_no_evidence}")
    if adversarial_disagreements: incidents.append(f"adversarial_disagreements:{adversarial_disagreements}")
    modifier = max(0.0, 1.0 - 0.05 * len(incidents))
    return IntegrityReport(not incidents, tuple(incidents), modifier)

def choose_wakeup(*, now: str, urgency_hours: float, data_freshness_hours: float, unresolved_count: int, base_hours: int = 24) -> dict[str, Any]:
    """Adaptive cadence suggestion. Scheduler executes it but does not decide trading logic."""
    urgency = max(1.0, min(float(base_hours), float(urgency_hours)))
    freshness = max(1.0, min(float(base_hours), float(data_freshness_hours)))
    pressure = 2.0 if unresolved_count >= 5 else 1.0
    hours = max(1.0, min(float(base_hours), min(urgency, freshness) / pressure))
    return {"recommended_hours": hours, "reason": "urgency/freshness/unresolved-work pressure", "computed_at": now}


def main(argv: list[str] | None = None) -> int:
    """Validate every committed host-authored goal."""
    argv = sys.argv[1:] if argv is None else argv
    root = (
        Path(argv[0]).resolve()
        if argv
        else profile_root() / "goals"
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
        errors.extend(f"{path.name}:{error}" for error in validate_goal(value))
    if errors:
        for error in errors:
            print(f"GOAL INVALID: {error}")
        return 1
    print(f"governance goals: valid ({count} artifacts)")
    return 0


def evaluate_goal_observations(
    requests: Sequence[Any],
) -> list[dict[str, Any]]:
    """Grade host-authored goals against host-supplied observations."""
    results = []
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            results.append({
                "index": index,
                "valid": False,
                "problems": ["goal_observation_not_an_object"],
            })
            continue
        if request.get("mode") == "create":
            goal = request.get("goal")
            causes = (
                list(goal.get("caused_by") or ())
                if isinstance(goal, Mapping)
                else []
            )
            problems = validate_goal_creation(
                request,
                cycle_as_of=(
                    str(goal.get("created_at", ""))
                    if isinstance(goal, Mapping)
                    else ""
                ),
                allowed_causes=causes,
            )
            if problems:
                results.append({
                    "index": index,
                    "valid": False,
                    "mode": "create",
                    "problems": problems,
                })
            else:
                results.append({
                    "index": index,
                    "valid": True,
                    "mode": "create",
                    "event": {
                        "goal_id": goal["goal_id"],
                        "event": "created",
                        "status": "open",
                        "opened_at": goal["created_at"],
                    },
                })
            continue
        if request.get("mode") == "progress":
            value = request.get("observed_value")
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
            results.append({
                "index": index,
                "valid": valid,
                "mode": "progress",
                "authority": "journal_prevalidation",
                **({
                    "event": {
                        "goal_id": request.get("goal_id"),
                        "event": "progress",
                        "status": "open",
                        "observed_value": value,
                    },
                } if valid else {
                    "problems": ["observed_value_not_finite_number"],
                }),
            })
            continue
        if request.get("mode") == "close":
            results.append({
                "index": index,
                "valid": True,
                "mode": "close",
                "authority": "journal_prevalidation",
                "event": {
                    "goal_id": request.get("goal_id"),
                    "event": "closed",
                    "status": "pending_journal_persistence",
                },
            })
            continue
        goal = request.get("goal")
        if not isinstance(goal, Mapping):
            results.append({
                "index": index,
                "valid": False,
                "problems": ["goal_not_an_object"],
            })
            continue
        errors = validate_goal(goal)
        if errors:
            results.append({
                "index": index,
                "valid": False,
                "problems": errors,
            })
            continue
        try:
            grade = grade_goal(
                goal,
                request.get("observed_value"),
                now=str(request["now"]),
                invalidated=bool(request.get("invalidated", False)),
            )
        except (KeyError, TypeError, ValueError) as error:
            results.append({
                "index": index,
                "valid": False,
                "problems": [f"{type(error).__name__}:{error}"],
            })
            continue
        results.append({"index": index, "valid": True, "grade": grade})
    return results


def latest_goal_states(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Latest append-order state for every journal-backed goal."""
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("record_type") != "goal_event":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        goal_id = str(payload.get("goal_id", "")).strip()
        if goal_id:
            latest[goal_id] = dict(payload)
    return latest


def created_goal_states(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Original creation payload for every journal-backed goal."""
    created: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("record_type") != "goal_event":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        goal_id = str(payload.get("goal_id", "")).strip()
        if goal_id and payload.get("event") == "created":
            created.setdefault(goal_id, dict(payload))
    return created


def closed_goal_states(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Latest valid closed state per goal, ordered by journal append."""
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, record in enumerate(records):
        if record.get("record_type") != "goal_event":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        goal_id = str(payload.get("goal_id", "")).strip()
        if goal_id:
            latest[goal_id] = (index, dict(payload))
    valid = []
    excluded = {"invalid_terminal_status": 0}
    for index, payload in sorted(latest.values()):
        if payload.get("status") != "closed":
            continue
        if payload.get("terminal_status") not in GOAL_TERMINAL_STATUSES:
            excluded["invalid_terminal_status"] += 1
            continue
        valid.append(payload)
    return valid, {
        reason: count
        for reason, count in excluded.items()
        if count
    }


def summarise_goals(
    records: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Latest durable goal states, with open goals bounded for feedback."""
    latest = latest_goal_states(records)
    created = created_goal_states(records)
    progress: dict[str, list[dict[str, Any]]] = {}
    closed_events, closed_exclusions = closed_goal_states(records)
    for record in records:
        if record.get("record_type") != "goal_event":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        goal_id = str(payload.get("goal_id", "")).strip()
        if not goal_id:
            continue
        if payload.get("event") == "progress":
            progress.setdefault(goal_id, []).append(dict(payload))
    current = now or datetime.now(timezone.utc)
    open_rows = []
    expired_count = 0
    for payload in latest.values():
        if payload.get("status") != "open":
            continue
        goal_id = str(payload.get("goal_id", ""))
        creation = created.get(goal_id, payload)
        goal_value = creation.get("goal")
        if not isinstance(goal_value, Mapping):
            continue
        goal = dict(goal_value)
        try:
            deadline = datetime.fromisoformat(
                str(goal.get("deadline", "")).replace("Z", "+00:00"))
            expired = deadline <= current
        except ValueError:
            expired = True
        if expired:
            expired_count += 1
        open_rows.append({
            **goal,
            "status": "open",
            "expired": expired,
            "source_cycle_id": creation.get("source_cycle_id"),
            "latest_observed_value": payload.get(
                "observed_value", goal.get("baseline")),
            "latest_assessment": payload.get("assessment"),
            "remaining_to_target": payload.get("remaining_to_target"),
            "last_progress_cycle_id": (
                payload.get("source_cycle_id")
                if payload.get("event") == "progress"
                else None
            ),
            "progress": [
                {
                    "observed_at": row.get("observed_at"),
                    "observed_value": row.get("observed_value"),
                    "previous_value": row.get("previous_value"),
                    "delta": row.get("delta"),
                    "remaining_to_target": row.get(
                        "remaining_to_target"),
                    "assessment": row.get("assessment"),
                    "source_cycle_id": row.get("source_cycle_id"),
                }
                for row in progress.get(goal_id, ())[-3:]
            ],
        })
    open_rows = open_rows[-limit:]
    recent_closed = []
    for payload in closed_events[-limit:]:
        goal_id = str(payload.get("goal_id", ""))
        creation = created.get(goal_id, payload)
        goal_value = creation.get("goal")
        goal = dict(goal_value) if isinstance(goal_value, Mapping) else {}
        recent_closed.append({
            **goal,
            "status": "closed",
            "terminal_status": payload.get("terminal_status"),
            "created_cycle_id": creation.get("source_cycle_id"),
            "closed_cycle_id": payload.get("source_cycle_id"),
            "closed_at": payload.get("closed_at"),
            "observed_value": payload.get("observed_value"),
            "last_observed_value": payload.get("last_observed_value"),
            "closure_basis": payload.get("closure_basis"),
            "invalidation_reason": payload.get("invalidation_reason"),
            "analysis": payload.get("analysis"),
            "progress_count": payload.get("progress_count", 0),
        })
    return {
        "open_count": sum(
            1 for payload in latest.values()
            if payload.get("status") == "open"
        ),
        "expired_count": expired_count,
        "open": open_rows,
        "closed_count": len(closed_events),
        "excluded_closed_count": sum(closed_exclusions.values()),
        "invalidated_count": sum(
            1 for payload in closed_events
            if payload.get("terminal_status") == "invalidated"
        ),
        "recent_closed": recent_closed,
        "what_this_means": (
            "The append-only journal is the live goal authority. Open goals "
            "are priorities to evaluate, not automatic portfolio actions. "
            "Progress values may improve, stay flat, or regress and do not "
            "close the goal. Closed goals carry a runtime-computed terminal "
            "status and the host's evidence-linked causal analysis."
        ),
    }


def _outcome_counts() -> dict[str, int]:
    return {
        "met": 0,
        "partially_met": 0,
        "missed": 0,
        "invalidated": 0,
    }


def _bounded_identity(field: str, value: str) -> dict[str, Any]:
    limit = 200
    if len(value) <= limit:
        return {field: value, f"{field}_truncated": False}
    return {
        field: value[:limit],
        f"{field}_truncated": True,
        f"{field}_sha256": hashlib.sha256(
            value.encode("utf-8")).hexdigest(),
    }


def summarise_goal_attribution(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Describe outcome associations without ranking or causal inference."""
    closed, exclusions = closed_goal_states(records)
    created = created_goal_states(records)
    outcome_counts = _outcome_counts()
    pattern_groups: dict[tuple[str, str, str], dict[str, int]] = {}
    origin_groups: dict[str, dict[str, int]] = {}
    source_groups: dict[str, dict[str, int]] = {}
    coverage = {
        "closures_with_goal_pattern": 0,
        "closures_with_origin_causes": 0,
        "closures_with_evidence_sources": 0,
        "closures_with_next_change": 0,
    }

    def add(
        groups: dict[Any, dict[str, int]],
        key: Any,
        terminal_status: str,
    ) -> None:
        counts = groups.setdefault(key, _outcome_counts())
        counts[terminal_status] += 1

    for payload in closed:
        terminal_status = str(payload["terminal_status"])
        outcome_counts[terminal_status] += 1
        goal_id = str(payload.get("goal_id", ""))
        creation = created.get(goal_id)
        goal_value = (
            creation.get("goal")
            if isinstance(creation, Mapping)
            else None
        )
        goal = goal_value if isinstance(goal_value, Mapping) else {}
        pattern = tuple(
            str(goal.get(field, "")).strip()
            for field in ("category", "metric_type", "direction")
        )
        if all(pattern):
            coverage["closures_with_goal_pattern"] += 1
            add(pattern_groups, pattern, terminal_status)
        causes = {
            str(value).strip()
            for value in (goal.get("caused_by") or ())
            if str(value).strip()
        } if isinstance(goal.get("caused_by"), list) else set()
        if causes:
            coverage["closures_with_origin_causes"] += 1
        for cause in causes:
            add(origin_groups, cause, terminal_status)
        evidence = payload.get("evidence")
        sources = {
            str(row.get("source", "")).strip()
            for row in evidence
            if isinstance(row, Mapping)
            and str(row.get("source", "")).strip()
        } if isinstance(evidence, list) else set()
        if sources:
            coverage["closures_with_evidence_sources"] += 1
        for source in sources:
            add(source_groups, source, terminal_status)
        analysis = payload.get("analysis")
        if (
            isinstance(analysis, Mapping)
            and str(analysis.get("next_change", "")).strip()
        ):
            coverage["closures_with_next_change"] += 1

    def table(
        groups: Mapping[Any, Mapping[str, int]],
        row_builder: Any,
    ) -> dict[str, Any]:
        keys = sorted(groups, key=lambda value: str(value))
        rows = []
        for key in keys[:limit]:
            outcomes = dict(groups[key])
            rows.append({
                **row_builder(key),
                "closed_count": sum(outcomes.values()),
                "outcomes": outcomes,
            })
        return {
            "distinct_count": len(keys),
            "not_shown": max(0, len(keys) - limit),
            "rows": rows,
        }

    sample_count = len(closed)
    return {
        "sample_count": sample_count,
        "outcome_counts": outcome_counts,
        "closures_excluded": {
            "count": sum(exclusions.values()),
            "reasons": exclusions,
        },
        "coverage": coverage,
        "goal_patterns": table(
            pattern_groups,
            lambda key: {
                **_bounded_identity("category", key[0]),
                "metric_type": key[1],
                "direction": key[2],
            },
        ),
        "origin_causes": table(
            origin_groups,
            lambda key: _bounded_identity("cause_id", key),
        ),
        "closure_evidence_sources": table(
            source_groups,
            lambda key: _bounded_identity("source", key),
        ),
        "what_this_means": (
            "No closed goals exist in the records supplied to this summary, "
            "so no quality attribution claim is available."
            if sample_count == 0
            else
            "These are descriptive outcome associations, not rankings or "
            "proof that a source or research path caused success. Origin "
            "cause IDs mix host-authored stage, finding, and decision-evidence "
            "labels. Source rows cover closure-time evidence only, not "
            "progress evidence. Goals are sequential rather than independent "
            "samples; use sample_count and coverage before drawing a lesson."
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
