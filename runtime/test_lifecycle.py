import unittest

from .lifecycle import apply_events, outcome_record, transition


class LifecycleTests(unittest.TestCase):
    def test_unexecuted_instruction_is_not_rejection(self):
        events = [
            transition(None, "candidate", recommendation_id="r1", event_id="e1"),
            transition("candidate", "recommended", recommendation_id="r1", event_id="e2"),
            transition("recommended", "instruction_created", recommendation_id="r1", event_id="e3"),
            transition("instruction_created", "unknown", recommendation_id="r1", event_id="e4"),
        ]
        self.assertEqual(apply_events(events)["r1"], "unknown")
        self.assertEqual(outcome_record(recommendation_id="r1", outcome_id="o1", engagement="unknown", execution_state="unknown")["engagement"], "unknown")

    def test_illegal_transition_fails_closed(self):
        with self.assertRaises(ValueError):
            transition("candidate", "closed", recommendation_id="r1", event_id="e1")

    def test_outcome_preserves_counterfactual_and_thesis(self):
        row = outcome_record(recommendation_id="r1", outcome_id="o1", engagement="executed", execution_state="closed", realized_return=0.12, experienced_risk=0.08, holding_period_days=120, counterfactual_result={"hold":0.07}, thesis_result="validated")
        self.assertEqual(row["counterfactual_result"], {"hold": 0.07})
        self.assertEqual(row["thesis_result"], "validated")

    def test_approval_can_precede_execution(self):
        events = [
            transition(
                None, "candidate", recommendation_id="r1", event_id="e1"),
            transition(
                "candidate", "recommended",
                recommendation_id="r1", event_id="e2"),
            transition(
                "recommended", "instruction_created",
                recommendation_id="r1", event_id="e3"),
            transition(
                "instruction_created", "approved",
                recommendation_id="r1", event_id="e4"),
            transition(
                "approved", "executed",
                recommendation_id="r1", event_id="e5"),
        ]
        self.assertEqual(apply_events(events)["r1"], "executed")

    def test_approval_is_a_valid_observed_outcome(self):
        row = outcome_record(
            recommendation_id="r1",
            outcome_id="o1",
            engagement="approved",
            execution_state="approved",
        )
        self.assertEqual(row["engagement"], "approved")

    def test_submission_and_deletion_are_distinct_outcomes(self):
        submitted = transition(
            "instruction_created",
            "submitted",
            recommendation_id="r1",
            event_id="submitted-1",
        )
        deleted = transition(
            "instruction_created",
            "deleted",
            recommendation_id="r2",
            event_id="deleted-1",
        )
        self.assertEqual(submitted.to_state, "submitted")
        self.assertEqual(deleted.to_state, "deleted")
        self.assertEqual(
            outcome_record(
                recommendation_id="r2",
                outcome_id="o2",
                engagement="deleted",
                execution_state="deleted",
            )["execution_state"],
            "deleted",
        )


if __name__ == "__main__":
    unittest.main()
