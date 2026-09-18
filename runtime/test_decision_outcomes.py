"""Thirteen decisions, none graded, while the evidence sat in the repo."""

import unittest

from .decision_outcomes import (
    observable_state,
    summarise,
    ungraded_decisions,
)


def cycle(as_of, status, *, nlv=None, msft=None, nested=False):
    snapshot = {}
    if nlv is not None:
        snapshot["account_summary" if nested else "net_liquidation"] = (
            {"net_liquidation": nlv} if nested else nlv)
    if msft is not None:
        if nested:
            snapshot["attention_positions"] = [
                {"symbol": "MSFT", "market_value": msft}]
        else:
            snapshot["positions"] = {"MSFT": {"market_value": msft}}
    return {"as_of": as_of, "snapshot": snapshot,
            "decision": {"status": status, "rationale": "because"}}


class AWaitIsADecisionWithAConsequenceTests(unittest.TestCase):
    """Declining to trim a position is a position held. An hour later it is
    worth something different, and that is attributable in exactly the way a
    fill would be."""

    def test_a_wait_is_paired_with_what_followed(self):
        rows = ungraded_decisions([cycle("t1", "wait", nlv=100.0, msft=50.0),
                                   cycle("t2", "wait", nlv=110.0, msft=55.0)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "wait")
        self.assertEqual(rows[0]["net_liquidation_change_pct"], 10.0)
        self.assertEqual(rows[0]["position_change_pct"]["MSFT"], 10.0)

    def test_the_latest_cycle_is_not_graded(self):
        """Nothing has happened after it, so there is nothing to compare."""
        rows = ungraded_decisions([cycle("t1", "wait", nlv=100.0),
                                   cycle("t2", "wait", nlv=110.0)])
        self.assertEqual([r["decided_at"] for r in rows], ["t1"])

    def test_one_cycle_cannot_be_graded_at_all(self):
        self.assertEqual(ungraded_decisions([cycle("t1", "wait", nlv=100.0)]), [])

    def test_a_missing_value_does_not_invent_a_change(self):
        rows = ungraded_decisions([cycle("t1", "wait"), cycle("t2", "wait")])
        self.assertIsNone(rows[0]["net_liquidation_change_pct"])
        self.assertEqual(rows[0]["position_change_pct"], {})


class BothSnapshotShapesAreReadTests(unittest.TestCase):
    """attention_positions and account_summary are what the host actually
    sends; positions and net_liquidation are what the worked example shows.
    Declaring one correct would have refused real data."""

    def test_the_nested_shape_the_host_uses(self):
        rows = ungraded_decisions([
            cycle("t1", "wait", nlv=100.0, msft=50.0, nested=True),
            cycle("t2", "wait", nlv=105.0, msft=52.5, nested=True)])
        self.assertEqual(rows[0]["net_liquidation_change_pct"], 5.0)
        self.assertEqual(rows[0]["position_change_pct"]["MSFT"], 5.0)

    def test_the_flat_shape_the_example_shows(self):
        state = observable_state(cycle("t1", "wait", nlv=100.0, msft=50.0))
        self.assertEqual(state["net_liquidation"], 100.0)
        self.assertEqual(state["positions"]["MSFT"], 50.0)

    def test_nested_snapshot_time_is_preserved(self):
        data = cycle("top", "wait", nlv=100.0, msft=50.0, nested=True)
        data["snapshot"]["as_of"] = "nested"
        state = observable_state(data)
        self.assertEqual(state["as_of"], "nested")


class TheRuntimeRefusesToScoreTests(unittest.TestCase):
    """A WAIT that avoided a loss and a WAIT that missed a gain look identical
    over one hour on the one path that happened. Scoring them would encode a
    view of what good looks like, which is the host's work."""

    def test_no_verdict_field_is_produced(self):
        rows = ungraded_decisions([cycle("t1", "wait", nlv=100.0),
                                   cycle("t2", "wait", nlv=110.0)])
        for banned in ("correct", "score", "grade", "verdict", "good"):
            self.assertNotIn(banned, rows[0])

    def test_the_guidance_says_the_numbers_are_not_a_verdict(self):
        text = summarise([cycle("t1", "wait", nlv=100.0),
                          cycle("t2", "wait", nlv=110.0)])["what_this_means"]
        self.assertIn("not say whether a", text.lower())
        self.assertIn("premature", text)
