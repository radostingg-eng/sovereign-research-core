"""Content-address the exact bounded worker leads shown in host feedback."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .audit_store import AuditJournal

WORKER_RESEARCH_PROJECTION_SCHEMA_VERSION = 1
WORKER_RESEARCH_PROJECTION_PREFIX = "worker-research-projection:v1:"
_PROJECTION_ID = re.compile(
    r"worker-research-projection:v1:[0-9a-f]{64}"
)
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_RESULT_FIELDS = (
    "role",
    "output_contract_version",
    "summary",
    "suggested_next_question",
    "evidence_needed",
    "counterevidence",
    "falsification_conditions",
)


def _projection_item(row: Mapping[str, Any]) -> dict[str, Any]:
    target = row.get("target")
    result = row.get("result")
    record_id = row.get("record_id")
    worker_id = row.get("worker_id")
    if (
        row.get("status") != "completed"
        or not isinstance(record_id, str)
        or _WORKER_ID.fullmatch(record_id) is None
        or not isinstance(worker_id, str)
        or _WORKER_ID.fullmatch(worker_id) is None
        or not isinstance(target, Mapping)
        or not isinstance(result, Mapping)
    ):
        raise ValueError("worker_research_projection_invalid")
    return {
        "record_id": record_id,
        "worker_id": worker_id,
        "status": "completed",
        "observed_at": row.get("observed_at"),
        "target": {
            "question_id": target.get("question_id"),
            "question": target.get("question"),
        },
        "result": {
            key: deepcopy(result[key])
            for key in _RESULT_FIELDS
            if key in result
        },
    }


def _projection_payload(
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    items = summary.get("items")
    required = summary.get("adoption_required_record_ids")
    if not isinstance(items, list) or not isinstance(required, list):
        raise ValueError("worker_research_projection_invalid")
    projected = [
        _projection_item(row)
        for row in items
        if isinstance(row, Mapping) and row.get("status") == "completed"
    ]
    record_ids = [row["record_id"] for row in projected]
    if required != record_ids or len(set(record_ids)) != len(record_ids):
        raise ValueError("worker_research_projection_invalid")
    return {
        "schema_version": WORKER_RESEARCH_PROJECTION_SCHEMA_VERSION,
        "adoption_required_record_ids": record_ids,
        "items": projected,
    }


def _projection_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return (
        WORKER_RESEARCH_PROJECTION_PREFIX
        + hashlib.sha256(encoded).hexdigest()
    )


def worker_projection_id(summary: Mapping[str, Any]) -> str:
    return _projection_id(_projection_payload(summary))


def persist_worker_projection(
    summary: Mapping[str, Any],
    journal: AuditJournal,
) -> str:
    payload = _projection_payload(summary)
    record_id = _projection_id(payload)
    if summary.get("projection_id") != record_id:
        raise ValueError("worker_research_projection_invalid")
    journal.append_idempotent(
        record_id=record_id,
        record_type="worker_research_projection",
        agent="runtime-host-cycle",
        payload=payload,
    )
    return record_id


def load_worker_projection(
    projection_id: Any,
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if (
        not isinstance(projection_id, str)
        or _PROJECTION_ID.fullmatch(projection_id) is None
    ):
        raise ValueError("worker_research_projection_invalid")
    matches = [
        row for row in records
        if row.get("record_id") == projection_id
    ]
    if not matches:
        raise ValueError("worker_research_projection_unknown")
    if len(matches) != 1:
        raise ValueError("worker_research_projection_invalid")
    record = matches[0]
    payload = record.get("payload")
    if (
        record.get("record_type") != "worker_research_projection"
        or record.get("agent") != "runtime-host-cycle"
        or not isinstance(payload, Mapping)
        or set(payload) != {
            "schema_version",
            "adoption_required_record_ids",
            "items",
        }
        or payload.get("schema_version")
        != WORKER_RESEARCH_PROJECTION_SCHEMA_VERSION
    ):
        raise ValueError("worker_research_projection_invalid")
    expected = _projection_payload(payload)
    if expected != payload or _projection_id(payload) != projection_id:
        raise ValueError("worker_research_projection_invalid")
    return deepcopy(expected["items"])
