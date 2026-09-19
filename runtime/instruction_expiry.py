"""Detect expiring saved instructions and persist standing host decisions."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Mapping, Sequence

from .audit_store import AuditJournal
from .timestamps import effective_as_of, parse_iso_timestamp
from .tool_provenance import resolve_tool_call

EXPIRY_WINDOW = timedelta(hours=48)
INSTRUCTION_EXPIRY_SCHEMA_VERSION = 1
MAX_EXPIRY_DECISIONS_PER_CYCLE = 8
MAX_TEXT_CHARS = 600
DECISIONS = frozenset({"let_expire", "delete", "recreate"})
_SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}")
_ROW_FIELDS = frozenset({
    "decision_id",
    "instruction_id",
    "observed_expiration",
    "decision",
    "rationale",
    "pre_read_tool_call_id",
    "activity_indexes",
    "replacement_instruction_id",
    "evidence",
    "supersedes_decision_id",
})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _instruction_id(value: Mapping[str, Any]) -> str:
    return _text(value.get("id") or value.get("instruction_id"))


def _instruction_rows(result: Any) -> list[Mapping[str, Any]]:
    if not isinstance(result, Mapping):
        return []
    rows = result.get("instructions")
    if rows is None:
        rows = result.get("order_instructions")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _saved_instruction_observation(
    data: Mapping[str, Any],
) -> tuple[str, list[Mapping[str, Any]]] | None:
    for wrapper in data.get("evidence_calls") or ():
        if (
            not isinstance(wrapper, Mapping)
            or _text(wrapper.get("producer")) != "saved_instructions"
        ):
            continue
        call = wrapper.get("call")
        if not isinstance(call, Mapping):
            continue
        call_id = _text(call.get("tool_call_id"))
        if call_id:
            return call_id, _instruction_rows(call.get("result"))
    return None


def _known_decisions(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[tuple[str, str], str], set[str]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    superseded: set[str] = set()
    for record in records:
        if (
            record.get("record_type") != "instruction_expiry_decision"
            or not isinstance(record.get("payload"), Mapping)
        ):
            continue
        payload = record["payload"]
        decision_id = _text(payload.get("decision_id"))
        if decision_id:
            by_id[decision_id] = payload
        prior = _text(payload.get("supersedes_decision_id"))
        if prior:
            superseded.add(prior)
    active = {
        (
            _text(payload.get("instruction_id")),
            _text(payload.get("observed_expiration")),
        ): decision_id
        for decision_id, payload in by_id.items()
        if decision_id not in superseded
    }
    return by_id, active, superseded


def expiring_instructions(
    data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    observation = _saved_instruction_observation(data)
    cycle_at = parse_iso_timestamp(effective_as_of(data))
    if observation is None or cycle_at is None:
        return []
    tool_call_id, instructions = observation
    rows = []
    for instruction in instructions:
        instruction_id = _instruction_id(instruction)
        expiration_text = _text(
            instruction.get("expiration")
            or instruction.get("expires_at")
            or instruction.get("expiry")
        )
        expiration = parse_iso_timestamp(expiration_text)
        if not instruction_id or expiration is None:
            continue
        remaining = expiration - cycle_at
        if remaining > EXPIRY_WINDOW:
            continue
        rows.append({
            "instruction_id": instruction_id,
            "observed_expiration": expiration_text,
            "pre_read_tool_call_id": tool_call_id,
            "state": (
                "expired" if remaining.total_seconds() <= 0
                else "due"
            ),
            "hours_remaining": round(
                remaining.total_seconds() / 3600,
                3,
            ),
        })
    return sorted(
        rows,
        key=lambda row: (
            row["observed_expiration"],
            row["instruction_id"],
        ),
    )


def _activity_rows(
    data: Mapping[str, Any],
    indexes: Any,
) -> tuple[list[Mapping[str, Any]], list[str]]:
    if not isinstance(indexes, list):
        return [], ["activity_indexes"]
    if len(indexes) != len(set(indexes)):
        return [], ["activity_indexes_duplicate"]
    source = data.get("order_instruction_activity")
    source = source if isinstance(source, list) else []
    rows = []
    errors = []
    for index in indexes:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(source)
        ):
            errors.append(f"activity_index:{index}")
            continue
        row = source[index]
        if not isinstance(row, Mapping):
            errors.append(f"activity_not_object:{index}")
            continue
        rows.append(row)
    return rows, errors


def _activity_instruction_id(row: Mapping[str, Any]) -> str:
    result = row.get("result")
    result = result if isinstance(result, Mapping) else {}
    return _text(
        row.get("instruction_id")
        or result.get("instruction_id")
        or result.get("id")
    )


def _post_read_ids(row: Mapping[str, Any]) -> set[str]:
    return {
        _instruction_id(instruction)
        for instruction in _instruction_rows(row.get("result"))
        if _instruction_id(instruction)
    }


def _evidence_anchors(data: Mapping[str, Any]) -> set[str]:
    anchors = {
        f"stage:{_text(stage.get('stage_id'))}"
        for stage in data.get("cognitive_stages") or ()
        if isinstance(stage, Mapping) and _text(stage.get("stage_id"))
    }
    anchors.update(
        f"finding:{_text(finding.get('id'))}"
        for finding in data.get("findings") or ()
        if isinstance(finding, Mapping) and _text(finding.get("id"))
    )
    return anchors


def validate_instruction_expiry_decisions(
    rows: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        return ["instruction_expiry_decisions_must_be_a_list"]
    if len(rows) > MAX_EXPIRY_DECISIONS_PER_CYCLE:
        return ["instruction_expiry_decisions_too_many"]
    if data.get("host_input_schema_version") not in {3, 4}:
        return ["instruction_expiry_decisions_require_schema_v3"]

    observation = _saved_instruction_observation(data)
    observed_rows = {
        (
            _instruction_id(instruction),
            _text(
                instruction.get("expiration")
                or instruction.get("expires_at")
                or instruction.get("expiry")
            ),
        ): instruction
        for instruction in (observation[1] if observation else ())
    }
    known, active, superseded = _known_decisions(records)
    cycle_at = parse_iso_timestamp(effective_as_of(data))
    anchors = _evidence_anchors(data)
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    errors = []
    for index, row in enumerate(rows):
        prefix = f"instruction_expiry_decision_invalid:{index}"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}:object")
            continue
        if set(row) != _ROW_FIELDS:
            errors.append(f"{prefix}:fields")
        decision_id = _text(row.get("decision_id"))
        if not decision_id or _SAFE_ID.fullmatch(decision_id) is None:
            errors.append(f"{prefix}:id")
        elif decision_id in seen_ids or decision_id in known:
            errors.append(
                f"instruction_expiry_decision_duplicate_id:"
                f"{index}:{decision_id}"
            )
        seen_ids.add(decision_id)
        instruction_id = _text(row.get("instruction_id"))
        expiration_text = _text(row.get("observed_expiration"))
        key = (instruction_id, expiration_text)
        if key in seen_keys:
            errors.append(
                f"instruction_expiry_decision_duplicate_instruction:{index}"
            )
        seen_keys.add(key)
        if key not in observed_rows:
            errors.append(f"{prefix}:current_instruction")
        expiration = parse_iso_timestamp(expiration_text)
        if expiration is None:
            errors.append(f"{prefix}:observed_expiration")
        elif (
            cycle_at is not None
            and expiration - cycle_at > EXPIRY_WINDOW
        ):
            errors.append(f"{prefix}:outside_window")
        decision = row.get("decision")
        if decision not in DECISIONS:
            errors.append(f"{prefix}:decision")
        rationale = _text(row.get("rationale"))
        if not rationale or len(rationale) > MAX_TEXT_CHARS:
            errors.append(f"{prefix}:rationale")

        pre_read_id = _text(row.get("pre_read_tool_call_id"))
        if observation is None or pre_read_id != observation[0]:
            errors.append(f"{prefix}:pre_read_tool_call_id")
        else:
            resolved = resolve_tool_call(data, pre_read_id)
            if resolved is None:
                errors.append(f"{prefix}:pre_read_unresolved")

        evidence = row.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(not _text(value) for value in evidence)
        ):
            errors.append(f"{prefix}:evidence")
        else:
            for evidence_index, value in enumerate(evidence):
                if _text(value) not in anchors:
                    errors.append(
                        f"{prefix}:evidence_dangling:{evidence_index}"
                    )

        activities, activity_errors = _activity_rows(
            data,
            row.get("activity_indexes"),
        )
        errors.extend(
            f"{prefix}:{problem}" for problem in activity_errors
        )
        operations = [
            _text(activity.get("operation")).lower()
            for activity in activities
        ]
        replacement_id = _text(row.get("replacement_instruction_id"))
        if decision == "let_expire":
            if activities:
                errors.append(f"{prefix}:let_expire_has_activity")
            if replacement_id:
                errors.append(f"{prefix}:let_expire_replacement")
        elif decision == "delete":
            if operations != ["delete", "get"]:
                errors.append(f"{prefix}:delete_activity_sequence")
            elif instruction_id in _post_read_ids(activities[-1]):
                errors.append(f"{prefix}:delete_not_confirmed")
            if replacement_id:
                errors.append(f"{prefix}:delete_replacement")
        elif decision == "recreate":
            if operations != ["delete", "create", "get"]:
                errors.append(f"{prefix}:recreate_activity_sequence")
            if not replacement_id or replacement_id == instruction_id:
                errors.append(f"{prefix}:replacement_instruction_id")
            elif activities:
                post_ids = _post_read_ids(activities[-1])
                if (
                    instruction_id in post_ids
                    or replacement_id not in post_ids
                ):
                    errors.append(f"{prefix}:recreate_not_confirmed")
                if len(activities) >= 2 and (
                    _activity_instruction_id(activities[1])
                    != replacement_id
                ):
                    errors.append(f"{prefix}:create_result_mismatch")

        supersedes_value = row.get("supersedes_decision_id")
        supersedes_id = (
            _text(supersedes_value)
            if supersedes_value is not None
            else ""
        )
        active_id = active.get(key)
        if active_id and supersedes_id != active_id:
            errors.append(
                "instruction_expiry_decision_supersession_required:"
                f"{index}:{active_id}"
            )
        if supersedes_id:
            prior = known.get(supersedes_id)
            prior_key = (
                _text(prior.get("instruction_id")),
                _text(prior.get("observed_expiration")),
            ) if isinstance(prior, Mapping) else None
            if (
                prior is None
                or supersedes_id in superseded
                or prior_key != key
            ):
                errors.append(
                    "instruction_expiry_decision_supersession_invalid:"
                    f"{index}:{supersedes_id}"
                )
        elif supersedes_value is not None:
            errors.append(f"{prefix}:supersedes_value")
    return sorted(set(errors))


def _payload(
    row: Mapping[str, Any],
    *,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    activities, errors = _activity_rows(
        data,
        row.get("activity_indexes"),
    )
    if errors:
        raise ValueError(
            "instruction_expiry_activity_invalid:" + ",".join(errors)
        )
    return {
        "schema_version": INSTRUCTION_EXPIRY_SCHEMA_VERSION,
        "decision_id": _text(row.get("decision_id")),
        "cycle_id": _text(data.get("cycle_id")),
        "instruction_id": _text(row.get("instruction_id")),
        "observed_expiration": row.get("observed_expiration"),
        "decision": row.get("decision"),
        "rationale": row.get("rationale"),
        "pre_read_tool_call_id": row.get("pre_read_tool_call_id"),
        "activity": [{
            "index": index,
            "operation": activity.get("operation"),
            "tool": activity.get("tool"),
            "instruction_id": _activity_instruction_id(activity) or None,
        } for index, activity in zip(
            row.get("activity_indexes") or (),
            activities,
        )],
        "replacement_instruction_id":
            _text(row.get("replacement_instruction_id")) or None,
        "evidence": list(row.get("evidence") or ()),
        "supersedes_decision_id":
            _text(row.get("supersedes_decision_id")) or None,
    }


def persist_instruction_expiry_decisions(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> int:
    rows = data.get("instruction_expiry_decisions")
    if not isinstance(rows, list) or not rows:
        return 0
    records = journal.read()
    errors = validate_instruction_expiry_decisions(
        rows,
        data=data,
        records=records,
    )
    if errors:
        raise ValueError(
            "instruction_expiry_decision_reconciliation_failed:"
            + ",".join(errors)
        )
    cycle_id = _text(receipt.get("cycle_id"))
    written = 0
    for row in rows:
        payload = _payload(row, data=data)
        record_id = (
            f"instruction-expiry-decision:{cycle_id}:"
            f"{payload['decision_id']}"
        )
        existing = next((
            record for record in journal.read()
            if record.get("record_id") == record_id
        ), None)
        if existing is not None:
            if existing.get("payload") != payload:
                raise ValueError(
                    "instruction_expiry_decision_payload_mismatch:"
                    f"{record_id}"
                )
            continue
        journal.append(
            record_id=record_id,
            record_type="instruction_expiry_decision",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{cycle_id}",),
            payload=payload,
        )
        written += 1
    return written


def instruction_expiry_record_ids(
    data: Mapping[str, Any],
    *,
    cycle_id: str,
) -> dict[str, str]:
    return {
        (
            f"instruction-expiry-decision:{cycle_id}:"
            f"{_text(row.get('decision_id'))}"
        ): "instruction_expiry_decision"
        for row in data.get("instruction_expiry_decisions") or ()
        if isinstance(row, Mapping) and _text(row.get("decision_id"))
    }


def instruction_expiry_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    latest_input: Mapping[str, Any] | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    known, active, _superseded = _known_decisions(records)
    current = expiring_instructions(latest_input or {})
    current_keys = {
        (row["instruction_id"], row["observed_expiration"])
        for row in current
    }
    items = []
    for row in current:
        key = (row["instruction_id"], row["observed_expiration"])
        decision_id = active.get(key)
        decision = known.get(decision_id, {})
        items.append({
            **row,
            "decision_id": decision_id,
            "decision": decision.get("decision"),
            "decision_status": (
                "decision_recorded" if decision_id
                else "decision_needed"
            ),
        })
    for key, decision_id in active.items():
        if key in current_keys:
            continue
        decision = known[decision_id]
        items.append({
            "instruction_id": key[0],
            "observed_expiration": key[1],
            "pre_read_tool_call_id": decision.get(
                "pre_read_tool_call_id"
            ),
            "state": "not_present_in_latest_read",
            "hours_remaining": None,
            "decision_id": decision_id,
            "decision": decision.get("decision"),
            "decision_status": "terminal_observation_pending_review",
        })
    return {
        "enforcement": "advisory_until_success_gate",
        "decision_window_hours": int(
            EXPIRY_WINDOW.total_seconds() // 3600
        ),
        "due_count": len(current),
        "decision_needed_count": sum(
            row["decision_status"] == "decision_needed"
            for row in items
        ),
        "active_decision_count": len(active),
        "items": items[:limit],
        "not_shown": max(0, len(items) - limit),
        "what_this_means": (
            "Expiry is computed from the captured saved-instructions "
            "response. Decisions are standing and idempotent for one "
            "instruction id plus observed expiration. Missing decisions are "
            "advisory until the post-foundation success gate is complete."
        ),
    }
