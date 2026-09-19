from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from runtime.audit_store import AuditJournal
from runtime.schedule_ledger import (
    acknowledge_incident,
    check_watchdog_heartbeat,
    expected_slots,
    run_watchdog,
    validate_schedule_context,
    validate_schedule_contract,
)


def _contract(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "enabled": True,
        "task_id": "task-hourly-1",
        "task_name": "Sovereign Research hourly cycle",
        "timezone": "Europe/Sofia",
        "cadence_minutes": 60,
        "anchor_at": "2026-09-19T10:00:00+00:00",
        "grace_minutes": 15,
        "source_max_age_minutes": 30,
        "accounting_window_hours": 48,
        "min_workflow_version": 2,
        "effective_core_commit": "a" * 40,
        "effective_host_input_schema_version": 1,
        "effective_prompt_sha256": "b" * 64,
    }
    value.update(overrides)
    return value


def _context(slot: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "task_id": "task-hourly-1",
        "platform_run_id": f"run-{slot}",
        "expected_slot": slot,
        "started_at": slot,
        "source_observed_at": slot,
        "trigger": "scheduled",
        "intervention": "none",
    }
    value.update(overrides)
    return value


def _write_contract(root: Path) -> None:
    path = root / "runs" / "SCHEDULE.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_contract()), encoding="utf-8")


def _write_candidate(root: Path, slot: str, cycle_id: str) -> Path:
    path = root / "host_input" / f"{cycle_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "cycle_id": cycle_id,
            "schedule_context": _context(slot),
        }),
        encoding="utf-8",
    )
    return path


def _finish_cycle(root: Path, cycle_id: str) -> None:
    journal = AuditJournal(root / "audit" / "journal.jsonl")
    journal.append(
        record_id=f"cycle-receipt:{cycle_id}",
        record_type="cycle_receipt",
        agent="test",
        payload={"cycle_id": cycle_id},
    )
    journal.append(
        record_id=f"cycle-finalization:{cycle_id}",
        record_type="cycle_finalization",
        agent="test",
        payload={"cycle_id": cycle_id},
    )


def _metadata(_: Path, path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = datetime.fromisoformat(
        data["schedule_context"]["expected_slot"].replace("Z", "+00:00")
    )
    return {
        "commit_sha": hashlib.sha1(path.read_bytes()).hexdigest(),
        "committer_email": "host@example.com",
        "committed_at": expected.replace(minute=5).isoformat(),
        "subject": "stage scheduled cycle",
    }


def _configuration(_: Path) -> dict[str, object]:
    return {
        "effective_core_commit": "a" * 40,
        "effective_prompt_sha256": "b" * 64,
        "effective_host_input_schema_version": 1,
    }


def test_contract_and_context_fail_closed_without_timing_refusal() -> None:
    assert validate_schedule_contract(_contract()) == []
    errors = validate_schedule_context(
        _context(
            "2026-09-19T10:00:00+00:00",
            started_at="2026-09-19T15:00:00+00:00",
        ),
        contract=_contract(),
    )
    assert errors == []

    errors = validate_schedule_context(
        _context(
            "2026-09-19T10:07:00+00:00",
            platform_run_id="",
        ),
        contract=_contract(),
    )
    assert errors == [
        "schedule_context_expected_slot_alignment",
        "schedule_context_platform_run_id",
    ]
    assert validate_schedule_context(
        None,
        contract=_contract(),
        candidate_as_of="2026-09-19T09:59:59+00:00",
    ) == []
    assert validate_schedule_context(
        None,
        contract=_contract(),
        candidate_as_of="2026-09-19T10:00:00+00:00",
    ) == ["schedule_context_required"]


def test_expected_slots_preserve_backlog_high_water() -> None:
    slots, backlog = expected_slots(
        _contract(),
        after=None,
        now=datetime(2026, 9, 19, 14, 20, tzinfo=timezone.utc),
        limit=2,
    )
    assert [slot.hour for slot in slots] == [10, 11]
    assert backlog is True

    slots, backlog = expected_slots(
        _contract(),
        after=slots[-1],
        now=datetime(2026, 9, 19, 14, 20, tzinfo=timezone.utc),
        limit=5,
    )
    assert [slot.hour for slot in slots] == [12, 13, 14]
    assert backlog is False


def test_watchdog_records_success_missing_and_recovery(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    _write_candidate(
        tmp_path,
        "2026-09-19T10:00:00+00:00",
        "cycle-10",
    )
    _finish_cycle(tmp_path, "cycle-10")

    first = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert first["healthy"] is True
    assert first["slots"][0]["status"] == "autonomous_success"

    missing = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 11, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert missing["healthy"] is False
    assert missing["current_incidents"] == [{
        "slot": "2026-09-19T11:00:00+00:00",
        "status": "missing",
    }]

    _write_candidate(
        tmp_path,
        "2026-09-19T11:00:00+00:00",
        "cycle-11",
    )
    _finish_cycle(tmp_path, "cycle-11")
    recovered = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert recovered["healthy"] is True
    assert recovered["slots"][0]["status"] == "autonomous_success"

    events = AuditJournal(
        tmp_path / "runs" / "SCHEDULE_EVENTS.jsonl"
    )
    assert events.validate()["valid"] is True
    states = [
        record["payload"].get("state")
        for record in events.read()
        if record["record_type"] == "schedule_incident"
    ]
    assert states == ["opened", "recovered"]


def test_watchdog_classifies_manual_and_stale_source(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    manual = _write_candidate(
        tmp_path,
        "2026-09-19T10:00:00+00:00",
        "manual-cycle",
    )
    value = json.loads(manual.read_text(encoding="utf-8"))
    value["schedule_context"]["intervention"] = "operator"
    manual.write_text(json.dumps(value), encoding="utf-8")
    _finish_cycle(tmp_path, "manual-cycle")
    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert result["slots"][0]["status"] == "manual_success"

    root = tmp_path / "stale"
    _write_contract(root)
    stale = _write_candidate(
        root,
        "2026-09-19T10:00:00+00:00",
        "stale-cycle",
    )
    value = json.loads(stale.read_text(encoding="utf-8"))
    value["schedule_context"]["source_observed_at"] = (
        "2026-09-19T09:00:00+00:00"
    )
    stale.write_text(json.dumps(value), encoding="utf-8")
    _finish_cycle(root, "stale-cycle")
    result = run_watchdog(
        root,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert result["healthy"] is False
    assert result["slots"][0]["status"] == "stale_source"


def test_scheduled_claim_without_git_metadata_is_not_autonomous(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    _write_candidate(
        tmp_path,
        "2026-09-19T10:00:00+00:00",
        "unverified-cycle",
    )
    _finish_cycle(tmp_path, "unverified-cycle")
    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=lambda _root, _path: None,
        configuration_reader=_configuration,
    )
    assert result["healthy"] is False
    assert result["slots"][0]["status"] == (
        "claimed_scheduled_unverified"
    )


def test_heartbeat_check_detects_missing_and_stale(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    assert check_watchdog_heartbeat(tmp_path) == [
        "schedule_watchdog_heartbeat_missing"
    ]
    run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert check_watchdog_heartbeat(
        tmp_path,
        now=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),
    ) == []
    assert check_watchdog_heartbeat(
        tmp_path,
        now=datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc),
    ) == ["schedule_watchdog_heartbeat_stale:3.7h"]


def test_invalid_event_chain_fails_closed(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    events = tmp_path / "runs" / "SCHEDULE_EVENTS.jsonl"
    events.write_text('{"record_id":"tampered"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="schedule_event_chain_invalid"):
        run_watchdog(
            tmp_path,
            now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
            configuration_reader=_configuration,
        )


def test_manual_and_late_cycles_remain_active_incidents(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    path = _write_candidate(
        tmp_path,
        "2026-09-19T10:00:00+00:00",
        "manual-cycle",
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schedule_context"]["trigger"] = "manual"
    path.write_text(json.dumps(value), encoding="utf-8")
    _finish_cycle(tmp_path, "manual-cycle")
    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert result["healthy"] is False
    assert result["current_incidents"][0]["status"] == "manual_success"

    acknowledge_incident(
        tmp_path,
        slot="2026-09-19T10:00:00+00:00",
        reason="Operator intentionally ran this recovery by hand.",
        actor="operator",
        observed_at=datetime(
            2026, 9, 19, 10, 25, tzinfo=timezone.utc,
        ),
    )
    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 30, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert result["healthy"] is True


def test_configuration_drift_is_an_incident(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc),
        configuration_reader=lambda _root: {
            "effective_core_commit": "c" * 40,
            "effective_prompt_sha256": "b" * 64,
            "effective_host_input_schema_version": 1,
        },
    )
    assert result["healthy"] is False
    assert result["configuration_problems"] == [
        "schedule_configuration_mismatch:effective_core_commit"
    ]


def test_workflow_version_gate_fails_closed(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    with pytest.raises(
        ValueError,
        match="schedule_watchdog_workflow_version_too_old:1:2",
    ):
        run_watchdog(
            tmp_path,
            now=datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc),
            workflow_version=1,
            configuration_reader=_configuration,
        )
