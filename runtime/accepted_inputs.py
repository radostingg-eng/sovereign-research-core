"""Causal selection of host inputs that produced valid cycle receipts."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .cycle_receipt import validate_audit_receipt_record


FEEDBACK_RECEIPT_STATUSES = frozenset({"completed", "blocked"})


def input_fingerprint(data: Mapping[str, Any]) -> str:
    """Content identity used by both execution and feedback selection."""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(
            "utf-8")
    ).hexdigest()[:16]


def feedback_snapshot_ids(
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Snapshots whose receipt is valid and useful for future reasoning."""
    accepted = set()
    for record in records:
        payload = record.get("payload")
        if (
            record.get("record_type") != "cycle_receipt"
            or not isinstance(payload, Mapping)
            or payload.get("status") not in FEEDBACK_RECEIPT_STATUSES
            or validate_audit_receipt_record(record)
        ):
            continue
        snapshot_id = str(payload.get("snapshot_id", "")).strip()
        if snapshot_id:
            accepted.add(snapshot_id)
    return accepted
