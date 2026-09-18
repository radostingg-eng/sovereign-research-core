import unittest

from .effectiveness import (build_ex_ante_snapshot, evaluate_decision,
                            evaluate_calibration_requests, snapshot_hash,
                            verify_snapshot_integrity)


class ExAnteSnapshotFreezeTests(unittest.TestCase):
    """A frozen snapshot must actually be frozen.

    dict(portfolio) is a SHALLOW copy, so nested data stayed shared with
    the caller. Mutating the source after building the snapshot changed
    the snapshot, and left its stored snapshot_hash no longer matching its
    own content.

    That is the anti-hindsight mechanism failing open. The point of an
    ex-ante snapshot is that it cannot acquire information which arrived
    after the decision; a snapshot that keeps tracking its source can, and
    the decision then looks as though it were made on data it never saw.
    """

    def test_mutating_the_source_portfolio_does_not_change_the_snapshot(self):
        portfolio = {"positions": {"MSFT": 100}}
        snapshot = build_ex_ante_snapshot(portfolio=portfolio, evidence=[{"id": "e1"}])
        portfolio["positions"]["AAPL"] = 50
        self.assertNotIn("AAPL", str(snapshot))

    def test_mutating_source_evidence_does_not_change_the_snapshot(self):
        evidence = [{"id": "e1", "claims": ["a"]}]
        snapshot = build_ex_ante_snapshot(portfolio={}, evidence=evidence)
        evidence[0]["claims"].append("arrived later")
        self.assertNotIn("arrived later", str(snapshot))

    def test_mutating_source_research_trace_does_not_change_the_snapshot(self):
        trace = [{"step": 1, "notes": ["a"]}]
        snapshot = build_ex_ante_snapshot(portfolio={}, evidence=[], research_trace=trace)
        trace[0]["notes"].append("arrived later")
        self.assertNotIn("arrived later", str(snapshot))

    def test_the_snapshot_still_hashes_to_its_stored_value_afterwards(self):
        # The load-bearing one. A snapshot whose content drifts away from
        # its own hash is worse than no snapshot: it looks verified.
        portfolio = {"positions": {"MSFT": 100}}
        snapshot = build_ex_ante_snapshot(portfolio=portfolio, evidence=[])
        portfolio["positions"]["AAPL"] = 50
        self.assertEqual(verify_snapshot_integrity(snapshot), [])


class SnapshotIntegrityTests(unittest.TestCase):
    """Freezing is only meaningful if something checks."""

    def snapshot(self):
        return build_ex_ante_snapshot(portfolio={"positions": {"MSFT": 100}},
                                      evidence=[{"id": "e1"}])

    def test_an_untouched_snapshot_verifies(self):
        self.assertEqual(verify_snapshot_integrity(self.snapshot()), [])

    def test_a_tampered_portfolio_is_detected(self):
        tampered = dict(self.snapshot())
        tampered["portfolio"] = {"positions": {"HACKED": 1}}
        self.assertIn("snapshot_hash_mismatch", verify_snapshot_integrity(tampered))

    def test_tampered_evidence_is_detected(self):
        tampered = dict(self.snapshot())
        tampered["evidence"] = [{"id": "e1", "conclusion": "added after the fact"}]
        self.assertIn("snapshot_hash_mismatch", verify_snapshot_integrity(tampered))

    def test_a_snapshot_with_no_hash_is_reported(self):
        self.assertEqual(verify_snapshot_integrity({"portfolio": {}}),
                         ["missing_snapshot_hash"])

    def test_the_hash_covers_content_and_not_itself(self):
        snapshot = self.snapshot()
        body = {k: v for k, v in snapshot.items() if k != "snapshot_hash"}
        self.assertEqual(snapshot_hash(body), snapshot["snapshot_hash"])


class MeasuredRequiresAMeasurementTests(unittest.TestCase):
    """"measured" used to mean only that attribution held.

    An outcome carrying nothing but ids reported measured with
    realized_return, realized_risk, benchmark_return, opportunity_regret and
    process_score all None: a measurement record containing no measurement.
    Downstream that reads as evidence, which is how insufficient data becomes
    a confident-looking row.
    """

    DECISION = {"decision_id": "d1", "snapshot_hash": "h1",
                "as_of": "2026-01-01T00:00:00Z"}
    BARE = {"outcome_id": "o1", "decision_id": "d1", "snapshot_hash": "h1"}

    def status(self, **extra):
        return evaluate_decision(decision=self.DECISION,
                                 outcome={**self.BARE, **extra}).status

    def test_ids_alone_are_not_a_measurement(self):
        self.assertEqual(self.status(), "attributed_unmeasured")

    def test_a_realized_return_is_a_measurement(self):
        self.assertEqual(self.status(realized_return=0.2), "measured")

    def test_a_process_trace_is_a_measurement(self):
        self.assertEqual(self.status(process_trace=[{"process_ok": 1}]), "measured")

    def test_unmeasured_is_distinct_from_blocked(self):
        """Blocked means the link does not hold; unmeasured means it does.

        Collapsing them would hide which problem the operator has.
        """
        broken = evaluate_decision(decision={**self.DECISION, "realized_return": 0.9},
                                   outcome=self.BARE)
        self.assertEqual(broken.status, "blocked")
        self.assertEqual(self.status(), "attributed_unmeasured")

    def test_a_boolean_is_not_a_measurement(self):
        """True is not a realized return.

        bool is a subclass of int, so an unguarded numeric check accepts it
        and float(True) becomes 1.0 -- nonsense data promoted to evidence.
        """
        self.assertEqual(self.status(realized_return=True), "attributed_unmeasured")
        self.assertEqual(self.status(realized_risk=False), "attributed_unmeasured")

    def test_attribution_is_still_reported_as_complete_when_unmeasured(self):
        result = evaluate_decision(decision=self.DECISION, outcome=self.BARE)
        self.assertTrue(result.attribution_complete)


class HostCalibrationRequestsTests(unittest.TestCase):
    def test_calibration_is_computed_without_changing_weights(self):
        result = evaluate_calibration_requests([{
            "observations": [
                {"probability": 0.8, "outcome": 1},
                {"probability": 0.2, "outcome": 0},
            ],
            "min_samples": 10,
            "bins": 5,
        }])[0]
        self.assertTrue(result["valid"])
        self.assertEqual(result["summary"]["status"], "insufficient_data")
        self.assertIsNotNone(result["summary"]["brier"])
        self.assertNotIn("weights", result["summary"])

    def test_sample_count_never_calls_calibration_learned(self):
        result = evaluate_calibration_requests([{
            "observations": [
                {"probability": 0.8, "outcome": 1}
                for _ in range(100)
            ],
            "min_samples": 100,
        }])[0]
        self.assertEqual(result["summary"]["status"], "sample_gate_met")
        self.assertNotEqual(result["summary"]["status"], "learned")
