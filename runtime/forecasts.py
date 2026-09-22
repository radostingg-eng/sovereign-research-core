"""Immutable ex-ante forecast registration and feedback projection."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import input_fingerprint
from .input_artifacts import load_input_data
from .audit_store import AuditJournal
from .integrity import order_chain
from .schema_versions import STRUCTURED_FULL_CYCLE_VERSIONS
from .timestamps import effective_as_of, parse_iso_timestamp

FORECAST_SCHEMA_VERSION = 1
MAX_FORECASTS_PER_CYCLE = 8
MAX_FORECAST_FEEDBACK_ITEMS = 12
MAX_FORECAST_TEXT_CHARS = 600
MAX_FORECAST_RISK_ASSUMPTIONS = 8
FORECAST_ASSESSMENT_SCHEMA_VERSION = 1
FORECAST_ASSESSMENT_STATUSES = frozenset({"required", "not_required"})
LEGACY_OBSERVATION_WINDOW_SECONDS = 48 * 60 * 60
FORECAST_UNITS = frozenset({
    "currency",
    "percent",
    "ratio",
    "count",
    "basis_points",
})
EXPECTATION_KINDS = frozenset({"direction", "range"})
DIRECTIONS = frozenset({"up", "down"})
ENTRY_KINDS = frozenset({
    "discovery_price",
    "recommendation_price",
    "hypothetical_entry",
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")
_EVIDENCE_REF = re.compile(
    r"^(?:stage:[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}|"
    r"finding:[A-Za-z0-9][A-Za-z0-9._:/-]{0,199})$"
)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _bounded(value: Any, limit: int = 280) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _aware_timestamp(value: Any) -> datetime | None:
    parsed = parse_iso_timestamp(value)
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed.utcoffset() is None
    ):
        return None
    return parsed


def _registered_at(data: Mapping[str, Any]) -> str:
    return _text(effective_as_of(data))


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


def _forecast_records(
    records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return _ordered_records(records, "forecast_registered")


def _opportunity_records(
    records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return _ordered_records(records, "opportunity_event")


def _forecast_key(payload: Mapping[str, Any]) -> tuple[str, str, str, str]:
    metric = payload.get("metric")
    metric = metric if isinstance(metric, Mapping) else {}
    source = metric.get("source")
    source = source if isinstance(source, Mapping) else {}
    horizon = payload.get("horizon")
    horizon = horizon if isinstance(horizon, Mapping) else {}
    return (
        _text(payload.get("opportunity_id")).casefold(),
        _text(metric.get("name")).casefold(),
        _text(source.get("stable_ref")).casefold(),
        _text(horizon.get("target_at")),
    )


def forecast_observation_deadline(
    payload: Mapping[str, Any],
) -> datetime | None:
    horizon = payload.get("horizon")
    horizon = horizon if isinstance(horizon, Mapping) else {}
    target = _aware_timestamp(horizon.get("target_at"))
    window = horizon.get(
        "observation_window_seconds",
        LEGACY_OBSERVATION_WINDOW_SECONDS,
    )
    if (
        target is None
        or isinstance(window, bool)
        or not isinstance(window, int)
        or window <= 0
    ):
        return None
    return target + timedelta(seconds=window)


def overdue_forecast_ids(
    records: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[str]:
    by_id, _, _superseded = _known_forecasts(records)
    outcomes = {
        _text(record["payload"].get("forecast_id"))
        for record in _ordered_records(records, "forecast_outcome")
        if _text(record["payload"].get("forecast_id"))
    }
    observed_now = now or datetime.now(timezone.utc)
    return sorted(
        forecast_id
        for forecast_id, payload in by_id.items()
        if forecast_id not in outcomes
        and (
            deadline := forecast_observation_deadline(payload)
        ) is not None
        and observed_now > deadline
    )


def _known_forecasts(
    records: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[tuple[str, str, str, str], str],
    set[str],
]:
    by_id: dict[str, Mapping[str, Any]] = {}
    superseded: set[str] = set()
    for record in _forecast_records(records):
        payload = record["payload"]
        forecast_id = _text(payload.get("forecast_id"))
        if forecast_id:
            by_id[forecast_id] = payload
        supersedes = _text(payload.get("supersedes_forecast_id"))
        if supersedes:
            superseded.add(supersedes)
    active_by_key = {
        _forecast_key(payload): forecast_id
        for forecast_id, payload in by_id.items()
        if forecast_id not in superseded
    }
    return by_id, active_by_key, superseded


def _known_opportunity_ids(
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    known = {
        _text(record["payload"].get("opportunity_id"))
        for record in _opportunity_records(records)
        if _text(record["payload"].get("opportunity_id"))
    }
    updates = data.get("opportunity_updates")
    updates = updates if isinstance(updates, list) else []
    known.update(
        _text(row.get("opportunity_id"))
        for row in updates
        if isinstance(row, Mapping) and _text(row.get("opportunity_id"))
    )
    return known


def _validate_source(
    value: Any,
    *,
    index: int,
    prefix: str,
) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"forecast_source_invalid:{index}:{prefix}:not_object"]
    expected = {"tool", "field", "instrument_ref", "stable_ref"}
    errors = []
    if set(value) != expected:
        errors.append(f"forecast_source_invalid:{index}:{prefix}:fields")
    for field in expected:
        text = _text(value.get(field))
        if not text or len(text) > MAX_FORECAST_TEXT_CHARS:
            errors.append(
                f"forecast_source_invalid:{index}:{prefix}:{field}"
            )
    return errors


def _validate_numeric_observation(
    value: Any,
    *,
    index: int,
    prefix: str,
    registered: datetime | None,
) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"forecast_metric_invalid:{index}:{prefix}:not_object"]
    expected = {
        "name",
        "unit",
        "baseline_value",
        "baseline_observed_at",
        "source",
    }
    errors = []
    if set(value) != expected:
        errors.append(f"forecast_metric_invalid:{index}:{prefix}:fields")
    name = _text(value.get("name"))
    if not name or len(name) > MAX_FORECAST_TEXT_CHARS:
        errors.append(f"forecast_metric_invalid:{index}:{prefix}:name")
    if value.get("unit") not in FORECAST_UNITS:
        errors.append(f"forecast_metric_invalid:{index}:{prefix}:unit")
    baseline = value.get("baseline_value")
    if not _finite_number(baseline):
        errors.append(
            f"forecast_metric_invalid:{index}:{prefix}:baseline_value"
        )
    observed = _aware_timestamp(value.get("baseline_observed_at"))
    if observed is None:
        errors.append(
            f"forecast_metric_invalid:{index}:{prefix}:baseline_observed_at"
        )
    elif registered is not None and observed > registered:
        errors.append(
            f"forecast_metric_invalid:{index}:{prefix}:baseline_in_future"
        )
    errors.extend(_validate_source(
        value.get("source"),
        index=index,
        prefix=f"{prefix}:source",
    ))
    return errors


def _validate_expectation(
    value: Any,
    *,
    index: int,
) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"forecast_expectation_invalid:{index}:not_object"]
    expected = {"kind", "direction", "lower_bound", "upper_bound"}
    errors = []
    if set(value) != expected:
        errors.append(f"forecast_expectation_invalid:{index}:fields")
    kind = value.get("kind")
    if kind not in EXPECTATION_KINDS:
        errors.append(f"forecast_expectation_invalid:{index}:kind")
    if kind == "direction":
        if value.get("direction") not in DIRECTIONS:
            errors.append(
                f"forecast_expectation_invalid:{index}:direction"
            )
        if (
            value.get("lower_bound") is not None
            or value.get("upper_bound") is not None
        ):
            errors.append(
                f"forecast_expectation_invalid:{index}:direction_bounds"
            )
    elif kind == "range":
        if value.get("direction") is not None:
            errors.append(
                f"forecast_expectation_invalid:{index}:range_direction"
            )
        lower = value.get("lower_bound")
        upper = value.get("upper_bound")
        if not _finite_number(lower) or not _finite_number(upper):
            errors.append(
                f"forecast_expectation_invalid:{index}:range_bounds"
            )
        elif float(lower) > float(upper):
            errors.append(
                f"forecast_expectation_invalid:{index}:range_order"
            )
    return errors


def _validate_entry_context(
    value: Any,
    *,
    index: int,
    registered: datetime | None,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [f"forecast_entry_context_invalid:{index}:not_object"]
    expected = {"kind", "expression", "price", "observed_at", "source"}
    errors = []
    if set(value) != expected:
        errors.append(f"forecast_entry_context_invalid:{index}:fields")
    if value.get("kind") not in ENTRY_KINDS:
        errors.append(f"forecast_entry_context_invalid:{index}:kind")
    expression = _text(value.get("expression"))
    if (
        not expression
        or len(expression) > MAX_FORECAST_TEXT_CHARS
    ):
        errors.append(f"forecast_entry_context_invalid:{index}:expression")
    price = value.get("price")
    if not _finite_number(price) or float(price) <= 0:
        errors.append(f"forecast_entry_context_invalid:{index}:price")
    observed = _aware_timestamp(value.get("observed_at"))
    if observed is None:
        errors.append(
            f"forecast_entry_context_invalid:{index}:observed_at"
        )
    elif registered is not None and observed > registered:
        errors.append(
            f"forecast_entry_context_invalid:{index}:observed_in_future"
        )
    errors.extend(_validate_source(
        value.get("source"),
        index=index,
        prefix="entry_context:source",
    ))
    return errors


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
        return [f"forecast_evidence_invalid:{index}:count"]
    errors = []
    seen = set()
    for evidence_index, raw in enumerate(value):
        ref = _text(raw)
        if (
            not ref
            or _EVIDENCE_REF.fullmatch(ref) is None
        ):
            errors.append(
                f"forecast_evidence_invalid:{index}:"
                f"invalid_ref:{evidence_index}"
            )
        elif ref in seen:
            errors.append(
                f"forecast_evidence_invalid:{index}:duplicate_ref:{ref}"
            )
        elif ref not in anchors:
            errors.append(
                f"forecast_evidence_invalid:{index}:dangling_ref:{ref}"
            )
        seen.add(ref)
    return errors


def _resolution_rule(expectation: Mapping[str, Any]) -> str:
    if expectation.get("kind") == "range":
        return "inside_inclusive_range"
    if expectation.get("direction") == "down":
        return "strictly_below_baseline_ties_false"
    return "strictly_above_baseline_ties_false"


def validate_forecast_registrations(
    registrations: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Validate measurable immutable forecasts without judging their merit."""
    if registrations is None:
        return []
    if data.get("host_input_schema_version") not in (
            STRUCTURED_FULL_CYCLE_VERSIONS):
        return ["forecast_registrations_require_schema_v3"]
    if not isinstance(registrations, list):
        return ["forecast_registrations_must_be_a_list"]
    if len(registrations) > MAX_FORECASTS_PER_CYCLE:
        return ["forecast_registrations_too_many"]
    # Overdue forecasts remain immutable calibration history and in the
    # denominator. They must not permanently prevent a distinct future
    # measurement event from being registered.
    registered_text = _registered_at(data)
    registered = _aware_timestamp(registered_text)
    anchors = _current_evidence_anchors(data)
    known_opportunities = _known_opportunity_ids(data, records)
    by_id, active_by_key, superseded = _known_forecasts(records)
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str, str, str]] = set()
    decision = data.get("decision")
    decision = decision if isinstance(decision, Mapping) else {}
    errors: list[str] = []
    expected_fields = {
        "forecast_id",
        "opportunity_id",
        "supersedes_forecast_id",
        "thesis",
        "metric",
        "horizon",
        "expectation",
        "confidence_probability",
        "benchmark",
        "entry_context",
        "risk_assumptions",
        "portfolio_context",
        "invalidation_condition",
        "evidence",
    }
    for index, row in enumerate(registrations):
        if not isinstance(row, Mapping):
            errors.append(f"forecast_registration_invalid:{index}:object")
            continue
        if set(row) != expected_fields:
            errors.append(f"forecast_registration_invalid:{index}:fields")
        forecast_id = _text(row.get("forecast_id"))
        if not forecast_id or _SAFE_ID.fullmatch(forecast_id) is None:
            errors.append(f"forecast_registration_invalid:{index}:id")
        elif forecast_id in seen_ids or forecast_id in by_id:
            errors.append(
                f"forecast_registration_duplicate_id:{index}:{forecast_id}"
            )
        seen_ids.add(forecast_id)

        opportunity_id = _text(row.get("opportunity_id"))
        if (
            not opportunity_id
            or _SAFE_ID.fullmatch(opportunity_id) is None
            or opportunity_id not in known_opportunities
        ):
            errors.append(
                f"forecast_opportunity_invalid:{index}:{opportunity_id}"
            )
        for field in (
            "thesis",
            "portfolio_context",
            "invalidation_condition",
        ):
            text = _text(row.get(field))
            if not text or len(text) > MAX_FORECAST_TEXT_CHARS:
                errors.append(
                    f"forecast_registration_invalid:{index}:{field}"
                )

        metric = row.get("metric")
        errors.extend(_validate_numeric_observation(
            metric,
            index=index,
            prefix="metric",
            registered=registered,
        ))
        horizon = row.get("horizon")
        if not isinstance(horizon, Mapping):
            errors.append(f"forecast_horizon_invalid:{index}:not_object")
        else:
            if set(horizon) != {
                "label",
                "target_at",
                "observation_window_seconds",
            }:
                errors.append(f"forecast_horizon_invalid:{index}:fields")
            label = _text(horizon.get("label"))
            if not label or len(label) > MAX_FORECAST_TEXT_CHARS:
                errors.append(f"forecast_horizon_invalid:{index}:label")
            target = _aware_timestamp(horizon.get("target_at"))
            if target is None:
                errors.append(
                    f"forecast_horizon_invalid:{index}:target_at"
                )
            elif registered is not None and target <= registered:
                errors.append(
                    f"forecast_horizon_invalid:{index}:not_future"
                )
            window = horizon.get("observation_window_seconds")
            if (
                isinstance(window, bool)
                or not isinstance(window, int)
                or window <= 0
            ):
                errors.append(
                    f"forecast_horizon_invalid:{index}:observation_window"
                )
        expectation = row.get("expectation")
        errors.extend(_validate_expectation(expectation, index=index))
        probability = row.get("confidence_probability")
        if (
            not _finite_number(probability)
            or not 0 <= float(probability) <= 1
        ):
            errors.append(
                f"forecast_confidence_invalid:{index}"
            )

        benchmark = row.get("benchmark")
        if benchmark is not None:
            errors.extend(_validate_numeric_observation(
                benchmark,
                index=index,
                prefix="benchmark",
                registered=registered,
            ))
        errors.extend(_validate_entry_context(
            row.get("entry_context"),
            index=index,
            registered=registered,
        ))
        assumptions = row.get("risk_assumptions")
        if (
            not isinstance(assumptions, list)
            or not assumptions
            or len(assumptions) > MAX_FORECAST_RISK_ASSUMPTIONS
            or any(
                not _text(item)
                or len(_text(item)) > MAX_FORECAST_TEXT_CHARS
                for item in assumptions
            )
        ):
            errors.append(
                f"forecast_risk_assumptions_invalid:{index}"
            )
        errors.extend(_validate_evidence(
            row.get("evidence"),
            index=index,
            anchors=anchors,
        ))

        if not isinstance(metric, Mapping) or not isinstance(horizon, Mapping):
            continue
        key = (
            opportunity_id.casefold(),
            _text(metric.get("name")).casefold(),
            _text(
                metric.get("source", {}).get("stable_ref")
                if isinstance(metric.get("source"), Mapping)
                else ""
            ).casefold(),
            _text(horizon.get("target_at")),
        )
        if key in seen_keys:
            errors.append(
                f"forecast_registration_duplicate_key:{index}"
            )
        seen_keys.add(key)
        active_id = active_by_key.get(key)
        supersedes = row.get("supersedes_forecast_id")
        supersedes_id = (
            _text(supersedes) if supersedes is not None else ""
        )
        if active_id and supersedes_id != active_id:
            errors.append(
                f"forecast_supersession_required:{index}:{active_id}"
            )
        if supersedes_id:
            prior = by_id.get(supersedes_id)
            if (
                prior is None
                or supersedes_id in superseded
                or _forecast_key(prior) != key
            ):
                errors.append(
                    f"forecast_supersession_invalid:{index}:{supersedes_id}"
                )
        elif supersedes is not None:
            errors.append(
                f"forecast_supersession_invalid:{index}:value"
            )
    return sorted(set(errors))


def validate_forecast_assessment(
    value: Any,
    *,
    data: Mapping[str, Any],
    required: bool,
) -> list[str]:
    """Validate the brain's decision-material forecast assessment."""
    if value is None:
        return ["forecast_assessment_required"] if required else []
    if not isinstance(value, Mapping):
        return ["forecast_assessment_invalid:not_object"]
    expected = {
        "status",
        "material_premise",
        "rationale",
        "forecast_ids",
    }
    errors = []
    if set(value) != expected:
        errors.append("forecast_assessment_invalid:fields")
    status = _text(value.get("status")).casefold()
    if status not in FORECAST_ASSESSMENT_STATUSES:
        errors.append("forecast_assessment_invalid:status")
    rationale = _text(value.get("rationale"))
    if (
        not isinstance(value.get("rationale"), str)
        or not rationale
        or len(rationale) > MAX_FORECAST_TEXT_CHARS
    ):
        errors.append("forecast_assessment_invalid:rationale")
    premise = value.get("material_premise")
    premise_text = _text(premise) if premise is not None else ""
    if premise is not None and not isinstance(premise, str):
        errors.append("forecast_assessment_invalid:material_premise")
    raw_ids = value.get("forecast_ids")
    if (
        not isinstance(raw_ids, list)
        or any(not isinstance(item, str) for item in raw_ids)
        or len(raw_ids) != len(set(map(str, raw_ids)))
    ):
        errors.append("forecast_assessment_invalid:forecast_ids")
        assessment_ids = []
    else:
        assessment_ids = [_text(item) for item in raw_ids]
        if any(
            not forecast_id
            or _SAFE_ID.fullmatch(forecast_id) is None
            for forecast_id in assessment_ids
        ):
            errors.append("forecast_assessment_invalid:forecast_ids")
    registrations = data.get("forecast_registrations")
    registrations = registrations if isinstance(registrations, list) else []
    registration_ids = [
        _text(row.get("forecast_id"))
        for row in registrations
        if isinstance(row, Mapping)
    ]
    if status == "required":
        if (
            not premise_text
            or len(premise_text) > MAX_FORECAST_TEXT_CHARS
        ):
            errors.append(
                "forecast_assessment_invalid:material_premise_required"
            )
        if not assessment_ids:
            errors.append(
                "forecast_assessment_invalid:forecast_ids_required"
            )
        elif not set(assessment_ids).issubset(set(registration_ids)):
            errors.append(
                "forecast_assessment_invalid:forecast_ids_mismatch"
            )
    elif status == "not_required":
        if premise is not None:
            errors.append(
                "forecast_assessment_invalid:material_premise_forbidden"
            )
        if assessment_ids:
            errors.append(
                "forecast_assessment_invalid:forecast_ids_forbidden"
            )
    return sorted(set(errors))


def forecast_assessment_record_ids(
    data: Mapping[str, Any],
    *,
    cycle_id: str | None = None,
) -> dict[str, str]:
    assessment = data.get("decision")
    assessment = (
        assessment.get("forecast_assessment")
        if isinstance(assessment, Mapping)
        else None
    )
    resolved_cycle_id = _text(cycle_id) or _text(data.get("cycle_id"))
    return (
        {
            f"forecast-assessment:{resolved_cycle_id}":
                "forecast_assessment"
        }
        if isinstance(assessment, Mapping) and resolved_cycle_id
        else {}
    )


def persist_forecast_assessment(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> int:
    decision = data.get("decision")
    decision = decision if isinstance(decision, Mapping) else {}
    assessment = decision.get("forecast_assessment")
    if not isinstance(assessment, Mapping):
        return 0
    cycle_id = _text(receipt.get("cycle_id"))
    record_id = f"forecast-assessment:{cycle_id}"
    payload = {
        "schema_version": FORECAST_ASSESSMENT_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "decision_status": _text(decision.get("status")).casefold(),
        "status": _text(assessment.get("status")).casefold(),
        "material_premise": (
            _text(assessment.get("material_premise")) or None
        ),
        "rationale": _text(assessment.get("rationale")),
        "forecast_ids": [
            _text(item) for item in assessment.get("forecast_ids") or ()
        ],
    }
    caused_by = [
        f"cycle-receipt:{cycle_id}",
        f"cycle-stage:{cycle_id}:decision",
    ]
    _record, created = journal.append_idempotent(
        record_id=record_id,
        record_type="forecast_assessment",
        agent="sovereign-host",
        caused_by=caused_by,
        payload=payload,
    )
    return int(created)


def forecast_assessment_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = MAX_FORECAST_FEEDBACK_ITEMS,
) -> dict[str, Any]:
    rows = []
    for record in _ordered_records(records, "forecast_assessment"):
        payload = record["payload"]
        rows.append({
            "record_id": record.get("record_id"),
            "cycle_id": payload.get("cycle_id"),
            "decision_status": payload.get("decision_status"),
            "status": payload.get("status"),
            "material_premise": (
                _bounded(payload.get("material_premise")) or None
            ),
            "rationale": _bounded(payload.get("rationale")),
            "forecast_ids": list(payload.get("forecast_ids") or ()),
        })
    recent = rows[-limit:]
    return {
        "total": len(rows),
        "required_count": sum(
            row.get("status") == "required" for row in rows
        ),
        "not_required_count": sum(
            row.get("status") == "not_required" for row in rows
        ),
        "recent": recent,
        "not_shown": max(0, len(rows) - len(recent)),
        "what_this_means": (
            "Brain-authored assessment of whether the decision rests on a "
            "material falsifiable premise. Runtime validates linkage but "
            "never chooses materiality."
        ),
    }


def _decision_record(
    records: Sequence[Mapping[str, Any]],
    cycle_id: str,
) -> Mapping[str, Any] | None:
    record_id = f"cycle-stage:{cycle_id}:decision"
    return next((
        record for record in records
        if record.get("record_id") == record_id
        and record.get("record_type") == "cycle_stage"
        and isinstance(record.get("payload"), Mapping)
    ), None)


def _opportunity_context(
    records: Sequence[Mapping[str, Any]],
    opportunity_id: str,
    *,
    registered_at: str,
) -> dict[str, Any] | None:
    rows = [
        record for record in _opportunity_records(records)
        if _text(record["payload"].get("opportunity_id")) == opportunity_id
    ]
    if not rows:
        return None
    registered = _aware_timestamp(registered_at)
    eligible = [
        record for record in rows
        if (
            registered is None
            or (
                observed := _aware_timestamp(
                    record["payload"].get("observed_at")
                )
            ) is None
            or observed <= registered
        )
    ]
    if not eligible:
        return None
    first = rows[0]
    latest = eligible[-1]
    return {
        "opportunity_id": opportunity_id,
        "record_id": latest.get("record_id"),
        "discovered_at": first["payload"].get("observed_at"),
        "state_at_registration": latest["payload"].get("to_state"),
    }


def _payload(
    row: Mapping[str, Any],
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    cycle_id = _text(data.get("cycle_id"))
    decision_record = _decision_record(records, cycle_id)
    if decision_record is None:
        raise ValueError(f"forecast_decision_stage_missing:{cycle_id}")
    decision_payload = decision_record["payload"]
    decision_output = decision_payload.get("output")
    decision_output = (
        decision_output if isinstance(decision_output, Mapping) else {}
    )
    snapshot_hash = _text(
        decision_output.get("snapshot_hash")
        or decision_output.get("ex_ante_snapshot_hash")
    )
    if not snapshot_hash:
        raise ValueError(f"forecast_snapshot_hash_missing:{cycle_id}")

    opportunity_id = _text(row.get("opportunity_id"))
    registered_at = _registered_at(data)
    opportunity = _opportunity_context(
        records,
        opportunity_id,
        registered_at=registered_at,
    )
    if opportunity is None:
        raise ValueError(
            f"forecast_opportunity_record_missing:{opportunity_id}"
        )
    metric = dict(row["metric"])
    registered = _aware_timestamp(registered_at)
    baseline = _aware_timestamp(metric.get("baseline_observed_at"))
    metric["baseline_age_seconds"] = (
        int((registered - baseline).total_seconds())
        if registered is not None and baseline is not None
        else None
    )
    expectation = dict(row["expectation"])
    expectation["resolution_rule"] = _resolution_rule(expectation)
    anchors = _current_evidence_anchors(data)
    evidence = [_text(ref) for ref in row.get("evidence") or ()]
    return {
        "schema_version": FORECAST_SCHEMA_VERSION,
        "forecast_id": _text(row.get("forecast_id")),
        "cycle_id": cycle_id,
        "registered_at": registered_at,
        "opportunity": opportunity,
        "opportunity_id": opportunity_id,
        "supersedes_forecast_id":
            _text(row.get("supersedes_forecast_id")) or None,
        "thesis": _text(row.get("thesis")),
        "metric": metric,
        "horizon": dict(row["horizon"]),
        "expectation": expectation,
        "confidence_probability": float(
            row["confidence_probability"]
        ),
        "benchmark": (
            dict(row["benchmark"])
            if isinstance(row.get("benchmark"), Mapping)
            else None
        ),
        "entry_context": (
            dict(row["entry_context"])
            if isinstance(row.get("entry_context"), Mapping)
            else None
        ),
        "risk_assumptions": [
            _text(item) for item in row.get("risk_assumptions") or ()
        ],
        "portfolio_context": _text(row.get("portfolio_context")),
        "invalidation_condition": _text(
            row.get("invalidation_condition")
        ),
        "evidence": evidence,
        "evidence_record_ids": list(dict.fromkeys(
            anchors[ref] for ref in evidence
        )),
        "decision": {
            "cycle_id": cycle_id,
            "status": decision_output.get("decision_status"),
            "record_id": decision_record.get("record_id"),
            "snapshot_hash": snapshot_hash,
        },
    }


def persist_forecast_registrations(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
    *,
    all_records: Sequence[Mapping[str, Any]] = (),
) -> int:
    registrations = data.get("forecast_registrations")
    if not isinstance(registrations, list) or not registrations:
        return 0
    combined = {
        _text(record.get("record_id")): record
        for record in [*all_records, *journal.read()]
        if _text(record.get("record_id"))
    }
    records = list(combined.values())
    cycle_id = _text(receipt.get("cycle_id"))
    existing = {
        _text(record.get("record_id")): record
        for record in records
        if record.get("record_type") == "forecast_registered"
    }
    new_rows = []
    for row in registrations:
        forecast_id = _text(row.get("forecast_id"))
        record_id = f"forecast:{forecast_id}"
        prior = existing.get(record_id)
        if prior is None:
            new_rows.append(row)
            continue
        payload = _payload(row, data=data, records=records)
        caused_by = [
            f"cycle-receipt:{cycle_id}",
            payload["decision"]["record_id"],
            payload["opportunity"]["record_id"],
            *payload["evidence_record_ids"],
        ]
        supersedes = payload.get("supersedes_forecast_id")
        if supersedes:
            caused_by.append(f"forecast:{supersedes}")
        caused_by = list(dict.fromkeys(caused_by))
        if (
            prior.get("payload") != payload
            or prior.get("caused_by") != caused_by
        ):
            raise ValueError(f"forecast_payload_mismatch:{record_id}")
    if not new_rows:
        return 0
    validation_records = [
        record for record in records
        if not (
            record.get("record_type") == "forecast_registered"
            and isinstance(record.get("payload"), Mapping)
            and _text(record["payload"].get("cycle_id")) == cycle_id
        )
    ]
    errors = validate_forecast_registrations(
        new_rows,
        data=data,
        records=validation_records,
    )
    if errors:
        raise ValueError(
            "forecast_registration_reconciliation_failed:"
            + ",".join(errors)
        )
    receipt_id = f"cycle-receipt:{cycle_id}"
    added = 0
    for row in new_rows:
        forecast_id = _text(row.get("forecast_id"))
        record_id = f"forecast:{forecast_id}"
        payload = _payload(row, data=data, records=records)
        caused_by = [
            receipt_id,
            payload["decision"]["record_id"],
            payload["opportunity"]["record_id"],
            *payload["evidence_record_ids"],
        ]
        supersedes = payload.get("supersedes_forecast_id")
        if supersedes:
            caused_by.append(f"forecast:{supersedes}")
        caused_by = list(dict.fromkeys(caused_by))
        prior = existing.get(record_id)
        if prior is not None:
            if (
                prior.get("payload") != payload
                or prior.get("caused_by") != caused_by
            ):
                raise ValueError(
                    f"forecast_payload_mismatch:{record_id}"
                )
            continue
        journal.append(
            record_id=record_id,
            record_type="forecast_registered",
            agent="sovereign-host",
            caused_by=caused_by,
            payload=payload,
        )
        added += 1
        new_record = journal.read()[-1]
        records.append(new_record)
        existing[record_id] = new_record
    return added


def backfill_forecast_registrations(
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
            or not isinstance(data.get("forecast_registrations"), list)
            or not data.get("forecast_registrations")
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
        added += persist_forecast_registrations(
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


def forecast_ledger_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = MAX_FORECAST_FEEDBACK_ITEMS,
    now: datetime | None = None,
) -> dict[str, Any]:
    rows = _forecast_records(records)
    superseded = {
        _text(row["payload"].get("supersedes_forecast_id"))
        for row in rows
        if _text(row["payload"].get("supersedes_forecast_id"))
    }
    outcomes = {
        _text(row["payload"].get("forecast_id")): row["payload"]
        for row in _ordered_records(records, "forecast_outcome")
        if _text(row["payload"].get("forecast_id"))
    }
    observed_now = now or datetime.now(timezone.utc)
    items = []
    for record in reversed(rows):
        payload = record["payload"]
        forecast_id = _text(payload.get("forecast_id"))
        metric = payload.get("metric")
        metric = metric if isinstance(metric, Mapping) else {}
        horizon = payload.get("horizon")
        horizon = horizon if isinstance(horizon, Mapping) else {}
        expectation = payload.get("expectation")
        expectation = (
            expectation if isinstance(expectation, Mapping) else {}
        )
        deadline = forecast_observation_deadline(payload)
        if forecast_id in outcomes:
            measurement_status = "measured"
        elif deadline is not None and observed_now > deadline:
            measurement_status = "overdue"
        else:
            measurement_status = "open"
        items.append({
            "forecast_id": forecast_id,
            "registration_status": (
                "superseded" if forecast_id in superseded else "active"
            ),
            "measurement_status": measurement_status,
            "supersedes_forecast_id":
                payload.get("supersedes_forecast_id"),
            "opportunity_id": payload.get("opportunity_id"),
            "registered_at": payload.get("registered_at"),
            "target_at": horizon.get("target_at"),
            "observation_window_seconds": horizon.get(
                "observation_window_seconds",
                LEGACY_OBSERVATION_WINDOW_SECONDS,
            ),
            "observation_deadline": (
                deadline.isoformat() if deadline is not None else None
            ),
            "horizon_label": horizon.get("label"),
            "metric": {
                "name": metric.get("name"),
                "unit": metric.get("unit"),
                "baseline_value": metric.get("baseline_value"),
                "baseline_observed_at":
                    metric.get("baseline_observed_at"),
                "baseline_age_seconds":
                    metric.get("baseline_age_seconds"),
                "source": metric.get("source"),
            },
            "expectation": expectation,
            "confidence_probability":
                payload.get("confidence_probability"),
            "benchmark": payload.get("benchmark"),
            "thesis": _bounded(payload.get("thesis")),
            "invalidation_condition": _bounded(
                payload.get("invalidation_condition")
            ),
            "decision": payload.get("decision"),
            "outcome": outcomes.get(forecast_id),
        })
    shown = items[:limit]
    return {
        "total": len(rows),
        "open_count": sum(
            1 for row in items if row["measurement_status"] == "open"
        ),
        "measured_count": sum(
            1 for row in items if row["measurement_status"] == "measured"
        ),
        "overdue_count": sum(
            1 for row in items if row["measurement_status"] == "overdue"
        ),
        "superseded_count": sum(
            1 for row in items
            if row["registration_status"] == "superseded"
        ),
        "items": shown,
        "not_shown": max(0, len(items) - len(shown)),
        "what_this_means": (
            "Immutable ex-ante numeric forecasts with frozen observation "
            "windows. Overdue forecasts remain visible in the matured "
            "denominator. Supersession does not erase measurement history, "
            "and invalidation remains an observable outcome disposition."
        ),
    }
