"""Tests for worker health incident tracking."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from .worker_health_incidents import (
    safe_error_code,
    get_active_incidents,
    compute_incident_transitions,
    replay_worker_incidents_from_records,
    apply_stale_detection_latest_only,
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

    def test_replay_worker_incidents_live_batch_auth_error_then_completed(self):
        """Replay records: auth_error then completed before one cycle.

        Matches live evidence (e.g., commit 33477e65): both records present
        in research_inbox before host cycle runs. replay should generate:
        - open incident for auth_error
        - resolve incident for completed recovery
        - no active incidents after
        """
        from .worker_health_incidents import replay_worker_incidents_from_records

        auth_error_record = sample_record(
            record_id="azure-a-20260919t100000z",
            worker_id="azure-a",
            status="auth_error",
            observed_at="2026-09-19T10:00:00Z",
            expires_at="2026-09-19T13:00:00Z",
            error={"code": "auth_error", "message": "Auth failed"},
        )
        completed_record = sample_record(
            record_id="azure-a-20260919t110000z",
            worker_id="azure-a",
            status="completed",
            observed_at="2026-09-19T11:00:00Z",
            expires_at="2026-09-20T02:00:00Z",
        )

        worker_records = {
            "azure-a": [auth_error_record, completed_record]
        }
        events = replay_worker_incidents_from_records(worker_records)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Should open incident for auth_error
        self.assertEqual(len(to_open), 1)
        self.assertEqual(to_open[0]["worker_id"], "azure-a")
        self.assertEqual(to_open[0]["condition"], "auth_error")
        self.assertEqual(to_open[0]["event_type"], "open")
        self.assertEqual(to_open[0]["source_record_id"], "azure-a-20260919t100000z")
        self.assertEqual(to_open[0]["observed_at"], "2026-09-19T10:00:00Z")

        # Should resolve the auth_error incident with completed record
        self.assertEqual(len(to_resolve), 1)
        self.assertEqual(to_resolve[0]["worker_id"], "azure-a")
        self.assertEqual(to_resolve[0]["condition"], "auth_error")
        self.assertEqual(to_resolve[0]["event_type"], "resolve")
        self.assertEqual(to_resolve[0]["source_record_id"], "azure-a-20260919t110000z")
        self.assertEqual(to_resolve[0]["resolved_at"], "2026-09-19T11:00:00Z")

    def test_replay_worker_incidents_multi_worker_batch(self):
        """Replay multiple workers independently."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        azure_a_error = sample_record(
            record_id="azure-a-err",
            worker_id="azure-a",
            status="model_error",
            observed_at="2026-09-19T10:00:00Z",
        )
        azure_b_error = sample_record(
            record_id="azure-b-err",
            worker_id="azure-b",
            status="quota_exhausted",
            observed_at="2026-09-19T10:30:00Z",
        )

        worker_records = {
            "azure-a": [azure_a_error],
            "azure-b": [azure_b_error],
        }
        events = replay_worker_incidents_from_records(worker_records)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        # Both workers should open incidents
        self.assertEqual(len(to_open), 2)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        workers = {inc["worker_id"] for inc in to_open}
        self.assertEqual(workers, {"azure-a", "azure-b"})

    def test_replay_worker_incidents_idempotent_rerun(self):
        """Replay twice should produce same transitions (deterministic IDs)."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        error_record = sample_record(
            record_id="azure-a-err",
            worker_id="azure-a",
            status="auth_error",
            observed_at="2026-09-19T10:00:00Z",
        )
        completed_record = sample_record(
            record_id="azure-a-ok",
            worker_id="azure-a",
            status="completed",
            observed_at="2026-09-19T11:00:00Z",
        )

        worker_records = {
            "azure-a": [error_record, completed_record]
        }

        # First run
        events_1 = replay_worker_incidents_from_records(
            worker_records,
        )

        to_open_1 = [e for e in events_1 if e.get('event_type') == 'open']
        to_resolve_1 = [e for e in events_1 if e.get('event_type') == 'resolve']

        # Second run (simulating retry)
        events_2 = replay_worker_incidents_from_records(
            worker_records,
        )

        to_open_2 = [e for e in events_2 if e.get('event_type') == 'open']
        to_resolve_2 = [e for e in events_2 if e.get('event_type') == 'resolve']

        # Should produce identical results (deterministic incident_ids)
        self.assertEqual(len(to_open_1), len(to_open_2))
        self.assertEqual(len(to_resolve_1), len(to_resolve_2))
        self.assertEqual(to_open_1[0]["incident_id"], to_open_2[0]["incident_id"])
        self.assertEqual(to_resolve_1[0]["incident_id"], to_resolve_2[0]["incident_id"])

    def test_replay_worker_incidents_condition_transition(self):
        """Replay failure -> different failure -> recovery."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        records = [
            sample_record(
                record_id="auth-err",
                worker_id="azure-a",
                status="auth_error",
                observed_at="2026-09-19T10:00:00Z",
            ),
            sample_record(
                record_id="model-err",
                worker_id="azure-a",
                status="model_error",
                observed_at="2026-09-19T10:30:00Z",
            ),
            sample_record(
                record_id="recovery",
                worker_id="azure-a",
                status="completed",
                observed_at="2026-09-19T11:00:00Z",
            ),
        ]

        worker_records = {"azure-a": records}
        events = replay_worker_incidents_from_records(
            worker_records,
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Should open two incidents (auth_error, model_error) and resolve both
        self.assertEqual(len(to_open), 2)
        self.assertEqual(len(to_resolve), 2)
        conditions = {inc["condition"] for inc in to_open}
        self.assertEqual(conditions, {"auth_error", "model_error"})

    def test_replay_worker_incidents_safe_fields_only(self):
        """Replay must not expose sensitive fields."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        error_record = sample_record(
            record_id="azure-a-err",
            worker_id="azure-a",
            status="auth_error",
            observed_at="2026-09-19T10:00:00Z",
            error={
                "code": "auth_error",
                "message": "SENSITIVE: credentials exposed in logs",
                "request_id": "abc123",
                "provider_response": "SENSITIVE_BODY_DATA",
            },
        )

        worker_records = {"azure-a": [error_record]}
        events = replay_worker_incidents_from_records(
            worker_records,
        )

        to_open = [e for e in events if e.get('event_type') == 'open']

        incident = to_open[0]
        # Only safe fields should be present
        allowed_keys = {
            "schema_version", "incident_id", "worker_id", "condition",
            "event_type", "source_record_id", "observed_at", "expires_at",
            "error_code"
        }
        self.assertTrue(set(incident.keys()).issubset(allowed_keys))
        # Sensitive message should not leak
        payload_str = json.dumps(incident)
        self.assertNotIn("SENSITIVE", payload_str)

    def test_replay_worker_incidents_condition_transition_resolves_prior(self):
        """Condition transition: auth_error -> model_error should resolve auth."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        auth_record = sample_record(
            record_id="auth-err",
            worker_id="azure-a",
            status="auth_error",
            observed_at="2026-09-19T10:00:00Z",
            error={"code": "auth_error", "message": "Auth failed"},
        )
        model_record = sample_record(
            record_id="model-err",
            worker_id="azure-a",
            status="model_error",
            observed_at="2026-09-19T10:30:00Z",
            error={"code": "model_error", "message": "Model failed"},
        )

        worker_records = {"azure-a": [auth_record, model_record]}
        events = replay_worker_incidents_from_records(
            worker_records,
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Should open auth_error and model_error
        self.assertEqual(len(to_open), 2)
        conditions = {inc["condition"] for inc in to_open}
        self.assertEqual(conditions, {"auth_error", "model_error"})

        # Should resolve auth_error when model_error is encountered
        self.assertEqual(len(to_resolve), 1)
        self.assertEqual(to_resolve[0]["condition"], "auth_error")
        self.assertEqual(to_resolve[0]["resolved_at"], "2026-09-19T10:30:00Z")

    def test_replay_worker_incidents_uses_safe_error_code(self):
        """Replay should extract error code using safe_error_code()."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        # Live record shape: status + nested error.code
        error_record = sample_record(
            record_id="azure-a-err",
            worker_id="azure-a",
            status="auth_error",
            observed_at="2026-09-19T10:00:00Z",
            error={"code": "auth_error", "message": "SENSITIVE: credentials exposed"},
        )

        worker_records = {"azure-a": [error_record]}
        events = replay_worker_incidents_from_records(
            worker_records,
        )

        to_open = [e for e in events if e.get('event_type') == 'open']

        # Should extract error_code using safe_error_code logic
        self.assertEqual(len(to_open), 1)
        self.assertIn("error_code", to_open[0])
        self.assertEqual(to_open[0]["error_code"], "auth_error")
        # Sensitive message should not appear
        self.assertNotIn("credentials exposed", json.dumps(to_open[0]))

    def test_replay_worker_incidents_stale_latest_resolves_on_newer_record(self):
        """When newer record appears, resolve any active stale incident."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        # Pre-existing stale incident from prior cycle
        active_incidents = {
            "stale-incident": {
                "incident_id": "stale-incident",
                "worker_id": "azure-a",
                "condition": "stale",
                "event_type": "open",
                "observed_at": "2026-09-19T09:00:00Z",
            }
        }

        # Newer explicit-failure record appears
        newer_record = sample_record(
            record_id="auth-err",
            worker_id="azure-a",
            status="auth_error",
            observed_at="2026-09-19T10:00:00Z",
            error={"code": "auth_error", "message": "Auth failed"},
        )

        worker_records = {"azure-a": [newer_record]}
        events = replay_worker_incidents_from_records(worker_records)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        # Replay opens auth_error incident
        self.assertEqual(len(to_open), 1)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        self.assertEqual(to_open[0]["condition"], "auth_error")

        # Apply stale detection to resolve the pre-existing stale incident
        events2 = apply_stale_detection_latest_only(
            worker_records,
            active_incidents,
            events,
        )

        to_open2 = [e for e in events2 if e.get('event_type') == 'open']
        to_resolve2 = [e for e in events2 if e.get('event_type') == 'resolve']

        # Should resolve the stale incident (newer record found)
        stale_resolves = [
            r for r in to_resolve2 if r.get("condition") == "stale"
        ]
        self.assertEqual(len(stale_resolves), 1)
        self.assertEqual(stale_resolves[0]["incident_id"], "stale-incident")

    def test_replay_worker_incidents_no_historical_stale_for_superseded_records(self):
        """Historical expired records do not create stale incidents."""
        from .worker_health_incidents import replay_worker_incidents_from_records

        # Two records, both before now. First one is expired but was superseded
        # by the second one before replay runs.
        old_record = sample_record(
            record_id="old-rec",
            worker_id="azure-a",
            status="completed",
            observed_at="2026-09-19T09:00:00Z",
            expires_at="2026-09-19T12:00:00Z",  # expired by 13:00
        )
        newer_record = sample_record(
            record_id="new-rec",
            worker_id="azure-a",
            status="completed",
            observed_at="2026-09-19T13:00:00Z",
            expires_at="2026-09-20T02:00:00Z",  # not expired
        )

        worker_records = {"azure-a": [old_record, newer_record]}
        events = replay_worker_incidents_from_records(
            worker_records,
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Should not create any incidents (both healthy, no failures)
        self.assertEqual(len(to_open), 0)
        self.assertEqual(len(to_resolve), 0)

    def test_apply_stale_detection_latest_only_opens_if_no_active_failures(self):
        """Stale detection only opens if no active explicit failures."""
        # Latest record is stale (no active explicit failures)
        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-id",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T10:00:00Z",
                    expires_at="2026-09-19T13:00:00Z",
                    stale=True,
                ),
            ]
        }

        events = apply_stale_detection_latest_only(
            worker_records,
            {},
            [],
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Should open stale incident
        self.assertEqual(len(to_open), 1)
        self.assertEqual(to_open[0]["condition"], "stale")

    def test_apply_stale_detection_latest_only_skips_if_active_failures(self):
        """Stale detection skips if explicit failures are active."""
        # Latest record is stale
        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-id",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T10:00:00Z",
                    expires_at="2026-09-19T13:00:00Z",
                    stale=True,
                ),
            ]
        }

        # But there's an active explicit-failure incident for this worker
        active_incidents = {
            "auth-incident": {
                "incident_id": "auth-incident",
                "worker_id": "azure-a",
                "condition": "auth_error",
                "event_type": "open",
            }
        }

        events = apply_stale_detection_latest_only(
            worker_records,
            active_incidents,
            [],
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Should NOT open stale incident
        self.assertEqual(len(to_open), 0)

    def test_chronological_reconstruction_old_completed_cannot_resolve_newer_auth(self):
        """Regression: old completed R1 + active auth_error R2 stays active.

        Chronological reconstruction must not pre-populate active state.
        An old completed record cannot retroactively resolve a newer
        auth_error that was encountered after it chronologically.

        Replay order: R1 (completed, old), R2 (auth_error, new)
        Result: R2 auth_error must stay active; R1 cannot resolve it.
        """
        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-1",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T10:00:00Z",  # Older
                ),
                sample_record(
                    record_id="rec-2",
                    worker_id="azure-a",
                    status="auth_error",
                    observed_at="2026-09-19T11:00:00Z",  # Newer
                    error={"code": "auth_error", "message": "Auth failed"},
                ),
            ]
        }

        # Replay: should open auth_error, never resolve it
        events = replay_worker_incidents_from_records(worker_records)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        # Should have opened auth_error, not resolved it
        self.assertEqual(len(to_open), 1)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        self.assertEqual(to_open[0]["condition"], "auth_error")
        self.assertEqual(to_open[0]["event_type"], "open")

        # Should NOT have any resolves (completed at R1 cannot resolve R2)
        self.assertEqual(len(to_resolve), 0)

    def test_chronological_reconstruction_active_stale_with_replayed_records_stays_active(self):
        """Regression: active stale R2 with R1/R2 replay stays active.

        An active stale incident with source R2 should stay active if
        the only newer replay data is R1, which is older. Stale incident
        is only resolved when a strictly newer record is discovered.
        """
        # Pre-existing active stale incident from R2
        active_incidents = {
            "stale-incident": {
                "incident_id": "stale-incident",
                "worker_id": "azure-a",
                "condition": "stale",
                "event_type": "open",
                "source_record_id": "rec-2",
                "observed_at": "2026-09-19T11:00:00Z",
            }
        }

        # Replay brings R1 (older) and R2 (same)
        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-1",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T10:00:00Z",
                    stale=False,
                ),
                sample_record(
                    record_id="rec-2",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T11:00:00Z",
                    stale=True,  # This is the stale one
                ),
            ]
        }

        events = apply_stale_detection_latest_only(
            worker_records,
            active_incidents,
            [],
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Stale incident should NOT be resolved (no strictly newer record)
        self.assertEqual(len(to_resolve), 0)

    def test_chronological_reconstruction_active_stale_with_newer_record_resolves(self):
        """Regression: active stale R2 + newer R3 resolves at R3.

        When a newer record R3 is discovered after an active stale
        incident on R2, the stale incident should be resolved using
        R3's observed_at and record_id.
        """
        # Pre-existing active stale incident from R2
        active_incidents = {
            "stale-incident": {
                "incident_id": "stale-incident",
                "worker_id": "azure-a",
                "condition": "stale",
                "event_type": "open",
                "source_record_id": "rec-2",
                "observed_at": "2026-09-19T11:00:00Z",
            }
        }

        # Replay brings R2 (stale) and R3 (newer, non-stale)
        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-2",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T11:00:00Z",
                    stale=True,  # Stale
                ),
                sample_record(
                    record_id="rec-3",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T12:00:00Z",
                    stale=False,  # Not stale (newer)
                ),
            ]
        }

        events = apply_stale_detection_latest_only(
            worker_records,
            active_incidents,
            [],
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Stale incident should be resolved using R3's data
        self.assertEqual(len(to_resolve), 1)
        self.assertEqual(to_resolve[0]["incident_id"], "stale-incident")
        self.assertEqual(to_resolve[0]["event_type"], "resolve")
        self.assertEqual(to_resolve[0]["source_record_id"], "rec-3")
        self.assertEqual(to_resolve[0]["resolved_at"], "2026-09-19T12:00:00Z")

    def test_chronological_reconstruction_explicit_resolved_then_stale_opens(self):
        """Regression: explicit latest R2 expired -> stale opens after explicit resolves.

        When an explicit failure resolves in this batch, and the now-latest
        record is stale, a stale incident should be opened (no active explicit
        failures suppress it). This tests that incidents_to_resolve is properly
        accounted for when deciding whether to suppress stale.
        """
        # R1: auth_error (older)
        # R2: completed (newer, but stale)
        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-1",
                    worker_id="azure-a",
                    status="auth_error",
                    observed_at="2026-09-19T10:00:00Z",
                    error={"code": "auth_error", "message": "Auth failed"},
                ),
                sample_record(
                    record_id="rec-2",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T11:00:00Z",
                    stale=True,  # This is the latest, but stale
                ),
            ]
        }

        # Replay resolves the auth_error, leaves R2 as latest
        events = replay_worker_incidents_from_records(worker_records)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        # R1 auth opens, R1 doesn't resolve (R2 is recovery)

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        self.assertEqual(len(to_open), 1)
        self.assertEqual(to_open[0]["condition"], "auth_error")

        # R2 recovery resolves R1 auth
        self.assertEqual(len(to_resolve), 1)
        self.assertEqual(to_resolve[0]["condition"], "auth_error")

        # Now apply stale detection with empty active incidents
        # Latest record (R2) is stale and no active explicit failures remain
        events_combined = to_open + to_resolve
        events2 = apply_stale_detection_latest_only(
            worker_records,
            {},  # No active incidents (auth was resolved above)
            events_combined,  # Combined events from replay
        )

        to_open2 = [e for e in events2 if e.get('event_type') == 'open']
        to_resolve2 = [e for e in events2 if e.get('event_type') == 'resolve']

        # Should open stale incident for R2
        stale_incidents = [inc for inc in to_open2 if inc.get("condition") == "stale"]
        self.assertEqual(len(stale_incidents), 1)
        self.assertEqual(stale_incidents[0]["source_record_id"], "rec-2")

    def test_chronological_reconstruction_idempotent_second_run(self):
        """Regression: second run with known_incident_ids adds no records.

        When the same records are replayed with known_incident_ids populated,
        no new incidents should be opened (all incident_ids already known).
        """
        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-1",
                    worker_id="azure-a",
                    status="auth_error",
                    observed_at="2026-09-19T10:00:00Z",
                    error={"code": "auth_error", "message": "Auth failed"},
                ),
            ]
        }

        # First run: opens incident
        events1 = replay_worker_incidents_from_records(worker_records)

        to_open1 = [e for e in events1 if e.get('event_type') == 'open']
        self.assertEqual(len(to_open1), 1)
        incident_id = to_open1[0]["incident_id"]

        # Second run: with known_incident_ids, should not re-open
        known_ids = {incident_id}
        events2 = replay_worker_incidents_from_records(
            worker_records,
            known_incident_ids=known_ids,
        )

        to_open2 = [e for e in events2 if e.get('event_type') == 'open']

        # Should have zero opens (already known)
        self.assertEqual(len(to_open2), 0)

    def test_active_incident_reopened_during_replay(self):
        """Regression: known-active incident is added to ephemeral state for recovery to resolve.

        When journal already contains active auth incident for R2, and replay brings R1 completed,
        R2 auth, R3 completed:
        - R1: completed (old)
        - R2: auth (known and currently active)
        - R3: completed (recovery)

        R2 must be in ephemeral state when R3 is processed so R3 can resolve it.
        Result: open incident for R2, resolve for R3, active_count becomes zero.
        """
        # Pre-generate R2 incident ID to match what journal would have
        worker_id = "azure-a"
        condition = "auth_error"
        r2_observed = "2026-09-19T11:00:00Z"
        r2_record_id = "rec-2"
        r2_incident_seed = f"{worker_id}:{condition}:{r2_observed}:{r2_record_id}"
        r2_incident_id = f"worker-incident-{sha256(r2_incident_seed.encode()).hexdigest()[:12]}"

        worker_records = {
            "azure-a": [
                sample_record(
                    record_id="rec-1",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T10:00:00Z",
                ),
                sample_record(
                    record_id="rec-2",
                    worker_id="azure-a",
                    status="auth_error",
                    observed_at="2026-09-19T11:00:00Z",
                    error={"code": "auth_error", "message": "Auth failed"},
                ),
                sample_record(
                    record_id="rec-3",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T12:00:00Z",
                ),
            ]
        }

        # Replay with R2 incident known and active
        known_ids = {r2_incident_id}
        active_ids = {r2_incident_id}
        events = replay_worker_incidents_from_records(
            worker_records,
            known_incident_ids=known_ids,
            active_incident_ids=active_ids,
        )

        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']
        to_open = [e for e in events if e.get('event_type') == 'open']
        to_resolve = [e for e in events if e.get('event_type') == 'resolve']

        # Should NOT open R2 (already known+active)
        self.assertEqual(len(to_open), 0)

        # Should resolve R2 when R3 recovery is encountered
        r2_resolves = [r for r in to_resolve if r["incident_id"] == r2_incident_id]
        self.assertEqual(len(r2_resolves), 1)
        self.assertEqual(r2_resolves[0]["event_type"], "resolve")
        self.assertEqual(r2_resolves[0]["resolved_at"], "2026-09-19T12:00:00Z")

    def test_stale_detection_with_stale_and_non_stale_records(self):
        """Regression: stale records detected only via computed flags from canonical loader.

        When records loaded via research_inbox_all_records_by_worker, stale/future
        flags must be computed (not manually injected). Stale detection opens
        incidents for latest stale records only.
        """
        from datetime import timedelta
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inbox_dir = root / "research_inbox" / "azure-a"
            inbox_dir.mkdir(parents=True)

            # R1: completed, not yet expired
            r1_expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            r1_rec = sample_record(
                record_id="rec-1",
                worker_id="azure-a",
                status="completed",
                observed_at="2026-09-19T10:00:00Z",
                expires_at=r1_expires,
            )
            (inbox_dir / "rec-1.json").write_text(json.dumps(r1_rec))

            # R2: completed, but already expired (stale)
            r2_expires = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            r2_rec = sample_record(
                record_id="rec-2",
                worker_id="azure-a",
                status="completed",
                observed_at="2026-09-19T11:00:00Z",
                expires_at=r2_expires,
            )
            (inbox_dir / "rec-2.json").write_text(json.dumps(r2_rec))

            # Load records with canonical loader (computes stale)
            from .research_inbox import research_inbox_all_records_by_worker
            worker_records = research_inbox_all_records_by_worker(root)

            self.assertIn("azure-a", worker_records)
            self.assertEqual(len(worker_records["azure-a"]), 2)

            # R1 should not be stale
            r1 = worker_records["azure-a"][0]
            self.assertFalse(r1.get("stale", False), "R1 should not be stale (not yet expired)")

            # R2 should be stale
            r2 = worker_records["azure-a"][1]
            self.assertTrue(r2.get("stale", False), "R2 should be stale (already expired)")

            # Apply stale detection: only R2 (latest, stale) should open incident
            events = apply_stale_detection_latest_only(
                worker_records,
                {},  # no active incidents
                [],
            )

            to_open = [e for e in events if e.get('event_type') == 'open']
            to_resolve = [e for e in events if e.get('event_type') == 'resolve']

            # Should NOT open for R1 (not stale)
            # Should open for R2 (latest and stale)
            stale_incidents = [inc for inc in to_open if inc.get("condition") == "stale"]
            self.assertEqual(len(stale_incidents), 1, "Should open one stale incident for latest stale record")
            self.assertEqual(stale_incidents[0]["source_record_id"], "rec-2")

    def test_pruned_active_failure_resolves_from_newer_recovery(self):
        active = {
            "worker-incident-pruned": {
                "incident_id": "worker-incident-pruned",
                "worker_id": "azure-a",
                "condition": "auth_error",
                "event_type": "open",
                "source_record_id": "removed-auth",
                "observed_at": "2026-09-19T11:00:00Z",
            },
        }
        records = {
            "azure-a": [sample_record(
                record_id="recovery",
                worker_id="azure-a",
                status="completed",
                observed_at="2026-09-19T12:00:00Z",
            )],
        }

        events = apply_stale_detection_latest_only(records, active, [])

        self.assertEqual(events, [{
            "schema_version": 1,
            "incident_id": "worker-incident-pruned",
            "worker_id": "azure-a",
            "condition": "auth_error",
            "event_type": "resolve",
            "source_record_id": "recovery",
            "resolved_at": "2026-09-19T12:00:00Z",
        }])

    def test_pruned_failure_transition_resolves_before_new_open(self):
        active = {
            "worker-incident-pruned": {
                "incident_id": "worker-incident-pruned",
                "worker_id": "azure-a",
                "condition": "auth_error",
                "event_type": "open",
                "source_record_id": "removed-auth",
                "observed_at": "2026-09-19T11:00:00Z",
            },
        }
        records = {
            "azure-a": [sample_record(
                record_id="model-failure",
                worker_id="azure-a",
                status="model_error",
                observed_at="2026-09-19T12:00:00+00:00",
                error={"code": "model_error", "message": "hidden"},
            )],
        }
        replay = replay_worker_incidents_from_records(
            records,
            known_incident_ids=set(active),
            active_incident_ids=set(active),
            active_incidents=active,
        )

        events = apply_stale_detection_latest_only(
            records,
            active,
            replay,
        )

        self.assertEqual(
            [event["event_type"] for event in events],
            ["resolve", "open"],
        )
        self.assertEqual(events[0]["incident_id"], "worker-incident-pruned")
        self.assertEqual(events[1]["condition"], "model_error")

    def test_pruned_active_failure_ignores_older_record(self):
        active = {
            "worker-incident-pruned": {
                "incident_id": "worker-incident-pruned",
                "worker_id": "azure-a",
                "condition": "auth_error",
                "event_type": "open",
                "source_record_id": "removed-auth",
                "observed_at": "2026-09-19T11:00:00+00:00",
            },
        }
        records = {
            "azure-a": [sample_record(
                record_id="older",
                worker_id="azure-a",
                status="completed",
                observed_at="2026-09-19T10:00:00Z",
            )],
        }

        events = apply_stale_detection_latest_only(records, active, [])

        self.assertEqual(events, [])

    def test_replay_events_are_chronological_with_resolve_before_open(self):
        records = {
            "azure-a": [
                sample_record(
                    record_id="auth",
                    worker_id="azure-a",
                    status="auth_error",
                    observed_at="2026-09-19T10:00:00Z",
                    error={"code": "auth_error"},
                ),
                sample_record(
                    record_id="model",
                    worker_id="azure-a",
                    status="model_error",
                    observed_at="2026-09-19T11:00:00Z",
                    error={"code": "model_error"},
                ),
                sample_record(
                    record_id="recovery",
                    worker_id="azure-a",
                    status="completed",
                    observed_at="2026-09-19T12:00:00Z",
                ),
            ],
        }

        events = replay_worker_incidents_from_records(records)

        self.assertEqual(
            [(event["event_type"], event["condition"]) for event in events],
            [
                ("open", "auth_error"),
                ("resolve", "auth_error"),
                ("open", "model_error"),
                ("resolve", "model_error"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
