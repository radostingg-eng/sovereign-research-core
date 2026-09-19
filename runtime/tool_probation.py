"""Persist host-authored, evidence-backed tool probation verdicts."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .audit_store import AuditJournal
from .timestamps import effective_as_of, parse_iso_timestamp
from .tool_inventory import (
    full_inventory_for_record,
    normalize_action_name,
    validate_tool_manifest_report,
)
from .tool_provenance import resolve_tool_call

TOOL_PROBATION_STATUSES = frozenset({
    "adopted",
    "experimental",
    "redundant",
    "unreliable",
    "unsafe",
    "unavailable",
})
TOOL_PROBATION_SCHEMA_VERSION = 1
MAX_TOOL_PROBATIONS_PER_CYCLE = 8
MAX_TEXT_CHARS = 600
_SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}")
_ROW_FIELDS = frozenset({
    "probation_id",
    "tool_inventory_record_id",
    "connector",
    "action",
    "status",
    "observed_at",
    "evidence_tool_call_ids",
    "assessment",
    "capability_review",
    "rationale",
    "supersedes_probation_id",
})
_ASSESSMENT_FIELDS = frozenset({
    "result_summary",
    "names_sources",
    "names_observation_time",
    "incremental_value",
    "failure_behavior",
})
_CAPABILITY_REVIEW_FIELDS = frozenset({
    "purpose",
    "authority_boundary",
    "risk_controls",
    "independent_operational_reason",
})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ordered_records(
    records: Sequence[Mapping[str, Any]],
    record_type: str,
) -> list[Mapping[str, Any]]:
    return [
        record for record in records
        if record.get("record_type") == record_type
        and isinstance(record.get("payload"), Mapping)
    ]


def _finalized_cycle_ids(
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    return {
        _text(record["payload"].get("cycle_id"))
        for record in _ordered_records(records, "cycle_finalization")
        if _text(record["payload"].get("cycle_id"))
    }


def _inventory_record(
    records: Sequence[Mapping[str, Any]],
    record_id: str,
) -> Mapping[str, Any] | None:
    record = next((
        record for record in records
        if record.get("record_id") == record_id
    ), None)
    if not isinstance(record, Mapping):
        return None
    if record.get("record_type") != "tool_inventory":
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None
    caused_by = [
        _text(value) for value in record.get("caused_by") or ()
    ]
    cycle_id = next((
        value.removeprefix("cycle-receipt:")
        for value in caused_by
        if value.startswith("cycle-receipt:")
    ), "")
    if cycle_id not in _finalized_cycle_ids(records):
        return None
    if validate_tool_manifest_report(payload):
        return None
    return record


def _inventory_action(
    payload: Mapping[str, Any],
    *,
    connector: str,
    action: str,
) -> Mapping[str, Any] | None:
    connector_key = connector.casefold()
    action_key = normalize_action_name(action)
    for connector_row in payload.get("connectors") or ():
        if not isinstance(connector_row, Mapping):
            continue
        if _text(connector_row.get("name")).casefold() != connector_key:
            continue
        for action_row in connector_row.get("actions") or ():
            if (
                isinstance(action_row, Mapping)
                and normalize_action_name(action_row.get("name"))
                == action_key
            ):
                return action_row
    return None


def _known_probations(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[tuple[str, str], str], set[str]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    superseded: set[str] = set()
    for record in _ordered_records(records, "tool_probation"):
        payload = record["payload"]
        probation_id = _text(payload.get("probation_id"))
        if probation_id:
            by_id[probation_id] = payload
        prior = _text(payload.get("supersedes_probation_id"))
        if prior:
            superseded.add(prior)
    active = {
        (
            _text(payload.get("connector")).casefold(),
            normalize_action_name(payload.get("action")),
        ): probation_id
        for probation_id, payload in by_id.items()
        if probation_id not in superseded
    }
    return by_id, active, superseded


def _valid_text(value: Any) -> bool:
    return bool(_text(value)) and len(_text(value)) <= MAX_TEXT_CHARS


def validate_tool_probations(
    rows: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        return ["tool_probations_must_be_a_list"]
    if len(rows) > MAX_TOOL_PROBATIONS_PER_CYCLE:
        return ["tool_probations_too_many"]
    if data.get("host_input_schema_version") not in {3, 4}:
        return ["tool_probations_require_schema_v3"]

    cycle_as_of = parse_iso_timestamp(effective_as_of(data))
    known, active, superseded = _known_probations(records)
    seen_ids: set[str] = set()
    seen_tools: set[tuple[str, str]] = set()
    errors: list[str] = []
    for index, row in enumerate(rows):
        prefix = f"tool_probation_invalid:{index}"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}:object")
            continue
        if set(row) != _ROW_FIELDS:
            errors.append(f"{prefix}:fields")
        probation_id = _text(row.get("probation_id"))
        if not probation_id or _SAFE_ID.fullmatch(probation_id) is None:
            errors.append(f"{prefix}:id")
        elif probation_id in seen_ids or probation_id in known:
            errors.append(f"tool_probation_duplicate_id:{index}:{probation_id}")
        seen_ids.add(probation_id)

        connector = _text(row.get("connector"))
        action = _text(row.get("action"))
        tool_key = (connector.casefold(), normalize_action_name(action))
        if not connector:
            errors.append(f"{prefix}:connector")
        if not action:
            errors.append(f"{prefix}:action")
        if tool_key in seen_tools:
            errors.append(f"tool_probation_duplicate_tool:{index}")
        seen_tools.add(tool_key)

        inventory_record_id = _text(row.get("tool_inventory_record_id"))
        inventory_record = _inventory_record(records, inventory_record_id)
        inventory_action = None
        if inventory_record is None:
            errors.append(f"{prefix}:inventory_record")
        else:
            inventory_action = _inventory_action(
                inventory_record["payload"],
                connector=connector,
                action=action,
            )
            if inventory_action is None:
                errors.append(f"{prefix}:inventory_action")

        status = row.get("status")
        if status not in TOOL_PROBATION_STATUSES:
            errors.append(f"{prefix}:status")
        observed_at = parse_iso_timestamp(row.get("observed_at"))
        if observed_at is None:
            errors.append(f"{prefix}:observed_at")
        elif cycle_as_of is not None and observed_at > cycle_as_of:
            errors.append(f"{prefix}:after_cycle")
        if not _valid_text(row.get("rationale")):
            errors.append(f"{prefix}:rationale")

        assessment = row.get("assessment")
        if not isinstance(assessment, Mapping):
            errors.append(f"{prefix}:assessment")
        else:
            if set(assessment) != _ASSESSMENT_FIELDS:
                errors.append(f"{prefix}:assessment_fields")
            for field in (
                "result_summary",
                "incremental_value",
                "failure_behavior",
            ):
                if not _valid_text(assessment.get(field)):
                    errors.append(f"{prefix}:assessment_{field}")
            for field in ("names_sources", "names_observation_time"):
                if not isinstance(assessment.get(field), bool):
                    errors.append(f"{prefix}:assessment_{field}")

        mode = (
            inventory_action.get("mode")
            if isinstance(inventory_action, Mapping)
            else None
        )
        review = row.get("capability_review")
        review_required = (
            mode in {"write", "write_nontransmitting"}
            or status in {"unsafe", "unavailable"}
        )
        if review_required:
            if not isinstance(review, Mapping):
                errors.append(f"{prefix}:capability_review")
            else:
                if set(review) != _CAPABILITY_REVIEW_FIELDS:
                    errors.append(f"{prefix}:capability_review_fields")
                for field in _CAPABILITY_REVIEW_FIELDS:
                    if not _valid_text(review.get(field)):
                        errors.append(
                            f"{prefix}:capability_review_{field}"
                        )
        elif review is not None and not isinstance(review, Mapping):
            errors.append(f"{prefix}:capability_review")

        evidence_ids = row.get("evidence_tool_call_ids")
        if not isinstance(evidence_ids, list) or len(evidence_ids) > 4:
            errors.append(f"{prefix}:evidence_tool_call_ids")
            evidence_ids = []
        elif len(evidence_ids) != len(set(evidence_ids)):
            errors.append(f"{prefix}:duplicate_evidence_tool_call_id")
        if status not in {"unsafe", "unavailable"} and not evidence_ids:
            errors.append(f"{prefix}:evidence_required")
        for evidence_index, raw_id in enumerate(evidence_ids):
            tool_call_id = _text(raw_id)
            resolved = resolve_tool_call(data, tool_call_id)
            if resolved is None:
                errors.append(
                    f"{prefix}:evidence_unresolved:{evidence_index}"
                )
                continue
            _metadata, call = resolved
            call_action = call.get("call")
            call_action = (
                call_action.get("action")
                if isinstance(call_action, Mapping)
                else None
            )
            if normalize_action_name(call_action) != tool_key[1]:
                errors.append(
                    f"{prefix}:evidence_action_mismatch:{evidence_index}"
                )

        supersedes_value = row.get("supersedes_probation_id")
        supersedes_id = (
            _text(supersedes_value)
            if supersedes_value is not None
            else ""
        )
        active_id = active.get(tool_key)
        if active_id and supersedes_id != active_id:
            errors.append(
                f"tool_probation_supersession_required:{index}:{active_id}"
            )
        if supersedes_id:
            prior = known.get(supersedes_id)
            prior_key = (
                _text(prior.get("connector")).casefold(),
                normalize_action_name(prior.get("action")),
            ) if isinstance(prior, Mapping) else None
            if (
                prior is None
                or supersedes_id in superseded
                or prior_key != tool_key
            ):
                errors.append(
                    f"tool_probation_supersession_invalid:"
                    f"{index}:{supersedes_id}"
                )
        elif supersedes_value is not None:
            errors.append(f"{prefix}:supersedes_value")
    return sorted(set(errors))


def _payload(
    row: Mapping[str, Any],
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    inventory_record_id = _text(row.get("tool_inventory_record_id"))
    inventory = full_inventory_for_record(records, inventory_record_id)
    action = _inventory_action(
        inventory,
        connector=_text(row.get("connector")),
        action=_text(row.get("action")),
    )
    if action is None:
        raise ValueError(
            "tool_probation_inventory_action_missing:"
            f"{row.get('probation_id')}"
        )
    evidence = []
    for raw_id in row.get("evidence_tool_call_ids") or ():
        resolved = resolve_tool_call(data, _text(raw_id))
        if resolved is None:
            raise ValueError(
                f"tool_probation_evidence_missing:{raw_id}"
            )
        metadata, _call = resolved
        evidence.append({
            key: metadata.get(key)
            for key in (
                "tool_call_id",
                "scope",
                "tool",
                "observed_at",
                "result_sha256",
                "capture_origin",
                "reconstruction_status",
            )
        })
    return {
        "schema_version": TOOL_PROBATION_SCHEMA_VERSION,
        "probation_id": _text(row.get("probation_id")),
        "cycle_id": _text(data.get("cycle_id")),
        "tool_inventory_record_id": inventory_record_id,
        "connector": _text(row.get("connector")),
        "action": _text(row.get("action")),
        "mode": action.get("mode"),
        "status": row.get("status"),
        "observed_at": row.get("observed_at"),
        "assessment": dict(row.get("assessment") or {}),
        "capability_review": (
            dict(row["capability_review"])
            if isinstance(row.get("capability_review"), Mapping)
            else None
        ),
        "rationale": row.get("rationale"),
        "supersedes_probation_id":
            _text(row.get("supersedes_probation_id")) or None,
        "evidence": evidence,
    }


def persist_tool_probations(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> int:
    rows = data.get("tool_probations")
    if not isinstance(rows, list) or not rows:
        return 0
    records = journal.read()
    errors = validate_tool_probations(
        rows,
        data=data,
        records=records,
    )
    if errors:
        raise ValueError(
            "tool_probation_reconciliation_failed:"
            + ",".join(errors)
        )
    cycle_id = _text(receipt.get("cycle_id"))
    written = 0
    for row in rows:
        payload = _payload(row, data=data, records=records)
        record_id = (
            f"tool-probation:{cycle_id}:{payload['probation_id']}"
        )
        existing = next((
            record for record in journal.read()
            if record.get("record_id") == record_id
        ), None)
        if existing is not None:
            if existing.get("payload") != payload:
                raise ValueError(
                    f"tool_probation_payload_mismatch:{record_id}"
                )
            continue
        journal.append(
            record_id=record_id,
            record_type="tool_probation",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{cycle_id}",),
            payload=payload,
        )
        records.append(journal.read()[-1])
        written += 1
    return written


def tool_probation_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = 12,
) -> dict[str, Any]:
    rows = _ordered_records(records, "tool_probation")
    superseded = {
        _text(record["payload"].get("supersedes_probation_id"))
        for record in rows
        if _text(record["payload"].get("supersedes_probation_id"))
    }
    active = [
        record["payload"]
        for record in rows
        if _text(record["payload"].get("probation_id")) not in superseded
    ]
    counts = {
        status: sum(row.get("status") == status for row in active)
        for status in sorted(TOOL_PROBATION_STATUSES)
        if any(row.get("status") == status for row in active)
    }
    items = [{
        "probation_id": row.get("probation_id"),
        "connector": row.get("connector"),
        "action": row.get("action"),
        "mode": row.get("mode"),
        "status": row.get("status"),
        "observed_at": row.get("observed_at"),
        "tool_inventory_record_id": row.get(
            "tool_inventory_record_id"
        ),
        "evidence": list(row.get("evidence") or ()),
        "rationale": row.get("rationale"),
    } for row in active[-limit:]]
    return {
        "total_records": len(rows),
        "active_count": len(active),
        "counts_by_status": counts,
        "items": items,
        "not_shown": max(0, len(active) - limit),
        "what_this_means": (
            "These are host-authored verdicts bound to an exact finalized "
            "tool inventory and current-cycle call hashes. The runtime "
            "validates evidence and authority but does not choose the verdict."
        ),
    }


def probation_record_ids(
    data: Mapping[str, Any],
    *,
    cycle_id: str | None = None,
) -> dict[str, str]:
    cycle_id = _text(cycle_id) or _text(data.get("cycle_id"))
    return {
        f"tool-probation:{cycle_id}:{_text(row.get('probation_id'))}":
            "tool_probation"
        for row in data.get("tool_probations") or ()
        if isinstance(row, Mapping) and _text(row.get("probation_id"))
    }
