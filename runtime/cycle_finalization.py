"""Versioned proof that one receipted cycle finished its required writes."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import canonical_input_sha256
from .audit_store import AuditJournal
from .cycle_receipt import FINALIZATION_SCHEMA_VERSION
from .tool_artifacts import build_artifact_specs, profile_root_for_journal
from .tool_provenance import persisted_tool_provenance_errors


def finalization_record_id(cycle_id: str) -> str:
    return f"cycle-finalization:{cycle_id}"


def _record_map(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(record.get("record_id")): record
        for record in records
        if str(record.get("record_id", "")).strip()
    }


def _descendants(
    records: Sequence[Mapping[str, Any]],
    receipt_id: str,
) -> list[dict[str, str]]:
    reachable = {receipt_id}
    selected: list[Mapping[str, Any]] = []
    changed = True
    while changed:
        changed = False
        for record in records:
            record_id = str(record.get("record_id", ""))
            if (
                not record_id
                or record_id in reachable
                or record_id == finalization_record_id(
                    receipt_id.removeprefix("cycle-receipt:")
                )
            ):
                continue
            causes = {
                str(value)
                for value in record.get("caused_by") or ()
            }
            if causes.intersection(reachable):
                reachable.add(record_id)
                selected.append(record)
                changed = True
    return sorted(
        (
            {
                "record_id": str(record["record_id"]),
                "record_type": str(record.get("record_type", "")),
                "record_hash": str(record.get("record_hash", "")),
            }
            for record in selected
        ),
        key=lambda row: row["record_id"],
    )


def _required_records(
    records: Sequence[Mapping[str, Any]],
    required: Mapping[str, str],
    *,
    receipt_id: str,
) -> list[dict[str, str]]:
    by_id = _record_map(records)
    rows = []
    for record_id, record_type in sorted(required.items()):
        record = by_id.get(record_id)
        if record is None:
            raise ValueError(
                f"cycle_finalization_required_record_missing:{record_id}"
            )
        if record.get("record_type") != record_type:
            raise ValueError(
                f"cycle_finalization_required_record_type:{record_id}"
            )
        if receipt_id not in (record.get("caused_by") or ()):
            raise ValueError(
                f"cycle_finalization_required_record_cause:{record_id}"
            )
        record_hash = str(record.get("record_hash", ""))
        if not record_hash:
            raise ValueError(
                f"cycle_finalization_required_record_hash:{record_id}"
            )
        rows.append({
            "record_id": record_id,
            "record_type": record_type,
            "record_hash": record_hash,
        })
    return rows


def _artifact_rows(
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    profile_root: Path,
) -> list[dict[str, Any]]:
    specs = build_artifact_specs(data, records=records)
    rows = []
    for spec in specs.values():
        relative = spec["artifact_path"]
        content = spec["content"]
        target = (profile_root / relative).resolve()
        if profile_root != target and profile_root not in target.parents:
            raise ValueError("cycle_finalization_artifact_path_escape")
        if not target.is_file():
            raise ValueError(
                f"cycle_finalization_artifact_missing:{relative}"
            )
        if target.read_bytes() != content:
            raise ValueError(
                f"cycle_finalization_artifact_mismatch:{relative}"
            )
        rows.append({
            "artifact_ref": str(spec["artifact_ref"]),
            "result_sha256": str(spec["result_sha256"]),
            "artifact_byte_length": int(spec["artifact_byte_length"]),
        })
    return sorted(rows, key=lambda row: row["artifact_ref"])


def _verify_descendant_causes(
    records: Sequence[Mapping[str, Any]],
    receipt_id: str,
) -> None:
    by_id = _record_map(records)
    descendants = _descendants(records, receipt_id)
    descendant_ids = {row["record_id"] for row in descendants}
    for record_id in sorted(descendant_ids):
        record = by_id[record_id]
        for cause in record.get("caused_by") or ():
            if cause not in by_id:
                raise ValueError(
                    "cycle_finalization_dangling_cause:"
                    f"{record_id}:{cause}"
                )


def persist_cycle_finalization(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
    *,
    input_name: str,
    required_record_types: Mapping[str, str],
) -> bool:
    """Append or verify one completion manifest after every required effect."""
    records = journal.read()
    cycle_id = str(receipt.get("cycle_id", "")).strip()
    receipt_id = f"cycle-receipt:{cycle_id}"
    receipt_record = _record_map(records).get(receipt_id)
    if (
        not cycle_id
        or not isinstance(receipt_record, Mapping)
        or receipt_record.get("payload") != dict(receipt)
    ):
        raise ValueError(
            f"cycle_finalization_receipt_mismatch:{receipt_id}"
        )

    required = _required_records(
        records,
        required_record_types,
        receipt_id=receipt_id,
    )
    provenance_id = f"tool-provenance:{cycle_id}"
    if provenance_id in required_record_types:
        provenance = _record_map(records)[provenance_id]
        problems = persisted_tool_provenance_errors(
            data,
            provenance.get("payload"),
            cycle_id=cycle_id,
            recorded_at=provenance.get("created_at"),
            artifact_specs=build_artifact_specs(data, records=records),
        )
        if problems:
            raise ValueError(
                "cycle_finalization_provenance_invalid:"
                + "|".join(problems)
            )

    profile_root = profile_root_for_journal(journal.path)
    artifacts = (
        []
        if receipt.get("evidence_completeness") == "partial"
        else _artifact_rows(
            data,
            records,
            profile_root=profile_root,
        )
    )
    _verify_descendant_causes(records, receipt_id)
    input_sha256 = canonical_input_sha256(data)
    payload = {
        "schema_version": FINALIZATION_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "input": {
            "name": input_name,
            "canonical_sha256": input_sha256,
            "fingerprint": input_sha256[:16],
        },
        "receipt": {
            "record_id": receipt_id,
            "record_hash": str(receipt_record.get("record_hash", "")),
            "receipt_hash": str(receipt.get("receipt_hash", "")),
        },
        "required_records": required,
        "artifacts": artifacts,
        "observed_descendants": _descendants(records, receipt_id),
    }
    record_id = finalization_record_id(cycle_id)
    existing = _record_map(records).get(record_id)
    if existing is not None:
        existing_payload = existing.get("payload")
        if not isinstance(existing_payload, Mapping):
            raise ValueError(
                f"cycle_finalization_payload_invalid:{record_id}"
            )
        for field in (
            "schema_version",
            "cycle_id",
            "input",
            "receipt",
            "required_records",
            "artifacts",
        ):
            if existing_payload.get(field) != payload[field]:
                raise ValueError(
                    f"cycle_finalization_payload_mismatch:{record_id}:{field}"
                )
        current = {
            (
                row["record_id"],
                row["record_type"],
                row["record_hash"],
            )
            for row in payload["observed_descendants"]
        }
        stored = existing_payload.get("observed_descendants")
        if not isinstance(stored, list) or any(
            not isinstance(row, Mapping)
            or (
                row.get("record_id"),
                row.get("record_type"),
                row.get("record_hash"),
            ) not in current
            for row in stored
        ):
            raise ValueError(
                f"cycle_finalization_descendants_mismatch:{record_id}"
            )
        return False

    causes = [receipt_id, *required_record_types]
    journal.append_idempotent(
        record_id=record_id,
        record_type="cycle_finalization",
        agent="sovereign-host",
        caused_by=causes,
        payload=payload,
    )
    return True
