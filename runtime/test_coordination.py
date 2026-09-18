import unittest
from datetime import datetime, timezone

from .coordination import (
    envelope_hash,
    main,
    validate_ack,
    validate_claim,
    validate_message,
    validate_snapshot,
    with_hash,
)


T0 = "2026-09-16T00:00:00Z"
T1 = "2026-09-16T01:00:00Z"


class CoordinationTests(unittest.TestCase):
    def message(self, **overrides):
        value = {
            "message_id": "msg-1",
            "agent_id": "codex-cli",
            "created_at": T0,
            "type": "finding",
            "subject": "runtime failure",
            "body": "Found a reproducible failure.",
            "caused_by": [],
            "priority": "high",
            "references": ["runtime/cycle_receipt.py"],
        }
        value.update(overrides)
        return value

    def claim(self, **overrides):
        value = {
            "claim_id": "claim-1",
            "task_id": "task-1",
            "owner_agent_id": "codex-cli",
            "created_at": T0,
            "expires_at": T1,
            "scope": ["runtime/coordination.py"],
            "parent_request": "msg-1",
            "status": "active",
        }
        value.update(overrides)
        return value

    def test_message_and_hash(self):
        message = with_hash(self.message())
        self.assertEqual(validate_message(message), [])
        self.assertEqual(message["envelope_hash"], envelope_hash(message))

    def test_message_tamper_is_detected(self):
        message = with_hash(self.message())
        message["body"] = "tampered"
        self.assertIn("message_hash_mismatch", validate_message(message))

    def test_claim_expiry_and_active_state(self):
        claim = self.claim()
        self.assertEqual(validate_claim(claim, now=datetime(2026, 9, 16, 0, 30, tzinfo=timezone.utc)), [])
        self.assertIn(
            "claim_expired_but_active",
            validate_claim(claim, now=datetime(2026, 9, 16, 2, 0, tzinfo=timezone.utc)),
        )

    def test_claim_parent_reference_is_required_by_snapshot(self):
        claim = self.claim(parent_request="missing")
        errors = validate_snapshot(messages=[self.message()], claims=[claim])
        self.assertIn("claim_missing_parent_request:missing", errors)

    def test_ack_must_reference_existing_message(self):
        ack = {"ack_id": "ack-1", "agent_id": "sovereign-host", "created_at": T1, "message_id": "missing", "status": "accepted"}
        self.assertEqual(validate_ack(ack), [])
        self.assertIn("ack_missing_message:missing", validate_snapshot(acks=[ack]))

    def test_message_causal_reference_must_exist(self):
        message = self.message(caused_by=["missing"])
        self.assertIn("message_missing_cause:missing", validate_snapshot(messages=[message]))

    def test_invalid_types_and_priority_fail(self):
        self.assertTrue(validate_message(self.message(type="unknown")))
        self.assertTrue(validate_message(self.message(priority="critical")))

    def test_the_committed_envelopes_validate(self):
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
