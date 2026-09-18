import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .audit_store import AuditJournal
from .forecast_outcomes import (
    backfill_forecast_outcomes,
    forecast_outcome_summary,
    persist_forecast_outcomes,
    validate_forecast_outcomes,
)
from .forecasts import (
    forecast_ledger_summary,
    overdue_forecast_ids,
    validate_forecast_registrations,
)
from .test_forecasts import forecast
from .test_research_allocation import valid_input


FORECAST_ID = "forecast-vrt-price-short"


def forecast_payload(**overrides):
    row = forecast(
        forecast_id=FORECAST_ID,
        horizon={
            "label": "short measurement horizon",
            "target_at": "2026-09-17T15:50:00Z",
            "observation_window_seconds": 3600,
        },
    )
    row.update(overrides)
    return {
        "schema_version": 1,
        "forecast_id": row["forecast_id"],
        "cycle_id": "cycle-registration",
        "registered_at": "2026-09-17T15:00:00Z",
        "opportunity_id": row["opportunity_id"],
        "opportunity": {
            "opportunity_id": row["opportunity_id"],
            "record_id": "opportunity-event:vrt-new",
            "discovered_at": "2026-09-17T14:00:00Z",
            "state_at_registration": "researching",
        },
        "supersedes_forecast_id": row["supersedes_forecast_id"],
        "thesis": row["thesis"],
        "metric": {
            **row["metric"],
            "baseline_age_seconds": 0,
        },
        "horizon": row["horizon"],
        "expectation": {
            **row["expectation"],
            "resolution_rule": "strictly_above_baseline_ties_false",
        },
        "confidence_probability": row["confidence_probability"],
        "benchmark": row["benchmark"],
        "entry_context": row["entry_context"],
        "risk_assumptions": row["risk_assumptions"],
        "portfolio_context": row["portfolio_context"],
        "invalidation_condition": row["invalidation_condition"],
        "evidence": row["evidence"],
        "evidence_record_ids": [
            "cycle-stage:cycle-registration:research_director",
        ],
        "decision": {
            "cycle_id": "cycle-registration",
            "status": "researching",
            "record_id": "cycle-stage:cycle-registration:decision",
            "snapshot_hash": "a" * 64,
        },
    }


def forecast_record(**overrides):
    payload = forecast_payload(**overrides)
    return {
        "record_id": f"forecast:{payload['forecast_id']}",
        "record_type": "forecast_registered",
        "payload": payload,
    }


def outcome_input(*, observed_at="2026-09-17T16:01:00Z",
                  value=130.0, origin="connector_response",
                  stable_ref="ibkr://price/VRT"):
    data = valid_input()
    data["cycle_id"] = "cycle-outcome"
    data["as_of"] = "2026-09-17T16:02:00Z"
    data["snapshot"]["as_of"] = "2026-09-17T16:02:00Z"
    sessions = data["market_sessions"]
    sessions["observed_at"] = "2026-09-17T16:02:00Z"
    sessions["markets"][0]["local_time"] = (
        "2026-09-17T18:02:00+02:00"
    )
    sessions["markets"][1]["local_time"] = (
        "2026-09-17T12:02:00-04:00"
    )
    call = data["research"][0]["tool_calls"][0]
    call["tool"] = "Interactive Brokers (IBKR)"
    call["result"] = {"last": value}
    call["provenance"] = {
        "result_origin": origin,
        "observed_at": observed_at,
        "source_refs": [{"kind": "uri", "value": stable_ref}],
    }
    data["forecast_outcomes"] = [{
        "forecast_id": FORECAST_ID,
        "tool_call_id": "research:0:0",
        "invalidation_reason": None,
        "evidence": ["stage:research_director"],
    }]
    data["forecast_registrations"] = []
    data["opportunity_updates"] = []
    return data


class ForecastOutcomeValidationTests(unittest.TestCase):
    def test_valid_connector_outcome_is_accepted(self):
        data = outcome_input()
        self.assertEqual(
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
            [],
        )

    def test_host_cannot_supply_an_observed_value_or_grade(self):
        data = outcome_input()
        data["forecast_outcomes"][0]["observed_value"] = 999.0
        self.assertIn(
            "forecast_outcome_invalid:0:fields",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_observation_before_target_is_refused(self):
        data = outcome_input(observed_at="2026-09-17T15:49:59Z")
        self.assertIn(
            "forecast_outcome_time_invalid:0:before_target",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_nested_cycle_time_blocks_a_late_observation(self):
        data = outcome_input(observed_at="2026-09-17T16:03:00Z")
        data["as_of"] = "2026-09-17T16:04:00Z"
        data["snapshot"]["as_of"] = "2026-09-17T16:00:00Z"
        self.assertIn(
            "forecast_outcome_time_invalid:0:after_cycle",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_observation_after_window_is_refused(self):
        data = outcome_input(observed_at="2026-09-17T16:50:01Z")
        data["as_of"] = "2026-09-17T16:51:00Z"
        data["snapshot"]["as_of"] = "2026-09-17T16:51:00Z"
        self.assertIn(
            "forecast_outcome_time_invalid:0:after_window",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_source_mismatch_is_refused(self):
        data = outcome_input(stable_ref="ibkr://price/OTHER")
        self.assertIn(
            "forecast_outcome_tool_call_invalid:0:stable_ref",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_host_summary_cannot_measure_forecast(self):
        data = outcome_input(origin="host_summary")
        self.assertIn(
            "forecast_outcome_tool_call_invalid:0:origin",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_malformed_provenance_is_a_refusal_not_a_crash(self):
        data = outcome_input()
        del data["research"][0]["tool_calls"][0]["provenance"]
        self.assertIn(
            "forecast_outcome_tool_call_invalid:0:unknown",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_one_terminal_row_per_forecast(self):
        data = outcome_input()
        data["forecast_outcomes"].append(
            dict(data["forecast_outcomes"][0])
        )
        self.assertIn(
            f"forecast_outcome_duplicate_forecast:1:{FORECAST_ID}",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record()],
            ),
        )

    def test_existing_terminal_record_is_refused(self):
        data = outcome_input()
        outcome = {
            "record_id": f"forecast-outcome:{FORECAST_ID}",
            "record_type": "forecast_outcome",
            "payload": {"forecast_id": FORECAST_ID},
        }
        self.assertIn(
            f"forecast_outcome_already_measured:0:{FORECAST_ID}",
            validate_forecast_outcomes(
                data["forecast_outcomes"],
                data=data,
                records=[forecast_record(), outcome],
            ),
        )


class ForecastOutcomePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="forecast-outcome-"))
        self.journal = AuditJournal(self.root / "journal.jsonl")
        self.forecast = forecast_record()
        self.journal.append(
            record_id=self.forecast["record_id"],
            record_type=self.forecast["record_type"],
            agent="test",
            payload=dict(self.forecast["payload"]),
        )
        for record_id, record_type in (
            ("cycle-receipt:cycle-outcome", "cycle_receipt"),
            ("cycle-stage:cycle-outcome:decision", "cycle_stage"),
            ("tool-provenance:cycle-outcome", "tool_provenance"),
            ("cycle-stage:cycle-outcome:research_director", "cycle_stage"),
        ):
            self.journal.append(
                record_id=record_id,
                record_type=record_type,
                agent="test",
                payload={
                    "cycle_id": "cycle-outcome",
                    **({
                        "snapshot_id": "outcome.json:pending",
                    } if record_type == "cycle_receipt" else {}),
                },
            )

    def test_runtime_extracts_and_resolves_connector_value(self):
        data = outcome_input(value=130.0)
        added = persist_forecast_outcomes(
            data,
            self.journal,
            {"cycle_id": "cycle-outcome"},
            all_records=self.journal.read(),
        )
        self.assertEqual(added, 1)
        record = next(
            row for row in self.journal.read()
            if row.get("record_type") == "forecast_outcome"
        )
        self.assertEqual(
            record["record_id"],
            f"forecast-outcome:{FORECAST_ID}",
        )
        self.assertEqual(record["payload"]["observed_value"], 130.0)
        self.assertEqual(record["payload"]["resolved_outcome"], 1)
        self.assertNotIn("brier_component", record["payload"])
        self.assertTrue(self.journal.validate()["valid"])

    def test_invalidation_remains_measured(self):
        data = outcome_input(value=100.0)
        data["forecast_outcomes"][0]["invalidation_reason"] = (
            "A current filing invalidated the thesis."
        )
        persist_forecast_outcomes(
            data,
            self.journal,
            {"cycle_id": "cycle-outcome"},
            all_records=self.journal.read(),
        )
        summary = forecast_outcome_summary(self.journal.read())
        self.assertEqual(summary["measured_count"], 1)
        self.assertEqual(summary["invalidated_count"], 1)
        self.assertEqual(summary["matured_count"], 1)
        self.assertEqual(summary["calibration"]["n"], 1)
        self.assertEqual(
            summary["calibration"]["source"],
            "persisted_forecast_outcomes",
        )

    def test_overdue_forecast_stays_in_matured_denominator(self):
        now = datetime(2026, 9, 17, 17, 0, tzinfo=timezone.utc)
        ledger = forecast_ledger_summary(self.journal.read(), now=now)
        self.assertEqual(ledger["overdue_count"], 1)
        self.assertEqual(
            overdue_forecast_ids(self.journal.read(), now=now),
            [FORECAST_ID],
        )
        summary = forecast_outcome_summary(self.journal.read())
        # Wall-clock summary remains descriptive; deterministic deadline
        # behavior is covered by the injected ledger assertion above.
        self.assertEqual(summary["measured_count"], 0)

    def test_overdue_forecast_does_not_block_new_registration(self):
        data = valid_input()
        data["forecast_registrations"] = [forecast(
            forecast_id="new-forecast",
            horizon={
                "label": "later",
                "target_at": "2026-10-01T20:00:00Z",
                "observation_window_seconds": 3600,
            },
        )]
        errors = validate_forecast_registrations(
            data["forecast_registrations"],
            data=data,
            records=self.journal.read(),
            block_on_overdue=True,
            now=datetime(
                2026, 9, 17, 17, 0, tzinfo=timezone.utc,
            ),
        )
        self.assertFalse(
            any(
                error.startswith("forecast_overdue_blocking_registration:")
                for error in errors
            ),
            errors,
        )

    def test_superseded_unmeasured_forecast_still_becomes_overdue(self):
        revised = forecast_payload(
            forecast_id="forecast-revision",
            supersedes_forecast_id=FORECAST_ID,
            horizon={
                "label": "later horizon",
                "target_at": "2026-10-01T20:00:00Z",
                "observation_window_seconds": 3600,
            },
        )
        self.journal.append(
            record_id="forecast:forecast-revision",
            record_type="forecast_registered",
            agent="test",
            payload=revised,
        )
        self.assertIn(
            FORECAST_ID,
            overdue_forecast_ids(
                self.journal.read(),
                now=datetime(
                    2026, 9, 17, 17, 0, tzinfo=timezone.utc,
                ),
            ),
        )

    def test_backfill_recovers_only_journal_bound_observation(self):
        data = outcome_input()
        path = self.root / "outcome.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        from .accepted_inputs import input_fingerprint

        receipt = next(
            row for row in self.journal.read()
            if row.get("record_id") == "cycle-receipt:cycle-outcome"
        )
        receipt["payload"]["snapshot_id"] = (
            f"outcome:{input_fingerprint(data)}"
        )
        # Replace the test journal with the same records and corrected receipt.
        lines = []
        for row in self.journal.read():
            if row.get("record_id") == receipt["record_id"]:
                row = receipt
            lines.append(json.dumps(row))
        self.journal.path.write_text("\n".join(lines) + "\n")

        self.assertEqual(
            backfill_forecast_outcomes(
                [path],
                self.journal,
                all_records=self.journal.read(),
            ),
            1,
        )
        self.assertEqual(
            backfill_forecast_outcomes(
                [path],
                self.journal,
                all_records=self.journal.read(),
            ),
            0,
        )

    def test_singular_outcome_key_is_refused(self):
        data = outcome_input()
        del data["forecast_outcomes"]
        data["forecast_outcome"] = []
        from .run_host_cycle import validate_input

        self.assertIn(
            "forecast_outcome_lookalike_key_unsupported:forecast_outcome",
            validate_input(
                data,
                "lookalike.json",
                records=[forecast_record()],
                require_full_schema=True,
            ),
        )


if __name__ == "__main__":
    unittest.main()
