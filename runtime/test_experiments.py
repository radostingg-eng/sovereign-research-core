import unittest
from .experiments import (
    calibration_update, evaluate_experiment_requests, evaluate_variant,
)

class ExperimentTests(unittest.TestCase):
    def test_promotion_requires_sample_and_counter_metric(self):
        self.assertEqual(evaluate_variant(baseline=[.1]*2,variant=[.2]*2,min_samples=3).status,"blocked")
        result=evaluate_variant(baseline=[.1]*30,variant=[.2]*30,baseline_counter=[1.0]*30,variant_counter=[1.0]*30,min_samples=30)
        self.assertEqual(result.status,"promote")

    def test_calibration_stays_locked_without_oos(self):
        result=calibration_update(seed={"x":1.0},observations=[{"proposed_weights":{"x":2},"oos_samples":99}]*100,min_samples=100,min_out_of_sample=100)
        self.assertEqual(result["status"],"seed_locked")

    def test_calibration_can_reach_host_assessment_with_explicit_evidence(self):
        result=calibration_update(seed={"x":1.0},observations=[{"proposed_weights":{"x":2},"oos_samples":40}]*100,min_samples=100,min_out_of_sample=40)
        self.assertEqual(result["status"],"sample_gate_met")
        self.assertEqual(result["proposed_weights"],{"x":2.0})
        self.assertEqual(result["seed_weights"],{"x":1.0})
        self.assertNotEqual(result["status"], "learned")

if __name__ == "__main__": unittest.main()


class PromotionGateFailClosedTests(unittest.TestCase):
    """evaluate_variant authorizes autonomous self-modification.

    Four ways it used to return "promote" on evidence that does not
    support promotion. Each is a false-success path into the system
    changing its own behaviour, so each fails closed now.
    """

    NAN = float("nan")
    INF = float("inf")

    def ok(self, **over):
        kw = dict(baseline=[0.1] * 30, variant=[0.2] * 30,
                  baseline_counter=[0.0] * 30, variant_counter=[0.0] * 30,
                  min_samples=30)
        kw.update(over)
        return kw

    def test_genuine_improvement_still_promotes(self):
        # Guards against fixing the gate by breaking it.
        self.assertEqual(evaluate_variant(**self.ok()).status, "promote")

    def test_nan_primary_is_blocked_not_promoted(self):
        # IEEE 754: NaN < 0.0 is False, so the rejection branch was
        # skipped and control fell through to promote. A metric that is
        # not a number is not evidence.
        r = evaluate_variant(**self.ok(baseline=[self.NAN] * 30, variant=[self.NAN] * 30))
        self.assertEqual(r.status, "blocked")
        self.assertEqual(r.reason, "non_finite_primary_measurements")

    def test_infinite_primary_is_blocked(self):
        r = evaluate_variant(**self.ok(variant=[self.INF] * 30))
        self.assertEqual(r.status, "blocked")

    def test_nan_counter_is_blocked(self):
        r = evaluate_variant(**self.ok(variant_counter=[self.NAN] * 30))
        self.assertEqual(r.status, "blocked")
        self.assertEqual(r.reason, "non_finite_counter_measurements")

    def test_exactly_zero_gain_does_not_promote(self):
        # The test was `primary < min_primary_delta` with a 0.0 default,
        # so a change measured to do nothing was promoted.
        r = evaluate_variant(**self.ok(variant=[0.1] * 30))
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, "primary_metric_not_improved")

    def test_unequal_samples_are_refused_not_truncated(self):
        # min(len(a), len(b)) silently compared 30 baseline observations
        # against 5 variant observations.
        r = evaluate_variant(**self.ok(variant=[0.2] * 5))
        self.assertEqual(r.status, "blocked")
        self.assertIn("unequal_sample_sizes", r.reason)

    def test_unequal_counter_samples_are_refused(self):
        r = evaluate_variant(**self.ok(variant_counter=[0.0] * 5))
        self.assertEqual(r.status, "blocked")
        self.assertIn("unequal_counter_sample_sizes", r.reason)

    def test_absent_counter_metric_blocks_promotion(self):
        # SELF_IMPROVEMENT_CONTRACT.md gate 4 is "no counter-metric
        # regression". Absent data cannot satisfy it; it used to default
        # counter to 0.0 and pass the gate trivially.
        r = evaluate_variant(baseline=[0.1] * 30, variant=[0.2] * 30, min_samples=30)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(r.reason, "missing_counter_metric")

    def test_counter_requirement_can_be_waived_only_explicitly(self):
        r = evaluate_variant(baseline=[0.1] * 30, variant=[0.2] * 30,
                             min_samples=30, require_counter_metric=False)
        self.assertEqual(r.status, "promote")

    def test_counter_regression_still_rejects(self):
        r = evaluate_variant(**self.ok(variant_counter=[0.5] * 30,
                                       baseline_counter=[0.0] * 30,
                                       max_counter_regression=0.0))
        self.assertEqual(r.status, "promote")  # counter improved, not regressed
        r2 = evaluate_variant(**self.ok(baseline_counter=[0.5] * 30,
                                        variant_counter=[0.0] * 30))
        self.assertEqual(r2.status, "rejected")
        self.assertEqual(r2.reason, "counter_metric_regressed")

    def test_insufficient_sample_still_blocks(self):
        r = evaluate_variant(**self.ok(baseline=[0.1] * 5, variant=[0.2] * 5,
                                       baseline_counter=[0.0] * 5,
                                       variant_counter=[0.0] * 5))
        self.assertEqual(r.status, "blocked")
        self.assertEqual(r.reason, "insufficient_sample")

    def test_empty_input_blocks(self):
        r = evaluate_variant(baseline=[], variant=[], min_samples=0)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(r.reason, "no_outcomes")


class HostExperimentRequestTests(unittest.TestCase):
    def test_paired_evidence_is_evaluated_without_applying_it(self):
        result = evaluate_experiment_requests([{
            "baseline": [0.1] * 3,
            "variant": [0.2] * 3,
            "baseline_counter": [0.0] * 3,
            "variant_counter": [0.0] * 3,
            "min_samples": 3,
        }])[0]
        self.assertTrue(result["valid"])
        self.assertEqual(result["decision"]["status"], "promote")
        self.assertNotIn("applied", result)
