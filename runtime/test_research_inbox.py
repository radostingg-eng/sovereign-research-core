import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .research_inbox import (
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
        self.assertTrue(summary["items"][0]["stale"])


if __name__ == "__main__":
    unittest.main()
