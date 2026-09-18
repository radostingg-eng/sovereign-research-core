"""Bounded empirical joins without scoring operator or investment quality."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from .forecast_outcomes import forecast_outcome_summary
from .forecasts import forecast_ledger_summary
from .integrity import order_chain

MAX_CALIBRATION_ROWS = 12


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _ordered_records(
    records: Sequence[Mapping[str, Any]],
    record_type: str,
) -> list[Mapping[str, Any]]:
    ordered, failures = order_chain(records)
    rows = ordered if not failures else list(records)
    return [
        row for row in rows
        if row.get("record_type") == record_type
        and isinstance(row.get("payload"), Mapping)
    ]


def _active_reconciliations(
    records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    rows = _ordered_records(records, "instruction_reconciliation")
    superseded = {
        _text(row["payload"].get("supersedes_reconciliation_id"))
        for row in rows
        if _text(row["payload"].get("supersedes_reconciliation_id"))
    }
    return [
        row["payload"] for row in rows
        if _text(row["payload"].get("reconciliation_id")) not in superseded
    ]


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        "status": (
            "denominator_present" if denominator else "insufficient_data"
        ),
    }


def _single_reconciliation_status(
    reconciliations: Sequence[Mapping[str, Any]],
) -> str | None:
    statuses = {
        _text(row.get("status"))
        for row in reconciliations
        if _text(row.get("status"))
    }
    return next(iter(statuses)) if len(statuses) == 1 else None


def _forecast_rows(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    outcomes = {
        _text(record["payload"].get("forecast_id")): record["payload"]
        for record in _ordered_records(records, "forecast_outcome")
        if _text(record["payload"].get("forecast_id"))
    }
    reconciliations = _active_reconciliations(records)
    by_recommendation: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in reconciliations:
        recommendation_id = _text(row.get("recommendation_id"))
        if recommendation_id:
            by_recommendation[recommendation_id].append(row)
    ledger = forecast_ledger_summary(records)
    ledger_by_id = {
        _text(row.get("forecast_id")): row
        for row in ledger.get("items") or ()
        if isinstance(row, Mapping) and _text(row.get("forecast_id"))
    }
    rows = []
    for record in _ordered_records(records, "forecast_registered"):
        forecast = record["payload"]
        forecast_id = _text(forecast.get("forecast_id"))
        decision = forecast.get("decision")
        decision = decision if isinstance(decision, Mapping) else {}
        recommendation_id = _text(decision.get("cycle_id"))
        expectation = forecast.get("expectation")
        expectation = (
            expectation if isinstance(expectation, Mapping) else {}
        )
        metric = forecast.get("metric")
        metric = metric if isinstance(metric, Mapping) else {}
        outcome = outcomes.get(forecast_id)
        attached = by_recommendation.get(recommendation_id, [])
        reconciliation_status = _single_reconciliation_status(attached)
        ledger_row = ledger_by_id.get(forecast_id, {})
        rows.append({
            "forecast_id": forecast_id,
            "opportunity_id": forecast.get("opportunity_id"),
            "recommendation_id": recommendation_id,
            "decision_status": decision.get("status"),
            "expectation_kind": expectation.get("kind"),
            "direction": expectation.get("direction"),
            "unit": metric.get("unit"),
            "confidence_probability":
                forecast.get("confidence_probability"),
            "measurement_status":
                ledger_row.get("measurement_status", "open"),
            "resolved_outcome": (
                outcome.get("resolved_outcome")
                if isinstance(outcome, Mapping)
                else None
            ),
            "invalidated": (
                outcome.get("invalidated")
                if isinstance(outcome, Mapping)
                else None
            ),
            "observed_value": (
                outcome.get("observed_value")
                if isinstance(outcome, Mapping)
                else None
            ),
            "delta_from_baseline": (
                outcome.get("delta_from_baseline")
                if isinstance(outcome, Mapping)
                else None
            ),
            "reconciliation_ids": [
                row.get("reconciliation_id") for row in attached
            ],
            "reconciliation_status": reconciliation_status,
            "reconciliation_conflict": (
                bool(attached) and reconciliation_status is None
            ),
        })
    return rows


def _directional_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    measured = [
        row for row in rows
        if row.get("expectation_kind") == "direction"
        and row.get("resolved_outcome") in {0, 1}
        and row.get("invalidated") is not True
    ]
    recommended = [
        row for row in measured
        if row.get("decision_status") == "recommended"
    ]
    accepted = [
        row for row in recommended
        if row.get("reconciliation_status") in {
            "accepted_unchanged", "accepted_modified",
        }
    ]
    rejected = [
        row for row in recommended
        if row.get("reconciliation_status") == "rejected"
    ]
    return {
        "recommended_direction_forecast_true_rate": _ratio(
            sum(row["resolved_outcome"] == 1 for row in recommended),
            len(recommended),
        ),
        "accepted_direction_forecast_true_rate": _ratio(
            sum(row["resolved_outcome"] == 1 for row in accepted),
            len(accepted),
        ),
        "rejected_direction_forecast_true_rate": {
            **_ratio(
                sum(row["resolved_outcome"] == 1 for row in rejected),
                len(rejected),
            ),
            "what_this_means": (
                "The rejected proposal's frozen directional claim resolved "
                "true. This is not a fill, P&L, or trade counterfactual."
            ),
        },
        "rejected_direction_forecast_false_rate": _ratio(
            sum(row["resolved_outcome"] == 0 for row in rejected),
            len(rejected),
        ),
        "invalidated_direction_rows_excluded": sum(
            1 for row in rows
            if row.get("expectation_kind") == "direction"
            and row.get("resolved_outcome") in {0, 1}
            and row.get("invalidated") is True
        ),
    }


def _timing_groups(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        delta = row.get("delta_from_baseline")
        if (
            not isinstance(delta, (int, float))
            or isinstance(delta, bool)
        ):
            continue
        key = (
            _text(row.get("unit")),
            _text(row.get("expectation_kind")),
            _text(row.get("direction")) or "none",
        )
        groups[key].append(float(delta))
    return [
        {
            "unit": key[0],
            "expectation_kind": key[1],
            "direction": key[2],
            "n": len(values),
            "mean_delta_from_baseline": sum(values) / len(values),
        }
        for key, values in sorted(groups.items())
    ]


def _implementation_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    reconciliations = _active_reconciliations(records)
    statuses = Counter(
        _text(row.get("status")) or "unknown"
        for row in reconciliations
    )
    modified_fields = Counter(
        _text(change.get("field"))
        for row in reconciliations
        for change in row.get("field_changes") or ()
        if isinstance(change, Mapping) and _text(change.get("field"))
    )
    accepted = [
        row for row in reconciliations
        if _text(row.get("status")) in {
            "accepted_unchanged", "accepted_modified",
        }
    ]
    return {
        "reconciliation_count": len(reconciliations),
        "counts_by_status": dict(sorted(statuses.items())),
        "modification_field_counts": dict(sorted(modified_fields.items())),
        "submitted_count": sum(
            row.get("submission_state") == "submitted"
            for row in accepted
        ),
        "executed_count": sum(
            row.get("execution_state") == "executed"
            for row in accepted
        ),
        "accepted_without_fill_count": sum(
            row.get("submission_state") == "submitted"
            and row.get("execution_state") != "executed"
            for row in accepted
        ),
        "unknown_or_disputed_count": sum(
            _text(row.get("status")) == "unknown"
            or bool(row.get("disagreements"))
            for row in reconciliations
        ),
    }


def empirical_calibration_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = _forecast_rows(records)
    reconciliation_rows = _active_reconciliations(records)
    matched_ids = {
        reconciliation_id
        for row in rows
        for reconciliation_id in row["reconciliation_ids"]
        if reconciliation_id
    }
    unmatched_reconciliations = [
        {
            "reconciliation_id": row.get("reconciliation_id"),
            "recommendation_id": row.get("recommendation_id"),
            "instruction_id": row.get("instruction_id"),
            "status": row.get("status"),
        }
        for row in reconciliation_rows
        if row.get("reconciliation_id") not in matched_ids
    ]
    unmatched_forecasts = [
        {
            "forecast_id": row["forecast_id"],
            "recommendation_id": row["recommendation_id"],
            "decision_status": row["decision_status"],
        }
        for row in rows
        if not row["reconciliation_ids"]
    ]
    unresolved = {
        "open_forecasts": sum(
            row["measurement_status"] == "open" for row in rows
        ),
        "overdue_forecasts": sum(
            row["measurement_status"] == "overdue" for row in rows
        ),
        "measured_invalidated_forecasts": sum(
            row.get("resolved_outcome") in {0, 1}
            and row.get("invalidated") is True
            for row in rows
        ),
        "forecast_reconciliation_conflicts": sum(
            row["reconciliation_conflict"] for row in rows
        ),
        "unmatched_forecasts": len(unmatched_forecasts),
        "unmatched_reconciliations": len(unmatched_reconciliations),
    }
    return {
        "forecast_rows": len(rows),
        "exact_forecast_reconciliation_matches": sum(
            bool(row["reconciliation_ids"]) for row in rows
        ),
        "forecast_outcome_calibration": forecast_outcome_summary(records)[
            "calibration"
        ],
        "directional_decision_metrics": _directional_metrics(rows),
        "timing_groups": _timing_groups(rows),
        "implementation": _implementation_summary(records),
        "unresolved": unresolved,
        "unmatched_forecasts": unmatched_forecasts[
            :MAX_CALIBRATION_ROWS
        ],
        "unmatched_reconciliations": unmatched_reconciliations[
            :MAX_CALIBRATION_ROWS
        ],
        "recent_rows": list(reversed(rows[-MAX_CALIBRATION_ROWS:])),
        "what_this_means": (
            "Descriptive empirical joins only. Probability calibration reuses "
            "persisted forecast outcomes. Direction and range claims are not "
            "pooled. Operator reconciliations join only by exact recommendation "
            "cycle; unmatched records remain visible. Forecast truth is not "
            "fill, slippage, P&L, or an operator quality verdict."
        ),
    }
