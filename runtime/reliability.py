"""Journal-derived operational reliability without inferred retry lineage."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import finalized_cycle_ids
from .audit_store import AuditJournal
from .cycle_receipt import validate_audit_receipt_record
from .integrity import order_chain
from .learning_dispositions import (
    LEARNING_DISPOSITION_SCHEMA_VERSIONS,
    LEARNING_STAGES,
    disposition_reconciliation_errors,
)

RECENT_WINDOW_LIMIT = 8
SCORECARD_BYTE_BUDGET = 12_000
REPLAY_COMPATIBILITY_INCIDENT_CUTOFF = datetime(
    2026, 9, 18, 21, 13, 32, tzinfo=timezone.utc,
)
_REPLAY_COMPATIBILITY_REASON = re.compile(
    r".*tool_provenance_payload_mismatch:tool-provenance:[^:]+$"
)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_cycle_candidate_refusal(record: Mapping[str, Any]) -> bool:
    payload = record.get("payload")
    if record.get("record_type") != "host_input_refusal" or not isinstance(
            payload, Mapping):
        return False
    name = str(payload.get("input", ""))
    return name.startswith("cycle-") and name.endswith(".json")


def _is_replay_compatibility_refusal(record: Mapping[str, Any]) -> bool:
    payload = record.get("payload")
    if record.get("record_type") != "host_input_refusal" or not isinstance(
            payload, Mapping):
        return False
    observed = _timestamp(payload.get("at")) or _timestamp(
        record.get("created_at")
    )
    return (
        observed is not None
        and observed <= REPLAY_COMPATIBILITY_INCIDENT_CUTOFF
        and _REPLAY_COMPATIBILITY_REASON.fullmatch(
            str(payload.get("reason", ""))
        ) is not None
    )


def _event_time(record: Mapping[str, Any]) -> datetime | None:
    payload = record.get("payload")
    if (
        record.get("record_type") == "host_input_refusal"
        and isinstance(payload, Mapping)
    ):
        return _timestamp(payload.get("at")) or _timestamp(
            record.get("created_at"))
    return _timestamp(record.get("created_at"))


def _refusal_window(
    refusals: Sequence[Mapping[str, Any]],
    receipt: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    pass_ids = {
        str((row.get("payload") or {}).get("pass_id", "")).strip()
        for row in refusals
        if isinstance(row.get("payload"), Mapping)
        and str((row.get("payload") or {}).get("pass_id", "")).strip()
    }
    legacy_count = sum(
        1 for row in refusals
        if not str((row.get("payload") or {}).get("pass_id", "")).strip()
    )
    started = _event_time(refusals[0])
    completed = _event_time(receipt)
    elapsed: float | None = None
    negative_elapsed = False
    if started is not None and completed is not None:
        seconds = (completed - started).total_seconds()
        if seconds >= 0:
            elapsed = round(seconds, 3)
        else:
            negative_elapsed = True
    payload = receipt.get("payload")
    cycle_id = (
        str(payload.get("cycle_id", ""))
        if isinstance(payload, Mapping)
        else ""
    )
    return {
        "accepted_cycle_id": cycle_id or None,
        "refusal_records": len(refusals),
        "known_refusal_passes": len(pass_ids),
        "legacy_refusals_without_pass_id": legacy_count,
        "journal_elapsed_seconds": elapsed,
    }, negative_elapsed


def operational_reliability(
    records: Sequence[Mapping[str, Any]],
    *,
    journal_path: str | Path,
) -> dict[str, Any]:
    """Summarize facts the current journal can prove without retry inference."""
    ordered, chain_failures = order_chain([dict(row) for row in records])
    all_receipts = [
        row for row in ordered if row.get("record_type") == "cycle_receipt"
    ]
    finalized = finalized_cycle_ids(ordered)
    incomplete_receipts = [
        row for row in all_receipts
        if isinstance(row.get("payload"), Mapping)
        and row["payload"].get("finalization_schema_version") == 1
        and str(row["payload"].get("cycle_id", "")).strip()
        not in finalized
    ]
    receipts = [
        row for row in all_receipts
        if row not in incomplete_receipts
    ]
    partial_receipts = [
        row for row in receipts
        if isinstance(row.get("payload"), Mapping)
        and row["payload"].get("evidence_completeness") == "partial"
    ]
    complete_receipts = [
        row for row in receipts
        if row not in partial_receipts
    ]
    receipt_record_ids = {
        str(row.get("record_id", ""))
        for row in receipts
    }
    all_refusals = [
        row for row in ordered
        if row.get("record_type") == "host_input_refusal"
    ]
    replay_refusals = [
        row for row in all_refusals
        if _is_replay_compatibility_refusal(row)
    ]
    refusals = [
        row for row in all_refusals
        if _is_cycle_candidate_refusal(row)
        and not _is_replay_compatibility_refusal(row)
    ]
    events = [
        row for row in ordered
        if str(row.get("record_id", "")) in receipt_record_ids
        or (
            _is_cycle_candidate_refusal(row)
            and not _is_replay_compatibility_refusal(row)
        )
    ]

    current_streak = 0
    max_streak = 0
    cognitive_streak = 0
    cognitive_max_streak = 0
    cognitive_qualifying = 0
    cognitive_nonqualifying = 0
    cognitive_not_scoreable = 0
    cognitive_refusal_resets = 0
    cognitive_recent: list[dict[str, Any]] = []
    pending_refusals: list[Mapping[str, Any]] = []
    windows: list[dict[str, Any]] = []
    negative_elapsed_count = 0
    for event in events:
        if event.get("record_type") == "host_input_refusal":
            current_streak = 0
            cognitive_streak = 0
            cognitive_refusal_resets += 1
            pending_refusals.append(event)
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        cycle_id = str(payload.get("cycle_id", ""))
        if payload.get("evidence_completeness") == "partial":
            current_streak = 0
            cognitive_streak = 0
            cognitive_nonqualifying += 1
            cognitive_recent.append({
                "cycle_id": cycle_id or None,
                "host_input_schema_version": payload.get(
                    "host_input_schema_version"
                ),
                "status": "research_only",
                "error_codes": list(
                    payload.get("evidence_advisories") or ()
                ),
            })
            if pending_refusals:
                window, negative = _refusal_window(
                    pending_refusals,
                    event,
                )
                windows.append(window)
                negative_elapsed_count += int(negative)
                pending_refusals = []
            continue
        current_streak += 1
        max_streak = max(max_streak, current_streak)
        version = payload.get("host_input_schema_version")
        if version not in LEARNING_DISPOSITION_SCHEMA_VERSIONS:
            cognitive_not_scoreable += 1
            cognitive_recent.append({
                "cycle_id": cycle_id or None,
                "host_input_schema_version": version,
                "status": "not_scoreable",
                "reason": (
                    "Only schema-v3/v4 receipts carry the versioned learning "
                    "disposition contract."
                ),
            })
        else:
            qualification_errors = disposition_reconciliation_errors(
                event, ordered)
            if payload.get("status") != "completed":
                qualification_errors.append("receipt_not_completed")
            stage_status = {
                str(stage.get("stage_id")): stage.get("status")
                for stage in payload.get("stages") or ()
                if isinstance(stage, Mapping)
            }
            qualification_errors.extend(
                f"learning_stage_not_completed:{stage_id}"
                for stage_id in LEARNING_STAGES
                if stage_status.get(stage_id) != "completed"
            )
            qualification_errors.extend(
                f"receipt_invalid:{error}"
                for error in validate_audit_receipt_record(event)
            )
            qualification_errors = sorted(set(qualification_errors))
            if qualification_errors:
                cognitive_streak = 0
                cognitive_nonqualifying += 1
                cognitive_recent.append({
                    "cycle_id": cycle_id or None,
                    "host_input_schema_version": version,
                    "status": "nonqualifying",
                    "error_codes": qualification_errors,
                })
            else:
                cognitive_streak += 1
                cognitive_max_streak = max(
                    cognitive_max_streak, cognitive_streak)
                cognitive_qualifying += 1
                cognitive_recent.append({
                    "cycle_id": cycle_id or None,
                    "host_input_schema_version": version,
                    "status": "qualifying",
                })
        if pending_refusals:
            window, negative = _refusal_window(pending_refusals, event)
            windows.append(window)
            negative_elapsed_count += int(negative)
            pending_refusals = []

    validation_failures = []
    for receipt in receipts:
        errors = validate_audit_receipt_record(receipt)
        if errors:
            validation_failures.append({
                "record_id": str(receipt.get("record_id", "")),
                "error_codes": sorted(set(errors)),
            })

    attempts = len(receipts) + len(refusals)
    scorecard = {
        "scope": {
            "kind": "since_journal_root",
            "journal": Path(journal_path).name,
            "root_record_hash": (
                str(ordered[0].get("record_hash", "")) if ordered else None
            ),
            "tip_record_hash": (
                str(ordered[-1].get("record_hash", "")) if ordered else None
            ),
            "record_count": len(ordered),
            "chain_failure_count": len(chain_failures),
        },
        "candidate_attempts": {
            "total": attempts,
            "accepted_receipts": len(complete_receipts),
            "research_only_receipts": len(partial_receipts),
            "cycle_candidate_refusals": len(refusals),
            "excluded_non_cycle_refusals": (
                len(all_refusals)
                - len(refusals)
                - sum(
                    _is_cycle_candidate_refusal(row)
                    for row in replay_refusals
                )
            ),
            "excluded_replay_compatibility_refusals": len(
                replay_refusals),
            "attempt_acceptance_rate": (
                    round(len(complete_receipts) / attempts, 4)
                    if attempts
                    else None
                ),
                "research_only_rate": (
                    round(len(partial_receipts) / attempts, 4)
                    if attempts
                    else None
                ),
        },
        "runtime_incidents": {
            "historical_replay_compatibility_refusals": len(
                replay_refusals),
            "receipts_pending_finalization": len(incomplete_receipts),
            "definition": (
                "Runtime incidents include immutable historical provenance "
                "replay refusals and new receipts whose required finalization "
                "manifest is absent. Neither is counted as a completed host "
                "candidate attempt."
            ),
        },
        "accepted_candidate_streak": {
            "current": current_streak,
            "maximum": max_streak,
            "definition": (
                "Consecutive complete receipt records in hash-chain order; "
                "a research-only partial receipt or cycle-candidate refusal "
                "resets the streak."
            ),
        },
        "cognitive_qualification_streak": {
            "current": cognitive_streak,
            "maximum": cognitive_max_streak,
            "qualifying_v3_receipts": cognitive_qualifying,
            "nonqualifying_v3_receipts": cognitive_nonqualifying,
            "not_scoreable_receipts": cognitive_not_scoreable,
            "refusal_resets": cognitive_refusal_resets,
            "recent": cognitive_recent[-RECENT_WINDOW_LIMIT:],
            "not_shown": max(
                0, len(cognitive_recent) - RECENT_WINDOW_LIMIT),
            "definition": (
                "Consecutive completed schema-v3 receipts whose three "
                "learning dispositions persist and reconcile. Refusals and "
                "nonqualifying v3 receipts reset the streak. V2 and "
                "unversioned receipts are not scoreable and do not change it."
            ),
        },
        "refusal_windows": {
            "completed_count": len(windows),
            "current_trailing_refusal_records": len(pending_refusals),
            "negative_elapsed_windows": negative_elapsed_count,
            "recent": windows[-RECENT_WINDOW_LIMIT:],
            "not_shown": max(0, len(windows) - RECENT_WINDOW_LIMIT),
            "elapsed_definition": (
                "Journal recording time from first refusal record to the "
                "next receipt. It is not host effort or true convergence "
                "time."
            ),
        },
        "receipt_validation": {
            "currently_valid": len(receipts) - len(validation_failures),
            "failing_current_validator": len(validation_failures),
            "recent_failures": validation_failures[-RECENT_WINDOW_LIMIT:],
            "not_shown": max(
                0, len(validation_failures) - RECENT_WINDOW_LIMIT),
        },
        "unavailable_metrics": {
            "first_pass_acceptance": (
                "Host inputs do not yet declare retry_of or supersedes "
                "lineage, so retries cannot be joined without guessing."
            ),
            "intervention_free_streak": (
                "The journal does not record human intervention boundaries."
            ),
            "true_time_to_convergence": (
                "Journal timestamps measure executor recording, not host "
                "repair effort."
            ),
        },
    }
    if len(json.dumps(scorecard, separators=(",", ":")).encode("utf-8")) > (
            SCORECARD_BYTE_BUDGET):
        raise ValueError("operational_reliability_scorecard_exceeds_byte_budget")
    return scorecard


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    path = Path(args.journal)
    print(json.dumps(
        operational_reliability(
            AuditJournal(path).read(), journal_path=path),
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
