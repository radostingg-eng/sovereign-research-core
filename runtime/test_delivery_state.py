import unittest

from .cycle_receipt import (NO_RECEIPT, RECEIPTS_NONE_VALID, VALID_RECEIPT,
                            derive_receipt_status)
from .integrity import load_journal_records

from .delivery_state import (
    main, parse_delivery_plan, reconcile_delivery_state,
    reconcile_receipt_status,
)


class DeliveryStateTests(unittest.TestCase):
    def test_parse_delivery_plan(self):
        text = "| HH-01 | Example | complete | evidence |\n| XL-01 | More | complete-with-integrity-debt | evidence |"
        parsed = parse_delivery_plan(text)
        self.assertEqual(parsed["HH-01"], "complete")
        self.assertEqual(parsed["XL-01"], "complete-with-integrity-debt")

    def test_reconcile_detects_drift(self):
        plan = "| HH-01 | Example | complete | evidence |"
        state = {"hard_haves": {"HH-01": "blocked"}}
        errors = reconcile_delivery_state(plan, state)
        self.assertEqual(errors, ["status_drift:HH-01:complete!=blocked"])

    def test_reconcile_passes_matching_state(self):
        plan = "| HH-01 | Example | complete | evidence |"
        state = {"hard_haves": {"HH-01": "complete"}}
        self.assertEqual(reconcile_delivery_state(plan, state), [])


if __name__ == "__main__":
    unittest.main()


class ReconciliationIsBidirectionalTests(unittest.TestCase):
    """Both ledgers must account for the other.

    Reconciliation only walked plan -> machine, so an EMPTY plan passed
    against any machine state at all. Dropping an item from the plan was
    therefore invisible, and dropping it is exactly how a tracked obligation
    stops being tracked.
    """

    STATE = {"hard_haves": {"HH-01": "complete", "HH-02": "complete"},
             "xl_items": {"XL-01": "complete"}}
    FULL = ("| HH-01 | desc | complete |\n"
            "| HH-02 | desc | complete |\n"
            "| XL-01 | desc | complete |\n")

    def test_an_empty_plan_no_longer_passes(self):
        errors = reconcile_delivery_state("", self.STATE)
        self.assertEqual(len(errors), 3)

    def test_an_item_dropped_from_the_plan_is_reported(self):
        without_xl = "| HH-01 | desc | complete |\n| HH-02 | desc | complete |\n"
        self.assertIn("missing_plan_entry:XL-01:complete",
                      reconcile_delivery_state(without_xl, self.STATE))

    def test_a_matching_plan_still_passes(self):
        self.assertEqual(reconcile_delivery_state(self.FULL, self.STATE), [])

    def test_drift_in_the_other_direction_is_still_caught(self):
        drifted = self.FULL.replace("| XL-01 | desc | complete |",
                                    "| XL-01 | desc | in_progress |")
        self.assertTrue(any(e.startswith("status_drift:XL-01")
                            for e in reconcile_delivery_state(drifted, self.STATE)))


class CuratedReceiptStatusMustMatchTheJournalTests(unittest.TestCase):
    """Prose outranks evidence because prose is what gets read.

    STATE.json said "migration_pending_first_persisted_receipt" while eleven
    receipt records existed, several of them valid. Nothing compared the two.
    """

    def state(self, curated):
        return {"validation": {"cycle_receipt_status": curated}}

    def test_a_curated_claim_contradicted_by_the_journal_is_refused(self):
        records = load_journal_records()
        errors = reconcile_receipt_status(
            self.state("migration_pending_first_persisted_receipt"), records)
        self.assertTrue(any(e.startswith("receipt_status_drift") for e in errors), errors)

    def test_the_committed_state_agrees_with_the_committed_journal(self):
        import json
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        state = json.loads((root / "STATE.json").read_text(encoding="utf-8"))
        self.assertEqual(reconcile_receipt_status(state, load_journal_records()), [])

    def test_a_missing_claim_is_refused_rather_than_assumed(self):
        self.assertEqual(reconcile_receipt_status({}, load_journal_records()),
                         ["missing_curated_receipt_status"])

    def test_no_receipts_derives_the_empty_status(self):
        self.assertEqual(derive_receipt_status([]), NO_RECEIPT)

    def test_receipts_that_all_fail_validation_are_not_reported_as_valid(self):
        """The distinction the status exists to make.

        "receipts exist" and "a sound receipt exists" are different facts,
        and only one of them is evidence of anything.
        """
        broken = [{"record_id": "cycle-receipt:c1", "record_type": "cycle_receipt",
                   "payload": {"cycle_id": "c1"}}]
        self.assertEqual(derive_receipt_status(broken), RECEIPTS_NONE_VALID)

    def test_the_derived_status_does_not_claim_the_cycle_is_proven(self):
        """It says a valid receipt exists, which is a far smaller claim."""
        self.assertEqual(derive_receipt_status(load_journal_records()), VALID_RECEIPT)


class DeliveryReconciliationRunsAsACommandTests(unittest.TestCase):
    def test_the_committed_ledgers_reconcile(self):
        self.assertEqual(main([]), 0)
