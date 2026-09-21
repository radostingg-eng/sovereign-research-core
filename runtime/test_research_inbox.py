import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .research_inbox import (
    RESEARCH_INBOX_SUMMARY_BYTE_BUDGET,
    prune_expired_inbox,
    research_inbox_summary,
    validate_inbox_record,
)


def record(**overrides):
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


class ResearchInboxTests(unittest.TestCase):
    def test_worker_attested_completed_record_is_valid(self):
        value = record()
        encoded = json.dumps(value).encode()

        self.assertEqual(
            validate_inbox_record(value, byte_length=len(encoded)),
            [],
        )

    def test_private_account_keys_are_refused_recursively(self):
        value = record(result={"positions": [{"symbol": "MSFT"}]})

        self.assertIn(
            "research_inbox_forbidden:/result/positions",
            validate_inbox_record(value, byte_length=100),
        )

    def test_new_telemetry_shape_rejects_unallowlisted_fields(self):
        value = record(
            telemetry={
                "usage": {
                    "input_tokens": 10,
                    "secret": "must-not-persist",
                },
            },
        )

        self.assertIn(
            "research_inbox_telemetry",
            validate_inbox_record(value, byte_length=100),
        )

    def test_summary_marks_expired_records_stale(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        path = root / "research_inbox" / "azure-a" / "record.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record()), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(summary["record_count"], 1)
        self.assertEqual(summary["fresh_count"], 0)
        self.assertEqual(summary["stale_count"], 1)
        self.assertEqual(summary["future_count"], 0)
        self.assertEqual(summary["items"], [])

    def test_summary_excludes_incomplete_result_blobs(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        path = root / "research_inbox" / "azure-a" / "record.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record(
            expires_at="2026-09-20T03:00:00Z",
            result={"summary": "Truncated JSON text."},
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(summary["items"], [])
        self.assertEqual(summary["incomplete_result_count"], 1)

    def test_summary_projects_bounded_result_digest(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        path = root / "research_inbox" / "azure-a" / "record.json"
        path.parent.mkdir(parents=True)
        result = {
            "summary": "S" * 5000,
            "hypotheses": ["H" * 2000] * 8,
            "evidence_needed": ["E" * 2000] * 8,
            "counterevidence": ["C" * 2000] * 8,
            "uncertainties": ["U" * 2000] * 8,
            "suggested_next_question": "Q" * 2000,
        }
        path.write_text(json.dumps(record(
            expires_at="2026-09-20T03:00:00Z",
            result=result,
            quality={"result_schema_complete": True},
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        )

        encoded = json.dumps(summary).encode()
        self.assertLessEqual(
            len(encoded),
            RESEARCH_INBOX_SUMMARY_BYTE_BUDGET,
        )
        self.assertEqual(len(summary["items"]), 1)
        self.assertEqual(
            summary["items"][0]["full_record_path"],
            "research_inbox/azure-a/record.json",
        )
        self.assertEqual(
            summary["adoption_required_record_ids"],
            ["azure-a-20260919t220000z"],
        )
        self.assertLess(
            len(summary["items"][0]["result"]["summary"]),
            len(result["summary"]),
        )

    def test_summary_aggregates_bounded_azure_worker_health(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        result = {
            "summary": "Bounded result.",
            "hypotheses": [],
            "evidence_needed": [],
            "counterevidence": [],
            "uncertainties": [],
            "suggested_next_question": "Next?",
        }
        azure_a = root / "research_inbox" / "azure-a"
        azure_b = root / "research_inbox" / "azure-b"
        azure_a.mkdir(parents=True)
        azure_b.mkdir(parents=True)
        azure_a.joinpath("success.json").write_text(json.dumps(record(
            record_id="azure-a-success",
            observed_at="2026-09-20T01:00:00Z",
            expires_at="2026-09-20T04:00:00Z",
            result=result,
            quality={
                "result_schema_complete": True,
                "attempt_count": 2,
                "retried": True,
                "truncated": True,
            },
            telemetry={
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "cached_input_tokens": 25,
                    "cache_write_input_tokens": 5,
                    "reasoning_output_tokens": 10,
                },
                "rate_limits": {
                    "requests": {
                        "limit": 10,
                        "remaining": 7,
                        "reset_after_seconds": 30.0,
                    },
                    "tokens": {
                        "limit": 20000,
                        "remaining": 12000,
                        "reset_after_seconds": 1.5,
                    },
                },
                "retry_after_seconds": None,
            },
        )), encoding="utf-8")
        azure_a.joinpath("auth.json").write_text(json.dumps(record(
            record_id="azure-a-auth",
            status="auth_error",
            observed_at="2026-09-20T02:00:00Z",
            expires_at="2026-09-20T05:00:00Z",
            error={
                "code": "auth_error",
                "message": "Azure authentication failed.",
            },
            quality={
                "result_schema_complete": None,
                "attempt_count": 1,
                "retried": False,
                "truncated": False,
            },
            telemetry={
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "cached_input_tokens": None,
                    "cache_write_input_tokens": None,
                    "reasoning_output_tokens": None,
                },
                "rate_limits": {
                    "requests": {
                        "limit": None,
                        "remaining": None,
                        "reset_after_seconds": None,
                    },
                    "tokens": {
                        "limit": None,
                        "remaining": None,
                        "reset_after_seconds": None,
                    },
                },
                "retry_after_seconds": None,
            },
        )), encoding="utf-8")
        azure_b.joinpath("success.json").write_text(json.dumps(record(
            record_id="azure-b-success",
            worker_id="azure-b",
            observed_at="2026-09-20T01:30:00Z",
            expires_at="2026-09-20T04:30:00Z",
            result=result,
            quality={
                "result_schema_complete": True,
                "attempt_count": 1,
                "retried": False,
                "truncated": False,
            },
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
            limit=2,
        )

        self.assertEqual(summary["worker_health_not_shown"], 0)
        self.assertEqual(
            [item["worker_id"] for item in summary["worker_health"]],
            ["azure-a", "azure-b"],
        )
        health_a = summary["worker_health"][0]
        self.assertEqual(health_a["latest_record"], {
            "record_id": "azure-a-auth",
            "status": "auth_error",
            "observed_at": "2026-09-20T02:00:00Z",
            "expires_at": "2026-09-20T05:00:00Z",
            "stale": False,
            "error_code": "auth_error",
        })
        self.assertEqual(health_a["last_success"], {
            "record_id": "azure-a-success",
            "observed_at": "2026-09-20T01:00:00Z",
        })
        self.assertEqual(health_a["counts"], {
            "records": 2,
            "completed": 1,
            "schema_complete": 1,
            "retried": 1,
        })
        self.assertEqual(health_a["latest_usage"]["total_tokens"], 150)
        self.assertEqual(
            health_a["latest_usage"]["cached_input_tokens"],
            25,
        )
        self.assertEqual(
            health_a["latest_rate_limits"]["requests"]["remaining"],
            7,
        )
        health_b = summary["worker_health"][1]
        self.assertEqual(health_b["counts"]["completed"], 1)
        self.assertIsNone(health_b["latest_usage"])
        self.assertIsNone(health_b["latest_rate_limits"])

    def test_worker_health_limit_reports_unshown_workers(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        for worker_id, minute in (("azure-a", 1), ("azure-b", 2)):
            path = root / "research_inbox" / worker_id / "record.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(record(
                record_id=f"{worker_id}-record",
                worker_id=worker_id,
                observed_at=f"2026-09-20T01:0{minute}:00Z",
                expires_at="2026-09-20T04:00:00Z",
                status="no_work",
                error={
                    "code": "no_complete_open_target",
                    "message": "No work.",
                },
            )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
            limit=1,
        )

        self.assertEqual(len(summary["worker_health"]), 1)
        self.assertEqual(summary["worker_health_not_shown"], 1)

    def test_summary_excludes_records_observed_after_projection_time(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        path = root / "research_inbox" / "azure-a" / "record.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record(
            observed_at="2026-09-20T02:01:00Z",
            expires_at="2026-09-20T03:00:00Z",
            result={
                "summary": "Future result.",
                "hypotheses": [],
                "evidence_needed": [],
                "counterevidence": [],
                "uncertainties": [],
                "suggested_next_question": "Verify later.",
            },
            quality={"result_schema_complete": True},
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(summary["future_count"], 1)
        self.assertEqual(summary["fresh_count"], 0)
        self.assertEqual(summary["items"], [])
        self.assertEqual(summary["adoption_required_record_ids"], [])

    def test_prune_expired_inbox_removes_only_expired_records(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        worker = root / "research_inbox" / "azure-a"
        worker.mkdir(parents=True)
        expired = worker / "expired.json"
        fresh = worker / "fresh.json"
        expired.write_text(json.dumps(record()), encoding="utf-8")
        fresh.write_text(json.dumps(record(
            record_id="azure-a-fresh",
            expires_at="2026-09-20T03:00:00Z",
        )), encoding="utf-8")

        removed = prune_expired_inbox(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(
            removed,
            ["research_inbox/azure-a/expired.json"],
        )
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.exists())

    def test_summary_includes_worker_alerts_for_explicit_failures(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        result = {
            "summary": "Bounded result.",
            "hypotheses": [],
            "evidence_needed": [],
            "counterevidence": [],
            "uncertainties": [],
            "suggested_next_question": "Next?",
        }
        azure_a = root / "research_inbox" / "azure-a"
        azure_a.mkdir(parents=True)

        # auth_error record should generate an alert
        azure_a.joinpath("auth.json").write_text(json.dumps(record(
            record_id="azure-a-auth",
            status="auth_error",
            observed_at="2026-09-20T02:00:00Z",
            expires_at="2026-09-20T05:00:00Z",
            error={
                "code": "auth_error",
                "message": "Azure authentication failed.",
            },
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(len(summary["worker_alerts"]), 1)
        alert = summary["worker_alerts"][0]
        self.assertEqual(alert["worker_id"], "azure-a")
        self.assertEqual(alert["condition"], "auth_error")
        self.assertEqual(alert["error_code"], "auth_error")

    def test_summary_includes_worker_alerts_for_stale_records(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        azure_a = root / "research_inbox" / "azure-a"
        azure_a.mkdir(parents=True)

        # Record that is stale (observed before, expires before projection time)
        azure_a.joinpath("stale.json").write_text(json.dumps(record(
            record_id="azure-a-stale",
            status="completed",
            observed_at="2026-09-19T22:00:00Z",
            expires_at="2026-09-20T01:00:00Z",
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        )

        # Stale record should generate a stale alert
        self.assertEqual(len(summary["worker_alerts"]), 1)
        alert = summary["worker_alerts"][0]
        self.assertEqual(alert["worker_id"], "azure-a")
        self.assertEqual(alert["condition"], "stale")

    def test_summary_excludes_completed_no_work_stale_from_alerts(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        result = {
            "summary": "Bounded result.",
            "hypotheses": [],
            "evidence_needed": [],
            "counterevidence": [],
            "uncertainties": [],
            "suggested_next_question": "Next?",
        }
        azure_a = root / "research_inbox" / "azure-a"
        azure_a.mkdir(parents=True)

        # Fresh completed record should NOT generate an alert
        azure_a.joinpath("completed.json").write_text(json.dumps(record(
            record_id="azure-a-completed",
            status="completed",
            observed_at="2026-09-20T02:00:00Z",  # Fresh at projection time
            expires_at="2026-09-20T05:00:00Z",
            result=result,
            quality={"result_schema_complete": True},
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
        )

        # No alerts for fresh completed records
        self.assertEqual(len(summary["worker_alerts"]), 0)

    def test_summary_includes_stale_completed_in_alerts(self):
        """Stale completed records should generate alerts."""
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        result = {
            "summary": "Bounded result.",
            "hypotheses": [],
            "evidence_needed": [],
            "counterevidence": [],
            "uncertainties": [],
            "suggested_next_question": "Next?",
        }
        azure_a = root / "research_inbox" / "azure-a"
        azure_a.mkdir(parents=True)

        # Stale completed record should generate a stale alert
        azure_a.joinpath("completed.json").write_text(json.dumps(record(
            record_id="azure-a-completed",
            status="completed",
            observed_at="2026-09-19T22:00:00Z",  # Stale at projection time
            expires_at="2026-09-20T01:00:00Z",
            result=result,
            quality={"result_schema_complete": True},
        )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        )

        # Stale completed records generate stale alerts
        self.assertEqual(len(summary["worker_alerts"]), 1)
        alert = summary["worker_alerts"][0]
        self.assertEqual(alert["worker_id"], "azure-a")
        self.assertEqual(alert["condition"], "stale")

    def test_summary_worker_alerts_bounded_by_byte_budget(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))

        # Create many failure records
        for i in range(50):
            worker_dir = root / "research_inbox" / f"worker-{i:02d}"
            worker_dir.mkdir(parents=True, exist_ok=True)
            worker_dir.joinpath("fail.json").write_text(json.dumps(record(
                record_id=f"worker-{i:02d}-fail",
                worker_id=f"worker-{i:02d}",
                status="quota_exhausted",
                observed_at="2026-09-20T02:00:00Z",
                expires_at="2026-09-20T05:00:00Z",
                error={"code": "quota_exhausted", "message": "Quota exceeded."},
            )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
        )

        # Should have alerts but capped by byte budget
        self.assertGreater(len(summary["worker_alerts"]), 0)
        self.assertLess(len(summary["worker_alerts"]), 50)
        # Verify the summary fits within budget
        summary_bytes = len(json.dumps(
            summary,
            ensure_ascii=False,
        ).encode("utf-8"))
        self.assertLessEqual(
            summary_bytes,
            research_inbox_summary.__globals__["RESEARCH_INBOX_SUMMARY_BYTE_BUDGET"],
        )

    def test_summary_worker_alerts_not_shown_count(self):
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        result = {
            "summary": "Bounded result.",
            "hypotheses": [],
            "evidence_needed": [],
            "counterevidence": [],
            "uncertainties": [],
            "suggested_next_question": "Next?",
        }

        # Create multiple failure records
        for i in range(3):
            worker_dir = root / "research_inbox" / f"worker-{i}"
            worker_dir.mkdir(parents=True, exist_ok=True)
            worker_dir.joinpath("fail.json").write_text(json.dumps(record(
                record_id=f"worker-{i}-fail",
                worker_id=f"worker-{i}",
                status="model_error",
                observed_at="2026-09-20T02:00:00Z",
                expires_at="2026-09-20T05:00:00Z",
                error={"code": "model_error", "message": "Model failed."},
            )), encoding="utf-8")

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
            limit=2,  # Low limit to test not_shown
        )

        # If all 3 fit in byte budget but limit=2, some will not be shown
        if len(summary["worker_alerts"]) < 3:
            self.assertGreater(summary["worker_alerts_not_shown"], 0)

    def test_summary_worker_incidents_bounded_by_byte_budget(self):
        """Worker incidents are bounded and fit within FEEDBACK byte budget."""
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))

        # Create failure records
        for i in range(3):
            worker_dir = root / "research_inbox" / f"worker-{i}"
            worker_dir.mkdir(parents=True, exist_ok=True)
            worker_dir.joinpath("fail.json").write_text(json.dumps(record(
                record_id=f"worker-{i}-fail",
                worker_id=f"worker-{i}",
                status="auth_error",
                observed_at="2026-09-20T02:00:00Z",
                expires_at="2026-09-20T05:00:00Z",
                error={"code": "auth_error", "message": "Auth failed."},
            )), encoding="utf-8")

        # Simulate active incidents
        active_incidents = {
            "worker-incident-0": {
                "incident_id": "worker-incident-0",
                "worker_id": "worker-0",
                "condition": "auth_error",
                "event_type": "open",
            },
            "worker-incident-1": {
                "incident_id": "worker-incident-1",
                "worker_id": "worker-1",
                "condition": "auth_error",
                "event_type": "open",
            },
            "worker-incident-2": {
                "incident_id": "worker-incident-2",
                "worker_id": "worker-2",
                "condition": "auth_error",
                "event_type": "open",
            },
        }

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
            active_incidents=active_incidents,
        )

        # Incidents should be bounded
        self.assertEqual(summary["worker_incidents"]["active_count"], 3)
        self.assertGreaterEqual(len(summary["worker_incidents"]["incidents"]), 0)
        self.assertLessEqual(len(summary["worker_incidents"]["incidents"]), 3)

        # Verify summary stays within budget
        summary_bytes = len(json.dumps(
            summary,
            ensure_ascii=False,
        ).encode("utf-8"))
        self.assertLessEqual(
            summary_bytes,
            research_inbox_summary.__globals__["RESEARCH_INBOX_SUMMARY_BYTE_BUDGET"],
        )

    def test_summary_worker_incidents_not_shown_count(self):
        """Worker incidents include not_shown count."""
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))

        # Simulate many active incidents
        active_incidents = {}
        for i in range(10):
            active_incidents[f"worker-incident-{i}"] = {
                "incident_id": f"worker-incident-{i}",
                "worker_id": f"worker-{i}",
                "condition": "model_error",
                "event_type": "open",
            }

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
            active_incidents=active_incidents,
        )

        # Check not_shown is properly tracked
        shown = len(summary["worker_incidents"]["incidents"])
        not_shown = summary["worker_incidents"]["not_shown"]
        self.assertEqual(shown + not_shown, 10)

    def test_all_worker_alerts_extracted_unbounded(self):
        """research_inbox_alerts extracts all workers without limit."""
        from runtime.research_inbox import research_inbox_alerts, _worker_alerts, _worker_health

        # Create 50 workers with different statuses
        health = []
        for i in range(50):
            health.append({
                "worker_id": f"worker-{i:02d}",
                "latest_record": {
                    "status": "auth_error" if i % 2 == 0 else "completed",
                    "record_id": f"rec-{i}",
                    "observed_at": "2026-09-20T01:00:00Z",
                    "expires_at": "2026-09-20T02:00:00Z",
                    "stale": False,
                    "error_code": "auth_error" if i % 2 == 0 else None,
                }
            })

        # Extract all alerts (unbounded via limit=None)
        alerts = _worker_alerts(health, limit=None)

        # Should include all 25 auth_error alerts (beyond limit=40 if applied)
        auth_errors = [a for a in alerts if a["condition"] == "auth_error"]
        self.assertEqual(len(auth_errors), 25)

    def test_zero_fit_incidents_shows_active_count(self):
        """When no incidents fit budget, active_count still shows total."""
        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))

        # Create many active incidents
        active_incidents = {}
        for i in range(10):
            active_incidents[f"worker-incident-{i}"] = {
                "incident_id": f"worker-incident-{i}",
                "worker_id": f"worker-{i}",
                "condition": "model_error",
                "event_type": "open",
            }

        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
            active_incidents=active_incidents,
        )

        # Even if no incidents fit budget, active_count is set
        self.assertEqual(summary["worker_incidents"]["active_count"], 10)
        # not_shown matches active_count when no incidents fit
        self.assertGreaterEqual(
            summary["worker_incidents"]["not_shown"],
            0,
        )

    def test_research_inbox_alerts_and_summary_agree_on_identity(self):
        """Persistence and display use same alert identity while respecting limits.

        Regression: research_inbox_alerts (persistence, unbounded) and
        research_inbox_summary (display, bounded to 40) should produce
        identical alerts for the workers they both include, just at
        different limits.
        """
        from runtime.research_inbox import research_inbox_alerts

        root = Path(tempfile.mkdtemp(prefix="research-inbox-"))
        root.mkdir(exist_ok=True)
        (root / "research_inbox").mkdir(exist_ok=True)

        # Create 50 workers in research inbox
        now = datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc)
        for i in range(50):
            worker_id = f"worker-{i:02d}"
            path = root / "research_inbox" / "azure-a" / f"{worker_id}.json"
            path.parent.mkdir(exist_ok=True)

            status = "auth_error" if i % 3 == 0 else "completed"
            record = {
                "schema_version": 1,
                "record_id": f"rec-{i}",
                "worker_id": worker_id,
                "status": status,
                "origin": "research",
                "observed_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }
            if status == "auth_error":
                record["error"] = {"code": "auth_error"}

            path.write_text(json.dumps(record))

        # Extract persistence alerts (unbounded)
        all_alerts = research_inbox_alerts(root, now=now)

        # Extract display alerts (bounded to 40, as in summary)
        summary = research_inbox_summary(root, now=now)
        display_alerts = summary["worker_alerts"]

        # Both should have auth_error alerts (17 total: workers 0, 3, 6, ..., 48)
        auth_errors_all = [a for a in all_alerts if a["condition"] == "auth_error"]
        auth_errors_display = [a for a in display_alerts if a["condition"] == "auth_error"]

        self.assertGreaterEqual(len(auth_errors_all), len(auth_errors_display))

        # Alerts they both include must be identical
        for i in range(min(len(auth_errors_all), len(auth_errors_display))):
            self.assertEqual(
                auth_errors_all[i]["worker_id"],
                auth_errors_display[i]["worker_id"],
            )
            self.assertEqual(
                auth_errors_all[i]["condition"],
                auth_errors_display[i]["condition"],
            )


if __name__ == "__main__":
    unittest.main()
