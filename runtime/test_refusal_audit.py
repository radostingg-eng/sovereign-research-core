import json
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .integrity import verify_chain
from .refusal_audit import sync_rejection_ledger
from .reliability import operational_reliability


class RefusalAuditTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="refusal-audit-"))
        self.ledger = self.root / "REJECTIONS.jsonl"
        self.journal_path = self.root / "journal.jsonl"
        self.journal = AuditJournal(self.journal_path)

    def write_events(self):
        events = [{
            "candidate_id": "cycle-one.semantic.json@sha256:" + "a" * 64,
            "input": "cycle-one.semantic.json",
            "sha256": "a" * 64,
            "refused_at": "2026-09-20T01:00:00Z",
            "codes": [
                "semantic_candidate_invalid:"
                "semantic_top_level_missing|/evidence_calls|evidence_calls",
            ],
        }, {
            "candidate_id": "cycle-two.semantic.json@sha256:" + "b" * 64,
            "input": "cycle-two.semantic.json",
            "sha256": "b" * 64,
            "refused_at": "2026-09-20T02:00:00Z",
            "codes": ["semantic_json_line_too_long:2000>1000"],
        }]
        self.ledger.write_text(
            "".join(json.dumps(row) + "\n" for row in events),
            encoding="utf-8",
        )

    def test_sync_is_idempotent_and_chain_valid(self):
        self.write_events()

        self.assertEqual(
            sync_rejection_ledger(self.ledger, self.journal),
            2,
        )
        self.assertEqual(
            sync_rejection_ledger(self.ledger, self.journal),
            0,
        )
        records = self.journal.read()
        self.assertEqual(
            [row["record_type"] for row in records],
            ["host_input_refusal", "host_input_refusal"],
        )
        self.assertEqual(verify_chain(records), [])

    def test_synced_refusal_resets_reliability_streak(self):
        self.write_events()
        sync_rejection_ledger(self.ledger, self.journal)

        score = operational_reliability(
            self.journal.read(),
            journal_path=self.journal_path,
        )

        self.assertEqual(
            score["candidate_attempts"]["cycle_candidate_refusals"],
            2,
        )
        self.assertEqual(
            score["accepted_candidate_streak"]["current"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
