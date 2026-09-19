import copy
import unittest
from datetime import datetime, timezone

from .evidence_coverage import (
    evidence_coverage_summary,
    validate_evidence_coverage,
)
from .tool_artifacts import build_artifact_specs
from .tool_provenance import build_tool_provenance_index


def call(call_id, action, result):
    return {
        "tool_call_id": call_id,
        "kind": "connector_lookup",
        "tool": "Test Connector",
        "call": {"action": action, "arguments": {}},
        "result": result,
        "provenance": {
            "result_origin": "connector_response",
            "observed_at": "2026-09-19T04:00:00Z",
            "source_refs": [],
            "capture": {
                "schema_version": 1,
                "representation": "canonical_response",
                "redactions": [],
            },
            "web_sources": [],
        },
    }


def wrapper(producer, evidence_call, bindings):
    return {
        "producer": producer,
        "projection": {
            "extractor": "json_pointer_v1",
            "bindings": [
                {"source_path": source, "target_path": target}
                for source, target in bindings
            ],
        },
        "call": evidence_call,
    }


def covered_data():
    return {
        "host_input_schema_version": 4,
        "evidence_coverage_schema_version": 1,
        "cycle_id": "cycle-evidence-coverage",
        "as_of": "2026-09-19T04:00:00Z",
        "order_instructions": [],
        "snapshot": {
            "net_liquidation_value": 1000.0,
            "cash": 10.0,
            "positions": [],
            "open_orders": [],
            "order_instructions": [],
        },
        "market_sessions": {
            "observed_at": "2026-09-19T04:00:00Z",
            "markets": [
                {
                    "region": "EU",
                    "is_open": False,
                    "evidence_tool_call_ids": ["market-eu"],
                },
                {
                    "region": "US",
                    "is_open": False,
                    "evidence_tool_call_ids": ["market-us"],
                },
            ],
        },
        "evidence_calls": [
            wrapper(
                "portfolio",
                call("portfolio", "get_portfolio", {
                    "nlv": 1000.0,
                    "cash": 10.0,
                    "positions": [],
                }),
                [
                    ("/nlv", "/snapshot/net_liquidation_value"),
                    ("/cash", "/snapshot/cash"),
                    ("/positions", "/snapshot/positions"),
                ],
            ),
            wrapper(
                "saved_instructions",
                call("instructions", "get_order_instructions", {
                    "instructions": [],
                }),
                [
                    ("/instructions", "/order_instructions"),
                    (
                        "/instructions",
                        "/snapshot/order_instructions",
                    ),
                ],
            ),
            wrapper(
                "account_orders",
                call("orders", "get_account_orders", {"orders": []}),
                [("/orders", "/snapshot/open_orders")],
            ),
            wrapper(
                "market_sessions",
                call("market-eu", "get_eu_session", {
                    "is_open": False,
                }),
                [(
                    "/is_open",
                    "/market_sessions/markets/0/is_open",
                )],
            ),
            wrapper(
                "market_sessions",
                call("market-us", "get_us_session", {
                    "is_open": False,
                }),
                [(
                    "/is_open",
                    "/market_sessions/markets/1/is_open",
                )],
            ),
        ],
        "research": [],
        "cognitive_stages": [],
    }


class EvidenceCoverageTests(unittest.TestCase):
    now = datetime(2026, 9, 19, 4, 5, tzinfo=timezone.utc)

    def test_declared_coverage_binds_every_required_projection(self):
        data = covered_data()

        self.assertEqual(
            validate_evidence_coverage(data, validation_now=self.now),
            [],
        )
        self.assertEqual(
            evidence_coverage_summary(data)["missing_producers"],
            [],
        )

    def test_old_v4_cycle_remains_replayable_before_cutoff(self):
        data = covered_data()
        data["as_of"] = "2026-09-18T23:59:00Z"
        del data["evidence_coverage_schema_version"]
        del data["evidence_calls"]

        self.assertEqual(
            validate_evidence_coverage(data, validation_now=self.now),
            [],
        )

    def test_projection_mismatch_and_redaction_fail_closed(self):
        mismatch = covered_data()
        mismatch["evidence_calls"][0]["call"]["result"]["cash"] = 11.0
        self.assertIn(
            "evidence_call_invalid:0:binding:1:mismatch",
            validate_evidence_coverage(mismatch, validation_now=self.now),
        )

        redacted = covered_data()
        redacted["evidence_calls"][0]["call"]["result"][
            "positions"
        ] = "__SOVEREIGN_REDACTED__"
        redacted["snapshot"]["positions"] = "__SOVEREIGN_REDACTED__"
        self.assertIn(
            "evidence_call_invalid:0:binding:2:redacted",
            validate_evidence_coverage(redacted, validation_now=self.now),
        )

    def test_order_instruction_copies_must_agree(self):
        data = covered_data()
        data["snapshot"]["order_instructions"] = [{"id": "different"}]

        errors = validate_evidence_coverage(
            data,
            validation_now=self.now,
        )

        self.assertIn("order_instruction_projection_conflict", errors)

    def test_trade_claim_requires_trade_capture_and_projection(self):
        data = covered_data()
        data["instruction_lifecycle_updates"] = [{
            "to_state": "executed",
        }]

        errors = validate_evidence_coverage(
            data,
            validation_now=self.now,
        )

        self.assertIn("evidence_producer_missing:account_trades", errors)
        self.assertIn(
            "evidence_projection_missing:account_trades:/snapshot/trades",
            errors,
        )

    def test_market_session_ids_resolve_to_session_producer(self):
        data = covered_data()
        data["market_sessions"]["markets"][0][
            "evidence_tool_call_ids"
        ] = ["unknown"]

        self.assertIn(
            "market_session_evidence_tool_call_id_unresolved",
            validate_evidence_coverage(data, validation_now=self.now),
        )

    def test_index_carries_bounded_producer_coverage(self):
        data = covered_data()
        records = [{"prev_hash": None, "record_hash": "a" * 64}]
        specs = build_artifact_specs(data, records=records)

        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs=specs,
        )

        evidence_rows = [
            row for row in payload["calls"]
            if row["scope"] == "evidence"
        ]
        self.assertEqual(len(evidence_rows), 5)
        self.assertEqual(
            payload["evidence_coverage"]["missing_producers"],
            [],
        )
        self.assertEqual(
            evidence_rows[0]["projection_extractor"],
            "json_pointer_v1",
        )

    def test_identical_call_can_be_referenced_by_two_producers(self):
        data = covered_data()
        duplicate = copy.deepcopy(data["evidence_calls"][0])
        duplicate["producer"] = "account_orders"
        duplicate["projection"]["bindings"] = [{
            "source_path": "/positions",
            "target_path": "/snapshot/open_orders",
        }]
        data["snapshot"]["open_orders"] = []
        duplicate["call"]["result"]["positions"] = []
        data["evidence_calls"].append(duplicate)

        errors = validate_evidence_coverage(
            data,
            validation_now=self.now,
        )

        self.assertNotIn(
            "tool_call_id_conflict:portfolio",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
