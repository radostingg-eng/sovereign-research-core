import tempfile
import unittest
from pathlib import Path

from .accepted_inputs import feedback_snapshot_ids
from .audit_store import AuditJournal
from .cycle_receipt import build_receipt


class FeedbackReceiptSelectionTests(unittest.TestCase):
    def receipt_record(self, status):
        root = Path(tempfile.mkdtemp(prefix="accepted-inputs-"))
        journal = AuditJournal(root / "journal.jsonl")
        journal.append(
            record_id="genesis",
            record_type="system_change",
            agent="test",
            caused_by=(),
            payload={"change": "test root"},
        )
        payload = build_receipt(
            cycle_id=f"cycle-{status}",
            run_id=f"run-{status}",
            started_at="2026-09-16T12:00:00Z",
            completed_at="2026-09-16T12:01:00Z",
            mode="host_input_replay",
            snapshot_id=f"{status}-input:abc123",
            stages=[{
                "stage_id": "portfolio",
                "agent_id": "portfolio",
                "status": status,
                "execution_order": 1,
                "started_at": "2026-09-16T12:00:00Z",
                "completed_at": "2026-09-16T12:01:00Z",
                "tools_used": [],
            }],
            tools_used=[],
            status=status,
            decision_status="blocked" if status == "blocked" else "wait",
            blockers=(
                ["source unavailable"] if status != "completed" else []
            ),
            required_stages=["portfolio"],
            self_improvement={"status": "none", "mutation_ids": [], "gates": {}},
            host={"cognitive_execution_claim": "test receipt"},
        )
        journal.append(
            record_id=f"cycle-receipt:cycle-{status}",
            record_type="cycle_receipt",
            agent="test",
            caused_by=("genesis",),
            payload=payload,
        )
        return journal.read()

    def test_valid_completed_receipt_is_eligible(self):
        self.assertEqual(
            feedback_snapshot_ids(self.receipt_record("completed")),
            {"completed-input:abc123"},
        )

    def test_valid_blocked_receipt_is_eligible(self):
        self.assertEqual(
            feedback_snapshot_ids(self.receipt_record("blocked")),
            {"blocked-input:abc123"},
        )

    def test_failed_receipt_is_not_eligible(self):
        self.assertEqual(
            feedback_snapshot_ids(self.receipt_record("failed")),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
