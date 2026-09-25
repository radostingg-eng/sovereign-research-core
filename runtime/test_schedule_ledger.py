from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime.audit_store import AuditJournal
from runtime.cycle_receipt import build_receipt
from runtime.schedule_ledger import (
    RECENT_RESET_RECORD_LIMIT,
    _configuration_identity,
    acknowledge_incident,
    check_watchdog_heartbeat,
    classify_gate_change,
    expected_slots,
    normalize_schedule_context,
    record_gate_window_reset,
    reliability_gate_summary,
    run_watchdog,
    validate_schedule_context,
    validate_schedule_contract,
)


def test_configuration_identity_ignores_runtime_tests(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
    )
    runtime = tmp_path / "runtime"
    prompts = tmp_path / "prompts"
    schemas = tmp_path / "schemas"
    runtime.mkdir()
    prompts.mkdir()
    schemas.mkdir()
    (runtime / "production.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "production"],
        cwd=tmp_path,
        check=True,
    )
    production_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        text=True,
    ).strip()
    (runtime / "test_production.py").write_text(
        "def test_value(): pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "tests only"],
        cwd=tmp_path,
        check=True,
    )

    identity = _configuration_identity(tmp_path)

    assert identity["effective_core_commit"] == production_commit


def _contract(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "enabled": True,
        "task_id": "task-hourly-1",
        "task_name": "Sovereign Research hourly cycle",
        "timezone": "Europe/Sofia",
        "cadence_minutes": 60,
        "anchor_at": "2026-09-19T10:00:00+00:00",
        "reliability_gate_activation_at":
            "2026-09-19T10:00:00+00:00",
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


def _write_contract(root: Path, **overrides: object) -> None:
    path = root / "runs" / "SCHEDULE.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_contract(**overrides)),
        encoding="utf-8",
    )


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


def _finish_gate_cycle(
    root: Path,
    cycle_id: str,
    *,
    partial: bool = False,
) -> None:
    journal = AuditJournal(root / "audit" / "journal.jsonl")
    receipt = build_receipt(
        cycle_id=cycle_id,
        run_id=f"run-{cycle_id}",
        started_at="2026-09-19T10:00:00Z",
        completed_at="2026-09-19T10:01:00Z",
        mode="e2e-smoke-manual",
        snapshot_id=f"{cycle_id}:snapshot",
        stages=[{
            "stage_id": "portfolio",
            "agent_id": "portfolio",
            "status": "completed",
            "execution_order": 1,
            "started_at": "2026-09-19T10:00:00Z",
            "completed_at": "2026-09-19T10:01:00Z",
            "tools_used": [],
        }],
        tools_used=[],
        status="completed",
        decision_status="wait",
        self_improvement={
            "status": "none",
            "mutation_ids": [],
            "gates": {},
        },
        host={"cognitive_execution_claim": "test cycle"},
        evidence_completeness="partial" if partial else "complete",
        evidence_advisories=(
            ["evidence_missing"] if partial else ()
        ),
    )
    journal.append_cycle_receipt(receipt)
    journal.append(
        record_id=f"cycle-finalization:{cycle_id}",
        record_type="cycle_finalization",
        agent="test",
        caused_by=(f"cycle-receipt:{cycle_id}",),
        payload={
            "schema_version": 1,
            "cycle_id": cycle_id,
            "input": {"canonical_sha256": "a" * 64},
            "receipt": {
                "record_id": f"cycle-receipt:{cycle_id}",
            },
        },
    )


def _gate_summary(
    root: Path,
    *,
    slots: int,
    complete: set[int],
    partial: set[int] = frozenset(),
    manual: set[int] = frozenset(),
    mature_slots: int | None = None,
    activation_index: int = 0,
) -> dict[str, object]:
    anchor = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    _write_contract(
        root,
        reliability_gate_activation_at=(
            anchor + activation_index * timedelta(hours=1)
        ).isoformat(),
    )
    for index in range(slots):
        slot = anchor + index * timedelta(hours=1)
        slot_text = slot.isoformat()
        cycle_id = f"cycle-gate-{index:02d}"
        path = _write_candidate(root, slot_text, cycle_id)
        if index in manual:
            value = json.loads(path.read_text(encoding="utf-8"))
            value["schedule_context"]["trigger"] = "manual"
            value["schedule_context"]["intervention"] = "operator"
            path.write_text(json.dumps(value), encoding="utf-8")
        if index in complete or index in partial:
            _finish_gate_cycle(
                root,
                cycle_id,
                partial=index in partial,
            )
    matured = slots if mature_slots is None else mature_slots
    now = anchor + (matured - 1) * timedelta(hours=1, minutes=0)
    now += timedelta(minutes=20)
    run_watchdog(
        root,
        now=now,
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    records = AuditJournal(root / "audit" / "journal.jsonl").read()
    return reliability_gate_summary(root, records=records)


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


def test_contract_and_context_fail_closed() -> None:
    assert validate_schedule_contract(_contract()) == []
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


def test_context_v2_reports_unavailable_platform_id_without_inventing_one() -> None:
    slot = "2026-09-19T10:00:00+00:00"
    unavailable = _context(
        slot,
        schema_version=2,
        platform_run_id=None,
        platform_run_id_status="unavailable",
    )
    assert validate_schedule_context(
        unavailable, contract=_contract(),
    ) == []
    assert validate_schedule_context(
        {
            **unavailable,
            "platform_run_id": "provider-run-42",
            "platform_run_id_status": "observed",
        },
        contract=_contract(),
    ) == []
    assert validate_schedule_context(
        {**unavailable, "platform_run_id": "made-up"},
        contract=_contract(),
    ) == ["schedule_context_platform_run_id"]
    assert validate_schedule_context(
        {**unavailable, "platform_run_id_status": "observed"},
        contract=_contract(),
    ) == ["schedule_context_platform_run_id"]
    assert validate_schedule_context(
        {**unavailable, "platform_run_id_status": "guessed"},
        contract=_contract(),
    ) == ["schedule_context_platform_run_id_status"]
    assert validate_schedule_context(
        {**unavailable, "platform_run_id_status": []},
        contract=_contract(),
    ) == ["schedule_context_platform_run_id_status"]
    assert validate_schedule_context(
        {**unavailable, "schema_version": 1},
        contract=_contract(),
    ) == ["schedule_context_platform_run_id", "schedule_context_platform_run_id_status"]
    assert validate_schedule_context(
        {**unavailable, "schema_version": None},
        contract=_contract(),
    ) == [
        "schedule_context_platform_run_id",
        "schedule_context_schema_version",
    ]


def test_unavailable_run_id_accounts_receipt_without_autonomy_or_alarm(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    slot = "2026-09-19T10:00:00+00:00"
    cycle_id = "cycle-unavailable-run-id"
    path = _write_candidate(tmp_path, slot, cycle_id)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schedule_context"] = _context(
        slot,
        schema_version=2,
        platform_run_id=None,
        platform_run_id_status="unavailable",
    )
    path.write_text(json.dumps(value), encoding="utf-8")
    _finish_gate_cycle(tmp_path, cycle_id)

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )
    assert result["healthy"] is True
    assert result["slots"][0]["status"] == "scheduled_unverified_run_id"
    events = AuditJournal(
        tmp_path / "runs" / "SCHEDULE_EVENTS.jsonl"
    ).read()
    publications = [
        row for row in events
        if row["record_type"] == "schedule_publication"
    ]
    assert len(publications) == 1
    assert publications[0]["payload"]["status"] == "scheduled_unverified_run_id"
    assert not [
        row for row in events
        if row["record_type"] == "schedule_incident"
    ]
    gate = reliability_gate_summary(
        tmp_path,
        records=AuditJournal(
            tmp_path / "audit" / "journal.jsonl"
        ).read(),
    )
    assert gate["gate_a"]["complete_count"] == 0
    assert gate["gate_b"]["complete_count"] == 1


def test_expected_slot_is_derived_from_started_at_and_grace() -> None:
    contract = _contract(anchor_at="2026-09-19T00:48:16Z")
    assert validate_schedule_context(
        _context(
            "2026-09-20T06:48:16Z",
            started_at="2026-09-20T07:47:47Z",
        ),
        contract=contract,
    ) == ["schedule_context_expected_slot_mismatch"]
    for started_at in (
        "2026-09-20T07:47:47Z",
        "2026-09-20T07:49:00Z",
    ):
        assert validate_schedule_context(
            _context(
                "2026-09-20T07:48:16Z",
                started_at=started_at,
            ),
            contract=contract,
        ) == []
    assert validate_schedule_context(
        _context(
            "2026-09-20T07:48:16Z",
            started_at="2026-09-20T08:20:00Z",
        ),
        contract=contract,
    ) == []


def test_structured_cycle_timestamps_allow_long_runs_and_opaque_ids() -> None:
    context = _context(
        "2026-09-19T10:00:00Z",
        started_at="2026-09-19T10:00:00Z",
    )
    assert validate_schedule_context(
        context,
        contract=_contract(),
        candidate_cycle_id="cycle-20260919T095600Z-skew",
    ) == []
    assert validate_schedule_context(
        context,
        contract=_contract(),
        candidate_cycle_id="cycle-20260919T120000Z-long-run",
    ) == []
    assert validate_schedule_context(
        context,
        contract=_contract(),
        candidate_cycle_id="cycle-20260919T095459Z-too-early",
    ) == ["schedule_context_cycle_before_started_at"]
    for opaque in (
        "legacy-cycle-r122",
        "cycle-20260919T100000Z",
        "cycle-not-a-timestamp-r122",
    ):
        assert validate_schedule_context(
            context,
            contract=_contract(),
            candidate_cycle_id=opaque,
        ) == []


def test_cycle_timestamp_after_commit_cannot_be_autonomous(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    cycle_id = "cycle-20260919T120500Z-local-clock"
    _write_candidate(
        tmp_path,
        "2026-09-19T10:00:00Z",
        cycle_id,
    )
    _finish_cycle(tmp_path, cycle_id)

    def committed_before_cycle(_root: Path, path: Path) -> dict[str, str]:
        return {
            "commit_sha": hashlib.sha1(path.read_bytes()).hexdigest(),
            "committer_email": "host@example.com",
            "committed_at": "2026-09-19T10:05:00Z",
            "subject": "stage scheduled cycle",
        }

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=committed_before_cycle,
        configuration_reader=_configuration,
    )
    assert result["slots"][0]["status"] == "invalid_schedule_context"
    assert result["slots"][0]["schedule_errors"] == [
        "schedule_context_cycle_after_commit"
    ]


def test_long_productive_run_remains_a_late_scheduled_run(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    cycle_id = "cycle-20260919T120000Z-long-run"
    path = _write_candidate(
        tmp_path,
        "2026-09-19T10:00:00Z",
        cycle_id,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schedule_context"]["started_at"] = "2026-09-19T10:20:00Z"
    path.write_text(json.dumps(value), encoding="utf-8")
    _finish_cycle(tmp_path, cycle_id)

    def committed_after_cycle(_root: Path, path: Path) -> dict[str, str]:
        return {
            "commit_sha": hashlib.sha1(path.read_bytes()).hexdigest(),
            "committer_email": "host@example.com",
            "committed_at": "2026-09-19T12:01:00Z",
            "subject": "stage scheduled cycle",
        }

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 12, 20, tzinfo=timezone.utc),
        metadata_reader=committed_after_cycle,
        configuration_reader=_configuration,
    )
    assert result["slots"][0]["status"] == "autonomous_late"
    assert "schedule_errors" not in result["slots"][0]


def test_task_name_alias_normalizes_to_canonical_task_id() -> None:
    context = _context(
        "2026-09-19T10:00:00+00:00",
        task_id="Sovereign Research hourly cycle",
    )

    assert validate_schedule_context(
        context,
        contract=_contract(),
    ) == []
    assert normalize_schedule_context(
        context,
        contract=_contract(),
    )["task_id"] == "task-hourly-1"
    assert context["task_id"] == "Sovereign Research hourly cycle"


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


def test_promoted_retry_outranks_same_slot_refusal(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    slot = "2026-09-19T10:00:00+00:00"
    _write_candidate(tmp_path, slot, "corrected-cycle")
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "input": "first-attempt.semantic.json",
            "cycle_id": "first-attempt",
            "schedule_context": _context(slot),
            "reason": "ValueError: malformed_json",
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    assert result["slots"][0]["status"] == "promoted_no_receipt"
    assert result["slots"][0]["cycle_id"] == "corrected-cycle"


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


def test_wrong_task_id_refusal_counts_as_attempt_not_missing(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "input": "wrong-task.json",
            "cycle_id": "wrong-task-cycle",
            "schedule_context": _context(
                "2026-09-19T10:00:00+00:00",
                task_id="Sovereign Research hourly cycle",
            ),
            "codes": ["schedule_context_task_id"],
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    assert result["healthy"] is False
    assert result["slots"][0]["status"] == "refused"
    assert result["slots"][0]["cycle_id"] == "wrong-task-cycle"
    assert result["slots"][0]["context"]["task_id"] == "task-hourly-1"


def test_rejected_context_near_anchor_counts_as_refused(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    context = _context("2026-09-19T10:01:15+00:00")
    context["started_at"] = "2026-09-19T10:01:15+00:00"
    context["source_observed_at"] = "2026-09-19T10:01:15+00:00"
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "input": "cycle-near-anchor.semantic.json",
            "cycle_id": "cycle-near-anchor",
            "schedule_context": context,
            "codes": ["malformed_json"],
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    slot = result["slots"][0]
    assert slot["status"] == "refused"
    assert slot["cycle_id"] == "cycle-near-anchor"
    assert slot["context"]["expected_slot"] == (
        "2026-09-19T10:01:15+00:00"
    )
    assert slot["schedule_errors"] == [
        "schedule_context_expected_slot_alignment",
    ]


def test_rejected_context_uses_validator_time_when_clock_is_wrong(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    context = _context("2026-09-19T12:57:00+00:00")
    context["started_at"] = "2026-09-19T12:57:00+00:00"
    context["source_observed_at"] = "2026-09-19T12:57:00+00:00"
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "input": "cycle-local-as-utc.semantic.json",
            "cycle_id": "cycle-local-as-utc",
            "schedule_context": context,
            "refused_at": "2026-09-19T10:02:00+00:00",
            "codes": ["semantic_tool_call_missing"],
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    slot = result["slots"][0]
    assert slot["status"] == "refused"
    assert slot["cycle_id"] == "cycle-local-as-utc"
    assert slot["context"]["started_at"] == (
        "2026-09-19T12:57:00+00:00"
    )


def test_rejection_recovers_schedule_context_from_archived_candidate(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    archive = "cycle-v48.semantic-hash.json"
    archived_value = {
        "cycle_id": "cycle-v48",
        "schedule_context": _context(
            "2026-09-19T10:00:00+00:00",
        ),
    }
    malformed_archive = (
        json.dumps(archived_value, indent=2)[:-2]
        + ',\n  BROKEN\n}'
    )
    (rejected / archive).write_text(
        malformed_archive,
        encoding="utf-8",
    )
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "archive": archive,
            "input": "cycle-v48.semantic.json",
            "cycle_id": "cycle-v48",
            "schedule_context": None,
            "codes": ["semantic_top_level_missing"],
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    slot = result["slots"][0]
    assert slot["status"] == "refused"
    assert slot["cycle_id"] == "cycle-v48"
    assert slot["context"]["trigger"] == "scheduled"
    assert slot["context"]["intervention"] == "none"


def test_malformed_rejection_infers_slot_without_claiming_success(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "input": "cycle-20260919T100130Z-v2r1.semantic.json",
            "cycle_id": "",
            "schedule_context": None,
            "codes": ["malformed_json"],
            "refused_at": "2026-09-19T10:02:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    slot = result["slots"][0]
    assert slot["status"] == "refused"
    assert slot["cycle_id"] == "cycle-20260919T100130Z-v2r1"
    assert slot["context"]["expected_slot"] == (
        "2026-09-19T10:00:00+00:00"
    )
    assert slot["context"]["context_origin"] == (
        "inferred_malformed_rejection_filename"
    )
    assert slot["context"]["trigger"] == "unknown"
    assert slot["context"]["intervention"] == "unknown"
    assert slot["schedule_errors"] == [
        "schedule_context_intervention",
        "schedule_context_source_observed_at",
        "schedule_context_trigger",
    ]


def test_malformed_rejection_outside_grace_remains_unmatched(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "input": "cycle-20260919T103000Z-v2r1.semantic.json",
            "cycle_id": "",
            "schedule_context": None,
            "codes": ["malformed_json"],
            "refused_at": "2026-09-19T10:31:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 45, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    assert result["slots"][0]["status"] == "missing"


def test_contextless_semantic_rejection_infers_refused_slot(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    rejected = tmp_path / "host_staging" / "rejected"
    rejected.mkdir(parents=True)
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps({
            "input": "cycle-20260919T100130Z-v48.semantic.json",
            "cycle_id": "cycle-20260919T100130Z-v48",
            "schedule_context": None,
            "codes": ["semantic_top_level_missing"],
            "refused_at": "2026-09-19T10:02:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )

    result = run_watchdog(
        tmp_path,
        now=datetime(2026, 9, 19, 10, 20, tzinfo=timezone.utc),
        metadata_reader=_metadata,
        configuration_reader=_configuration,
    )

    slot = result["slots"][0]
    assert slot["status"] == "refused"
    assert slot["cycle_id"] == "cycle-20260919T100130Z-v48"
    assert slot["context"]["context_origin"] == (
        "inferred_contextless_rejection_filename"
    )
    assert slot["context"]["trigger"] == "unknown"
    assert slot["context"]["intervention"] == "unknown"
    assert slot["schedule_errors"] == [
        "schedule_context_intervention",
        "schedule_context_source_observed_at",
        "schedule_context_trigger",
    ]


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
    with unittest.TestCase().assertRaisesRegex(
        ValueError,
        "schedule_event_chain_invalid",
    ):
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
    with unittest.TestCase().assertRaisesRegex(
        ValueError,
        "schedule_watchdog_workflow_version_too_old:1:2",
    ):
        run_watchdog(
            tmp_path,
            now=datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc),
            workflow_version=1,
            configuration_reader=_configuration,
        )


def test_gate_a_passes_at_seven_of_ten_claimed_scheduled_slots(
    tmp_path: Path,
) -> None:
    summary = _gate_summary(
        tmp_path,
        slots=10,
        complete=set(range(7)),
    )

    assert summary["provenance"] == "host_claimed"
    assert summary["gate_a"]["status"] == "passed"
    assert summary["gate_a"]["complete_count"] == 7
    assert len(summary["gate_a"]["window_slots"]) == 10


def test_slot_outcomes_count_full_window_when_detail_is_bounded(
    tmp_path: Path,
) -> None:
    summary = _gate_summary(
        tmp_path, slots=27, complete=set(range(18))
    )
    outcomes = summary["slot_outcomes"]

    assert outcomes["expected"] == 27
    assert outcomes["accounted"] == 27
    assert outcomes["opened"] == 27
    assert outcomes["promoted_complete"] == 18
    assert outcomes["refused"] == 0
    assert outcomes["missing"] == 0
    assert sum(outcomes["statuses"].values()) == 27
    assert summary["mature_slots_not_shown"] == 3


def test_slot_outcomes_distinguish_refused_missing_and_unaccounted(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    anchor = datetime(2026, 9, 19, 10, tzinfo=timezone.utc)
    events = AuditJournal(tmp_path / "runs" / "SCHEDULE_EVENTS.jsonl")
    for index, status, cycle_id in (
        (0, "refused", "cycle-refused"),
        (1, "missing", None),
    ):
        events.append(
            record_id=f"schedule-incident:{index}",
            record_type="schedule_incident",
            agent="schedule-watchdog",
            payload={
                "schema_version": 1,
                "task_id": "task-hourly-1",
                "slot": (anchor + timedelta(hours=index)).isoformat(),
                "state": "opened",
                "status": status,
                "cycle_id": cycle_id,
                "schedule_errors": [],
            },
        )
    events.append(
        record_id="watchdog-heartbeat:three-slots",
        record_type="watchdog_heartbeat",
        agent="schedule-watchdog",
        payload={
            "schema_version": 1,
            "task_id": "task-hourly-1",
            "evaluated_through": (anchor + timedelta(hours=2)).isoformat(),
        },
    )

    outcomes = reliability_gate_summary(
        tmp_path, records=[]
    )["slot_outcomes"]

    assert outcomes == {
        "scope": "since_activation",
        "expected": 3,
        "accounted": 2,
        "opened": 1,
        "promoted_complete": 0,
        "refused": 1,
        "missing": 1,
        "statuses": {
            "missing": 1, "refused": 1, "unaccounted": 1,
        },
    }


def test_gate_a_excludes_partial_and_intervened_receipts(
    tmp_path: Path,
) -> None:
    summary = _gate_summary(
        tmp_path,
        slots=10,
        complete=set(range(7)),
        partial={7},
        manual={0},
    )

    assert summary["gate_a"]["status"] == "blocked"
    assert summary["gate_a"]["complete_count"] == 6
    by_cycle = {
        row["cycle_id"]: row
        for row in summary["mature_slots"]
    }
    assert "intervention_present:operator" in (
        by_cycle["cycle-gate-00"]["reasons"]
    )
    assert "trigger_not_scheduled:manual" in (
        by_cycle["cycle-gate-00"]["reasons"]
    )
    assert "partial_receipt_excluded" in (
        by_cycle["cycle-gate-07"]["reasons"]
    )


def test_gate_a_excludes_slots_before_declared_activation(
    tmp_path: Path,
) -> None:
    summary = _gate_summary(
        tmp_path,
        slots=11,
        complete=set(range(7)),
        activation_index=1,
    )

    assert summary["gate_a"]["status"] == "blocked"
    assert summary["gate_a"]["complete_count"] == 6
    assert summary["gate_a"]["window_slots"][0] == (
        "2026-09-19T11:00:00+00:00"
    )


def test_gate_b_passes_at_eighteen_of_twenty_four_accounted_slots(
    tmp_path: Path,
) -> None:
    summary = _gate_summary(
        tmp_path,
        slots=24,
        complete=set(range(18)),
    )

    assert summary["gate_b"]["status"] == "passed"
    assert summary["gate_b"]["accounted_count"] == 24
    assert summary["gate_b"]["complete_count"] == 18


def test_gate_b_blocks_below_threshold(tmp_path: Path) -> None:
    summary = _gate_summary(
        tmp_path,
        slots=24,
        complete=set(range(17)),
    )

    assert summary["gate_b"]["status"] == "blocked"
    assert summary["gate_b"]["complete_count"] == 17
    assert summary["gate_b"]["reasons"] == [
        "complete_receipt_threshold_not_met:17/18"
    ]


def test_gate_b_blocks_when_one_of_twenty_four_slots_is_unaccounted(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path)
    anchor = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    schedule_events = AuditJournal(
        tmp_path / "runs" / "SCHEDULE_EVENTS.jsonl"
    )
    for index in range(23):
        slot = (anchor + index * timedelta(hours=1)).isoformat()
        schedule_events.append(
            record_id=f"schedule-publication:{index}",
            record_type="schedule_publication",
            agent="test",
            payload={
                "schema_version": 1,
                "task_id": "task-hourly-1",
                "slot": slot,
                "cycle_id": f"cycle-gate-{index:02d}",
            },
        )
        if index < 18:
            _finish_gate_cycle(
                tmp_path,
                f"cycle-gate-{index:02d}",
            )
    schedule_events.append(
        record_id="watchdog-heartbeat:test",
        record_type="watchdog_heartbeat",
        agent="test",
        payload={
            "schema_version": 1,
            "task_id": "task-hourly-1",
            "observed_at": (
                anchor + timedelta(hours=23, minutes=20)
            ).isoformat(),
            "evaluated_through": (
                anchor + timedelta(hours=23)
            ).isoformat(),
            "backlog_remaining": False,
            "configuration_problems": [],
        },
    )
    summary = reliability_gate_summary(
        tmp_path,
        records=AuditJournal(
            tmp_path / "audit" / "journal.jsonl"
        ).read(),
    )

    assert summary["gate_b"]["status"] == "blocked"
    assert summary["gate_b"]["complete_count"] == 18
    assert summary["gate_b"]["accounted_count"] == 23
    assert summary["gate_b"]["reasons"][0].startswith(
        "unaccounted_slots:"
    )


def test_immature_slots_are_excluded_from_gate_windows(
    tmp_path: Path,
) -> None:
    summary = _gate_summary(
        tmp_path,
        slots=10,
        complete=set(range(10)),
        mature_slots=9,
    )

    assert summary["gate_a"]["status"] == "pending"
    assert summary["gate_a"]["mature_slot_count"] == 9
    assert "2026-09-19T19:00:00+00:00" not in (
        summary["gate_a"]["window_slots"]
    )


# Gate-window reset audit tests


def _prior_window_summary(
    *,
    evaluated_through: str = "2026-09-19T11:00:00+00:00",
    slot_count: int = 2,
    complete_count: int = 1,
) -> dict[str, object]:
    return {
        "evaluated_through": evaluated_through,
        "gate_a": {
            "slot_count": slot_count,
            "complete_count": complete_count,
        },
        "gate_b": {
            "slot_count": slot_count,
            "complete_count": complete_count,
        },
    }


def _append_gate_audit_evidence(
    root: Path,
    *,
    slot: str,
    evaluated_through: str,
) -> AuditJournal:
    journal = AuditJournal(root / "runs" / "SCHEDULE_EVENTS.jsonl")
    journal.append(
        record_id=f"schedule-incident:{slot}",
        record_type="schedule_incident",
        agent="schedule-watchdog",
        payload={
            "schema_version": 1,
            "task_id": "task-hourly-1",
            "slot": slot,
            "state": "opened",
            "status": "missing",
        },
    )
    journal.append(
        record_id=f"watchdog-heartbeat:{evaluated_through}",
        record_type="watchdog_heartbeat",
        agent="schedule-watchdog",
        payload={
            "schema_version": 1,
            "task_id": "task-hourly-1",
            "observed_at": evaluated_through,
            "evaluated_through": evaluated_through,
            "backlog_remaining": False,
            "configuration_problems": [],
        },
    )
    return journal


def test_gate_change_taxonomy_separates_reset_from_acceptance() -> None:
    old = datetime(2026, 9, 20, 13, 57, tzinfo=timezone.utc)
    new = datetime(2026, 9, 20, 20, 57, tzinfo=timezone.utc)

    assert classify_gate_change(
        "behavior_changing_deployment",
        old,
        new,
    ) == "behavior_changing_deployment"
    assert classify_gate_change(
        "producer_restoration",
        old,
        new,
    ) == "producer_restoration"
    assert classify_gate_change(
        "acceptance_only",
        old,
        old,
    ) == "acceptance_only"
    with unittest.TestCase().assertRaisesRegex(
        ValueError,
        "producer_restoration_must_advance_activation",
    ):
        classify_gate_change("producer_restoration", old, old)
    with unittest.TestCase().assertRaisesRegex(
        ValueError,
        "acceptance_only_must_preserve_activation",
    ):
        classify_gate_change("acceptance_only", old, new)


def test_reliability_gate_summary_accepts_valid_reset(
    tmp_path: Path,
) -> None:
    old_activation = "2026-09-19T10:00:00+00:00"
    evaluated_through = "2026-09-19T13:00:00+00:00"
    new_activation = "2026-09-19T14:00:00+00:00"
    prior = _gate_summary(
        tmp_path,
        slots=4,
        complete={1, 3},
        manual={1},
    )
    assert prior["activation_at"] == old_activation
    assert prior["evaluated_through"] == evaluated_through
    assert prior["gate_a"]["complete_count"] == 1
    assert prior["gate_b"]["complete_count"] == 2
    _write_contract(
        tmp_path,
        reliability_gate_activation_at=new_activation,
    )
    records = AuditJournal(tmp_path / "audit" / "journal.jsonl").read()
    record = record_gate_window_reset(
        tmp_path,
        change_type="behavior_changing_deployment",
        old_activation_at=old_activation,
        new_activation_at=new_activation,
        reason="Runtime behavior changed before the next scheduled slot",
        triggering_reference="commit:abc123",
        prior_evaluated_through_at=evaluated_through,
        records=records,
        actor="sovereign-executor",
    )

    summary = reliability_gate_summary(tmp_path, records=records)

    assert record["record_type"] == "gate_window_reset"
    assert record["payload"]["prior_window_summary"] == {
        "evaluated_through": evaluated_through,
        "gate_a": {"slot_count": 4, "complete_count": 1},
        "gate_b": {"slot_count": 4, "complete_count": 2},
    }
    assert summary["reset_count"] == 1
    assert summary["audit_problems"] == []
    assert summary["recent_resets"] == [{
        "record_id": record["record_id"],
        "created_at": record["created_at"],
        "old_activation_at": old_activation,
        "new_activation_at": new_activation,
        "change_type": "behavior_changing_deployment",
        "reason": "Runtime behavior changed before the next scheduled slot",
        "triggering_reference": "commit:abc123",
        "actor": "sovereign-executor",
        "prior_window_summary": {
            "evaluated_through": evaluated_through,
            "gate_a": {"slot_count": 4, "complete_count": 1},
            "gate_b": {"slot_count": 4, "complete_count": 2},
        },
    }]


def test_reliability_gate_summary_accepts_producer_restoration_reset(
    tmp_path: Path,
) -> None:
    old_activation = "2026-09-19T10:00:00+00:00"
    evaluated_through = "2026-09-20T09:00:00+00:00"
    new_activation = "2026-09-20T10:00:00+00:00"
    prior = _gate_summary(
        tmp_path,
        slots=24,
        complete=set(),
    )
    assert prior["gate_a"]["complete_count"] == 0
    assert prior["gate_b"]["complete_count"] == 0
    _write_contract(
        tmp_path,
        reliability_gate_activation_at=new_activation,
    )
    records = AuditJournal(tmp_path / "audit" / "journal.jsonl").read()

    record = record_gate_window_reset(
        tmp_path,
        change_type="producer_restoration",
        old_activation_at=old_activation,
        new_activation_at=new_activation,
        reason="Existing connector-enabled phone producer resumed",
        triggering_reference="commit:producer-restoration-proof",
        prior_evaluated_through_at=evaluated_through,
        records=records,
        actor="sovereign-executor",
    )
    summary = reliability_gate_summary(tmp_path, records=records)

    assert record["payload"]["change_type"] == "producer_restoration"
    assert record["payload"]["prior_window_summary"] == {
        "evaluated_through": evaluated_through,
        "gate_a": {"slot_count": 10, "complete_count": 0},
        "gate_b": {"slot_count": 24, "complete_count": 0},
    }
    assert summary["audit_problems"] == []
    assert summary["recent_resets"][0]["change_type"] == (
        "producer_restoration"
    )


def test_reliability_gate_summary_reports_missing_reset(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        reliability_gate_activation_at="2026-09-19T12:00:00+00:00",
    )
    _append_gate_audit_evidence(
        tmp_path,
        slot="2026-09-19T10:00:00+00:00",
        evaluated_through="2026-09-19T12:00:00+00:00",
    )

    summary = reliability_gate_summary(tmp_path, records=[])

    assert summary["reset_count"] == 0
    assert summary["recent_resets"] == []
    assert summary["audit_problems"] == [
        "gate_activation_changed_without_matching_reset"
    ]


def test_reliability_gate_summary_reports_malformed_reset(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        reliability_gate_activation_at="2026-09-19T12:00:00+00:00",
    )
    journal = _append_gate_audit_evidence(
        tmp_path,
        slot="2026-09-19T10:00:00+00:00",
        evaluated_through="2026-09-19T12:00:00+00:00",
    )
    journal.append(
        record_id="schedule:gate_window_reset:malformed",
        record_type="gate_window_reset",
        agent="sovereign-executor",
        payload={
            "schema_version": 1,
            "task_id": "task-hourly-1",
            "change_type": "behavior_changing_deployment",
            "old_activation_at": "2026-09-19T10:00:00+00:00",
            "new_activation_at": "2026-09-19T12:00:00+00:00",
            "reason": "",
            "triggering_reference": "commit:abc123",
            "prior_window_summary": _prior_window_summary(),
            "actor": "sovereign-executor",
        },
    )

    summary = reliability_gate_summary(tmp_path, records=[])

    assert summary["reset_count"] == 1
    assert summary["recent_resets"] == []
    assert (
        "gate_window_reset_invalid:"
        "schedule:gate_window_reset:malformed:reason"
    ) in summary["audit_problems"]
    assert (
        "gate_window_reset_invalid:"
        "schedule:gate_window_reset:malformed:record_id"
    ) in summary["audit_problems"]
    assert (
        "gate_activation_changed_without_matching_reset"
        in summary["audit_problems"]
    )


def test_reliability_gate_summary_unchanged_activation_needs_no_reset(
    tmp_path: Path,
) -> None:
    summary = _gate_summary(
        tmp_path,
        slots=10,
        complete=set(range(7)),
    )

    assert summary["reset_count"] == 0
    assert summary["recent_resets"] == []
    assert summary["audit_problems"] == []


def test_record_gate_window_reset_rejects_evaluation_at_new_activation(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        reliability_gate_activation_at="2026-09-19T12:00:00+00:00",
    )

    with unittest.TestCase().assertRaisesRegex(
        ValueError,
        "prior_evaluated_through_outside_window",
    ):
        record_gate_window_reset(
            tmp_path,
            change_type="behavior_changing_deployment",
            old_activation_at="2026-09-19T10:00:00+00:00",
            new_activation_at="2026-09-19T12:00:00+00:00",
            reason="Runtime behavior changed",
            triggering_reference="commit:abc123",
            prior_evaluated_through_at=(
                "2026-09-19T12:00:00+00:00"
            ),
            records=[],
            actor="sovereign-executor",
        )


def test_recent_resets_are_bounded_without_capping_reset_count(
    tmp_path: Path,
) -> None:
    anchor = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    total_resets = RECENT_RESET_RECORD_LIMIT + 2
    for index in range(total_resets):
        old_activation = anchor + timedelta(hours=index)
        new_activation = old_activation + timedelta(hours=1)
        _write_contract(
            tmp_path,
            reliability_gate_activation_at=new_activation.isoformat(),
        )
        record_gate_window_reset(
            tmp_path,
            change_type="behavior_changing_deployment",
            old_activation_at=old_activation.isoformat(),
            new_activation_at=new_activation.isoformat(),
            reason=f"Behavior-changing deployment {index}",
            triggering_reference=f"commit:{index:040x}",
            prior_evaluated_through_at=old_activation.isoformat(),
            records=[],
            actor="sovereign-executor",
        )
    final_activation = anchor + timedelta(hours=total_resets)
    journal = AuditJournal(tmp_path / "runs" / "SCHEDULE_EVENTS.jsonl")
    journal.append(
        record_id="watchdog-heartbeat:after-resets",
        record_type="watchdog_heartbeat",
        agent="schedule-watchdog",
        payload={
            "schema_version": 1,
            "task_id": "task-hourly-1",
            "observed_at": final_activation.isoformat(),
            "evaluated_through": final_activation.isoformat(),
            "backlog_remaining": False,
            "configuration_problems": [],
        },
    )

    summary = reliability_gate_summary(tmp_path, records=[])

    assert summary["reset_count"] == total_resets
    assert len(summary["recent_resets"]) == RECENT_RESET_RECORD_LIMIT
    assert summary["audit_problems"] == []
