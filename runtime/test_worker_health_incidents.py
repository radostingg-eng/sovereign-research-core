"""Tests for worker health incident tracking."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .worker_health_incidents import (
    safe_error_code,
    get_active_incidents,
    compute_incident_transitions,
)


def sample_record(**overrides):
    """Create a sample worker record for testing."""
    value = {
        "schema_version": 1,
        "record_id": "azure-a-20260919t220000z",
        "worker_id": "azure-a",
        "status": "completed",
        "origin": "worker_attested",
        "observed_at": "2026-09-19T22:00:00Z",
        "expires_at": "2026-09-20T01:00:00Z",
        "deployment": {"deployment": "gpt-test"},
        "selection": {"rule": "stable"},
        "target": {"question": "What changes the decision?"},
        "request": {"sha256": "a" * 64},
        "result": {"summary": "Bounded result."},
    }
    value.update(overrides)
    return value


class WorkerHealthIncidentsTests(unittest.TestCase):
    def test_safe_error_code_extracts_failure_code(self):
        record = sample_record(
            status="auth_error",
            error={"code": "auth_error", "message": "Auth failed."},
        )
        self.assertEqual(safe_error_code(record), "auth_error")

    def test_safe_error_code_ignores_message_field(self):
        """Ensure sensitive error message is not exposed."""
        record = sample_record(
            status="auth_error",
            error={
                "code": "auth_error",
                "message": "Sensitive credentials exposed",
                "request_id": "abc123",
            },
        )
        code = safe_error_code(record)
        self.assertEqual(code, "auth_error")
        # The function should not return the message
        self.assertNotEqual(code, "Sensitive credentials exposed")

    def test_safe_error_code_returns_none_for_success(self):
        record = sample_record(status="completed")
        self.assertIsNone(safe_error_code(record))

    def test_safe_error_code_returns_none_for_no_work(self):
        record = sample_record(status="no_work")
        self.assertIsNone(safe_error_code(record))

    def test_safe_error_code_handles_malformed_error_field(self):
        record = sample_record(
            status="model_error",
            error="not_a_dict",  # Malformed
        )
        self.assertIsNone(safe_error_code(record))

    def test_get_active_incidents_returns_empty_for_no_journal(self):
        active = get_active_incidents([])
        self.assertEqual(active, {})

    def test_get_active_incidents_tracks_open_incidents(self):
        journal_records = [
            {
                "record_type": "worker_health_incident",
                "payload": {
                    "incident_id": "worker-incident-abc123",
                    "worker_id": "azure-a",
                    "event_type": "open",
                    "condition": "auth_error",
                },
            },
        ]
        active = get_active_incidents(journal_records)
        self.assertEqual(len(active), 1)
        self.assertIn("worker-incident-abc123", active)
        self.assertEqual(
            active["worker-incident-abc123"]["worker_id"],
            "azure-a",
        )

    def test_get_active_incidents_ignores_resolved_incidents(self):
        journal_records = [
            {
                "record_type": "worker_health_incident",
                "payload": {
                    "incident_id": "worker-incident-abc123",
                    "worker_id": "azure-a",
                    "event_type": "open",
                    "condition": "auth_error",
                },
            },
            {
                "record_type": "worker_health_incident",
                "payload": {
                    "incident_id": "worker-incident-abc123",
                    "worker_id": "azure-a",
                    "event_type": "resolve",
                    "condition": "auth_error",
                },
            },
        ]
        active = get_active_incidents(journal_records)
        # Incident was resolved, so not in active set
        self.assertEqual(len(active), 0)

    def test_get_active_incidents_tracks_per_incident_id_independently(self):
        """Each incident is tracked independently by incident_id, not per worker."""
        journal_records = [
            {
                "record_type": "worker_health_incident",
                "payload": {
                    "incident_id": "worker-incident-v1",
                    "worker_id": "azure-a",
                    "event_type": "open",
                    "condition": "auth_error",
                },
            },
            {
                "record_type": "worker_health_incident",
                "payload": {
                    "incident_id": "worker-incident-v2",
                    "worker_id": "azure-a",
                    "event_type": "open",
                    "condition": "auth_error",
                },
            },
        ]
        active = get_active_incidents(journal_records)
        # Both incidents are active independently
        self.assertEqual(len(active), 2)
        self.assertIn("worker-incident-v1", active)
        self.assertIn("worker-incident-v2", active)

    def test_compute_incident_transitions_opens_new_alert(self):
        """New alert without active incident triggers open."""
        alerts = [
            {
                "worker_id": "azure-a",
                "condition": "auth_error",
                "source_record_id": "rec-001",
                "observed_at": "2026-09-20T01:00:00Z",
                "expires_at": "2026-09-20T02:00:00Z",
                "error_code": "auth_error",
            },
        ]
        active_incidents = {}

        to_open, to_resolve = compute_incident_transitions(
            alerts,
            active_incidents,
        )

        self.assertEqual(len(to_open), 1)
        self.assertEqual(to_open[0]["worker_id"], "azure-a")
        self.assertEqual(to_open[0]["event_type"], "open")
        self.assertEqual(to_open[0]["error_code"], "auth_error")
        self.assertEqual(len(to_resolve), 0)

    def test_compute_incident_transitions_resolves_stale_incident(self):
        """Missing alert for active incident triggers resolve."""
        active_incidents = {
            "worker-incident-abc": {
                "incident_id": "worker-incident-abc",
                "worker_id": "azure-a",
                "condition": "auth_error",
                "event_type": "open",
            },
        }
        alerts = []

        to_open, to_resolve = compute_incident_transitions(
            alerts,
            active_incidents,
        )

        self.assertEqual(len(to_resolve), 1)
        self.assertEqual(to_resolve[0]["worker_id"], "azure-a")
        self.assertEqual(to_resolve[0]["event_type"], "resolve")
        self.assertEqual(len(to_open), 0)

    def test_compute_incident_transitions_idempotent_for_stable_alert(self):
        """Stable alert with active incident produces no transitions."""
        alerts = [
            {
                "worker_id": "azure-a",
                "condition": "auth_error",
                "source_record_id": "rec-001",
                "observed_at": "2026-09-20T01:00:00Z",
                "expires_at": "2026-09-20T02:00:00Z",
                "error_code": "auth_error",
            },
        ]
        active_incidents = {
            "worker-incident-abc": {
                "incident_id": "worker-incident-abc",
                "worker_id": "azure-a",
                "condition": "auth_error",
                "event_type": "open",
            },
        }

        to_open, to_resolve = compute_incident_transitions(
            alerts,
            active_incidents,
        )

        self.assertEqual(len(to_open), 0)
        self.assertEqual(len(to_resolve), 0)

    def test_compute_incident_transitions_handles_multiple_workers(self):
        """Multiple workers with different states transition independently."""
        alerts = [
            {
                "worker_id": "azure-a",
                "condition": "auth_error",
                "source_record_id": "rec-001",
                "observed_at": "2026-09-20T01:00:00Z",
                "expires_at": "2026-09-20T02:00:00Z",
                "error_code": "auth_error",
            },
            {
                "worker_id": "azure-c",
                "condition": "stale",
                "source_record_id": "rec-003",
                "observed_at": "2026-09-20T01:00:00Z",
                "expires_at": "2026-09-20T02:00:00Z",
            },
        ]
        active_incidents = {
            "worker-incident-b": {
                "incident_id": "worker-incident-b",
                "worker_id": "azure-b",
                "condition": "model_error",
                "event_type": "open",
            },
        }

        to_open, to_resolve = compute_incident_transitions(
            alerts,
            active_incidents,
        )

        self.assertEqual(len(to_open), 2)
        self.assertEqual(len(to_resolve), 1)

        worker_ids = {inc["worker_id"] for inc in to_open}
        self.assertEqual(worker_ids, {"azure-a", "azure-c"})

        resolved_id = to_resolve[0]["worker_id"]
        self.assertEqual(resolved_id, "azure-b")

    def test_compute_incident_transitions_preserves_error_code_for_stale(self):
        """Stale alerts do not include error_code field."""
        alerts = [
            {
                "worker_id": "azure-a",
                "condition": "stale",
                "source_record_id": "rec-001",
                "observed_at": "2026-09-20T01:00:00Z",
                "expires_at": "2026-09-20T02:00:00Z",
            },
        ]
        active_incidents = {}

        to_open, _ = compute_incident_transitions(alerts, active_incidents)

        self.assertEqual(len(to_open), 1)
        self.assertNotIn("error_code", to_open[0])

    def test_compute_incident_transitions_deterministic_incident_id(self):
        """Incident IDs are deterministic based on worker+condition+record."""
        alert = {
            "worker_id": "azure-a",
            "condition": "auth_error",
            "source_record_id": "rec-001",
            "observed_at": "2026-09-20T01:00:00Z",
            "expires_at": "2026-09-20T02:00:00Z",
            "error_code": "auth_error",
        }
        alerts = [alert]
        active_incidents = {}

        to_open_1, _ = compute_incident_transitions(
            alerts,
            active_incidents,
        )
        to_open_2, _ = compute_incident_transitions(
            alerts,
            active_incidents,
        )

        self.assertEqual(
            to_open_1[0]["incident_id"],
            to_open_2[0]["incident_id"],
        )

    def test_get_active_incidents_tracks_per_incident_id_not_worker(self):
        """Multiple incident types per worker are tracked independently.

        Regression: when appending auth_error open then model_error open
        then auth_error resolve, the latest-per-incident tracking keeps
        both open incidents, whereas latest-per-worker would erase the
        model_error incident when processing the auth_error resolve.
        """
        # First: auth_error incident opens
        journal_records = [
            {
                "record_type": "worker_health_incident",
                "payload": {
                    "incident_id": "worker-incident-auth",
                    "worker_id": "azure-a",
                    "condition": "auth_error",
                    "event_type": "open",
                },
            },
        ]
        active = get_active_incidents(journal_records)
        self.assertEqual(len(active), 1)
        self.assertIn("worker-incident-auth", active)

        # Second: model_error incident opens (same worker, different condition)
        journal_records.append({
            "record_type": "worker_health_incident",
            "payload": {
                "incident_id": "worker-incident-model",
                "worker_id": "azure-a",
                "condition": "model_error",
                "event_type": "open",
            },
        })
        active = get_active_incidents(journal_records)
        self.assertEqual(len(active), 2)
        self.assertIn("worker-incident-auth", active)
        self.assertIn("worker-incident-model", active)

        # Third: auth_error incident resolves
        journal_records.append({
            "record_type": "worker_health_incident",
            "payload": {
                "incident_id": "worker-incident-auth",
                "worker_id": "azure-a",
                "condition": "auth_error",
                "event_type": "resolve",
            },
        })
        active = get_active_incidents(journal_records)
        # CRITICAL: model_error incident must still be active
        self.assertEqual(len(active), 1)
        self.assertNotIn("worker-incident-auth", active)
        self.assertIn("worker-incident-model", active)

    def test_compute_incident_transitions_includes_schema_version(self):
        """Open/resolve payloads include schema_version for typed durable records."""
        alerts = [
            {
                "worker_id": "azure-a",
                "condition": "auth_error",
                "source_record_id": "rec-001",
                "observed_at": "2026-09-20T01:00:00Z",
                "expires_at": "2026-09-20T02:00:00Z",
                "error_code": "auth_error",
            },
        ]
        active_incidents = {
            "existing-incident": {
                "incident_id": "existing-incident",
                "worker_id": "azure-b",
                "condition": "model_error",
                "event_type": "open",
            }
        }

        to_open, to_resolve = compute_incident_transitions(
            alerts,
            active_incidents,
        )

        # New open incident includes schema_version
        self.assertEqual(len(to_open), 1)
        self.assertIn("schema_version", to_open[0])
        self.assertEqual(to_open[0]["schema_version"], 1)

        # Resolve incident includes schema_version
        self.assertEqual(len(to_resolve), 1)
        self.assertIn("schema_version", to_resolve[0])
        self.assertEqual(to_resolve[0]["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
