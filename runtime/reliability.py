"""Journal-derived operational reliability without inferred retry lineage."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
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
from .schedule_ledger import reliability_gate_summary

RECENT_WINDOW_LIMIT = 8
SCORECARD_BYTE_BUDGET = 12_000
REPLAY_COMPATIBILITY_INCIDENT_CUTOFF = datetime(
    2026, 9, 18, 21, 13, 32, tzinfo=timezone.utc,
)
_REPLAY_COMPATIBILITY_REASON = re.compile(
    r".*tool_provenance_payload_mismatch:tool-provenance:[^:]+$"
)


def _scorecard_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(
        value,
        separators=(",", ":"),
    ).encode("utf-8"))


def _bound_scorecard(scorecard: dict[str, Any]) -> dict[str, Any]:
    gate = scorecard.get("gate_summary")
    gate = gate if isinstance(gate, dict) else {}
    resets = gate.get("recent_resets")
    if isinstance(resets, list):
        gate["recent_resets_not_shown"] = max(
            0,
            int(gate.get("reset_count", len(resets))) - len(resets),
        )
    audit_problems = gate.get("audit_problems")
    if isinstance(audit_problems, list):
        gate["audit_problem_count"] = len(audit_problems)
        gate["audit_problems_not_shown"] = 0

    detail_lists = (
        (gate, "recent_resets", "recent_resets_not_shown", -1),
        (
            scorecard.get("cognitive_qualification_streak"),
            "recent",
            "not_shown",
            0,
        ),
        (
            scorecard.get("receipt_validation"),
            "recent_failures",
            "not_shown",
            0,
        ),
        (
            scorecard.get("time_to_convergence_seconds"),
            "recent",
            "not_shown",
            0,
        ),
        (gate, "mature_slots", "mature_slots_not_shown", 0),
        (
            scorecard.get("refusal_windows"),
            "recent",
            "not_shown",
            0,
        ),
        (gate, "audit_problems", "audit_problems_not_shown", -1),
    )
    while _scorecard_size(scorecard) > SCORECARD_BYTE_BUDGET:
        changed = False
        for container, list_key, omitted_key, index in detail_lists:
            if not isinstance(container, dict):
                continue
            rows = container.get(list_key)
            if not isinstance(rows, list) or not rows:
                continue
            rows.pop(index)
            container[omitted_key] = int(container.get(omitted_key, 0)) + 1
            changed = True
            break
        if not changed:
            break
    scorecard["detail_projection"] = {
        "bounded": True,
        "byte_budget": SCORECARD_BYTE_BUDGET,
        "encoded_bytes": 0,
    }
    while True:
        encoded_bytes = _scorecard_size(scorecard)
        scorecard["detail_projection"]["encoded_bytes"] = encoded_bytes
        if _scorecard_size(scorecard) <= SCORECARD_BYTE_BUDGET:
            break
        changed = False
        for container, list_key, omitted_key, index in detail_lists:
            if not isinstance(container, dict):
                continue
            rows = container.get(list_key)
            if not isinstance(rows, list) or not rows:
                continue
            rows.pop(index)
            container[omitted_key] = int(container.get(omitted_key, 0)) + 1
            changed = True
            break
        if not changed:
            scorecard["detail_projection"]["bounded"] = False
            break
    scorecard["detail_projection"]["encoded_bytes"] = _scorecard_size(
        scorecard
    )
    return scorecard


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


def _lineage_value(
    payload: Mapping[str, Any],
) -> tuple[bool, str | None, bool]:
    if "corrects_candidate_id" not in payload:
        return False, None, False
    value = payload.get("corrects_candidate_id")
    if value is None:
        return True, None, True
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        return True, None, False
    return True, value, True


def _retry_lineage_metrics(
    refusals: Sequence[Mapping[str, Any]],
    complete_receipts: Sequence[Mapping[str, Any]],
    *,
    partial_receipt_count: int,
    pending_finalization_count: int,
) -> dict[str, Any]:
    refusal_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for record in refusals:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        candidate_id = str(payload.get("candidate_id", "")).strip()
        if not candidate_id:
            continue
        if candidate_id in refusal_by_id:
            duplicate_ids.add(candidate_id)
        refusal_by_id[candidate_id] = record

    chain_cache: dict[str, list[Mapping[str, Any]] | None] = {}

    def chain(candidate_id: str) -> list[Mapping[str, Any]] | None:
        if candidate_id in chain_cache:
            return chain_cache[candidate_id]
        cursor = candidate_id
        seen = set()
        rows = []
        while cursor:
            if cursor in seen or cursor in duplicate_ids:
                chain_cache[candidate_id] = None
                return None
            seen.add(cursor)
            record = refusal_by_id.get(cursor)
            if record is None:
                chain_cache[candidate_id] = None
                return None
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                chain_cache[candidate_id] = None
                return None
            declared, parent, valid = _lineage_value(payload)
            if not declared or not valid:
                chain_cache[candidate_id] = None
                return None
            rows.append(record)
            if parent is None:
                chain_cache[candidate_id] = rows
                return rows
            cursor = parent
        chain_cache[candidate_id] = None
        return None

    accepted_distribution: dict[str, int] = {}
    accepted_refs: set[str] = set()
    first_pass = 0
    unjoinable_receipts = 0
    convergence_rows = []
    retry_acceptances = 0
    for receipt in complete_receipts:
        payload = receipt.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        declared, parent, valid = _lineage_value(payload)
        if not declared or not valid:
            unjoinable_receipts += 1
            continue
        if parent is None:
            attempt = 1
            first_pass += 1
        else:
            lineage = chain(parent)
            if lineage is None:
                unjoinable_receipts += 1
                continue
            accepted_refs.add(parent)
            retry_acceptances += 1
            attempt = len(lineage) + 1
            root_payload = lineage[-1].get("payload")
            root_payload = (
                root_payload
                if isinstance(root_payload, Mapping)
                else {}
            )
            started = _timestamp(root_payload.get("at"))
            completed = _timestamp(payload.get("completed_at"))
            if (
                started is not None
                and completed is not None
                and completed >= started
            ):
                convergence_rows.append({
                    "cycle_id": (
                        str(payload.get("cycle_id", "")).strip()
                        or None
                    ),
                    "retry_attempt": attempt,
                    "seconds": round(
                        (completed - started).total_seconds(),
                        3,
                    ),
                })
        key = str(attempt)
        accepted_distribution[key] = (
            accepted_distribution.get(key, 0) + 1
        )

    valid_refusal_ids = {
        candidate_id
        for candidate_id in refusal_by_id
        if chain(candidate_id) is not None
    }
    root_ids = {
        str(
            (lineage[-1].get("payload") or {}).get(
                "candidate_id", ""
            )
        )
        for candidate_id in valid_refusal_ids
        if (lineage := chain(candidate_id))
    }
    parent_ids = set()
    for candidate_id in valid_refusal_ids:
        payload = refusal_by_id[candidate_id].get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        _declared, parent, _valid = _lineage_value(payload)
        if parent is not None:
            parent_ids.add(parent)
    abandoned_distribution: dict[str, int] = {}
    for candidate_id in sorted(
        valid_refusal_ids - parent_ids - accepted_refs
    ):
        lineage = chain(candidate_id)
        if lineage is None:
            continue
        key = str(len(lineage))
        abandoned_distribution[key] = (
            abandoned_distribution.get(key, 0) + 1
        )

    unjoinable_refusals = len(refusals) - len(valid_refusal_ids)
    denominator = first_pass + len(root_ids)
    times = [float(row["seconds"]) for row in convergence_rows]
    return {
        "first_pass_acceptance": {
            "numerator": first_pass,
            "denominator": denominator,
            "rate": (
                round(first_pass / denominator, 4)
                if denominator
                else None
            ),
            "legacy_or_unjoinable_count": (
                unjoinable_refusals + unjoinable_receipts
            ),
            "definition": (
                "Complete accepted first attempts divided by explicit "
                "lineage roots. Records without a declared, fully joinable "
                "corrects_candidate_id chain are excluded, never inferred."
            ),
        },
        "retry_attempt_distribution": {
            "accepted": dict(sorted(
                accepted_distribution.items(),
                key=lambda row: int(row[0]),
            )),
            "abandoned": dict(sorted(
                abandoned_distribution.items(),
                key=lambda row: int(row[0]),
            )),
            "legacy_or_unjoinable_refusals": unjoinable_refusals,
            "legacy_or_unjoinable_complete_receipts": (
                unjoinable_receipts
            ),
            "partial_receipts_excluded": partial_receipt_count,
            "receipts_pending_finalization_excluded": (
                pending_finalization_count
            ),
        },
        "time_to_convergence_seconds": {
            "measured_count": len(times),
            "unmeasured_retry_acceptances": (
                retry_acceptances - len(times)
            ),
            "minimum": round(min(times), 3) if times else None,
            "median": round(float(median(times)), 3) if times else None,
            "maximum": round(max(times), 3) if times else None,
            "recent": convergence_rows[-RECENT_WINDOW_LIMIT:],
            "not_shown": max(
                0, len(convergence_rows) - RECENT_WINDOW_LIMIT
            ),
            "definition": (
                "Elapsed time from the root refusal's refused-at timestamp "
                "to the accepted receipt's completed-at timestamp. Reported "
                "only for fully joined retry chains with both timestamps."
            ),
        },
    }


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
    retry_metrics = _retry_lineage_metrics(
        refusals,
        complete_receipts,
        partial_receipt_count=len(partial_receipts),
        pending_finalization_count=len(incomplete_receipts),
    )
    resolved_journal = Path(journal_path).resolve()
    journal_parent = resolved_journal.parent
    gate_summary = reliability_gate_summary(
        (
            journal_parent.parent
            if journal_parent.name == "audit"
            else journal_parent
        ),
        records=ordered,
    )
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
        **retry_metrics,
        "gate_summary": gate_summary,
        "unavailable_metrics": {
            "independent_autonomy_proof": (
                "Trigger and intervention remain host-supplied. Gate A is "
                "therefore host_claimed until an independent trigger source "
                "is available."
            ),
        },
    }
    return _bound_scorecard(scorecard)


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
