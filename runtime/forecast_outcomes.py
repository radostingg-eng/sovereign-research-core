"""Deterministic forecast measurement from frozen connector observations."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import input_fingerprint
from .input_artifacts import load_input_data
from .audit_store import AuditJournal
from .effectiveness import calibration_ledger
from .forecasts import (
    MAX_FORECAST_TEXT_CHARS,
    _aware_timestamp,
    _bounded,
    _forecast_records,
    _ordered_records,
    _text,
    forecast_ledger_summary,
    forecast_observation_deadline,
)
from .schema_versions import STRUCTURED_FULL_CYCLE_VERSIONS
from .tool_provenance import (
    resolve_tool_call,
)
from .timestamps import effective_as_of

MAX_FORECAST_OUTCOMES_PER_CYCLE = 8


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _current_evidence_anchors(
    data: Mapping[str, Any],
) -> dict[str, str]:
    cycle_id = _text(data.get("cycle_id"))
    anchors = {
        f"stage:{stage_id}": f"cycle-stage:{cycle_id}:{stage_id}"
        for row in data.get("cognitive_stages") or ()
        if isinstance(row, Mapping)
        and (stage_id := _text(row.get("stage_id")))
    }
    decision_anchor = f"cycle-stage:{cycle_id}:decision"
    anchors.update({
        f"finding:{finding_id}": decision_anchor
        for row in data.get("findings") or ()
        if isinstance(row, Mapping)
        and (finding_id := _text(row.get("id")))
    })
    return anchors


def _raw_tool_call(
    data: Mapping[str, Any],
    tool_call_id: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    return resolve_tool_call(data, tool_call_id)


def _extract_field(value: Any, path: str) -> Any:
    current = value
    for token in path.split("."):
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif (
            isinstance(current, list)
            and token.isdigit()
            and int(token) < len(current)
        ):
            current = current[int(token)]
        else:
            raise KeyError(path)
    return current


def _resolve_outcome(
    forecast: Mapping[str, Any],
    observed_value: float,
) -> int:
    metric = forecast.get("metric")
    metric = metric if isinstance(metric, Mapping) else {}
    expectation = forecast.get("expectation")
    expectation = (
        expectation if isinstance(expectation, Mapping) else {}
    )
    rule = expectation.get("resolution_rule")
    baseline = float(metric["baseline_value"])
    if rule == "inside_inclusive_range":
        return int(
            float(expectation["lower_bound"])
            <= observed_value
            <= float(expectation["upper_bound"])
        )
    if rule == "strictly_below_baseline_ties_false":
        return int(observed_value < baseline)
    if rule == "strictly_above_baseline_ties_false":
        return int(observed_value > baseline)
    raise ValueError(f"forecast_resolution_rule_invalid:{rule}")


def _validate_evidence(
    value: Any,
    *,
    index: int,
    anchors: Mapping[str, str],
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 8
    ):
        return [f"forecast_outcome_evidence_invalid:{index}:count"]
    errors = []
    seen = set()
    for evidence_index, raw in enumerate(value):
        ref = _text(raw)
        if not (
            ref.startswith("stage:")
            or ref.startswith("finding:")
        ):
            errors.append(
                f"forecast_outcome_evidence_invalid:{index}:"
                f"invalid_ref:{evidence_index}"
            )
        elif ref in seen:
            errors.append(
                f"forecast_outcome_evidence_invalid:{index}:duplicate:{ref}"
            )
        elif ref not in anchors:
            errors.append(
                f"forecast_outcome_evidence_invalid:{index}:dangling:{ref}"
            )
        seen.add(ref)
    return errors


def _known_forecast_payloads(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    return {
        _text(record["payload"].get("forecast_id")): record["payload"]
        for record in _forecast_records(records)
        if _text(record["payload"].get("forecast_id"))
    }


def _outcome_forecast_ids(
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    return {
        _text(record["payload"].get("forecast_id"))
        for record in _ordered_records(records, "forecast_outcome")
        if _text(record["payload"].get("forecast_id"))
    }


def validate_forecast_outcomes(
    outcomes: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    if outcomes is None:
        return []
    if data.get("host_input_schema_version") not in (
            STRUCTURED_FULL_CYCLE_VERSIONS):
        return ["forecast_outcomes_require_schema_v3"]
    if not isinstance(outcomes, list):
        return ["forecast_outcomes_must_be_a_list"]
    if len(outcomes) > MAX_FORECAST_OUTCOMES_PER_CYCLE:
        return ["forecast_outcomes_too_many"]
    forecasts = _known_forecast_payloads(records)
    measured = _outcome_forecast_ids(records)
    anchors = _current_evidence_anchors(data)
    cycle_as_of = _aware_timestamp(effective_as_of(data))
    seen: set[str] = set()
    errors: list[str] = []
    expected = {
        "forecast_id",
        "tool_call_id",
        "invalidation_reason",
        "evidence",
    }
    for index, row in enumerate(outcomes):
        if not isinstance(row, Mapping):
            errors.append(f"forecast_outcome_invalid:{index}:object")
            continue
        if set(row) != expected:
            errors.append(f"forecast_outcome_invalid:{index}:fields")
        forecast_id = _text(row.get("forecast_id"))
        if forecast_id not in forecasts:
            errors.append(
                f"forecast_outcome_unknown_forecast:{index}:{forecast_id}"
            )
            continue
        if forecast_id in seen:
            errors.append(
                f"forecast_outcome_duplicate_forecast:{index}:{forecast_id}"
            )
        seen.add(forecast_id)
        if forecast_id in measured:
            errors.append(
                f"forecast_outcome_already_measured:{index}:{forecast_id}"
            )
        tool_call_id = _text(row.get("tool_call_id"))
        resolved = _raw_tool_call(data, tool_call_id)
        if resolved is None:
            errors.append(
                f"forecast_outcome_tool_call_invalid:{index}:unknown"
            )
            continue
        provenance_row, call = resolved
        forecast = forecasts[forecast_id]
        metric = forecast.get("metric")
        metric = metric if isinstance(metric, Mapping) else {}
        source = metric.get("source")
        source = source if isinstance(source, Mapping) else {}
        if provenance_row.get("result_origin") != "connector_response":
            errors.append(
                f"forecast_outcome_tool_call_invalid:{index}:origin"
            )
        if _text(provenance_row.get("tool")) != _text(source.get("tool")):
            errors.append(
                f"forecast_outcome_tool_call_invalid:{index}:tool"
            )
        source_values = {
            _text(ref.get("value"))
            for ref in provenance_row.get("source_refs") or ()
            if isinstance(ref, Mapping)
        }
        if _text(source.get("stable_ref")) not in source_values:
            errors.append(
                f"forecast_outcome_tool_call_invalid:{index}:stable_ref"
            )
        observed_at = _aware_timestamp(
            provenance_row.get("observed_at")
        )
        horizon = forecast.get("horizon")
        horizon = horizon if isinstance(horizon, Mapping) else {}
        target = _aware_timestamp(horizon.get("target_at"))
        deadline = forecast_observation_deadline(forecast)
        if observed_at is None:
            errors.append(
                f"forecast_outcome_time_invalid:{index}:observed_at"
            )
        else:
            if target is not None and observed_at < target:
                errors.append(
                    f"forecast_outcome_time_invalid:{index}:before_target"
                )
            if deadline is not None and observed_at > deadline:
                errors.append(
                    f"forecast_outcome_time_invalid:{index}:after_window"
                )
            if cycle_as_of is not None and observed_at > cycle_as_of:
                errors.append(
                    f"forecast_outcome_time_invalid:{index}:after_cycle"
                )
        try:
            observed_value = _extract_field(
                call.get("result"),
                _text(source.get("field")),
            )
        except KeyError:
            errors.append(
                f"forecast_outcome_value_invalid:{index}:field"
            )
        else:
            if not _finite_number(observed_value):
                errors.append(
                    f"forecast_outcome_value_invalid:{index}:number"
                )
        reason = row.get("invalidation_reason")
        if reason is not None and (
            not _text(reason)
            or len(_text(reason)) > MAX_FORECAST_TEXT_CHARS
        ):
            errors.append(
                f"forecast_outcome_invalid:{index}:invalidation_reason"
            )
        errors.extend(_validate_evidence(
            row.get("evidence"),
            index=index,
            anchors=anchors,
        ))
    return sorted(set(errors))


def _payload(
    row: Mapping[str, Any],
    *,
    data: Mapping[str, Any],
    forecast: Mapping[str, Any],
) -> dict[str, Any]:
    tool_call_id = _text(row.get("tool_call_id"))
    resolved = _raw_tool_call(data, tool_call_id)
    if resolved is None:
        raise ValueError(f"forecast_outcome_tool_call_missing:{tool_call_id}")
    provenance_row, call = resolved
    metric = forecast["metric"]
    observed_value = float(_extract_field(
        call.get("result"),
        _text(metric["source"]["field"]),
    ))
    outcome = _resolve_outcome(forecast, observed_value)
    anchors = _current_evidence_anchors(data)
    evidence = [_text(ref) for ref in row.get("evidence") or ()]
    return {
        "schema_version": 1,
        "cycle_id": _text(data.get("cycle_id")),
        "forecast_id": _text(row.get("forecast_id")),
        "observed_at": provenance_row.get("observed_at"),
        "observed_value": observed_value,
        "tool_call_id": tool_call_id,
        "result_sha256": provenance_row.get("result_sha256"),
        "source": dict(metric["source"]),
        "baseline_value": metric.get("baseline_value"),
        "expectation": dict(forecast["expectation"]),
        "confidence_probability": forecast.get(
            "confidence_probability"),
        "resolved_outcome": outcome,
        "delta_from_baseline": (
            observed_value - float(metric["baseline_value"])
        ),
        "invalidated": row.get("invalidation_reason") is not None,
        "invalidation_reason": row.get("invalidation_reason"),
        "evidence": evidence,
        "evidence_record_ids": list(dict.fromkeys(
            anchors[ref] for ref in evidence
        )),
    }


def persist_forecast_outcomes(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
    *,
    all_records: Sequence[Mapping[str, Any]] = (),
) -> int:
    outcomes = data.get("forecast_outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        return 0
    combined = {
        _text(record.get("record_id")): record
        for record in [*all_records, *journal.read()]
        if _text(record.get("record_id"))
    }
    records = list(combined.values())
    cycle_id = _text(receipt.get("cycle_id"))
    validation_records = [
        record for record in records
        if not (
            record.get("record_type") == "forecast_outcome"
            and isinstance(record.get("payload"), Mapping)
            and _text(record["payload"].get("cycle_id")) == cycle_id
        )
    ]
    errors = validate_forecast_outcomes(
        outcomes,
        data=data,
        records=validation_records,
    )
    if errors:
        raise ValueError(
            "forecast_outcome_reconciliation_failed:" + ",".join(errors)
        )
    forecasts = _known_forecast_payloads(records)
    existing = {
        _text(record.get("record_id")): record
        for record in records
        if record.get("record_type") == "forecast_outcome"
    }
    receipt_id = f"cycle-receipt:{cycle_id}"
    added = 0
    for row in outcomes:
        forecast_id = _text(row.get("forecast_id"))
        record_id = f"forecast-outcome:{forecast_id}"
        payload = _payload(
            row,
            data=data,
            forecast=forecasts[forecast_id],
        )
        caused_by = [
            receipt_id,
            f"cycle-stage:{cycle_id}:decision",
            f"forecast:{forecast_id}",
            f"tool-provenance:{cycle_id}",
            *payload["evidence_record_ids"],
        ]
        caused_by = list(dict.fromkeys(caused_by))
        prior = existing.get(record_id)
        if prior is not None:
            if (
                prior.get("payload") != payload
                or prior.get("caused_by") != caused_by
            ):
                raise ValueError(
                    f"forecast_outcome_payload_mismatch:{forecast_id}"
                )
            continue
        journal.append(
            record_id=record_id,
            record_type="forecast_outcome",
            agent="sovereign-host",
            caused_by=caused_by,
            payload=payload,
        )
        added += 1
    return added


def backfill_forecast_outcomes(
    inputs: Sequence[Path],
    journal: AuditJournal,
    *,
    all_records: Sequence[Mapping[str, Any]] = (),
) -> int:
    candidates: dict[tuple[str, str], Mapping[str, Any]] = {}
    for path in inputs:
        try:
            import json
            data = load_input_data(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("forecast_outcomes"), list)
            or not data.get("forecast_outcomes")
        ):
            continue
        cycle_id = _text(data.get("cycle_id"))
        if cycle_id:
            candidates[(cycle_id, input_fingerprint(data))] = data
    combined = {
        _text(record.get("record_id")): record
        for record in [*all_records, *journal.read()]
        if _text(record.get("record_id"))
    }
    records = list(combined.values())
    added = 0
    for record in _ordered_records(records, "cycle_receipt"):
        payload = record["payload"]
        cycle_id = _text(payload.get("cycle_id"))
        snapshot_id = _text(payload.get("snapshot_id"))
        fingerprint = (
            snapshot_id.rsplit(":", 1)[-1] if ":" in snapshot_id else ""
        )
        data = candidates.get((cycle_id, fingerprint))
        if data is None:
            continue
        added += persist_forecast_outcomes(
            data,
            journal,
            payload,
            all_records=records,
        )
        combined.update({
            _text(row.get("record_id")): row
            for row in journal.read()
            if _text(row.get("record_id"))
        })
        records = list(combined.values())
    return added


def forecast_outcome_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ledger = forecast_ledger_summary(records)
    outcomes = [
        record["payload"]
        for record in _ordered_records(records, "forecast_outcome")
    ]
    observations = [
        {
            "probability": row.get("confidence_probability"),
            "outcome": row.get("resolved_outcome"),
        }
        for row in outcomes
    ]
    calibration = calibration_ledger(observations)
    matured = ledger["measured_count"] + ledger["overdue_count"]
    return {
        "measured_count": ledger["measured_count"],
        "overdue_count": ledger["overdue_count"],
        "open_count": ledger["open_count"],
        "matured_count": matured,
        "measurement_coverage": (
            ledger["measured_count"] / matured if matured else None
        ),
        "invalidated_count": sum(
            1 for row in outcomes if row.get("invalidated") is True
        ),
        "outcome_counts": {
            "resolved_true": sum(
                1 for row in outcomes
                if row.get("resolved_outcome") == 1
            ),
            "resolved_false": sum(
                1 for row in outcomes
                if row.get("resolved_outcome") == 0
            ),
        },
        "calibration": {
            "source": "persisted_forecast_outcomes",
            "n": calibration.n,
            "brier": calibration.brier,
            "log_loss": calibration.log_loss,
            "hit_rate": calibration.hit_rate,
            "reliability": list(calibration.reliability),
            "status": calibration.status,
        },
        "recent": [
            {
                "forecast_id": row.get("forecast_id"),
                "observed_at": row.get("observed_at"),
                "observed_value": row.get("observed_value"),
                "resolved_outcome": row.get("resolved_outcome"),
                "invalidated": row.get("invalidated"),
                "delta_from_baseline": row.get("delta_from_baseline"),
            }
            for row in reversed(outcomes[-12:])
        ],
        "what_this_means": (
            "Outcomes are extracted from current-cycle connector results "
            "using each forecast's frozen source field and observation "
            "window. Overdue forecasts remain in the matured denominator. "
            "Calibration status is descriptive and never changes strategy "
            "or prompts automatically."
        ),
    }
