"""Synchronize staged semantic refusals into the append-only audit journal."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .audit_store import AuditJournal


def _refusal_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    if (
        event.get("record_type") == "host_input_refusal"
        and isinstance(payload, Mapping)
    ):
        return payload
    return event


def _lineage_invalid(event: Mapping[str, Any]) -> bool:
    payload = _refusal_payload(event)
    return any(
        str(code).startswith("retry_lineage_")
        for code in payload.get("codes", ())
    )


def retry_lineage_errors(
    value: Mapping[str, Any],
    *,
    refusals: Sequence[Mapping[str, Any]],
    candidate_id: str,
) -> list[str]:
    """Validate an explicit retry edge without inventing missing ancestry."""
    if "corrects_candidate_id" not in value:
        return []
    reference = value.get("corrects_candidate_id")
    if reference is None:
        return []
    if (
        not isinstance(reference, str)
        or not reference
        or reference != reference.strip()
    ):
        return ["retry_lineage_identifier_invalid"]
    if reference == candidate_id:
        return ["retry_lineage_self_reference"]

    by_id = {
        str(payload.get("candidate_id", "")): event
        for event in refusals
        if (
            payload := _refusal_payload(event)
        )
        and str(payload.get("candidate_id", "")).strip()
    }
    if reference not in by_id:
        return [f"retry_lineage_reference_missing:{reference}"]

    seen = {candidate_id}
    cursor = reference
    while cursor:
        if cursor in seen:
            return [f"retry_lineage_cycle:{cursor}"]
        seen.add(cursor)
        event = by_id.get(cursor)
        if event is None:
            return [f"retry_lineage_reference_missing:{cursor}"]
        if _lineage_invalid(event):
            break
        payload = _refusal_payload(event)
        if "corrects_candidate_id" not in payload:
            break
        parent = payload.get("corrects_candidate_id")
        if parent is None:
            break
        if (
            not isinstance(parent, str)
            or not parent
            or parent != parent.strip()
        ):
            return [f"retry_lineage_reference_invalid:{cursor}"]
        cursor = parent
    return []


def _reason(event: Mapping[str, Any]) -> str:
    codes = event.get("codes")
    codes = codes if isinstance(codes, list) else []
    return (
        "ValueError: invalid_host_input:"
        f"{event.get('input', '')}:"
        + ",".join(str(code) for code in codes)
    )


def sync_rejection_ledger(
    ledger_path: Path | str,
    journal: AuditJournal,
) -> int:
    path = Path(ledger_path)
    if not path.is_file():
        return 0
    existing = {
        str(record.get("record_id", ""))
        for record in journal.read()
    }
    written = 0
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError(
                f"rejection_ledger_invalid:{path.name}:{line_number}"
            )
        input_name = str(value.get("input", "")).strip()
        sha256 = str(value.get("sha256", "")).strip()
        if not input_name or len(sha256) < 12:
            raise ValueError(
                f"rejection_ledger_invalid:{path.name}:{line_number}"
            )
        fingerprint = sha256[:12]
        record_id = (
            f"host-input-refusal:{Path(input_name).stem}:"
            f"{fingerprint}"
        )
        if record_id in existing:
            continue
        payload = {
            "input": input_name,
            "input_sha256_12": fingerprint,
            "candidate_id": value.get("candidate_id"),
            "reason": _reason(value),
            "codes": list(value.get("codes") or ()),
            "at": value.get("refused_at"),
            "source": "staging_rejection_ledger",
        }
        if "corrects_candidate_id" in value:
            payload["corrects_candidate_id"] = value.get(
                "corrects_candidate_id"
            )
        journal.append(
            record_id=record_id,
            record_type="host_input_refusal",
            agent="sovereign-runtime",
            caused_by=(),
            payload=payload,
        )
        existing.add(record_id)
        written += 1
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile-root", default=".")
    parser.add_argument("--ledger")
    parser.add_argument("--journal")
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.profile_root).resolve()
    ledger = (
        Path(args.ledger)
        if args.ledger
        else root / "host_staging" / "rejected" / "REJECTIONS.jsonl"
    )
    if args.journal:
        journal_path = Path(args.journal)
    else:
        journals = sorted((root / "audit").glob("*.jsonl"))
        if not journals:
            raise ValueError("refusal_audit_journal_missing")
        journal_path = journals[-1]
    written = sync_rejection_ledger(
        ledger,
        AuditJournal(journal_path),
    )
    print(json.dumps({
        "ledger": str(ledger),
        "journal": str(journal_path),
        "records_added": written,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
