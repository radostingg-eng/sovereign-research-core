"""Three cycles in thirty minutes, one trade, nothing resolved."""

import unittest

from .recommendations import open_recommendations, summarise


def receipt(cycle_id, *, status="recommended", supersedes=(), at="2026-09-16T10:00:00Z"):
    return {"record_type": "cycle_receipt",
            "payload": {"cycle_id": cycle_id, "decision_status": status,
                        "completed_at": at, "supersedes": list(supersedes)}}


def lifecycle(recommendation_id, to_state, *, from_state="recommended"):
    return {"record_type": "lifecycle_event",
            "caused_by": [f"cycle-receipt:{recommendation_id}"],
            "payload": {"recommendation_id": recommendation_id,
                        "event_id": f"event:{recommendation_id}:{to_state}",
                        "from_state": from_state,
                        "to_state": to_state}}


class AccumulationIsVisibleTests(unittest.TestCase):
    """The host reads FEEDBACK.json at the start of every run, and it said
    nothing about what the host had already proposed. So each cycle proposed
    the same trade again, at a new price, with no mention of the others."""

    def test_every_unresolved_recommendation_is_open(self):
        rows = open_recommendations([receipt("a"), receipt("b"), receipt("c")])
        self.assertEqual([r["cycle_id"] for r in rows], ["a", "b", "c"])

    def test_a_wait_is_not_a_recommendation(self):
        self.assertEqual(open_recommendations([receipt("a", status="wait")]), [])

    def test_superseding_closes_the_earlier_one(self):
        rows = open_recommendations([receipt("a"),
                                     receipt("b", supersedes=["a"])])
        self.assertEqual([r["cycle_id"] for r in rows], ["b"])

    def test_superseding_several_at_once(self):
        rows = open_recommendations([receipt("a"), receipt("b"),
                                     receipt("c", supersedes=["a", "b"])])
        self.assertEqual([r["cycle_id"] for r in rows], ["c"])


class ResolutionClosesTheObligationTests(unittest.TestCase):

    def test_execution_closes_it(self):
        self.assertEqual(
            open_recommendations([
                receipt("a"),
                lifecycle("a", "instruction_created"),
                lifecycle("a", "executed", from_state="instruction_created"),
            ]), [])

    def test_an_illegal_direct_execution_does_not_close_it(self):
        rows = open_recommendations([
            receipt("a"), lifecycle("a", "executed")])
        self.assertEqual([row["cycle_id"] for row in rows], ["a"])

    def test_rejection_closes_it(self):
        """Declining to trade is a real answer, not an unresolved one."""
        self.assertEqual(
            open_recommendations([receipt("a"), lifecycle("a", "rejected")]), [])

    def test_expiry_closes_it(self):
        self.assertEqual(
            open_recommendations([receipt("a"), lifecycle("a", "expired")]), [])

    def test_unknown_does_NOT_close_it(self):
        """An engagement nobody could establish is still an obligation.

        Treating unknown as resolved would quietly discharge exactly the
        cases that most need a human to look at them.
        """
        rows = open_recommendations([receipt("a"), lifecycle("a", "unknown")])
        self.assertEqual([r["cycle_id"] for r in rows], ["a"])

    def test_instruction_created_does_not_close_it_either(self):
        """The order exists but has not filled. Still outstanding."""
        rows = open_recommendations(
            [receipt("a"), lifecycle("a", "instruction_created")])
        self.assertEqual([r["cycle_id"] for r in rows], ["a"])

    def test_reconciliation_closes_operator_decision(self):
        reconciliation = {
            "record_type": "instruction_reconciliation",
            "payload": {
                "recommendation_id": "a",
                "status": "deleted_saved_only",
            },
        }
        self.assertEqual(
            open_recommendations([receipt("a"), reconciliation]),
            [],
        )


class TheSummaryTellsTheHostWhatToDoTests(unittest.TestCase):

    def test_it_names_the_supersedes_field(self):
        summary = summarise([receipt("a")])
        self.assertIn("supersedes", summary["what_this_means"])

    def test_it_does_not_forbid_a_repriced_recommendation(self):
        """A repriced recommendation an hour later may be entirely correct.

        Suppressing duplicates would hide a real change of mind; the host
        must account for the earlier one, not be prevented from replacing it.
        """
        summary = summarise([receipt("a")])
        self.assertIn("Reaffirming", summary["what_this_means"])

    def test_an_empty_board_says_so(self):
        summary = summarise([receipt("a", status="wait")])
        self.assertEqual(summary["count"], 0)
        self.assertIn("No open recommendations", summary["what_this_means"])


def staged_receipt(cycle_id, instruction_id="ibkr-1"):
    return {"record_type": "cycle_receipt",
            "payload": {"cycle_id": cycle_id, "decision_status": "recommended",
                        "completed_at": "2026-09-16T10:00:00Z",
                        "instruction_staged": True,
                        "ibkr_instruction_id": instruction_id,
                        "supersedes": []}}


class AStagedInstructionIsADifferentObligationTests(unittest.TestCase):
    """A staged instruction exists in IBKR. The operator can see it, and it
    can still be transmitted days later on a thesis that has since broken.

    A bare recommendation exists only in a JSON file. The host can DELETE the
    staged ones, so it has to be able to tell them apart."""

    def test_a_staged_instruction_is_flagged(self):
        rows = open_recommendations([staged_receipt("a")])
        self.assertTrue(rows[0]["staged_in_ibkr"])
        self.assertEqual(rows[0]["ibkr_instruction_id"], "ibkr-1")

    def test_a_bare_recommendation_is_not(self):
        rows = open_recommendations([receipt("a")])
        self.assertFalse(rows[0]["staged_in_ibkr"])

    def test_a_verified_create_event_marks_the_recommendation_staged(self):
        event = {
            "record_type": "order_instruction_event",
            "payload": {
                "cycle_id": "a",
                "operation": "create",
                "verified_present": True,
                "instruction_id": "ibkr-2",
                "instruction": {"action": "BUY", "symbol": "XYZ"},
            },
        }
        rows = open_recommendations([receipt("a"), event])
        self.assertTrue(rows[0]["staged_in_ibkr"])
        self.assertEqual(rows[0]["ibkr_instruction_id"], "ibkr-2")

    def test_the_guidance_tells_the_host_to_delete_stale_ones(self):
        text = summarise([staged_receipt("a")])["what_this_means"]
        self.assertIn("DELETE", text)
        self.assertIn("staged_in_ibkr", text)

    def test_superseding_a_staged_one_still_closes_it(self):
        rows = open_recommendations([staged_receipt("a"),
                                     receipt("b", supersedes=["a"])])
        self.assertEqual([r["cycle_id"] for r in rows], ["b"])
