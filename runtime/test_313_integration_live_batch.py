"""Integration test for #313: live batch auth_error + completed scenario.

Tests that incident replay correctly handles all historical worker records
without needing full run_host_cycle integration (which requires complete input).
Uses unit tests on replay functions with realistic journal/FEEDBACK structures.
"""
import json
import tempfile
import unittest
from pathlib import Path

from .worker_health_incidents import (
    replay_worker_incidents_from_records,
    apply_stale_detection_latest_only,
    get_active_incidents,
    INCIDENT_SCHEMA_VERSION,
)
from .research_inbox import research_inbox_all_records_by_worker
from .audit_store import AuditJournal


def sample_worker_record(**overrides):
    """Create a sample worker record matching live research_inbox structure."""
    value = {
        "schema_version": 1,
        "record_id": "azure-a-20260919t100000z",
        "worker_id": "azure-a",
        "status": "completed",
        "origin": "worker_attested",
        "observed_at": "2026-09-19T10:00:00Z",
        "expires_at": "2026-09-20T01:00:00Z",
        "deployment": {"deployment": "gpt-test"},
        "selection": {"rule": "stable"},
        "target": {"question": "What changes the decision?"},
        "request": {"sha256": "a" * 64},
        "result": {"summary": "Bounded result."},
    }
    value.update(overrides)
    return value


class WorkerIncidentsLiveBatchTests(unittest.TestCase):
    """Integration tests for #313 worker incident replay."""

    def test_live_batch_scenario_auth_error_then_completed(self):
        """Live evidence: auth_error + completed in same inbox before cycle.

        Matches real commits like 33477e65 where both records are present
        in research_inbox before one host_cycle runs. Asserts:
        - Replay generates both open and resolve incidents
        - No active incidents after both transitions
        - Incidents use safe_error_code extraction
        - Journal round-trip preserves all incidents
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inbox_dir = root / "research_inbox" / "azure-a"
            inbox_dir.mkdir(parents=True)

            # Write both records (auth_error first, then completed)
            auth_error_rec = sample_worker_record(
                record_id="azure-a-20260919t100000z",
                worker_id="azure-a",
                status="auth_error",
                observed_at="2026-09-19T10:00:00Z",
                expires_at="2026-09-19T13:00:00Z",
                error={"code": "auth_error", "message": "Auth failed"},
            )
            (inbox_dir / "azure-a-20260919t100000z.json").write_text(
                json.dumps(auth_error_rec), encoding="utf-8"
            )

            completed_rec = sample_worker_record(
                record_id="azure-a-20260919t110000z",
                worker_id="azure-a",
                status="completed",
                observed_at="2026-09-19T11:00:00Z",
                expires_at="2026-09-20T02:00:00Z",
            )
            (inbox_dir / "azure-a-20260919t110000z.json").write_text(
                json.dumps(completed_rec), encoding="utf-8"
            )

            # Load records and replay
            worker_records = research_inbox_all_records_by_worker(root)
            self.assertIn("azure-a", worker_records)
            self.assertEqual(len(worker_records["azure-a"]), 2)

            events = replay_worker_incidents_from_records(
                worker_records,
            )

            to_open = [e for e in events if e.get('event_type') == 'open']
            to_resolve = [e for e in events if e.get('event_type') == 'resolve']

            # Verify both open and resolve
            self.assertEqual(len(to_open), 1)
            self.assertEqual(len(to_resolve), 1)

            self.assertEqual(to_open[0]["worker_id"], "azure-a")
            self.assertEqual(to_open[0]["condition"], "auth_error")
            self.assertEqual(to_open[0]["event_type"], "open")
            self.assertEqual(to_open[0]["observed_at"], "2026-09-19T10:00:00Z")
            # Safe error code should be extracted
            self.assertEqual(to_open[0]["error_code"], "auth_error")

            self.assertEqual(to_resolve[0]["worker_id"], "azure-a")
            self.assertEqual(to_resolve[0]["condition"], "auth_error")
            self.assertEqual(to_resolve[0]["event_type"], "resolve")
            self.assertEqual(to_resolve[0]["resolved_at"], "2026-09-19T11:00:00Z")

            # Simulate journal persistence and reload
            journal_path = Path(tmpdir) / "journal.jsonl"
            journal = AuditJournal(journal_path)

            # Append incidents
            for incident in to_open:
                journal.append_idempotent(
                    record_id=incident["incident_id"],
                    record_type="worker_health_incident",
                    agent="test",
                    payload=incident,
                )
            for incident in to_resolve:
                journal.append_idempotent(
                    record_id=f"{incident['incident_id']}:resolve",
                    record_type="worker_health_incident",
                    agent="test",
                    payload=incident,
                )

            # Reload and verify no active incidents
            records = journal.read()
            active = get_active_incidents(records)
            self.assertEqual(len(active), 0)

            # Second replay should be idempotent
            from .worker_health_incidents import get_known_incident_ids
            known_ids = get_known_incident_ids(records)
            events2 = replay_worker_incidents_from_records(
                worker_records,
                known_incident_ids=known_ids,
            )

            to_open2 = [e for e in events2 if e.get('event_type') == 'open']
            to_resolve2 = [e for e in events2 if e.get('event_type') == 'resolve']

            self.assertEqual(len(to_open2), 0)
            self.assertEqual(len(to_resolve2), 0)

    def test_condition_transition_resolves_other_failures(self):
        """Condition transition: auth -> model resolves auth at new record."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inbox_dir = root / "research_inbox" / "azure-a"
            inbox_dir.mkdir(parents=True)

            auth_rec = sample_worker_record(
                record_id="auth-err",
                worker_id="azure-a",
                status="auth_error",
                observed_at="2026-09-19T10:00:00Z",
                error={"code": "auth_error"},
            )
            model_rec = sample_worker_record(
                record_id="model-err",
                worker_id="azure-a",
                status="model_error",
                observed_at="2026-09-19T10:30:00Z",
                error={"code": "model_error"},
            )

            (inbox_dir / "auth-err.json").write_text(json.dumps(auth_rec))
            (inbox_dir / "model-err.json").write_text(json.dumps(model_rec))

            worker_records = research_inbox_all_records_by_worker(root)
            events = replay_worker_incidents_from_records(
                worker_records,
            )

            to_open = [e for e in events if e.get('event_type') == 'open']
            to_resolve = [e for e in events if e.get('event_type') == 'resolve']

            # Should open both, resolve auth when model appears
            self.assertEqual(len(to_open), 2)
            self.assertEqual(len(to_resolve), 1)

            open_conditions = {inc["condition"] for inc in to_open}
            self.assertEqual(open_conditions, {"auth_error", "model_error"})

            # Resolve should be for auth_error (the prior one)
            self.assertEqual(to_resolve[0]["condition"], "auth_error")
            self.assertEqual(to_resolve[0]["resolved_at"], "2026-09-19T10:30:00Z")

    def test_stale_resolution_on_newer_record(self):
        """Active stale incident resolved when newer record appears."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inbox_dir = root / "research_inbox" / "azure-a"
            inbox_dir.mkdir(parents=True)

            newer_rec = sample_worker_record(
                record_id="auth-err",
                worker_id="azure-a",
                status="auth_error",
                observed_at="2026-09-19T10:00:00Z",
                error={"code": "auth_error"},
            )
            (inbox_dir / "auth-err.json").write_text(json.dumps(newer_rec))

            # Pre-existing stale incident
            active_incidents = {
                "stale-incident": {
                    "incident_id": "stale-incident",
                    "worker_id": "azure-a",
                    "condition": "stale",
                    "event_type": "open",
                    "observed_at": "2026-09-19T09:00:00Z",
                }
            }

            worker_records = research_inbox_all_records_by_worker(root)
            events = replay_worker_incidents_from_records(
                worker_records,
            )

            to_open = [e for e in events if e.get('event_type') == 'open']
            to_resolve = [e for e in events if e.get('event_type') == 'resolve']

            # Replay should open auth_error
            self.assertEqual(len(to_open), 1)
            self.assertEqual(to_open[0]["condition"], "auth_error")

            # Apply stale detection to resolve pre-existing stale incident
            from .worker_health_incidents import apply_stale_detection_latest_only
            events2 = apply_stale_detection_latest_only(
                worker_records,
                active_incidents,
                events,  # Pass combined events list
            )

            to_open2 = [e for e in events2 if e.get('event_type') == 'open']
            to_resolve2 = [e for e in events2 if e.get('event_type') == 'resolve']

            # Should resolve stale, keep auth_error
            stale_resolves = [
                r for r in to_resolve2 if r["condition"] == "stale"
            ]
            self.assertEqual(len(stale_resolves), 1)
            self.assertEqual(stale_resolves[0]["incident_id"], "stale-incident")

    def test_incident_schema_version_included(self):
        """Incident payloads include schema_version for versioning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inbox_dir = root / "research_inbox" / "azure-a"
            inbox_dir.mkdir(parents=True)

            error_rec = sample_worker_record(
                record_id="auth-err",
                worker_id="azure-a",
                status="auth_error",
                observed_at="2026-09-19T10:00:00Z",
                error={"code": "auth_error"},
            )
            completed_rec = sample_worker_record(
                record_id="completed",
                worker_id="azure-a",
                status="completed",
                observed_at="2026-09-19T11:00:00Z",
            )
            (inbox_dir / "auth-err.json").write_text(json.dumps(error_rec))
            (inbox_dir / "completed.json").write_text(json.dumps(completed_rec))

            worker_records = research_inbox_all_records_by_worker(root)
            events = replay_worker_incidents_from_records(
                worker_records,
            )

            to_open = [e for e in events if e.get('event_type') == 'open']
            to_resolve = [e for e in events if e.get('event_type') == 'resolve']

            # All payloads must include schema_version
            self.assertIn("schema_version", to_open[0])
            self.assertEqual(to_open[0]["schema_version"], INCIDENT_SCHEMA_VERSION)
            self.assertIn("schema_version", to_resolve[0])
            self.assertEqual(to_resolve[0]["schema_version"], INCIDENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
