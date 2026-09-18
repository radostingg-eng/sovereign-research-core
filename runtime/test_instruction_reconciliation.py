import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .accepted_inputs import input_fingerprint
from .audit_store import AuditJournal
from .instruction_reconciliation import (
    _payload,
    backfill_instruction_reconciliations,
    instruction_reconciliation_summary,
    persist_instruction_reconciliations,
    validate_instruction_reconciliations,
)
from .run_host_cycle import run_one
from .test_research_allocation import valid_input
from .test_tool_provenance import upgrade_tool_calls_to_v4


RECOMMENDATION_ID = "cycle-recommendation"
INSTRUCTION_ID = "102"


def proposal_record():
    return {
        "record_id": "order-instruction-recovery:cycle-recovery:102",
        "record_type": "order_instruction_event",
        "payload": {
            "cycle_id": "cycle-recovery",
            "recovered_from_cycle": RECOMMENDATION_ID,
            "operation": "recovered_create",
            "instruction_id": INSTRUCTION_ID,
            "instruction": {
                "action": "BUY_TO_CLOSE",
                "contract_id_ex": "776900613@SMART",
                "contract_description": "WHR Jan 15 2027 $40 PUT",
                "quantity": 6,
                "order_type": "LIMIT",
                "limit_price": 10.0,
                "time_in_force": "DAY",
            },
            "fresh_get": {
                "result": {
                    "order_instructions": [{
                        "id": INSTRUCTION_ID,
                        "contract_id_ex": "776900613@SMART",
                        "creation_time": "2026-09-17T10:00:00Z",
                    }],
                },
            },
        },
    }


def operator_lifecycle_record():
    return {
        "record_id": "lifecycle:instruction-102-deleted",
        "record_type": "lifecycle_event",
        "payload": {
            "recommendation_id": RECOMMENDATION_ID,
            "event_id": "lifecycle:instruction-102-deleted",
            "from_state": "instruction_created",
            "to_state": "deleted",
            "evidence_ids": [INSTRUCTION_ID],
            "metadata": {"ibkr_instruction_id": INSTRUCTION_ID},
        },
    }


def account_order(**overrides):
    value = {
        "order_id": "order-1",
        "contract_id": 776900613,
        "symbol": "WHR",
        "side": "BUY",
        "quantity": 6,
        "order_type": "LIMIT",
        "limit_price": 10.0,
        "tif": "DAY",
        "creation_time": "2026-09-17T15:00:00Z",
    }
    value.update(overrides)
    return value


def account_trade(**overrides):
    value = {
        "trade_id": "trade-1",
        "order_id": "order-1",
        "contract_id": 776900613,
        "symbol": "WHR",
        "side": "BUY",
        "size": 6,
        "price": 9.8,
        "trade_time": "2026-09-17T15:30:00Z",
    }
    value.update(overrides)
    return value


def reconciliation_input(
    *,
    disposition="deleted",
    app_visible=False,
    orders=(),
    trades=(),
    reconciliation_id="reconciliation-102-first",
    supersedes=None,
):
    data = valid_input()
    data["cycle_id"] = "cycle-reconciliation"
    data["order_instructions"] = []
    data["snapshot"]["order_instructions"] = []
    data["research"][0]["tool_calls"] = [
        {
            "tool": "Interactive Brokers (IBKR).get_account_orders",
            "call": "get_account_orders",
            "result": {"orders": list(orders)},
            "provenance": {
                "result_origin": "connector_response",
                "observed_at": "2026-09-17T15:58:00Z",
                "source_refs": [{
                    "kind": "uri",
                    "value": "ibkr://get_account_orders/current-cycle",
                }],
            },
        },
        {
            "tool": "Interactive Brokers (IBKR).get_account_trades",
            "call": "get_account_trades",
            "result": {"trades": list(trades)},
            "provenance": {
                "result_origin": "connector_response",
                "observed_at": "2026-09-17T15:59:00Z",
                "source_refs": [{
                    "kind": "uri",
                    "value": "ibkr://get_account_trades/current-cycle",
                }],
            },
        },
    ]
    upgrade_tool_calls_to_v4(data)
    calls = data["research"][0]["tool_calls"]
    data["instruction_reconciliations"] = [{
        "reconciliation_id": reconciliation_id,
        "recommendation_id": RECOMMENDATION_ID,
        "instruction_id": INSTRUCTION_ID,
        "supersedes_reconciliation_id": supersedes,
        "operator_observation": {
            "observed_at": "2026-09-17T15:57:00Z",
            "disposition": disposition,
            "app_saved_instruction_visible": app_visible,
            "quote": "Operator confirmed the saved instruction was deleted.",
        },
        "account_orders_tool_call_id": calls[0]["tool_call_id"],
        "account_trades_tool_call_id": calls[1]["tool_call_id"],
        "evidence": ["stage:research_director"],
    }]
    return data


class InstructionReconciliationValidationTests(unittest.TestCase):
    def test_deleted_saved_only_row_is_valid(self):
        data = reconciliation_input()
        self.assertEqual(
            validate_instruction_reconciliations(
                data["instruction_reconciliations"],
                data=data,
                records=[proposal_record(), operator_lifecycle_record()],
            ),
            [],
        )

    def test_historical_cycle_may_omit_reconciliation(self):
        data = valid_input()
        self.assertEqual(
            validate_instruction_reconciliations(
                None,
                data=data,
                records=[],
            ),
            [],
        )

    def test_missing_frozen_proposal_is_refused(self):
        data = reconciliation_input()
        self.assertIn(
            "instruction_reconciliation_proposal_missing:0",
            validate_instruction_reconciliations(
                data["instruction_reconciliations"],
                data=data,
                records=[],
            ),
        )

    def test_nested_cycle_time_blocks_a_late_operator_observation(self):
        data = reconciliation_input()
        data["as_of"] = "2026-09-17T16:04:00Z"
        data["snapshot"]["as_of"] = "2026-09-17T16:00:00Z"
        data["instruction_reconciliations"][0]["operator_observation"][
            "observed_at"
        ] = "2026-09-17T16:03:00Z"
        self.assertIn(
            "instruction_reconciliation_operator_invalid:0:after_cycle",
            validate_instruction_reconciliations(
                data["instruction_reconciliations"],
                data=data,
                records=[proposal_record(), operator_lifecycle_record()],
            ),
        )

    def test_account_calls_must_be_current_connector_responses(self):
        data = reconciliation_input()
        data["research"][0]["tool_calls"][0]["provenance"][
            "result_origin"
        ] = "host_summary"
        self.assertIn(
            "instruction_reconciliation_tool_invalid:"
            "0:account_orders_tool_call_id:origin",
            validate_instruction_reconciliations(
                data["instruction_reconciliations"],
                data=data,
                records=[proposal_record()],
            ),
        )


class InstructionReconciliationDerivationTests(unittest.TestCase):
    def records(self):
        return [proposal_record(), operator_lifecycle_record()]

    def test_deleted_means_saved_copy_only(self):
        data = reconciliation_input()
        payload = _payload(
            data["instruction_reconciliations"][0],
            data=data,
            records=self.records(),
        )
        self.assertEqual(payload["status"], "deleted_saved_only")
        self.assertTrue(payload["saved_instruction_deleted"])
        self.assertFalse(payload["live_order_observed"])
        self.assertIsNone(payload["live_order_deleted"])
        self.assertEqual(
            payload["proposal_provenance"],
            "recovery_restatement",
        )
        self.assertEqual(payload["prior_lifecycle_state"], "unknown")
        self.assertTrue(payload["prior_lifecycle_errors"])

    def test_exact_submitted_terms_are_accepted_unchanged(self):
        data = reconciliation_input(
            disposition="accepted",
            orders=[account_order()],
        )
        payload = _payload(
            data["instruction_reconciliations"][0],
            data=data,
            records=[proposal_record()],
        )
        self.assertEqual(payload["status"], "accepted_unchanged")
        self.assertEqual(payload["field_changes"], [])
        self.assertEqual(payload["submission_state"], "submitted")
        self.assertEqual(payload["execution_state"], "not_observed")

    def test_limit_change_is_accepted_modified(self):
        data = reconciliation_input(
            disposition="accepted",
            orders=[account_order(limit_price=9.5)],
        )
        payload = _payload(
            data["instruction_reconciliations"][0],
            data=data,
            records=[proposal_record()],
        )
        self.assertEqual(payload["status"], "accepted_modified")
        self.assertEqual(payload["field_changes"], [{
            "field": "limit_price",
            "proposed": 10.0,
            "submitted": 9.5,
        }])

    def test_symbol_only_derivative_order_never_matches(self):
        data = reconciliation_input(
            disposition="accepted",
            orders=[account_order(contract_id=None)],
        )
        payload = _payload(
            data["instruction_reconciliations"][0],
            data=data,
            records=[proposal_record()],
        )
        self.assertEqual(payload["status"], "unknown")
        self.assertFalse(payload["live_order_observed"])
        self.assertIn(
            "accepted_without_matching_account_order",
            payload["disagreements"],
        )

    def test_matching_trade_advances_execution(self):
        data = reconciliation_input(
            disposition="accepted",
            orders=[account_order()],
            trades=[account_trade()],
        )
        payload = _payload(
            data["instruction_reconciliations"][0],
            data=data,
            records=[proposal_record()],
        )
        self.assertEqual(payload["status"], "accepted_unchanged")
        self.assertTrue(payload["live_trade_observed"])
        self.assertEqual(payload["execution_state"], "executed")

    def test_rejected_with_account_activity_remains_unknown(self):
        data = reconciliation_input(
            disposition="rejected",
            orders=[account_order()],
        )
        payload = _payload(
            data["instruction_reconciliations"][0],
            data=data,
            records=[proposal_record()],
        )
        self.assertEqual(payload["status"], "unknown")
        self.assertIn(
            "rejected_with_account_activity",
            payload["disagreements"],
        )


class InstructionReconciliationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="instruction-reconcile-"))
        self.journal = AuditJournal(self.root / "journal.jsonl")
        for record in (proposal_record(), operator_lifecycle_record()):
            self.journal.append(
                record_id=record["record_id"],
                record_type=record["record_type"],
                agent="test",
                payload=dict(record["payload"]),
            )
        for record_id, record_type in (
            ("cycle-receipt:cycle-reconciliation", "cycle_receipt"),
            (
                "cycle-stage:cycle-reconciliation:research_director",
                "cycle_stage",
            ),
            ("tool-provenance:cycle-reconciliation", "tool_provenance"),
        ):
            self.journal.append(
                record_id=record_id,
                record_type=record_type,
                agent="test",
                payload={"cycle_id": "cycle-reconciliation"},
            )

    def test_reconciliation_persists_and_is_summarized(self):
        data = reconciliation_input()
        self.assertEqual(
            persist_instruction_reconciliations(
                data,
                self.journal,
                {"cycle_id": "cycle-reconciliation"},
                all_records=self.journal.read(),
            ),
            1,
        )
        record = next(
            row for row in self.journal.read()
            if row.get("record_type") == "instruction_reconciliation"
        )
        self.assertIn(
            proposal_record()["record_id"],
            record["caused_by"],
        )
        self.assertIn(
            operator_lifecycle_record()["record_id"],
            record["caused_by"],
        )
        summary = instruction_reconciliation_summary(
            self.journal.read())
        self.assertEqual(
            summary["counts_by_status"],
            {"deleted_saved_only": 1},
        )
        self.assertIsNone(summary["items"][0]["live_order_deleted"])
        self.assertTrue(self.journal.validate()["valid"])

    def test_later_evidence_requires_visible_supersession(self):
        data = reconciliation_input()
        persist_instruction_reconciliations(
            data,
            self.journal,
            {"cycle_id": "cycle-reconciliation"},
            all_records=self.journal.read(),
        )
        later = reconciliation_input(
            disposition="accepted",
            orders=[account_order()],
            reconciliation_id="reconciliation-102-later",
        )
        errors = validate_instruction_reconciliations(
            later["instruction_reconciliations"],
            data=later,
            records=self.journal.read(),
        )
        self.assertIn(
            "instruction_reconciliation_supersession_required:"
            "0:reconciliation-102-first",
            errors,
        )
        later["instruction_reconciliations"][0][
            "supersedes_reconciliation_id"
        ] = "reconciliation-102-first"
        self.assertEqual(
            validate_instruction_reconciliations(
                later["instruction_reconciliations"],
                data=later,
                records=self.journal.read(),
            ),
            [],
        )

    def test_run_one_persists_reconciliation_after_tool_provenance(self):
        data = reconciliation_input()
        path = self.root / "run-one.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        journal = AuditJournal(self.root / "run-one.jsonl")
        for record in (proposal_record(), operator_lifecycle_record()):
            journal.append(
                record_id=record["record_id"],
                record_type=record["record_type"],
                agent="test",
                payload=dict(record["payload"]),
            )
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            side_effect=lambda: journal.read(),
        ):
            run_one(path, journal)
        record = next(
            row for row in journal.read()
            if row.get("record_type") == "instruction_reconciliation"
        )
        self.assertIn(
            "tool-provenance:cycle-reconciliation",
            record["caused_by"],
        )
        self.assertEqual(record["payload"]["status"], "deleted_saved_only")

    def test_backfill_is_idempotent(self):
        data = reconciliation_input()
        path = self.root / "backfill.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        journal = AuditJournal(self.root / "backfill.jsonl")
        for record in (proposal_record(), operator_lifecycle_record()):
            journal.append(
                record_id=record["record_id"],
                record_type=record["record_type"],
                agent="test",
                payload=dict(record["payload"]),
            )
        for record_id, record_type, payload in (
            (
                "cycle-receipt:cycle-reconciliation",
                "cycle_receipt",
                {
                    "cycle_id": "cycle-reconciliation",
                    "snapshot_id": (
                        f"backfill:{input_fingerprint(data)}"
                    ),
                },
            ),
            (
                "cycle-stage:cycle-reconciliation:research_director",
                "cycle_stage",
                {"cycle_id": "cycle-reconciliation"},
            ),
            (
                "tool-provenance:cycle-reconciliation",
                "tool_provenance",
                {"cycle_id": "cycle-reconciliation"},
            ),
        ):
            journal.append(
                record_id=record_id,
                record_type=record_type,
                agent="test",
                payload=payload,
            )
        self.assertEqual(
            backfill_instruction_reconciliations(
                [path],
                journal,
                all_records=journal.read(),
            ),
            1,
        )
        self.assertEqual(
            backfill_instruction_reconciliations(
                [path],
                journal,
                all_records=journal.read(),
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
