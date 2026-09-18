"""Recommendation-to-outcome lifecycle primitives.

The lifecycle is append-only at the record layer and deliberately separates
user engagement from execution. An unexecuted instruction is never treated as
rejection; engagement may remain ``unknown``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

STATES = (
    "candidate", "researching", "experiment", "recommended", "instruction_created",
    "approved", "submitted", "executed", "modified", "deleted", "closed",
    "expired", "rejected", "unknown",
)
RESOLVED_STATES = frozenset({
    "executed", "deleted", "rejected", "expired", "closed",
})

# Legal transitions. Terminal states are intentionally explicit.
TRANSITIONS = {
    "candidate": {"researching", "experiment", "recommended", "rejected", "expired", "unknown"},
    "researching": {"candidate", "experiment", "recommended", "rejected", "expired", "unknown"},
    "experiment": {"researching", "recommended", "rejected", "expired", "unknown"},
    "recommended": {"instruction_created", "rejected", "expired", "unknown"},
    "instruction_created": {
        "approved", "submitted", "executed", "modified", "deleted",
        "rejected", "expired", "unknown",
    },
    "approved": {
        "submitted", "executed", "modified", "deleted", "rejected",
        "expired", "unknown",
    },
    "submitted": {
        "executed", "modified", "closed", "rejected", "expired", "unknown",
    },
    "executed": {"modified", "closed", "unknown"},
    "modified": {"executed", "closed", "unknown"},
    "deleted": set(),
    "closed": set(),
    "expired": set(),
    "rejected": set(),
    "unknown": {
        "researching", "recommended", "instruction_created", "approved",
        "submitted", "executed", "modified", "deleted", "closed", "rejected",
        "expired",
    },
}

@dataclass(frozen=True)
class LifecycleEvent:
    recommendation_id: str
    from_state: str | None
    to_state: str
    event_id: str
    caused_by: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    metadata: Mapping[str, Any]


def validate_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"invalid_state:{state}")


def transition(current: str | None, new: str, *, recommendation_id: str,
               event_id: str, caused_by: tuple[str, ...] = (),
               evidence_ids: tuple[str, ...] = (),
               metadata: Mapping[str, Any] | None = None) -> LifecycleEvent:
    """Create a legal lifecycle transition; first state must be candidate."""
    validate_state(new)
    if current is None:
        if new != "candidate":
            raise ValueError("first_state_must_be_candidate")
    else:
        validate_state(current)
        if new not in TRANSITIONS[current]:
            raise ValueError(f"illegal_transition:{current}->{new}")
    if not recommendation_id or not event_id:
        raise ValueError("missing_identity")
    return LifecycleEvent(recommendation_id, current, new, event_id,
                          tuple(caused_by), tuple(evidence_ids), dict(metadata or {}))


def apply_events(events: list[LifecycleEvent]) -> dict[str, str]:
    """Validate a collection of lifecycle events and return terminal/current state."""
    states: dict[str, str] = {}
    seen_events: set[str] = set()
    for event in events:
        if event.event_id in seen_events:
            raise ValueError(f"duplicate_event:{event.event_id}")
        seen_events.add(event.event_id)
        current = states.get(event.recommendation_id)
        if current != event.from_state:
            raise ValueError(f"state_mismatch:{event.recommendation_id}:{current}!={event.from_state}")
        transition(current, event.to_state, recommendation_id=event.recommendation_id,
                   event_id=event.event_id, caused_by=event.caused_by,
                   evidence_ids=event.evidence_ids, metadata=event.metadata)
        states[event.recommendation_id] = event.to_state
    return states


def outcome_record(*, recommendation_id: str, outcome_id: str,
                   engagement: str, execution_state: str,
                   realized_return: float | None = None,
                   experienced_risk: float | None = None,
                   holding_period_days: int | None = None,
                   counterfactual_result: Any = None,
                   thesis_result: str = "unknown",
                   caused_by: tuple[str, ...] = ()) -> dict[str, Any]:
    """Normalize an observed outcome without inferring user intent or execution."""
    if engagement not in {
        "approved", "submitted", "executed", "modified", "deleted",
        "rejected", "expired", "unknown",
    }:
        raise ValueError("invalid_engagement")
    validate_state(execution_state)
    if execution_state not in {
        "approved", "submitted", "executed", "modified", "deleted", "closed",
        "rejected", "expired", "unknown",
    }:
        raise ValueError("invalid_execution_state")
    if realized_return is not None and not isinstance(realized_return, (int, float)):
        raise ValueError("invalid_realized_return")
    if experienced_risk is not None and experienced_risk < 0:
        raise ValueError("invalid_experienced_risk")
    return {
        "outcome_id": outcome_id,
        "recommendation_id": recommendation_id,
        "engagement": engagement,
        "execution_state": execution_state,
        "realized_return": realized_return,
        "experienced_risk": experienced_risk,
        "holding_period_days": holding_period_days,
        "counterfactual_result": counterfactual_result,
        "thesis_result": thesis_result,
        "caused_by": list(caused_by) or [recommendation_id],
    }


def validate_event_mapping(value: Mapping[str, Any],
                           *, caused_by: tuple[str, ...] = ()) -> list[str]:
    """Validate a persisted lifecycle event without applying it."""
    try:
        transition(
            value.get("from_state"),
            str(value.get("to_state", "")),
            recommendation_id=str(value.get("recommendation_id", "")),
            event_id=str(value.get("event_id", "")),
            caused_by=caused_by,
            evidence_ids=tuple(value.get("evidence_ids") or ()),
            metadata=value.get("metadata")
            if isinstance(value.get("metadata"), Mapping)
            else {},
        )
    except ValueError as error:
        return [str(error)]
    return []


def apply_from_state(initial_state: str,
                     events: list[LifecycleEvent]) -> str:
    """Apply ordered events after a recommendation already exists."""
    validate_state(initial_state)
    current = initial_state
    seen: set[str] = set()
    for event in events:
        if event.event_id in seen:
            raise ValueError(f"duplicate_event:{event.event_id}")
        seen.add(event.event_id)
        if event.from_state != current:
            raise ValueError(
                f"state_mismatch:{event.recommendation_id}:"
                f"{current}!={event.from_state}")
        transition(
            current,
            event.to_state,
            recommendation_id=event.recommendation_id,
            event_id=event.event_id,
            caused_by=event.caused_by,
            evidence_ids=event.evidence_ids,
            metadata=event.metadata,
        )
        current = event.to_state
    return current


def event_from_mapping(value: Mapping[str, Any],
                       *, caused_by: tuple[str, ...] = ()) -> LifecycleEvent:
    """Create one validated event from persisted fields."""
    errors = validate_event_mapping(value, caused_by=caused_by)
    if errors:
        raise ValueError(errors[0])
    return LifecycleEvent(
        recommendation_id=str(value["recommendation_id"]),
        from_state=value.get("from_state"),
        to_state=str(value["to_state"]),
        event_id=str(value["event_id"]),
        caused_by=caused_by,
        evidence_ids=tuple(value.get("evidence_ids") or ()),
        metadata=dict(value.get("metadata") or {}),
    )
