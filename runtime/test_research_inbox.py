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
        self.assertLess(
            len(summary["items"][0]["result"]["summary"]),
            len(result["summary"]),
        )

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
