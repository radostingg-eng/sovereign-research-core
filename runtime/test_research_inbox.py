import json
import tempfile
import unittest
from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
