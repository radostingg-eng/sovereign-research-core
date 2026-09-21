"""Validate and summarize host-authored reviews of repeated decisions."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from .accepted_inputs import finalized_cycle_ids
from .cycle_receipt import ALLOWED_DECISIONS
from .opportunity_ledger import open_missing_information_ids
from .timestamps import parse_iso_timestamp


REPETITION_REVIEW_FIELDS = frozenset({
    "prior_cycle_id",
    "disposition",
    "evidence_delta",
    "unresolved_question_ids",
    "rationale",
})
REPETITION_REVIEW_DISPOSITIONS = frozenset({
    "new_evidence",
    "bounded_experiment",
    "deliberate_wait",
})
EXPERIMENT_FIELDS = (
    "hypothesis",
    "mechanism",
    "measurement",
    "counter_metric",
    "evaluation_window",
    "rollback_condition",
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _finalized_receipt_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    current_cycle_id: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    finalized = finalized_cycle_ids(records)
    finalization_order = {
        cycle_id: record_order
        for record_order, record in enumerate(records)
        if record.get("record_type") == "cycle_finalization"
        and isinstance((payload := record.get("payload")), Mapping)
        and (cycle_id := _text(payload.get("cycle_id"))) in finalized
        and cycle_id != current_cycle_id
    }
    rows = []
    unusable = []
    seen = set()
    advisories = []
    for record_order, record in enumerate(records):
        payload = record.get("payload")
        if (
            record.get("record_type") != "cycle_receipt"
            or not isinstance(payload, Mapping)
        ):
            continue
        cycle_id = _text(payload.get("cycle_id"))
        if (
            cycle_id not in finalized
            or cycle_id == current_cycle_id
        ):
            continue
        seen.add(cycle_id)
        completed_at = parse_iso_timestamp(_text(payload.get("completed_at")))
        if completed_at is None:
            advisories.append(
                "decision_repetition_receipt_completed_at_invalid:"
                f"{cycle_id}"
            )
            unusable.append({
                "cycle_id": cycle_id,
                "record_order": finalization_order.get(
                    cycle_id,
                    record_order,
                ),
            })
            continue
        repetition = payload.get("decision_repetition")
        review = (
            repetition.get("review")
            if isinstance(repetition, Mapping)
            else None
        )
        rows.append({
            "cycle_id": cycle_id,
            "completed_at": completed_at,
            "record_order": record_order,
            "decision_status": _text(
                payload.get("decision_status")
            ).lower(),
            "repetition_review": (
                deepcopy(dict(review))
                if isinstance(review, Mapping)
                else None
            ),
        })
    for cycle_id in sorted(finalized - seen - {current_cycle_id}):
        advisories.append(
            f"decision_repetition_finalized_receipt_missing:{cycle_id}"
        )
        unusable.append({
            "cycle_id": cycle_id,
            "record_order": finalization_order.get(cycle_id, -1),
        })
    rows.sort(key=lambda row: (
        row["completed_at"],
        row["record_order"],
    ))
    return rows, unusable, sorted(set(advisories))


def latest_prior_finalized_decision_selection(
    records: Sequence[Mapping[str, Any]],
    *,
    current_cycle_id: str,
) -> dict[str, Any]:
    """Select by receipt completion time, then append order."""
    rows, unusable, advisories = _finalized_receipt_rows(
        records,
        current_cycle_id=current_cycle_id,
    )
    latest_valid = rows[-1] if rows else None
    latest_unusable = max(
        unusable,
        key=lambda row: row["record_order"],
        default=None,
    )
    if (
        latest_unusable is not None
        and (
            latest_valid is None
            or latest_unusable["record_order"]
            > latest_valid["record_order"]
        )
    ):
        cycle_id = latest_unusable["cycle_id"]
        advisories.append(
            f"decision_repetition_latest_receipt_unusable:{cycle_id}"
        )
        return {
            "prior": None,
            "latest_cycle_id": cycle_id,
            "latest_decision_status": None,
            "advisories": sorted(set(advisories)),
        }
    if latest_valid is None:
        return {
            "prior": None,
            "latest_cycle_id": None,
            "latest_decision_status": None,
            "advisories": [],
        }
    latest = latest_valid
    cycle_id = latest["cycle_id"]
    status = latest["decision_status"]
    if not status:
        advisories.append(
            f"decision_repetition_latest_status_missing:{cycle_id}"
        )
        prior = None
    elif status not in ALLOWED_DECISIONS:
        advisories.append(
            "decision_repetition_latest_status_invalid:"
            f"{cycle_id}:{status}"
        )
        prior = None
    else:
        prior = {
            "cycle_id": cycle_id,
            "decision_status": status,
            "repetition_review": latest["repetition_review"],
        }
    return {
        "prior": prior,
        "latest_cycle_id": cycle_id,
        "latest_decision_status": status or None,
        "advisories": sorted(set(advisories)),
    }


def latest_prior_finalized_decision(
    records: Sequence[Mapping[str, Any]],
    *,
    current_cycle_id: str,
) -> dict[str, Any] | None:
    """Return the latest finalized receipt decision before this cycle."""
    return latest_prior_finalized_decision_selection(
        records,
        current_cycle_id=current_cycle_id,
    )["prior"]


def experiment_contract_errors(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["experiment_contract_required"]
    return [
        f"experiment_field_required:{field}"
        for field in EXPERIMENT_FIELDS
        if not _text(value.get(field))
    ]


def _current_evidence_refs(data: Mapping[str, Any]) -> set[str]:
    refs = {
        f"stage:{stage_id}"
        for row in data.get("cognitive_stages") or ()
        if isinstance(row, Mapping)
        and (stage_id := _text(row.get("stage_id")))
    }
    refs.update(
        f"finding:{finding_id}"
        for row in data.get("findings") or ()
        if isinstance(row, Mapping)
        and (finding_id := _text(row.get("id")))
    )
    return refs


def validate_decision_repetition_review(
    data: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
    required: bool,
) -> list[str]:
    """Require a structured host review only when the prior status repeats."""
    if not required:
        return []
    cycle_id = _text(data.get("cycle_id"))
    if cycle_id in finalized_cycle_ids(records):
        return []

    decision = data.get("decision")
    if not isinstance(decision, Mapping):
        return []
    if "repetition_review" not in decision:
        return ["decision_repetition_review_field_required"]
    status = _text(decision.get("status")).lower()
    review = decision.get("repetition_review")
    selection = latest_prior_finalized_decision_selection(
        records,
        current_cycle_id=cycle_id,
    )
    prior = selection["prior"]
    repeated = (
        prior is not None
        and status == prior["decision_status"]
    )

    if not repeated:
        return (
            ["decision_repetition_review_unexpected"]
            if review is not None
            else []
        )
    if review is None:
        return [
            "decision_repetition_review_required:"
            f"{prior['cycle_id']}:{prior['decision_status']}"
        ]
    if not isinstance(review, Mapping):
        return ["decision_repetition_review_not_object"]

    errors: list[str] = []
    actual_fields = set(review)
    if actual_fields != REPETITION_REVIEW_FIELDS:
        missing = "|".join(sorted(REPETITION_REVIEW_FIELDS - actual_fields))
        extra = "|".join(sorted(actual_fields - REPETITION_REVIEW_FIELDS))
        errors.append(
            "decision_repetition_review_fields:"
            f"missing={missing or '-'}:extra={extra or '-'}"
        )

    prior_cycle_id = _text(review.get("prior_cycle_id"))
    if prior_cycle_id != prior["cycle_id"]:
        errors.append(
            "decision_repetition_prior_cycle_mismatch:"
            f"expected={prior['cycle_id']}:actual={prior_cycle_id or 'missing'}"
        )

    disposition = _text(review.get("disposition"))
    if disposition not in REPETITION_REVIEW_DISPOSITIONS:
        errors.append(
            "decision_repetition_disposition_invalid:"
            f"{disposition or 'missing'}"
        )

    evidence_delta = review.get("evidence_delta")
    if not isinstance(evidence_delta, list):
        errors.append("decision_repetition_evidence_delta_not_list")
        evidence_delta = []
    unresolved = review.get("unresolved_question_ids")
    if not isinstance(unresolved, list):
        errors.append(
            "decision_repetition_unresolved_question_ids_not_list"
        )
        unresolved = []
    elif any(not _text(value) for value in unresolved):
        errors.extend(
            "decision_repetition_unresolved_question_id_invalid:"
            f"{index}"
            for index, value in enumerate(unresolved)
            if not _text(value)
        )
    if not _text(review.get("rationale")):
        errors.append("decision_repetition_rationale_required")

    if disposition == "new_evidence":
        if not evidence_delta:
            errors.append("decision_repetition_evidence_delta_required")
        current_refs = _current_evidence_refs(data)
        errors.extend(
            f"decision_repetition_evidence_ref_invalid:{index}"
            for index, ref in enumerate(evidence_delta)
            if _text(ref) not in current_refs
        )
    elif disposition == "bounded_experiment":
        errors.extend(experiment_contract_errors(
            decision.get("experiment")
        ))
    elif disposition == "deliberate_wait":
        if not unresolved:
            errors.append(
                "decision_repetition_unresolved_question_ids_required"
            )
        durable_open_ids = open_missing_information_ids(
            records,
            exclude_cycle_id=cycle_id,
        )
        errors.extend(
            f"decision_repetition_unresolved_question_id_unknown:{item_id}"
            for item_id in map(_text, unresolved)
            if item_id and item_id not in durable_open_ids
        )
    return sorted(set(errors))


def decision_repetition_receipt_context(
    data: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Bind a new-input review to the prior decision used by validation."""
    decision = data.get("decision")
    if (
        not isinstance(decision, Mapping)
        or "repetition_review" not in decision
    ):
        return None
    selection = latest_prior_finalized_decision_selection(
        records,
        current_cycle_id=_text(data.get("cycle_id")),
    )
    prior = selection["prior"]
    status = _text(decision.get("status")).lower()
    review_required = (
        prior is not None
        and status == prior["decision_status"]
    )
    review = decision.get("repetition_review")
    return {
        "review_required": review_required,
        "prior_cycle_id": (
            prior["cycle_id"] if prior is not None else None
        ),
        "prior_decision_status": (
            prior["decision_status"] if prior is not None else None
        ),
        "review": (
            deepcopy(dict(review))
            if review_required and isinstance(review, Mapping)
            else None
        ),
        "selection_advisories": list(selection["advisories"]),
    }


def decision_repetition_feedback(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = 3,
) -> dict[str, Any]:
    """Project the next-cycle rule and recent finalized reviews."""
    rows, _unusable, row_advisories = _finalized_receipt_rows(records)
    selection = latest_prior_finalized_decision_selection(
        records,
        current_cycle_id="",
    )
    latest = selection["prior"]
    reviewed = [
        row for row in rows
        if isinstance(row.get("repetition_review"), Mapping)
    ]
    recent = reviewed[-limit:]
    return {
        "latest_finalized_cycle_id": selection["latest_cycle_id"],
        "latest_finalized_decision_status": (
            selection["latest_decision_status"]
        ),
        "review_required_for_repeated_status": latest is not None,
        "recent_reviews": [{
            "cycle_id": row["cycle_id"],
            "decision_status": row["decision_status"],
            "repetition_review": row["repetition_review"],
        } for row in recent],
        "not_shown": max(0, len(reviewed) - len(recent)),
        "advisories": sorted(set(
            row_advisories + list(selection["advisories"])
        )),
    }
