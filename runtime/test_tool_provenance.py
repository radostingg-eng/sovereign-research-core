import math
import unittest

from .tool_provenance import (
    build_tool_provenance_index,
    canonical_result_hash,
    latest_tool_provenance,
    provenance_required,
    validate_tool_call_provenance,
)


class ToolProvenanceTests(unittest.TestCase):
    as_of = "2026-09-17T16:00:00Z"

    def connector_call(self, **provenance_overrides):
        provenance = {
            "result_origin": "connector_response",
            "observed_at": "2026-09-17T16:01:00Z",
            "source_refs": [],
        }
        provenance.update(provenance_overrides)
        return {
            "tool": "IBKR.positions",
            "result": {"positions": []},
            "provenance": provenance,
        }

    def test_effective_date_preserves_old_v2_replay(self):
        self.assertFalse(provenance_required("2026-09-17T15:33:43Z"))
        self.assertTrue(provenance_required("2026-09-17T15:33:44Z"))
        self.assertFalse(provenance_required("not-a-timestamp"))

    def test_structured_connector_response_needs_no_invented_locator(self):
        self.assertEqual(
            validate_tool_call_provenance(
                self.connector_call(), cycle_as_of=self.as_of,
            ),
            [],
        )

    def test_host_summary_requires_prose_and_stable_locator(self):
        call = self.connector_call(
            result_origin="host_summary",
            source_refs=[],
        )
        call["result"] = "The filing reports lower deferred revenue."
        self.assertEqual(
            validate_tool_call_provenance(
                call, cycle_as_of=self.as_of,
            ),
            ["host_summary_stable_ref_required"],
        )
        call["provenance"]["source_refs"] = [{
            "kind": "url",
            "value": "https://example.com/filing",
        }]
        self.assertEqual(
            validate_tool_call_provenance(
                call, cycle_as_of=self.as_of,
            ),
            [],
        )

    def test_scalar_connector_response_cannot_disguise_host_prose(self):
        call = self.connector_call()
        call["result"] = "Market closed"
        self.assertIn(
            "scalar_connector_stable_ref_required",
            validate_tool_call_provenance(
                call, cycle_as_of=self.as_of,
            ),
        )

    def test_observed_time_is_timezone_qualified_and_sane(self):
        call = self.connector_call(observed_at="2026-09-17T16:01:00")
        self.assertIn(
            "observed_at_timezone_required",
            validate_tool_call_provenance(
                call, cycle_as_of=self.as_of,
            ),
        )
        call = self.connector_call(observed_at="2026-09-20T16:01:00Z")
        self.assertIn(
            "observed_at_outside_cycle_window",
            validate_tool_call_provenance(
                call, cycle_as_of=self.as_of,
            ),
        )

    def test_references_are_exact_bounded_and_deduplicated(self):
        call = self.connector_call(source_refs=[
            {"kind": "URL", "value": "https://example.com/report"},
            {"kind": "url", "value": "https://example.com/report"},
            {"kind": "note", "value": "x", "extra": "not allowed"},
        ])
        problems = validate_tool_call_provenance(
            call, cycle_as_of=self.as_of,
        )
        self.assertIn("source_ref_1_duplicate", problems)
        self.assertIn("source_ref_2_unexpected_fields", problems)

        call = self.connector_call(source_refs=[{
            "kind": "response_id",
            "value": {"not": "a string"},
        }])
        self.assertIn(
            "source_ref_0_value_not_string",
            validate_tool_call_provenance(
                call, cycle_as_of=self.as_of,
            ),
        )

    def test_invalid_urls_and_non_json_numbers_are_refused(self):
        call = self.connector_call(source_refs=[{
            "kind": "url",
            "value": "not a URL",
        }])
        call["result"] = {"value": math.nan}
        problems = validate_tool_call_provenance(
            call, cycle_as_of=self.as_of,
        )
        self.assertIn("source_ref_0_url_invalid", problems)
        self.assertIn("result_not_canonical_json", problems)

    def test_canonical_hash_ignores_object_key_order(self):
        first = canonical_result_hash({"b": 2, "a": [1, 3]})
        second = canonical_result_hash({"a": [1, 3], "b": 2})
        changed = canonical_result_hash({"a": [1, 4], "b": 2})
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_index_preserves_coordinates_and_not_result_content(self):
        call = self.connector_call(source_refs=[{
            "kind": "response_id",
            "value": "response-1",
        }])
        data = {
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [call],
            }],
        }
        payload = build_tool_provenance_index(data, cycle_id="cycle-one")
        self.assertEqual(payload["cycle_id"], "cycle-one")
        self.assertEqual(
            payload["calls"][0]["specialist_stage_id"],
            "macro_specialist",
        )
        self.assertEqual(
            payload["calls"][0]["result_sha256"],
            canonical_result_hash(call["result"]),
        )
        self.assertNotIn("result", payload["calls"][0])

    def test_index_includes_market_scout_calls_with_stable_ids(self):
        call = self.connector_call(source_refs=[{
            "kind": "response_id",
            "value": "scout-response-1",
        }])
        call.update({
            "tool_call_id": "scout-call-one",
            "kind": "connector_lookup",
            "call": {"query": "current market movers"},
        })
        data = {
            "cognitive_stages": [{
                "stage_id": "market_scout",
                "output": {
                    "market_scout_report": {"tool_calls": [call]},
                },
            }],
            "research": [],
        }
        payload = build_tool_provenance_index(data, cycle_id="cycle-scout")
        self.assertEqual(len(payload["calls"]), 1)
        row = payload["calls"][0]
        self.assertEqual(row["scope"], "market_scout")
        self.assertEqual(row["tool_call_id"], "scout-call-one")
        self.assertEqual(row["specialist_stage_id"], "market_scout")
        self.assertEqual(
            row["result_sha256"],
            canonical_result_hash(call["result"]),
        )

    def test_feedback_accepts_legacy_research_only_rows(self):
        summary = latest_tool_provenance([{
            "record_id": "tool-provenance:legacy-cycle",
            "record_type": "tool_provenance",
            "caused_by": ["cycle-receipt:legacy-cycle"],
            "payload": {
                "cycle_id": "legacy-cycle",
                "calls": [{
                    "research_index": 0,
                    "call_index": 0,
                    "specialist_stage_id": "macro_specialist",
                    "tool": "IBKR.positions",
                    "result_origin": "connector_response",
                    "observed_at": self.as_of,
                    "source_refs": [],
                    "result_sha256": "a" * 64,
                }],
            },
        }])
        self.assertEqual(summary["rows"][0]["scope"], "research")
        self.assertEqual(
            summary["rows"][0]["tool_call_id"],
            "research:0:0",
        )

    def test_feedback_is_bounded_and_states_truth_limit(self):
        calls = []
        for index in range(3):
            calls.append({
                "research_index": 0,
                "call_index": index,
                "specialist_stage_id": "macro_specialist",
                "tool": f"tool-{index}",
                "result_origin": (
                    "host_summary" if index == 2
                    else "connector_response"
                ),
                "observed_at": self.as_of,
                "source_refs": [{
                    "kind": "url",
                    "value": "https://example.com/" + ("x" * 250),
                }],
                "result_sha256": str(index) * 64,
            })
        summary = latest_tool_provenance([{
            "record_id": "tool-provenance:cycle-one",
            "record_type": "tool_provenance",
            "caused_by": ["cycle-receipt:cycle-one"],
            "payload": {"cycle_id": "cycle-one", "calls": calls},
        }], row_limit=2)
        self.assertEqual(summary["call_count"], 3)
        self.assertEqual(summary["connector_response_count"], 2)
        self.assertEqual(summary["host_summary_count"], 1)
        self.assertEqual(len(summary["rows"]), 2)
        self.assertEqual(summary["not_shown"], 1)
        self.assertTrue(
            summary["rows"][0]["source_refs"][0]["value_truncated"])
        self.assertIn(
            "do not prove the connector returned it",
            summary["what_this_means"],
        )

    def test_feedback_refuses_malformed_persisted_hashes(self):
        with self.assertRaisesRegex(
            ValueError,
            "tool_provenance_record_invalid:call_0_result_sha256",
        ):
            latest_tool_provenance([{
                "record_id": "tool-provenance:cycle-one",
                "record_type": "tool_provenance",
                "caused_by": ["cycle-receipt:cycle-one"],
                "payload": {
                    "cycle_id": "cycle-one",
                    "calls": [{
                        "research_index": 0,
                        "call_index": 0,
                        "specialist_stage_id": "macro_specialist",
                        "tool": "IBKR.positions",
                        "result_origin": "connector_response",
                        "observed_at": self.as_of,
                        "source_refs": [],
                        "result_sha256": "not-a-hash",
                    }],
                },
            }])


if __name__ == "__main__":
    unittest.main()
