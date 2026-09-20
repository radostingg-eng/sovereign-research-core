"""Account for every expected host schedule slot without making decisions."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .audit_store import AuditJournal
from .engine import canonical_json
from .input_artifacts import load_input_data
from .timestamps import parse_iso_timestamp

SCHEDULE_SCHEMA_VERSION = 1
SCHEDULE_CONTEXT_SCHEMA_VERSION = 1
SCHEDULE_EVENT_SCHEMA_VERSION = 1
WATCHDOG_WORKFLOW_VERSION = 2
DEFAULT_MAX_SLOTS_PER_RUN = 48
TRIGGERS = frozenset({"scheduled", "manual", "recovery"})
INTERVENTIONS = frozenset({"none", "operator", "automation"})
SUCCESS_STATUSES = frozenset({"autonomous_success"})
CYCLE_ID_TIMESTAMP_PATTERN = re.compile(
    r"^cycle-(?P<timestamp>\d{8}T\d{6}Z)-.+$"
)
CYCLE_ID_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
CYCLE_TIMESTAMP_CLOCK_SKEW = timedelta(minutes=5)


def _parse(value: Any) -> datetime | None:
    parsed = parse_iso_timestamp(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def validate_schedule_contract(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["schedule_contract_not_object"]
    errors = []
    if value.get("schema_version") != SCHEDULE_SCHEMA_VERSION:
        errors.append("schedule_contract_schema_version")
    if not isinstance(value.get("enabled"), bool):
        errors.append("schedule_contract_enabled")
    for field in ("task_id", "task_name", "timezone"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            errors.append(f"schedule_contract_{field}")
    try:
        ZoneInfo(str(value.get("timezone", "")))
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("schedule_contract_timezone")
    for field in (
        "cadence_minutes",
        "grace_minutes",
        "source_max_age_minutes",
        "accounting_window_hours",
        "min_workflow_version",
    ):
        number = value.get(field)
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
        ):
            errors.append(f"schedule_contract_{field}")
    if _parse(value.get("anchor_at")) is None:
        errors.append("schedule_contract_anchor_at")
    core_commit = value.get("effective_core_commit")
    if (
        not isinstance(core_commit, str)
        or len(core_commit) != 40
        or any(character not in "0123456789abcdef" for character in core_commit)
    ):
        errors.append("schedule_contract_effective_core_commit")
    prompt_hash = value.get("effective_prompt_sha256")
    if (
        not isinstance(prompt_hash, str)
        or len(prompt_hash) != 64
        or any(character not in "0123456789abcdef" for character in prompt_hash)
    ):
        errors.append("schedule_contract_effective_prompt_sha256")
    schema = value.get("effective_host_input_schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        errors.append("schedule_contract_effective_schema_version")
    return sorted(set(errors))


def load_schedule_contract(profile_root: Path | str) -> dict[str, Any] | None:
    path = Path(profile_root) / "runs" / "SCHEDULE.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_schedule_contract(value)
    if errors:
        raise ValueError("schedule_contract_invalid:" + ",".join(errors))
    return dict(value)


def _slot_number(contract: Mapping[str, Any], slot: datetime) -> int | None:
    anchor = _parse(contract.get("anchor_at"))
    if anchor is None:
        return None
    cadence = int(contract["cadence_minutes"]) * 60
    delta = int((slot - anchor).total_seconds())
    if delta < 0 or delta % cadence:
        return None
    return delta // cadence


def _expected_slot_for_started_at(
    contract: Mapping[str, Any],
    started_at: datetime,
) -> datetime | None:
    anchor = _parse(contract.get("anchor_at"))
    if anchor is None:
        return None
    cadence = timedelta(minutes=int(contract["cadence_minutes"]))
    grace = timedelta(minutes=int(contract["grace_minutes"]))
    if started_at < anchor:
        return anchor if anchor - started_at <= grace else None
    slot_number = int(
        (started_at - anchor).total_seconds()
        // cadence.total_seconds()
    )
    preceding = anchor + slot_number * cadence
    following = preceding + cadence
    if following - started_at <= grace:
        return following
    return preceding


def _cycle_id_timestamp(value: Any) -> tuple[bool, datetime | None]:
    if not isinstance(value, str):
        return False, None
    match = CYCLE_ID_TIMESTAMP_PATTERN.fullmatch(value)
    if match is None:
        return False, None
    try:
        parsed = datetime.strptime(
            match.group("timestamp"),
            CYCLE_ID_TIMESTAMP_FORMAT,
        )
    except ValueError:
        return True, None
    return True, parsed.replace(tzinfo=timezone.utc)


def normalize_schedule_context(
    value: Any,
    *,
    contract: Mapping[str, Any] | None,
) -> Any:
    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    if (
        contract is not None
        and contract.get("enabled") is True
        and normalized.get("task_id") == contract.get("task_name")
    ):
        normalized["task_id"] = contract.get("task_id")
    return normalized


def validate_schedule_context(
    value: Any,
    *,
    contract: Mapping[str, Any] | None,
    candidate_as_of: Any = None,
    candidate_cycle_id: Any = None,
    candidate_committed_at: Any = None,
) -> list[str]:
    if contract is None or contract.get("enabled") is not True:
        return []
    anchor = _parse(contract.get("anchor_at"))
    observed = _parse(candidate_as_of)
    if value is None and (
        anchor is not None
        and observed is not None
        and observed < anchor
    ):
        return []
    if not isinstance(value, Mapping):
        return ["schedule_context_required"]
    value = normalize_schedule_context(value, contract=contract)
    errors = []
    if value.get("schema_version") != SCHEDULE_CONTEXT_SCHEMA_VERSION:
        errors.append("schedule_context_schema_version")
    if value.get("task_id") != contract.get("task_id"):
        errors.append("schedule_context_task_id")
    for field in ("expected_slot", "started_at", "source_observed_at"):
        if _parse(value.get(field)) is None:
            errors.append(f"schedule_context_{field}")
    expected = _parse(value.get("expected_slot"))
    started = _parse(value.get("started_at"))
    if expected is not None and _slot_number(contract, expected) is None:
        errors.append("schedule_context_expected_slot_alignment")
    elif expected is not None and started is not None:
        derived = _expected_slot_for_started_at(contract, started)
        if derived is None or expected != derived:
            errors.append("schedule_context_expected_slot_mismatch")
    cycle_id_matches, cycle_timestamp = _cycle_id_timestamp(
        candidate_cycle_id
    )
    if cycle_id_matches and cycle_timestamp is None:
        errors.append("schedule_context_cycle_timestamp")
    elif cycle_timestamp is not None:
        if (
            started is not None
            and cycle_timestamp + CYCLE_TIMESTAMP_CLOCK_SKEW < started
        ):
            errors.append("schedule_context_cycle_before_started_at")
        committed = _parse(candidate_committed_at)
        if (
            committed is not None
            and cycle_timestamp > committed + CYCLE_TIMESTAMP_CLOCK_SKEW
        ):
            errors.append("schedule_context_cycle_after_commit")
    if value.get("trigger") not in TRIGGERS:
        errors.append("schedule_context_trigger")
    if value.get("intervention") not in INTERVENTIONS:
        errors.append("schedule_context_intervention")
    run_id = value.get("platform_run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        errors.append("schedule_context_platform_run_id")
    return sorted(set(errors))


def expected_slots(
    contract: Mapping[str, Any],
    *,
    after: datetime | None,
    now: datetime,
    limit: int = DEFAULT_MAX_SLOTS_PER_RUN,
) -> tuple[list[datetime], bool]:
    anchor = _parse(contract["anchor_at"])
    assert anchor is not None
    cadence = timedelta(minutes=int(contract["cadence_minutes"]))
    grace = timedelta(minutes=int(contract["grace_minutes"]))
    latest = now.astimezone(timezone.utc) - grace
    cursor = anchor
    if after is not None and after >= anchor:
        cursor = after + cadence
    slots = []
    while cursor <= latest and len(slots) < limit:
        slots.append(cursor)
        cursor += cadence
    return slots, cursor <= latest


def _event_id(
    task_id: str,
    event_type: str,
    slot: str,
    detail: str = "",
) -> str:
    digest = hashlib.sha256(
        canonical_json([
            task_id,
            event_type,
            slot,
            detail,
        ]).encode("utf-8")
    ).hexdigest()[:20]
    return f"schedule:{event_type}:{digest}"


def _git_metadata(root: Path, path: Path) -> dict[str, Any] | None:
    result = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--format=%H%x00%ae%x00%cI%x00%s",
            "--",
            str(path.relative_to(root)),
        ],
        cwd=root,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    sha, email, committed_at, subject = result.stdout.strip().split("\0", 3)
    return {
        "commit_sha": sha,
        "committer_email": email,
        "committed_at": committed_at,
        "subject": subject,
    }


def _configuration_identity(root: Path) -> dict[str, Any]:
    core_lock = root / "core.lock"
    if core_lock.is_file():
        core_commit = core_lock.read_text(encoding="utf-8").strip()
    else:
        result = subprocess.run(
            [
                "git",
                "rev-list",
                "-1",
                "HEAD",
                "--",
                "runtime",
                "prompts",
                "schemas",
            ],
            cwd=root,
            text=True,
            capture_output=True,
        )
        core_commit = (
            result.stdout.strip()
            if result.returncode == 0
            else ""
        )
    module_root = Path(__file__).resolve().parents[1]
    prompt = module_root / "prompts" / "host-standing-schedule.md"
    prompt_hash = (
        hashlib.sha256(prompt.read_bytes()).hexdigest()
        if prompt.is_file()
        else ""
    )
    from .semantic_candidate import SEMANTIC_INPUT_SCHEMA_VERSION

    return {
        "effective_core_commit": core_commit,
        "effective_prompt_sha256": prompt_hash,
        "effective_host_input_schema_version": (
            SEMANTIC_INPUT_SCHEMA_VERSION
        ),
    }


def _configuration_problems(
    contract: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[str]:
    return [
        f"schedule_configuration_mismatch:{field}"
        for field in (
            "effective_core_commit",
            "effective_prompt_sha256",
            "effective_host_input_schema_version",
        )
        if contract.get(field) != actual.get(field)
    ]


def _candidate_rows(
    root: Path,
    *,
    metadata_reader: Callable[[Path, Path], Mapping[str, Any] | None],
    contract: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((root / "host_input").glob("*.json")):
        if path.name == "FEEDBACK.json" or path.name.startswith("."):
            continue
        try:
            data = load_input_data(path, profile_root=root)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        context = data.get("schedule_context")
        if not isinstance(context, Mapping):
            continue
        rows.append({
            "path": str(path.relative_to(root)),
            "cycle_id": str(data.get("cycle_id", "")),
            "context": dict(normalize_schedule_context(
                context,
                contract=contract,
            )),
            "metadata": dict(metadata_reader(root, path) or {}),
        })
    rejected_ledger = (
        root / "host_staging" / "rejected" / "REJECTIONS.jsonl"
    )
    if rejected_ledger.is_file():
        for line in rejected_ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            context = event.get("schedule_context")
            if isinstance(context, Mapping):
                rows.append({
                    "path": event.get("input"),
                    "cycle_id": event.get("cycle_id"),
                    "context": dict(normalize_schedule_context(
                        context,
                        contract=contract,
                    )),
                    "rejection": dict(event),
                    "metadata": {},
                })
    return rows


def _audit_by_cycle(records: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    by_cycle: dict[str, set[str]] = {}
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        cycle_id = str(payload.get("cycle_id", "")).strip()
        if not cycle_id:
            continue
        by_cycle.setdefault(cycle_id, set()).add(
            str(record.get("record_type", ""))
        )
    return by_cycle


def _slot_status(
    slot: datetime,
    candidates: Sequence[Mapping[str, Any]],
    records_by_cycle: Mapping[str, set[str]],
    contract: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    slot_text = slot.isoformat()
    slot_candidates = [
        row for row in candidates
        if _parse(row["context"].get("expected_slot")) == slot
    ]
    if not slot_candidates:
        return "missing", {}
    matching = [
        row for row in slot_candidates
        if row["context"].get("task_id") == contract.get("task_id")
    ]
    if not matching:
        rejected = [
            row for row in slot_candidates if row.get("rejection")
        ]
        row = rejected[-1] if rejected else slot_candidates[-1]
        return "refused" if rejected else "invalid_schedule_context", {
            "slot": slot_text,
            "cycle_id": str(row.get("cycle_id", "")) or None,
            "candidate_path": row.get("path"),
            "commit": row.get("metadata"),
            "context": dict(row["context"]),
        }
    row = next(
        (
            candidate for candidate in reversed(matching)
            if not candidate.get("rejection")
        ),
        matching[-1],
    )
    for candidate in reversed(matching):
        cycle_id = str(candidate.get("cycle_id", ""))
        types = records_by_cycle.get(cycle_id, set())
        if {"cycle_receipt", "cycle_finalization"} <= types:
            row = candidate
            break
    context = row["context"]
    cycle_id = str(row.get("cycle_id", ""))
    types = records_by_cycle.get(cycle_id, set())
    metadata = row.get("metadata")
    committed_at = (
        metadata.get("committed_at")
        if isinstance(metadata, Mapping)
        else None
    )
    schedule_errors = validate_schedule_context(
        context,
        contract=contract,
        candidate_cycle_id=cycle_id,
        candidate_committed_at=committed_at,
    )
    if row.get("rejection"):
        status = "refused"
    elif schedule_errors:
        status = "invalid_schedule_context"
    elif "cycle_receipt" not in types:
        status = "promoted_no_receipt"
    elif "cycle_finalization" not in types:
        status = "incomplete"
    else:
        started = _parse(context.get("started_at"))
        source = _parse(context.get("source_observed_at"))
        grace = timedelta(minutes=int(contract["grace_minutes"]))
        source_max = timedelta(
            minutes=int(contract["source_max_age_minutes"])
        )
        if source is None or slot - source > source_max:
            status = "stale_source"
        elif (
            context.get("trigger") != "scheduled"
            or context.get("intervention") != "none"
            or not context.get("platform_run_id")
        ):
            status = "manual_success"
        else:
            committed = _parse(committed_at)
            if committed is None:
                status = "claimed_scheduled_unverified"
            elif (
                started is not None
                and max(started, committed) > slot + grace
            ):
                status = "autonomous_late"
            else:
                status = "autonomous_success"
    detail = {
        "slot": slot_text,
        "cycle_id": cycle_id or None,
        "candidate_path": row.get("path"),
        "commit": row.get("metadata"),
        "context": dict(context),
    }
    if schedule_errors:
        detail["schedule_errors"] = schedule_errors
    return status, detail


def run_watchdog(
    profile_root: Path | str,
    *,
    now: datetime | None = None,
    metadata_reader: Callable[
        [Path, Path], Mapping[str, Any] | None
    ] = _git_metadata,
    max_slots: int = DEFAULT_MAX_SLOTS_PER_RUN,
    workflow_version: int | None = WATCHDOG_WORKFLOW_VERSION,
    configuration_reader: Callable[
        [Path], Mapping[str, Any]
    ] = _configuration_identity,
) -> dict[str, Any]:
    root = Path(profile_root).resolve()
    contract = load_schedule_contract(root)
    if contract is None or contract.get("enabled") is not True:
        return {"enabled": False, "healthy": True, "slots": []}
    if (
        workflow_version is None
        or workflow_version < int(contract["min_workflow_version"])
    ):
        raise ValueError(
            "schedule_watchdog_workflow_version_too_old:"
            f"{workflow_version}:"
            f"{contract['min_workflow_version']}"
        )
    configuration = dict(configuration_reader(root))
    configuration_problems = _configuration_problems(
        contract,
        configuration,
    )
    events_path = root / "runs" / "SCHEDULE_EVENTS.jsonl"
    journal = AuditJournal(events_path)
    validation = journal.validate()
    if not validation["valid"]:
        raise ValueError(f"schedule_event_chain_invalid:{validation}")
    prior = journal.read()
    last_evaluated = None
    for record in prior:
        payload = record.get("payload")
        if (
            record.get("record_type") == "watchdog_heartbeat"
            and isinstance(payload, Mapping)
        ):
            parsed = _parse(payload.get("evaluated_through"))
            if parsed is not None:
                last_evaluated = parsed
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    slots, backlog = expected_slots(
        contract,
        after=last_evaluated,
        now=current,
        limit=max_slots,
    )
    from .integrity import load_journal_records

    candidates = _candidate_rows(
        root,
        metadata_reader=metadata_reader,
        contract=contract,
    )
    records = load_journal_records(root / "audit")
    by_cycle = _audit_by_cycle(records)
    prior_ids = {
        str(record.get("record_id", ""))
        for record in prior
    }
    terminal_slots = {
        str(record["payload"].get("slot"))
        for record in prior
        if record.get("record_type") == "schedule_incident"
        and isinstance(record.get("payload"), Mapping)
        and record["payload"].get("state") in {
            "recovered",
            "acknowledged",
            "expired_unresolved",
        }
    }
    open_slots = {
        parsed
        for record in prior
        if record.get("record_type") == "schedule_incident"
        and isinstance(record.get("payload"), Mapping)
        and record["payload"].get("state") == "opened"
        and str(record["payload"].get("slot")) not in terminal_slots
        and (parsed := _parse(record["payload"].get("slot"))) is not None
    }
    slots = sorted(set(slots) | open_slots)
    current_incidents = []
    results = []
    acknowledged_slots = {
        str(record["payload"].get("slot"))
        for record in prior
        if record.get("record_type") == "schedule_incident"
        and isinstance(record.get("payload"), Mapping)
        and record["payload"].get("state") == "acknowledged"
    }
    for slot in slots:
        status, detail = _slot_status(
            slot,
            candidates,
            by_cycle,
            contract,
        )
        results.append({"slot": slot.isoformat(), "status": status, **detail})
        incident_id = _event_id(
            str(contract["task_id"]),
            "incident_opened",
            slot.isoformat(),
            status,
        )
        recovered_prefix = _event_id(
            str(contract["task_id"]),
            "incident_recovered",
            slot.isoformat(),
        )
        if status in SUCCESS_STATUSES:
            commit = detail.get("commit")
            candidate_path = detail.get("candidate_path")
            commit_sha = (
                str(commit.get("commit_sha", ""))
                if isinstance(commit, Mapping)
                else ""
            )
            if commit_sha and candidate_path:
                candidate_file = root / str(candidate_path)
                content_digest = (
                    hashlib.sha256(candidate_file.read_bytes()).hexdigest()
                    if candidate_file.is_file()
                    else None
                )
                publication_id = _event_id(
                    str(contract["task_id"]),
                    "publication_observed",
                    slot.isoformat(),
                    commit_sha,
                )
                if publication_id not in prior_ids:
                    journal.append_idempotent(
                        record_id=publication_id,
                        record_type="schedule_publication",
                        agent="schedule-watchdog",
                        payload={
                            "schema_version": SCHEDULE_EVENT_SCHEMA_VERSION,
                            "task_id": contract["task_id"],
                            "slot": slot.isoformat(),
                            "cycle_id": detail.get("cycle_id"),
                            "commit_sha": commit_sha,
                            "path": candidate_path,
                            "content_sha256": content_digest,
                            "observed_at": current.isoformat(),
                        },
                    )
            opened = [
                record for record in prior
                if record.get("record_type") == "schedule_incident"
                and isinstance(record.get("payload"), Mapping)
                and record["payload"].get("slot") == slot.isoformat()
                and record["payload"].get("state") == "opened"
            ]
            if opened and recovered_prefix not in prior_ids:
                journal.append_idempotent(
                    record_id=recovered_prefix,
                    record_type="schedule_incident",
                    agent="schedule-watchdog",
                    payload={
                        "schema_version": SCHEDULE_EVENT_SCHEMA_VERSION,
                        "task_id": contract["task_id"],
                        "slot": slot.isoformat(),
                        "state": "recovered",
                        "status": status,
                        "observed_at": current.isoformat(),
                    },
                )
        else:
            window = timedelta(
                hours=int(contract["accounting_window_hours"])
            )
            expired = current - slot > window
            if not expired and slot.isoformat() not in acknowledged_slots:
                current_incidents.append({
                    "slot": slot.isoformat(),
                    "status": status,
                })
            if incident_id not in prior_ids:
                journal.append_idempotent(
                    record_id=incident_id,
                    record_type="schedule_incident",
                    agent="schedule-watchdog",
                    payload={
                        "schema_version": SCHEDULE_EVENT_SCHEMA_VERSION,
                        "task_id": contract["task_id"],
                        "slot": slot.isoformat(),
                        "state": "opened",
                        "status": status,
                        "observed_at": current.isoformat(),
                        **detail,
                    },
                )
            expiry_id = _event_id(
                str(contract["task_id"]),
                "incident_expired",
                slot.isoformat(),
            )
            if expired and expiry_id not in prior_ids:
                journal.append_idempotent(
                    record_id=expiry_id,
                    record_type="schedule_incident",
                    agent="schedule-watchdog",
                    payload={
                        "schema_version": SCHEDULE_EVENT_SCHEMA_VERSION,
                        "task_id": contract["task_id"],
                        "slot": slot.isoformat(),
                        "state": "expired_unresolved",
                        "status": status,
                        "observed_at": current.isoformat(),
                    },
                )
    if configuration_problems:
        config_id = _event_id(
            str(contract["task_id"]),
            "configuration_incident",
            current.isoformat(),
            ",".join(configuration_problems),
        )
        journal.append_idempotent(
            record_id=config_id,
            record_type="schedule_configuration_incident",
            agent="schedule-watchdog",
            payload={
                "schema_version": SCHEDULE_EVENT_SCHEMA_VERSION,
                "task_id": contract["task_id"],
                "observed_at": current.isoformat(),
                "problems": configuration_problems,
                "expected": {
                    key: contract.get(key)
                    for key in configuration
                },
                "actual": configuration,
            },
        )
    evaluated_through = max(
        (
            value for value in (
                last_evaluated,
                slots[-1] if slots else None,
            )
            if value is not None
        ),
        default=None,
    )
    journal.append_idempotent(
        record_id=_event_id(
            str(contract["task_id"]),
            "watchdog_heartbeat",
            current.isoformat(),
        ),
        record_type="watchdog_heartbeat",
        agent="schedule-watchdog",
        payload={
            "schema_version": SCHEDULE_EVENT_SCHEMA_VERSION,
            "task_id": contract["task_id"],
            "observed_at": current.isoformat(),
            "evaluated_through": (
                evaluated_through.isoformat()
                if evaluated_through is not None
                else None
            ),
            "backlog_remaining": backlog,
            "configuration_problems": configuration_problems,
        },
    )
    return {
        "enabled": True,
        "healthy": (
            not current_incidents
            and not backlog
            and not configuration_problems
        ),
        "slots": results,
        "current_incidents": current_incidents,
        "backlog_remaining": backlog,
        "configuration_problems": configuration_problems,
        "evaluated_through": (
            evaluated_through.isoformat()
            if evaluated_through is not None
            else None
        ),
    }


def acknowledge_incident(
    profile_root: Path | str,
    *,
    slot: str,
    reason: str,
    actor: str,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    root = Path(profile_root)
    contract = load_schedule_contract(root)
    if contract is None or contract.get("enabled") is not True:
        raise ValueError("schedule_contract_not_enabled")
    parsed_slot = _parse(slot)
    if parsed_slot is None or _slot_number(contract, parsed_slot) is None:
        raise ValueError("schedule_ack_slot_invalid")
    if len(reason.strip()) < 20:
        raise ValueError("schedule_ack_reason_too_short")
    if not actor.strip():
        raise ValueError("schedule_ack_actor_required")
    current = (observed_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    journal = AuditJournal(root / "runs" / "SCHEDULE_EVENTS.jsonl")
    record, _ = journal.append_idempotent(
        record_id=_event_id(
            str(contract["task_id"]),
            "incident_acknowledged",
            parsed_slot.isoformat(),
            hashlib.sha256(reason.strip().encode("utf-8")).hexdigest(),
        ),
        record_type="schedule_incident",
        agent=actor.strip(),
        payload={
            "schema_version": SCHEDULE_EVENT_SCHEMA_VERSION,
            "task_id": contract["task_id"],
            "slot": parsed_slot.isoformat(),
            "state": "acknowledged",
            "reason": reason.strip(),
            "observed_at": current.isoformat(),
        },
    )
    return record


def check_watchdog_heartbeat(
    profile_root: Path | str,
    *,
    now: datetime | None = None,
    max_age_hours: float = 3.0,
) -> list[str]:
    root = Path(profile_root)
    contract = load_schedule_contract(root)
    if contract is None or contract.get("enabled") is not True:
        return []
    journal = AuditJournal(root / "runs" / "SCHEDULE_EVENTS.jsonl")
    newest = None
    for record in journal.read():
        if record.get("record_type") != "watchdog_heartbeat":
            continue
        payload = record.get("payload")
        stamp = (
            _parse(payload.get("observed_at"))
            if isinstance(payload, Mapping)
            else None
        )
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    if newest is None:
        return ["schedule_watchdog_heartbeat_missing"]
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - newest).total_seconds() / 3600
    if age > max_age_hours:
        return [f"schedule_watchdog_heartbeat_stale:{age:.1f}h"]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-root", default=".")
    parser.add_argument("--check-heartbeat-only", action="store_true")
    parser.add_argument("--workflow-version", type=int)
    parser.add_argument("--ack-slot")
    parser.add_argument("--ack-reason")
    parser.add_argument("--ack-actor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    if args.ack_slot:
        if not args.ack_reason or not args.ack_actor:
            parser.error("--ack-slot requires --ack-reason and --ack-actor")
        result = acknowledge_incident(
            args.profile_root,
            slot=args.ack_slot,
            reason=args.ack_reason,
            actor=args.ack_actor,
        )
        result = {"healthy": True, "record": result}
    elif args.check_heartbeat_only:
        problems = check_watchdog_heartbeat(args.profile_root)
        result = {"healthy": not problems, "problems": problems}
    else:
        result = run_watchdog(
            args.profile_root,
            workflow_version=args.workflow_version,
        )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result, sort_keys=True))
    return 0 if result.get("healthy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
