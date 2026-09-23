import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .instruction_expiry import (
    expiring_instructions,
    expiring_instructions_from_history,
    instruction_expiry_summary,
    persist_instruction_expiry_decisions,
    validate_instruction_expiry_decisions,
)
from .run_host_cycle import partition_validation_errors, run_one
from .semantic_candidate import build_semantic_candidate
from .test_semantic_candidate import semantic_candidate


def expiry_input(*, decision="let_expire"):
    semantic = semantic_candidate()
    semantic["cycle_id"] = "cycle-instruction-expiry"
    built = build_semantic_candidate(
        semantic,
        filename="cycle-instruction-expiry.semantic.json",
    )
    data = copy.deepcopy(built.canonical)
    instruction = {
        "id": "101",
        "symbol": "META",
        "side": "SELL",
        "quantity": 6,
        "order_type": "LIMIT",
        "limit_price": 5.65,
        "tif": "DAY",
        "expiration": "2026-09-18T16:00:00Z",
    }
    data["order_instructions"] = [copy.deepcopy(instruction)]
    data["snapshot"]["order_instructions"] = [
        copy.deepcopy(instruction)
    ]
    saved = next(
        row for row in data["evidence_calls"]
        if row["producer"] == "saved_instructions"
    )
    saved["call"]["result"] = {
        "order_instructions": [copy.deepcopy(instruction)],
    }
    saved["projection"] = {
        "extractor": "json_pointer_v1",
        "bindings": [
            {
                "source_path": "/order_instructions",
                "target_path": "/order_instructions",
            },
            {
                "source_path": "/order_instructions",
                "target_path": "/snapshot/order_instructions",
            },
        ],
    }
    row = {
        "decision_id": "expiry-decision-101",
        "instruction_id": "101",
        "observed_expiration": instruction["expiration"],
        "decision": decision,
        "rationale": "Let the current proposal expire without replacement.",
        "pre_read_tool_call_id": saved["call"]["tool_call_id"],
        "activity_indexes": [],
        "replacement_instruction_id": None,
        "evidence": ["stage:decision"],
        "supersedes_decision_id": None,
    }
    data["instruction_expiry_decisions"] = [row]
    return data


class InstructionExpiryValidationTests(unittest.TestCase):
    def test_due_instruction_is_detected_from_saved_call_result(self):
        data = expiry_input()

        rows = expiring_instructions(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instruction_id"], "101")
        self.assertEqual(rows[0]["state"], "due")
        self.assertEqual(rows[0]["hours_remaining"], 24.0)

    def test_missing_decision_is_advisory_in_first_release(self):
        data = expiry_input()
        data.pop("instruction_expiry_decisions")

        self.assertEqual(
            validate_instruction_expiry_decisions(
                None,
                data=data,
                records=[],
            ),
            [],
        )
        summary = instruction_expiry_summary(
            [],
            latest_input=data,
        )
        self.assertEqual(summary["decision_needed_count"], 1)
        self.assertEqual(
            summary["enforcement"],
            "advisory_until_success_gate",
        )

    def test_feedback_summary_uses_current_time_with_stale_input(self):
        data = expiry_input()
        expiration = "2026-09-24T19:01:13.635Z"
        data["as_of"] = "2026-09-20T12:57:00Z"
        data["snapshot"]["as_of"] = "2026-09-20T12:57:00Z"
        saved = next(
            row for row in data["evidence_calls"]
            if row["producer"] == "saved_instructions"
        )
        saved["call"]["result"]["order_instructions"][0][
            "expiration"
        ] = expiration

        self.assertEqual(expiring_instructions(data), [])
        summary = instruction_expiry_summary(
            [],
            latest_input=data,
            observed_at=datetime(
                2026, 9, 23, 13, 1, 13, 635000,
                tzinfo=timezone.utc,
            ),
        )

        self.assertEqual(summary["due_count"], 1)
        self.assertEqual(summary["decision_needed_count"], 1)
        self.assertEqual(summary["items"][0]["state"], "due")
        self.assertEqual(summary["items"][0]["hours_remaining"], 30.0)

    def test_feedback_uses_prior_expiration_for_current_instruction(self):
        prior = expiry_input()
        current = copy.deepcopy(prior)
        current["cycle_id"] = "cycle-instruction-current"
        current["as_of"] = "2026-09-23T12:59:00Z"
        current["snapshot"]["as_of"] = "2026-09-23T12:59:00Z"
        saved = next(
            row for row in current["evidence_calls"]
            if row["producer"] == "saved_instructions"
        )
        saved["call"]["tool_call_id"] = "saved-instructions-current"
        saved["call"]["result"]["order_instructions"][0].pop(
            "expiration"
        )
        observed_at = datetime(
            2026, 9, 23, 13, 1, 13, 635000,
            tzinfo=timezone.utc,
        )

        rows = expiring_instructions_from_history(
            [prior, current],
            observed_at=observed_at,
        )
        summary = instruction_expiry_summary(
            [],
            recent_inputs=[prior, current],
            observed_at=observed_at,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["pre_read_tool_call_id"],
            "saved-instructions-current",
        )
        self.assertEqual(summary["due_count"], 1)
        self.assertEqual(summary["decision_needed_count"], 1)

    def test_feedback_does_not_resurrect_absent_instruction(self):
        prior = expiry_input()
        current = copy.deepcopy(prior)
        saved = next(
            row for row in current["evidence_calls"]
            if row["producer"] == "saved_instructions"
        )
        saved["call"]["result"]["order_instructions"] = []

        rows = expiring_instructions_from_history(
            [prior, current],
            observed_at=datetime(
                2026, 9, 23, 13, 1, 13, 635000,
                tzinfo=timezone.utc,
            ),
        )

        self.assertEqual(rows, [])

    def test_let_expire_is_a_valid_standing_decision(self):
        data = expiry_input()

        self.assertEqual(
            validate_instruction_expiry_decisions(
                data["instruction_expiry_decisions"],
                data=data,
                records=[],
            ),
            [],
        )

    def test_delete_requires_delete_then_post_read(self):
        data = expiry_input(decision="delete")
        data["order_instruction_activity"] = [{
            "operation": "delete",
            "tool": "IBKR delete order instruction",
            "request": {"instruction_id": "101"},
            "result": {"instruction_id": "101"},
            "instruction_id": "101",
        }, {
            "operation": "get",
            "tool": "IBKR get order instructions",
            "result": {"order_instructions": []},
        }]
        data["instruction_expiry_decisions"][0][
            "activity_indexes"
        ] = [0, 1]

        self.assertEqual(
            validate_instruction_expiry_decisions(
                data["instruction_expiry_decisions"],
                data=data,
                records=[],
            ),
            [],
        )

    def test_recreate_requires_new_id_and_confirmed_post_read(self):
        data = expiry_input(decision="recreate")
        replacement = {
            "id": "202",
            "expiration": "2026-10-01T16:00:00Z",
        }
        data["order_instruction_activity"] = [{
            "operation": "delete",
            "tool": "IBKR delete order instruction",
            "request": {"instruction_id": "101"},
            "result": {"instruction_id": "101"},
            "instruction_id": "101",
        }, {
            "operation": "create",
            "tool": "IBKR create order instruction",
            "request": {"symbol": "META"},
            "result": {"instruction_id": "202"},
            "instruction_id": "202",
        }, {
            "operation": "get",
            "tool": "IBKR get order instructions",
            "result": {"order_instructions": [replacement]},
        }]
        row = data["instruction_expiry_decisions"][0]
        row["activity_indexes"] = [0, 1, 2]
        row["replacement_instruction_id"] = "202"

        self.assertEqual(
            validate_instruction_expiry_decisions(
                data["instruction_expiry_decisions"],
                data=data,
                records=[],
            ),
            [],
        )

        row["replacement_instruction_id"] = "101"
        self.assertIn(
            "instruction_expiry_decision_invalid:0:"
            "replacement_instruction_id",
            validate_instruction_expiry_decisions(
                data["instruction_expiry_decisions"],
                data=data,
                records=[],
            ),
        )

    def test_partial_cycle_allows_wait_but_not_mutation(self):
        wait_data = expiry_input()
        blocking, advisory = partition_validation_errors(wait_data, [
            "evidence_call_invalid:0:provenance:capture_missing",
        ])
        self.assertTrue(advisory)
        self.assertFalse(any(
            code.startswith(
                "partial_cycle_instruction_expiry_mutation_forbidden"
            )
            for code in blocking
        ))

        delete_data = expiry_input(decision="delete")
        blocking, advisory = partition_validation_errors(delete_data, [
            "evidence_call_invalid:0:provenance:capture_missing",
        ])
        self.assertTrue(advisory)
        self.assertIn(
            "partial_cycle_instruction_expiry_mutation_forbidden:0",
            blocking,
        )


class InstructionExpiryPersistenceTests(unittest.TestCase):
    def test_decision_is_receipt_caused_and_summarized(self):
        directory = Path(tempfile.mkdtemp(prefix="instruction-expiry-"))
        journal = AuditJournal(directory / "journal.jsonl")
        data = expiry_input()
        receipt = {"cycle_id": data["cycle_id"]}

        self.assertEqual(
            persist_instruction_expiry_decisions(
                data,
                journal,
                receipt,
            ),
            1,
        )

        record = journal.read()[0]
        self.assertEqual(
            record["record_type"],
            "instruction_expiry_decision",
        )
        self.assertEqual(
            record["caused_by"],
            [f"cycle-receipt:{data['cycle_id']}"],
        )
        summary = instruction_expiry_summary(
            journal.read(),
            latest_input=data,
        )
        self.assertEqual(summary["decision_needed_count"], 0)
        self.assertEqual(
            summary["items"][0]["decision_status"],
            "decision_recorded",
        )

    def test_real_cycle_finalization_requires_decision_record(self):
        directory = Path(tempfile.mkdtemp(
            prefix="instruction-expiry-cycle-"
        ))
        path = directory / "cycle.json"
        journal = AuditJournal(directory / "audit" / "journal.jsonl")
        data = expiry_input()
        path.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            side_effect=lambda: journal.read(),
        ):
            receipt = run_one(path, journal)

        records = journal.read()
        decision = next(
            row for row in records
            if row["record_type"] == "instruction_expiry_decision"
        )
        finalization = next(
            row for row in records
            if row["record_type"] == "cycle_finalization"
            and row["payload"].get("cycle_id") == receipt["cycle_id"]
        )
        self.assertIn(
            decision["record_id"],
            {
                row["record_id"]
                for row in finalization["payload"]["required_records"]
            },
        )


if __name__ == "__main__":
    unittest.main()
