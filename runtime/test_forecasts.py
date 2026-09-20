import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .forecasts import (
    _payload,
    backfill_forecast_registrations,
    forecast_ledger_summary,
    persist_forecast_registrations,
    validate_forecast_registrations,
)
from .run_host_cycle import run_one, validate_input
from .test_opportunity_ledger import event, research_state
from .test_research_allocation import valid_input


def forecast(**overrides):
    value = {
        "forecast_id": "forecast-vrt-price-20261001",
        "opportunity_id": "vrt-special-situation",
        "supersedes_forecast_id": None,
        "thesis": "Current evidence supports a higher VRT price by the target.",
        "metric": {
            "name": "instrument_price",
            "unit": "currency",
            "baseline_value": 120.0,
            "baseline_observed_at": "2026-09-17T16:00:00Z",
            "source": {
                "tool": "Interactive Brokers (IBKR)",
                "field": "last",
                "instrument_ref": "VRT",
                "stable_ref": "ibkr://price/VRT",
            },
        },
        "horizon": {
            "label": "through October 1, 2026",
            "target_at": "2026-10-01T20:00:00Z",
            "observation_window_seconds": 172800,
        },
        "expectation": {
            "kind": "direction",
            "direction": "up",
            "lower_bound": None,
            "upper_bound": None,
        },
        "confidence_probability": 0.65,
        "benchmark": None,
        "entry_context": {
            "kind": "discovery_price",
            "expression": "Unexecuted price context only.",
            "price": 120.0,
            "observed_at": "2026-09-17T16:00:00Z",
            "source": {
                "tool": "Interactive Brokers (IBKR)",
                "field": "last",
                "instrument_ref": "VRT",
                "stable_ref": "ibkr://price/VRT",
            },
        },
        "risk_assumptions": [
            "The same IBKR field remains observable at the target.",
            "No order or capital allocation is implied.",
        ],
        "portfolio_context": "VRT is unheld in the current snapshot.",
        "invalidation_condition": (
            "Company evidence invalidates the expected earnings bridge."
        ),
        "evidence": ["stage:research_director"],
    }
    value.update(overrides)
    return value


def forecast_input(*rows, cycle_id="cycle-forecast"):
    data = valid_input()
    data["cycle_id"] = cycle_id
    data["opportunity_updates"] = [
        event(
            research_state=research_state(),
            evidence=["stage:research_director"],
        )
    ]
    scout = next(
        stage for stage in data["cognitive_stages"]
        if stage["stage_id"] == "market_scout"
    )
    scout["output"]["market_scout_report"]["budget"][
        "opportunity_updates"
    ] = 1
    data["forecast_registrations"] = list(rows)
    return data


class ForecastValidationTests(unittest.TestCase):
    def test_valid_same_cycle_opportunity_forecast_is_stageable(self):
        data = forecast_input(forecast())
        self.assertEqual(
            validate_input(
                data,
                "forecast.json",
                require_full_schema=True,
            ),
            [],
        )

    def test_historical_cycle_may_omit_forecast_registrations(self):
        data = valid_input()
        self.assertNotIn(
            "forecast_registrations_must_be_a_list",
            validate_input(data, "historical.json"),
        )

    def test_new_staged_cycle_may_honestly_register_no_forecast(self):
        self.assertEqual(
            validate_input(
                valid_input(),
                "new-without-forecast.json",
                require_full_schema=True,
            ),
            [],
        )

    def test_direction_is_binary_and_flat_requires_a_range(self):
        data = forecast_input(forecast())
        data["forecast_registrations"][0]["expectation"]["direction"] = "flat"
        self.assertIn(
            "forecast_expectation_invalid:0:direction",
            validate_forecast_registrations(
                data["forecast_registrations"],
                data=data,
                records=[],
            ),
        )

    def test_range_has_inclusive_ordered_bounds(self):
        data = forecast_input(forecast(expectation={
            "kind": "range",
            "direction": None,
            "lower_bound": 130.0,
            "upper_bound": 125.0,
        }))
        self.assertIn(
            "forecast_expectation_invalid:0:range_order",
            validate_forecast_registrations(
                data["forecast_registrations"],
                data=data,
                records=[],
            ),
        )

    def test_metric_source_is_required_for_later_measurement(self):
        data = forecast_input(forecast())
        del data["forecast_registrations"][0]["metric"]["source"]["field"]
        self.assertIn(
            "forecast_source_invalid:0:metric:source:fields",
            validate_forecast_registrations(
                data["forecast_registrations"],
                data=data,
                records=[],
            ),
        )

    def test_confidence_probability_is_bounded(self):
        data = forecast_input(forecast(confidence_probability=1.2))
        self.assertIn(
            "forecast_confidence_invalid:0",
            validate_forecast_registrations(
                data["forecast_registrations"],
                data=data,
                records=[],
            ),
        )

    def test_benchmark_is_optional_context_with_its_own_source(self):
        data = forecast_input(forecast(benchmark={
            "name": "sector_index_price",
            "unit": "currency",
            "baseline_value": 450.0,
            "baseline_observed_at": "2026-09-17T16:00:00Z",
            "source": {
                "tool": "Interactive Brokers (IBKR)",
                "field": "last",
                "instrument_ref": "SECTOR-INDEX",
                "stable_ref": "ibkr://price/SECTOR-INDEX",
            },
        }))
        self.assertEqual(
            validate_forecast_registrations(
                data["forecast_registrations"],
                data=data,
                records=[],
            ),
            [],
        )

    def test_overdue_forecast_does_not_block_distinct_registration(self):
        data = forecast_input(
            forecast(forecast_id="forecast-vrt-price-new")
        )
        overdue = {
            "record_id": "forecast:forecast-vrt-price-overdue",
            "record_type": "forecast_registered",
            "payload": {
                "forecast_id": "forecast-vrt-price-overdue",
                "opportunity_id": "vrt-special-situation",
                "metric": {
                    "name": "instrument_price",
                    "source": {"stable_ref": "ibkr://price/VRT"},
                },
                "horizon": {
                    "target_at": "2026-09-18T16:00:00Z",
                    "observation_window_seconds": 1800,
                },
                "supersedes_forecast_id": None,
            },
        }
        errors = validate_forecast_registrations(
            data["forecast_registrations"],
            data=data,
            records=[overdue],
        )
        self.assertEqual(errors, [])

    def test_overdue_history_survives_beside_new_open_forecast(self):
        records = [
            {
                "record_id": "forecast:forecast-overdue",
                "record_type": "forecast_registered",
                "payload": {
                    "forecast_id": "forecast-overdue",
                    "opportunity_id": "opportunity-one",
                    "metric": {
                        "name": "instrument_price",
                        "source": {"stable_ref": "ibkr://price/ONE"},
                    },
                    "horizon": {
                        "target_at": "2026-09-18T16:00:00Z",
                        "observation_window_seconds": 1800,
                    },
                    "supersedes_forecast_id": None,
                },
            },
            {
                "record_id": "forecast:forecast-new",
                "record_type": "forecast_registered",
                "payload": {
                    "forecast_id": "forecast-new",
                    "opportunity_id": "opportunity-one",
                    "metric": {
                        "name": "instrument_price",
                        "source": {"stable_ref": "ibkr://price/ONE"},
                    },
                    "horizon": {
                        "target_at": "2026-09-19T16:00:00Z",
                        "observation_window_seconds": 1800,
                    },
                    "supersedes_forecast_id": None,
                },
            },
        ]
        summary = forecast_ledger_summary(
            records,
            now=datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["overdue_count"], 1)
        self.assertEqual(summary["open_count"], 1)
        self.assertEqual(
            {
                row["forecast_id"]: row["measurement_status"]
                for row in summary["items"]
            },
            {
                "forecast-new": "open",
                "forecast-overdue": "overdue",
            },
        )

    def test_unknown_opportunity_is_refused(self):
        data = forecast_input(forecast(opportunity_id="missing"))
        self.assertIn(
            "forecast_opportunity_invalid:0:missing",
            validate_forecast_registrations(
                data["forecast_registrations"],
                data=data,
                records=[],
            ),
        )

    def test_baseline_and_horizon_order_are_frozen(self):
        data = forecast_input(forecast())
        row = data["forecast_registrations"][0]
        row["metric"]["baseline_observed_at"] = "2026-09-18T00:00:00Z"
        row["horizon"]["target_at"] = "2026-09-17T15:00:00Z"
        errors = validate_forecast_registrations(
            data["forecast_registrations"],
            data=data,
            records=[],
        )
        self.assertIn(
            "forecast_metric_invalid:0:metric:baseline_in_future",
            errors,
        )
        self.assertIn("forecast_horizon_invalid:0:not_future", errors)

    def test_unknown_fields_are_refused(self):
        data = forecast_input(forecast())
        data["forecast_registrations"][0]["outcome"] = "worked"
        self.assertIn(
            "forecast_registration_invalid:0:fields",
            validate_forecast_registrations(
                data["forecast_registrations"],
                data=data,
                records=[],
            ),
        )


class ForecastPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="forecast-ledger-"))
        self.journal = AuditJournal(self.root / "journal.jsonl")

    def execute(self, data, name):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(path, self.journal)
        return path

    def test_forecast_persists_after_same_cycle_opportunity(self):
        self.execute(forecast_input(forecast()), "first.json")
        records = self.journal.read()
        row = next(
            record for record in records
            if record.get("record_type") == "forecast_registered"
        )
        payload = row["payload"]
        self.assertEqual(row["record_id"], "forecast:forecast-vrt-price-20261001")
        self.assertEqual(
            payload["opportunity"]["record_id"],
            "opportunity-event:vrt-new",
        )
        self.assertEqual(
            payload["decision"]["record_id"],
            "cycle-stage:cycle-forecast:decision",
        )
        self.assertTrue(payload["decision"]["snapshot_hash"])
        self.assertEqual(
            payload["expectation"]["resolution_rule"],
            "strictly_above_baseline_ties_false",
        )
        self.assertEqual(payload["metric"]["baseline_age_seconds"], 0)
        self.assertTrue(self.journal.validate()["valid"])

    def test_same_measurable_event_requires_visible_supersession(self):
        self.execute(forecast_input(forecast()), "first.json")
        second = forecast_input(
            forecast(forecast_id="forecast-vrt-price-revision"),
            cycle_id="cycle-second",
        )
        second["opportunity_updates"] = []
        errors = validate_forecast_registrations(
            second["forecast_registrations"],
            data=second,
            records=self.journal.read(),
        )
        self.assertIn(
            "forecast_supersession_required:"
            "0:forecast-vrt-price-20261001",
            errors,
        )

        second["forecast_registrations"][0][
            "supersedes_forecast_id"
        ] = "forecast-vrt-price-20261001"
        self.assertEqual(
            validate_forecast_registrations(
                second["forecast_registrations"],
                data=second,
                records=self.journal.read(),
            ),
            [],
        )

    def test_summary_marks_visible_supersession(self):
        self.execute(forecast_input(forecast()), "first.json")
        second = forecast_input(
            forecast(
                forecast_id="forecast-vrt-price-revision",
                supersedes_forecast_id="forecast-vrt-price-20261001",
                confidence_probability=0.55,
            ),
            cycle_id="cycle-second",
        )
        second["opportunity_updates"] = []
        self.execute(second, "second.json")

        summary = forecast_ledger_summary(self.journal.read())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["open_count"], 2)
        self.assertEqual(summary["measured_count"], 0)
        self.assertEqual(summary["overdue_count"], 0)
        self.assertEqual(summary["superseded_count"], 1)
        self.assertEqual(
            summary["items"][0]["forecast_id"],
            "forecast-vrt-price-revision",
        )

    def test_backfill_recovers_forecast_without_rewriting(self):
        data = forecast_input(forecast())
        path = self.root / "backfill.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with (
            patch(
                "runtime.run_host_cycle.load_journal_records",
                return_value=[],
            ),
            patch(
                "runtime.run_host_cycle.persist_forecast_registrations",
                return_value=0,
            ),
        ):
            run_one(path, self.journal)
        self.assertFalse(any(
            row.get("record_type") == "forecast_registered"
            for row in self.journal.read()
        ))
        self.assertEqual(
            backfill_forecast_registrations(
                [path],
                self.journal,
                all_records=self.journal.read(),
            ),
            1,
        )
        self.assertEqual(
            backfill_forecast_registrations(
                [path],
                self.journal,
                all_records=self.journal.read(),
            ),
            0,
        )

    def test_forecast_context_never_becomes_an_order(self):
        data = forecast_input(forecast())
        self.execute(data, "no-order.json")
        records = self.journal.read()
        self.assertFalse(any(
            record.get("record_type") == "order_instruction_event"
            and (record.get("payload") or {}).get("operation") == "create"
            for record in records
        ))

    def test_existing_legacy_forecast_reconciles_without_window_field(self):
        data = forecast_input(forecast())
        del data["forecast_registrations"][0]["horizon"][
            "observation_window_seconds"
        ]
        decision = {
            "record_id": "cycle-stage:cycle-forecast:decision",
            "record_type": "cycle_stage",
            "payload": {
                "cycle_id": "cycle-forecast",
                "output": {
                    "decision_status": "wait",
                    "snapshot_hash": "a" * 64,
                },
            },
        }
        opportunity = {
            "record_id": "opportunity-event:vrt-new",
            "record_type": "opportunity_event",
            "payload": {
                "cycle_id": "cycle-forecast",
                "opportunity_id": "vrt-special-situation",
                "observed_at": data["as_of"],
                "to_state": "new",
            },
        }
        records = [decision, opportunity]
        payload = _payload(
            data["forecast_registrations"][0],
            data=data,
            records=records,
        )
        forecast_record = {
            "record_id": "forecast:forecast-vrt-price-20261001",
            "record_type": "forecast_registered",
            "payload": payload,
            "caused_by": [
                "cycle-receipt:cycle-forecast",
                decision["record_id"],
                opportunity["record_id"],
                "cycle-stage:cycle-forecast:research_director",
            ],
        }

        class ExistingJournal:
            def read(self):
                return [*records, forecast_record]

            def append(self, **_kwargs):
                raise AssertionError("existing legacy forecast must not append")

        self.assertEqual(
            persist_forecast_registrations(
                data,
                ExistingJournal(),
                {"cycle_id": "cycle-forecast"},
                all_records=[*records, forecast_record],
            ),
            0,
        )

    def test_later_opportunity_event_does_not_rewrite_forecast_context(self):
        data = forecast_input(forecast())
        decision = {
            "record_id": "cycle-stage:cycle-forecast:decision",
            "record_type": "cycle_stage",
            "payload": {
                "cycle_id": "cycle-forecast",
                "output": {
                    "decision_status": "wait",
                    "snapshot_hash": "a" * 64,
                },
            },
        }
        opportunity_at_registration = {
            "record_id": "opportunity-event:vrt-at-registration",
            "record_type": "opportunity_event",
            "payload": {
                "cycle_id": "cycle-prior",
                "opportunity_id": "vrt-special-situation",
                "observed_at": "2026-09-17T15:30:00Z",
                "to_state": "researching",
            },
        }
        later_opportunity = {
            "record_id": "opportunity-event:vrt-later",
            "record_type": "opportunity_event",
            "payload": {
                "cycle_id": "cycle-later",
                "opportunity_id": "vrt-special-situation",
                "observed_at": "2026-09-17T17:00:00Z",
                "to_state": "actionable",
            },
        }
        records_at_registration = [decision, opportunity_at_registration]
        payload = _payload(
            data["forecast_registrations"][0],
            data=data,
            records=records_at_registration,
        )
        forecast_record = {
            "record_id": "forecast:forecast-vrt-price-20261001",
            "record_type": "forecast_registered",
            "payload": payload,
            "caused_by": [
                "cycle-receipt:cycle-forecast",
                decision["record_id"],
                opportunity_at_registration["record_id"],
                "cycle-stage:cycle-forecast:research_director",
            ],
        }
        records = [
            *records_at_registration,
            later_opportunity,
            forecast_record,
        ]

        class ExistingJournal:
            def read(self):
                return records

            def append(self, **_kwargs):
                raise AssertionError("existing forecast must not append")

        self.assertEqual(
            persist_forecast_registrations(
                data,
                ExistingJournal(),
                {"cycle_id": "cycle-forecast"},
                all_records=records,
            ),
            0,
        )
        self.assertEqual(
            payload["opportunity"]["record_id"],
            opportunity_at_registration["record_id"],
        )


if __name__ == "__main__":
    unittest.main()
