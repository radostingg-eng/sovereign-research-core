"""Causal selection of host inputs that produced valid cycle receipts."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .cycle_receipt import validate_audit_receipt_record


FEEDBACK_RECEIPT_STATUSES = frozenset({"completed", "blocked"})


def canonical_input_sha256(data: Mapping[str, Any]) -> str:
    """Full content identity for one parsed host input."""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(
            "utf-8")
    ).hexdigest()


def input_fingerprint(data: Mapping[str, Any]) -> str:
    """Content identity used by both execution and feedback selection."""
    return canonical_input_sha256(data)[:16]


def finalized_cycle_ids(
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Cycles with a structurally bound version-1 completion manifest."""
    finalized = set()
    for record in records:
        payload = record.get("payload")
        if (
            record.get("record_type") != "cycle_finalization"
            or not isinstance(payload, Mapping)
            or payload.get("schema_version") != 1
        ):
            continue
        cycle_id = str(payload.get("cycle_id", "")).strip()
        receipt = payload.get("receipt")
        input_identity = payload.get("input")
        if (
            not cycle_id
            or record.get("record_id") != f"cycle-finalization:{cycle_id}"
            or f"cycle-receipt:{cycle_id}"
            not in (record.get("caused_by") or ())
            or not isinstance(receipt, Mapping)
            or receipt.get("record_id") != f"cycle-receipt:{cycle_id}"
            or not isinstance(input_identity, Mapping)
            or not str(input_identity.get("canonical_sha256", "")).strip()
        ):
            continue
        finalized.add(cycle_id)
    return finalized


def feedback_snapshot_ids(
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Snapshots whose receipt is valid and useful for future reasoning."""
    finalized = finalized_cycle_ids(records)
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
        cycle_id = str(payload.get("cycle_id", "")).strip()
        if (
            payload.get("finalization_schema_version") == 1
            and cycle_id not in finalized
        ):
            continue
        snapshot_id = str(payload.get("snapshot_id", "")).strip()
        if snapshot_id:
            accepted.add(snapshot_id)
    return accepted
