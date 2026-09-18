import unittest
from unittest.mock import patch

from .delivery_acceptance import (
    FULL_CYCLE_STAGES, acceptance_status, blocking_reconciliation_errors,
    live_proofs,
    reconcile_live_proofs,
)
from .tool_inventory import (
    NONTRANSMITTING_WRITE_ACTIONS,
    REQUIRED_IBKR_ACTIONS,
    WRITE_ACTIONS,
)


def receipt(*, full=True):
    stages = FULL_CYCLE_STAGES if full else {"portfolio", "decision"}
    return {
        "record_id": "cycle-receipt:c1",
        "record_type": "cycle_receipt",
        "agent": "sovereign-host",
        "payload": {
            "mode": "production-host-full-cycle",
            "status": "completed",
            "stages": [
                {"stage_id": stage, "status": "completed"}
                for stage in stages
            ],
        },
    }


def memory_records(*, admitted=True, status="active"):
    return [
        receipt(),
        {
            "record_id": "memory-distillation:d1",
            "record_type": "memory_distillation",
            "agent": "sovereign-host",
            "caused_by": ["cycle-receipt:c1"],
            "payload": {
                "evaluation": {"admitted": admitted},
            },
        },
        {
            "record_id": "memory:m1",
            "record_type": "memory",
            "agent": "sovereign-host",
            "caused_by": ["memory-distillation:d1"],
            "payload": {
                "memory_id": "m1",
                "status": status,
                "reconstruction_status": "passed",
            },
        },
    ]


def instruction_records(*, verified=True):
    return [
        receipt(),
        {
            "record_id": "order-instruction:c1:0:create",
            "record_type": "order_instruction_event",
            "agent": "sovereign-host",
            "caused_by": ["cycle-receipt:c1"],
            "payload": {
                "cycle_id": "c1",
                "decision_status": "recommended",
                "operation": "create",
                "instruction_id": "ibkr-1",
                "instruction": {
                    "rationale_one_line": "Bounded evidence-backed action.",
                    "review_condition": "Review before transmission.",
                    "rollback_condition": "Do not transmit if evidence changes.",
                },
                "verified_present": verified,
                "order_submission_used": False,
            },
        },
    ]


def tool_inventory_records(*, complete=True):
    return [
        receipt(),
        {
            "record_id": "tool-inventory:c1",
            "record_type": "tool_inventory",
            "agent": "sovereign-host",
            "caused_by": ["cycle-receipt:c1"],
            "payload": {
                "observed_at": "2026-09-16T22:34:48Z",
                "complete_for_current_session": complete,
                "connectors": [{
                    "name": "Interactive Brokers (IBKR)",
                    "actions": [{
                        "name": name,
                        "inputs": [],
                        "returns": "result",
                        "mode": (
                            "write_nontransmitting"
                            if name in NONTRANSMITTING_WRITE_ACTIONS
                            else "write"
                            if name in WRITE_ACTIONS
                            else "read"
                        ),
                    } for name in sorted(REQUIRED_IBKR_ACTIONS)],
                }],
                "manifest_discrepancies": [],
                "unreachable_manifest_connectors": [],
            },
        },
    ]


def market_session_records():
    sessions = {
        "observed_at": "2026-09-16T14:00:00Z",
        "markets": [
            {
                "region": "EU",
                "venue": "XETRA",
                "timezone": "Europe/Berlin",
                "local_time": "2026-09-16T16:00:00+02:00",
                "status": "open",
                "is_open": True,
                "next_open": "2026-09-17T09:00:00+02:00",
                "next_close": "2026-09-16T17:30:00+02:00",
                "evidence": [{
                    "tool": "official Xetra calendar",
                    "result": {"is_open": True},
                }],
            },
            {
                "region": "US",
                "venue": "NYSE",
                "timezone": "America/New_York",
                "local_time": "2026-09-16T10:00:00-04:00",
                "status": "open",
                "is_open": True,
                "next_open": "2026-09-17T09:30:00-04:00",
                "next_close": "2026-09-16T16:00:00-04:00",
                "evidence": [{
                    "tool": "Alpaca get clock",
                    "result": {"is_open": True},
                }],
            },
        ],
        "overlap": "both_open",
    }
    return [
        receipt(),
        {
            "record_id": "market-sessions:c1",
            "record_type": "market_sessions",
            "agent": "sovereign-host",
            "caused_by": ["cycle-receipt:c1"],
            "payload": sessions,
        },
    ]


class LiveProofTests(unittest.TestCase):
    def test_no_records_means_all_proofs_are_missing(self):
        self.assertEqual(
            live_proofs([]),
            {
                "full_cycle_receipt": False,
                "active_memory": False,
                "staged_order_instruction": False,
                "complete_tool_inventory": False,
                "market_session_awareness": False,
            },
        )

    def test_a_full_receipt_closes_only_its_probe(self):
        self.assertEqual(
            live_proofs([receipt()]),
            {
                "full_cycle_receipt": True,
                "active_memory": False,
                "staged_order_instruction": False,
                "complete_tool_inventory": False,
                "market_session_awareness": False,
            },
        )

    def test_a_partial_receipt_is_not_full_cycle_proof(self):
        self.assertFalse(live_proofs([receipt(full=False)])[
            "full_cycle_receipt"])

    def test_admitted_memory_closes_its_probe(self):
        self.assertTrue(live_proofs(memory_records())["active_memory"])

    def test_stale_memory_does_not(self):
        self.assertFalse(
            live_proofs(memory_records(status="stale"))["active_memory"])

    def test_bare_active_memory_record_is_not_proof(self):
        self.assertFalse(
            live_proofs([memory_records()[-1]])["active_memory"])

    def test_rejected_distillation_is_not_proof(self):
        self.assertFalse(
            live_proofs(memory_records(admitted=False))["active_memory"])

    def test_broken_causal_link_is_not_proof(self):
        records = memory_records()
        records[-1]["caused_by"] = ["memory-distillation:missing"]
        self.assertFalse(live_proofs(records)["active_memory"])

    def test_verified_staged_instruction_closes_its_probe(self):
        self.assertTrue(
            live_proofs(instruction_records())["staged_order_instruction"])

    def test_unverified_create_is_not_instruction_proof(self):
        self.assertFalse(
            live_proofs(
                instruction_records(verified=False))["staged_order_instruction"])

    def test_bare_instruction_event_is_not_proof(self):
        self.assertFalse(
            live_proofs(
                [instruction_records()[-1]])["staged_order_instruction"])

    def test_complete_tool_inventory_closes_its_probe(self):
        self.assertTrue(
            live_proofs(tool_inventory_records())["complete_tool_inventory"])

    def test_incomplete_tool_inventory_is_not_proof(self):
        self.assertFalse(
            live_proofs(
                tool_inventory_records(complete=False)
            )["complete_tool_inventory"])

    def test_source_backed_market_sessions_close_their_probe(self):
        self.assertTrue(
            live_proofs(
                market_session_records())["market_session_awareness"])


class LedgerClaimsFollowLiveProofTests(unittest.TestCase):
    PLAN = (
        "| HH-08 | memory | in-progress-live-proof |\n"
        "| XL-11 | memory | in-progress-live-proof |\n"
        "| XL-02 | adversarial | in-progress-live-proof |\n"
        "| EFF-05 | meta | in-progress-live-proof |\n"
        "| HH-13 | instruction | in-progress-live-proof |\n"
        "| HH-14 | tools | in-progress-live-proof |\n"
        "| HH-15 | sessions | in-progress-live-proof |\n"
    )

    def test_current_missing_proofs_match_in_progress_status(self):
        self.assertEqual(reconcile_live_proofs(self.PLAN, []), [])

    def test_complete_without_proof_is_refused(self):
        plan = self.PLAN.replace(
            "| XL-02 | adversarial | in-progress-live-proof |",
            "| XL-02 | adversarial | complete |",
        )
        self.assertIn(
            "complete_without_live_proof:XL-02:full_cycle_receipt",
            reconcile_live_proofs(plan, []),
        )

    def test_landed_proof_requires_the_ledger_to_advance(self):
        errors = reconcile_live_proofs(self.PLAN, memory_records())
        self.assertIn(
            "live_proof_landed_but_status_stale:XL-02:full_cycle_receipt",
            errors,
        )
        self.assertIn(
            "live_proof_landed_but_status_stale:HH-08:active_memory",
            errors,
        )

    def test_landed_proof_staleness_is_a_nonblocking_ci_warning(self):
        errors = [
            "live_proof_landed_but_status_stale:HH-13:"
            "staged_order_instruction",
        ]
        self.assertEqual(blocking_reconciliation_errors(errors), [])

    def test_strict_closeout_still_blocks_stale_ledgers(self):
        errors = [
            "live_proof_landed_but_status_stale:HH-13:"
            "staged_order_instruction",
        ]
        self.assertEqual(
            blocking_reconciliation_errors(errors, strict_stale=True),
            errors,
        )

    def test_false_completion_always_blocks(self):
        errors = [
            "complete_without_live_proof:HH-13:staged_order_instruction",
        ]
        self.assertEqual(blocking_reconciliation_errors(errors), errors)

    def test_feedback_names_both_actions(self):
        status = acceptance_status([])
        self.assertIn("full_cycle_receipt", status["instructions"])
        self.assertIn("active_memory", status["instructions"])
        self.assertIn("staged_order_instruction", status["instructions"])
        self.assertIn("complete_tool_inventory", status["instructions"])
        self.assertIn("market_session_awareness", status["instructions"])
        self.assertIn(
            "next cycle",
            status["instructions"]["active_memory"],
        )
        contract = status["contracts"]["active_memory"]
        self.assertEqual(contract["top_level_key"], "memory_distillation")
        self.assertIn("active_brain_proposals", contract["envelope_fields"])
        self.assertEqual(
            contract["required_proposal_values"]["reconstruction_status"],
            "passed",
        )
        self.assertTrue(contract["additive_to_schema_v3_cycle"])
        self.assertEqual(contract["field_types"]["ex_post_material"], "list")
        self.assertEqual(
            contract["memory_object_enums"]["reconstruction_status"],
            ["not_run", "passed", "failed", "blocked"],
        )
        self.assertEqual(
            contract["retirement_fields"],
            ["memory_id", "status", "reason", "source_ids"],
        )
        self.assertEqual(
            contract["retirement_statuses"],
            ["stale", "archived"],
        )
        instruction_contract = status["contracts"]["staged_order_instruction"]
        self.assertEqual(
            instruction_contract["required_operations"], ["create", "get"])
        self.assertIn(
            "order_submission_used remains false",
            instruction_contract["safety_boundary"],
        )
        self.assertEqual(
            instruction_contract["required_instruction_fields"],
            [
                "action",
                "quantity",
                "order_type",
                "time_in_force",
                "rationale_one_line",
                "review_condition",
                "rollback_condition",
            ],
        )
        self.assertIn(
            "contract_description",
            instruction_contract["identity_fields_any_of"],
        )
        self.assertEqual(
            instruction_contract["plugin_aliases_are_not_committed_fields"][
                "tif"],
            "time_in_force",
        )
        recovery = instruction_contract["known_recovery_source"]
        self.assertEqual(recovery["instruction_id"], "102")
        self.assertEqual(
            recovery["source_cycle_id"],
            "cycle-20260917T000356Z-r27s3",
        )
        self.assertEqual(
            recovery["source_file"],
            "host_input/cycle-20260917T000356Z-r27s3.json",
        )
        self.assertEqual(
            recovery["source_sha256"],
            "8b61783cea4942a865df80face57f4270"
            "e4df4e78d7de11e0daf6b03d62b5bff",
        )
        self.assertIn(
            "fresh current-session get",
            recovery["reuse_rule"],
        )
        inventory_contract = status["contracts"]["complete_tool_inventory"]
        self.assertIn(
            "get account trades", inventory_contract["known_ibkr_minimum"])
        self.assertIn("mode", inventory_contract["action_fields"])
        sessions_contract = status["contracts"]["market_session_awareness"]
        self.assertEqual(
            sessions_contract["required_regions"], ["EU", "US"])
        self.assertIn("both_open", sessions_contract["overlap_values"])

    @patch("runtime.delivery_acceptance.live_proofs")
    def test_completed_probes_omit_recurring_contracts(self, mocked):
        mocked.return_value = {
            "full_cycle_receipt": True,
            "active_memory": True,
            "staged_order_instruction": True,
            "complete_tool_inventory": True,
            "market_session_awareness": True,
        }
        status = acceptance_status([])
        self.assertEqual(status["missing"], [])
        self.assertEqual(status["instructions"], {})
        self.assertEqual(status["contracts"], {})

    @patch("runtime.delivery_acceptance.live_proofs")
    def test_only_missing_probe_keeps_its_guidance(self, mocked):
        mocked.return_value = {
            "full_cycle_receipt": True,
            "active_memory": False,
            "staged_order_instruction": True,
            "complete_tool_inventory": True,
            "market_session_awareness": True,
        }
        status = acceptance_status([])
        self.assertEqual(status["missing"], ["active_memory"])
        self.assertEqual(
            list(status["instructions"]), ["active_memory"])
        self.assertEqual(list(status["contracts"]), ["active_memory"])


if __name__ == "__main__":
    unittest.main()
