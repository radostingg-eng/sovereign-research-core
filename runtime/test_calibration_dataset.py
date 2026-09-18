import unittest

from .calibration_dataset import empirical_calibration_summary


def forecast_record(
    forecast_id,
    *,
    cycle_id,
    decision_status="recommended",
    kind="direction",
    direction="up",
    unit="currency",
):
    return {
        "record_id": f"forecast:{forecast_id}",
        "record_type": "forecast_registered",
        "payload": {
            "forecast_id": forecast_id,
            "cycle_id": cycle_id,
            "opportunity_id": "opportunity-one",
            "decision": {
                "cycle_id": cycle_id,
                "status": decision_status,
            },
            "metric": {
                "unit": unit,
                "baseline_value": 100.0,
            },
            "horizon": {
                "target_at": "2026-09-18T10:00:00Z",
                "observation_window_seconds": 3600,
            },
            "expectation": {
                "kind": kind,
                "direction": direction,
            },
            "confidence_probability": 0.7,
        },
    }


def outcome_record(forecast_id, *, resolved=1, delta=5.0,
                   invalidated=False):
    return {
        "record_id": f"forecast-outcome:{forecast_id}",
        "record_type": "forecast_outcome",
        "payload": {
            "forecast_id": forecast_id,
            "resolved_outcome": resolved,
            "delta_from_baseline": delta,
            "invalidated": invalidated,
            "confidence_probability": 0.7,
            "observed_at": "2026-09-18T10:05:00Z",
            "observed_value": 105.0,
        },
    }


def reconciliation(reconciliation_id, *, recommendation_id,
                   status="accepted_unchanged", supersedes=None,
                   execution_state="executed", field_changes=()):
    return {
        "record_id": f"instruction-reconciliation:{reconciliation_id}",
        "record_type": "instruction_reconciliation",
        "payload": {
            "reconciliation_id": reconciliation_id,
            "supersedes_reconciliation_id": supersedes,
            "recommendation_id": recommendation_id,
            "instruction_id": "102",
            "status": status,
            "submission_state": "submitted",
            "execution_state": execution_state,
            "field_changes": list(field_changes),
            "disagreements": [],
        },
    }


class EmpiricalCalibrationTests(unittest.TestCase):
    def test_directional_accepted_forecast_is_measured_without_accuracy_label(self):
        records = [
            forecast_record("f1", cycle_id="r1"),
            outcome_record("f1", resolved=1),
            reconciliation("x1", recommendation_id="r1"),
        ]
        summary = empirical_calibration_summary(records)
        directional = summary["directional_decision_metrics"]
        self.assertEqual(
            directional["accepted_direction_forecast_true_rate"],
            {
                "numerator": 1,
                "denominator": 1,
                "value": 1.0,
                "status": "denominator_present",
            },
        )
        self.assertNotIn("accuracy", str(summary).lower())

    def test_range_forecasts_never_enter_directional_metrics(self):
        records = [
            forecast_record(
                "f1",
                cycle_id="r1",
                kind="range",
                direction=None,
            ),
            outcome_record("f1", resolved=1),
            reconciliation("x1", recommendation_id="r1"),
        ]
        summary = empirical_calibration_summary(records)
        self.assertEqual(
            summary["directional_decision_metrics"][
                "accepted_direction_forecast_true_rate"
            ]["denominator"],
            0,
        )
        self.assertEqual(
            summary["forecast_outcome_calibration"]["n"],
            1,
        )

    def test_rejected_true_rate_is_not_called_trade_counterfactual(self):
        records = [
            forecast_record("f1", cycle_id="r1"),
            outcome_record("f1", resolved=1),
            reconciliation(
                "x1",
                recommendation_id="r1",
                status="rejected",
                execution_state="not_observed",
            ),
        ]
        summary = empirical_calibration_summary(records)
        metric = summary["directional_decision_metrics"][
            "rejected_direction_forecast_true_rate"
        ]
        self.assertEqual(metric["value"], 1.0)
        self.assertIn("not a fill", metric["what_this_means"])

    def test_unmatched_records_remain_visible(self):
        records = [
            forecast_record(
                "f1",
                cycle_id="research-cycle",
                decision_status="researching",
            ),
            reconciliation("x1", recommendation_id="other-cycle"),
        ]
        summary = empirical_calibration_summary(records)
        self.assertEqual(
            summary["unresolved"]["unmatched_forecasts"], 1)
        self.assertEqual(
            summary["unresolved"]["unmatched_reconciliations"], 1)

    def test_multiple_reconciliations_are_not_collapsed(self):
        records = [
            forecast_record("f1", cycle_id="r1"),
            outcome_record("f1"),
            reconciliation("x1", recommendation_id="r1"),
            reconciliation(
                "x2",
                recommendation_id="r1",
                status="rejected",
            ),
        ]
        summary = empirical_calibration_summary(records)
        row = summary["recent_rows"][0]
        self.assertEqual(len(row["reconciliation_ids"]), 2)
        self.assertTrue(row["reconciliation_conflict"])
        self.assertIsNone(row["reconciliation_status"])

    def test_timing_deltas_are_stratified_by_unit_kind_direction(self):
        records = [
            forecast_record("f1", cycle_id="r1"),
            outcome_record("f1", delta=5.0),
            forecast_record(
                "f2",
                cycle_id="r2",
                unit="percent",
                direction="down",
            ),
            outcome_record("f2", delta=-2.0),
        ]
        groups = empirical_calibration_summary(records)["timing_groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {(row["unit"], row["direction"]) for row in groups},
            {("currency", "up"), ("percent", "down")},
        )

    def test_implementation_preserves_modified_and_no_fill_counts(self):
        records = [
            reconciliation(
                "x1",
                recommendation_id="r1",
                status="accepted_modified",
                execution_state="not_observed",
                field_changes=({
                    "field": "limit_price",
                    "proposed": 10,
                    "submitted": 9.5,
                },),
            ),
        ]
        implementation = empirical_calibration_summary(records)[
            "implementation"
        ]
        self.assertEqual(
            implementation["counts_by_status"],
            {"accepted_modified": 1},
        )
        self.assertEqual(
            implementation["modification_field_counts"],
            {"limit_price": 1},
        )
        self.assertEqual(
            implementation["accepted_without_fill_count"], 1)

    def test_invalidated_forecast_stays_in_probability_calibration(self):
        records = [
            forecast_record("f1", cycle_id="r1"),
            outcome_record("f1", invalidated=True),
        ]
        summary = empirical_calibration_summary(records)
        self.assertEqual(
            summary["forecast_outcome_calibration"]["n"], 1)
        self.assertEqual(
            summary["directional_decision_metrics"][
                "invalidated_direction_rows_excluded"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
