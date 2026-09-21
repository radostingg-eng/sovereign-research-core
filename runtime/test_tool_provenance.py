import math
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from .tool_provenance import (
    build_tool_provenance_index,
    canonical_result_hash,
    latest_tool_provenance,
    persisted_tool_provenance_errors,
    provenance_required,
    resolve_tool_call,
    validate_tool_call_id_consistency,
    validate_tool_call_provenance,
)
from .tool_artifacts import iter_tool_calls
from .tool_artifacts import MAX_ARTIFACT_BYTES, build_artifact_specs


def upgrade_tool_calls_to_v4(data):
    data["host_input_schema_version"] = 4
    decision = data.get("decision")
    if isinstance(decision, dict):
        decision.setdefault("repetition_review", None)
    for stage in data.get("cognitive_stages") or ():
        if (
            isinstance(stage, dict)
            and stage.get("stage_id") == "decision"
            and isinstance(stage.get("output"), dict)
        ):
            stage["output"].setdefault("repetition_review", None)
    for descriptor in iter_tool_calls(data):
        call = descriptor["call"]
        call.setdefault(
            "tool_call_id",
            "test-"
            + descriptor["scope"]
            + "-"
            + str(descriptor["research_index"])
            + "-"
            + str(descriptor["call_index"]),
        )
        call.setdefault("kind", "connector_lookup")
        previous = call.get("call")
        if isinstance(previous, dict):
            action = previous.get("action") or str(call.get("tool", "call"))
            arguments = {
                key: value
                for key, value in previous.items()
                if key != "action"
            }
        elif isinstance(previous, str) and previous.strip():
            action = previous.strip()
            arguments = {}
        else:
            action = str(call.get("tool", "call"))
            arguments = {}
        call["call"] = {
            "action": action,
            "arguments": arguments,
        }
        provenance = call.get("provenance")
        if not isinstance(provenance, dict):
            snapshot = data.get("snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            provenance = {
                "result_origin": "connector_response",
                "observed_at": (
                    snapshot.get("as_of")
                    or data.get("as_of")
                    or "2026-09-18T19:00:00Z"
                ),
                "source_refs": [],
            }
            call["provenance"] = provenance
        origin = provenance.get("result_origin")
        if origin == "connector_response" and isinstance(
            call.get("result"),
            str,
        ):
            call["result"] = {"text": call["result"]}
        provenance["capture"] = {
            "schema_version": 1,
            "representation": (
                "host_summary_no_response"
                if origin == "host_summary"
                else "canonical_response"
            ),
            "redactions": [],
        }
        web_sources = []
        for ref in provenance.get("source_refs") or []:
            if (
                isinstance(ref, dict)
                and str(ref.get("kind", "")).casefold() in {"url", "link"}
            ):
                web_sources.append({
                    "url": ref["value"],
                    "title": "unknown",
                    "published_at": None,
                    "retrieved_at": provenance["observed_at"],
                })
        provenance["web_sources"] = web_sources
    return data


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

    def v4_call(self, **overrides):
        call = {
            "tool_call_id": "call-v4",
            "kind": "connector_lookup",
            "tool": "IBKR",
            "call": {
                "action": "get_price_snapshot",
                "arguments": {"contract_id": 1},
            },
            "result": {"last": 10},
            "provenance": {
                "result_origin": "connector_response",
                "observed_at": "2026-09-18T19:00:00Z",
                "source_refs": [],
                "capture": {
                    "schema_version": 1,
                    "representation": "canonical_response",
                    "redactions": [],
                },
                "web_sources": [],
            },
        }
        call.update(overrides)
        return call

    def data_with_cross_scope_calls(self, scout_call, research_call):
        return {
            "host_input_schema_version": 4,
            "cycle_id": "cycle-cross-scope",
            "cognitive_stages": [{
                "stage_id": "market_scout",
                "output": {
                    "market_scout_report": {
                        "tool_calls": [scout_call],
                    },
                },
            }],
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [research_call],
            }],
        }

    def test_identical_cross_scope_call_id_resolves_deterministically(self):
        call = self.v4_call(tool_call_id="shared-call")
        data = self.data_with_cross_scope_calls(
            copy.deepcopy(call),
            copy.deepcopy(call),
        )

        self.assertEqual(validate_tool_call_id_consistency(data), [])
        metadata, resolved = resolve_tool_call(data, "shared-call")
        self.assertEqual(metadata["scope"], "market_scout")
        self.assertEqual(resolved, call)

    def test_divergent_cross_scope_call_id_is_refused(self):
        first = self.v4_call(tool_call_id="shared-call")
        second = copy.deepcopy(first)
        second["result"] = {"last": 11}
        data = self.data_with_cross_scope_calls(first, second)

        self.assertEqual(
            validate_tool_call_id_consistency(data),
            ["tool_call_id_conflict:shared-call"],
        )
        with self.assertRaisesRegex(
            ValueError,
            "tool_call_id_conflict:shared-call",
        ):
            resolve_tool_call(data, "shared-call")

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

    def test_legacy_index_verifies_only_the_fields_it_persisted(self):
        call = self.connector_call(source_refs=[{
            "kind": "response_id",
            "value": "response-1",
        }])
        call["tool_call_id"] = "explicit-current-id"
        data = {
            "cycle_id": "cycle-legacy-index",
            "host_input_schema_version": 3,
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [call],
            }],
            "cognitive_stages": [],
        }
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
        )
        payload.pop("schema_version")
        row = payload["calls"][0]
        row["tool_call_id"] = "research:0:0"

        self.assertEqual(
            persisted_tool_provenance_errors(
                data,
                payload,
                recorded_at="2026-09-18T18:00:00Z",
            ),
            [],
        )
        row.pop("scope")
        row.pop("tool_call_id")
        self.assertEqual(
            persisted_tool_provenance_errors(data, payload),
            [],
        )

    def test_legacy_index_still_detects_changed_result(self):
        call = self.connector_call()
        data = {
            "cycle_id": "cycle-legacy-tamper",
            "host_input_schema_version": 3,
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [call],
            }],
            "cognitive_stages": [],
        }
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
        )
        payload.pop("schema_version")
        payload["calls"][0].pop("scope")
        payload["calls"][0].pop("tool_call_id")
        call["result"] = {"positions": [{"symbol": "CHANGED"}]}

        self.assertIn(
            "call_0:result_sha256",
            persisted_tool_provenance_errors(data, payload),
        )

    def test_legacy_index_detects_changed_persisted_evidence_fields(self):
        call = self.connector_call(source_refs=[{
            "kind": "response_id",
            "value": "response-1",
        }])
        data = {
            "cycle_id": "cycle-legacy-fields",
            "host_input_schema_version": 3,
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [call],
            }],
            "cognitive_stages": [],
        }
        original = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
        )
        original.pop("schema_version")
        original["calls"][0].pop("scope")
        original["calls"][0].pop("tool_call_id")
        cases = {
            "tool": lambda value: value["research"][0]["tool_calls"][0].update(
                {"tool": "changed-tool"}
            ),
            "observed_at": lambda value: value["research"][0]["tool_calls"][0][
                "provenance"
            ].update({"observed_at": "2026-09-17T16:02:00Z"}),
            "source_refs": lambda value: value["research"][0]["tool_calls"][0][
                "provenance"
            ].update({"source_refs": [{
                "kind": "response_id",
                "value": "changed-response",
            }]}),
            "result_sha256": lambda value: value["research"][0][
                "tool_calls"
            ][0].update({"result": {"positions": [{"symbol": "CHANGED"}]}}),
        }
        for field, mutate in cases.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(data)
                mutate(changed)
                self.assertIn(
                    f"call_0:{field}",
                    persisted_tool_provenance_errors(changed, original),
                )

    def test_legacy_index_detects_added_and_removed_calls(self):
        first = self.connector_call()
        data = {
            "cycle_id": "cycle-legacy-coverage",
            "host_input_schema_version": 3,
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [first],
            }],
            "cognitive_stages": [],
        }
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
        )
        payload.pop("schema_version")
        payload["calls"][0].pop("scope")
        payload["calls"][0].pop("tool_call_id")

        added = copy.deepcopy(data)
        added["research"][0]["tool_calls"].append(self.connector_call())
        self.assertIn(
            "missing_call:research:0:1",
            persisted_tool_provenance_errors(added, payload),
        )

        removed = copy.deepcopy(data)
        removed["research"][0]["tool_calls"] = []
        self.assertIn(
            "call_0:coordinates",
            persisted_tool_provenance_errors(removed, payload),
        )

    def test_new_indexes_declare_their_format_version(self):
        payload = build_tool_provenance_index(
            {
                "cycle_id": "cycle-versioned-index",
                "research": [{
                    "specialist_stage_id": "macro_specialist",
                    "tool_calls": [self.connector_call()],
                }],
                "cognitive_stages": [],
            },
            cycle_id="cycle-versioned-index",
        )
        self.assertEqual(payload["schema_version"], 2)

    def test_reader_versions_are_independent_from_the_current_writer(self):
        data = {
            "cycle_id": "cycle-versioned-reader",
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [self.connector_call()],
            }],
            "cognitive_stages": [],
        }
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
        )
        with patch(
            "runtime.tool_provenance."
            "TOOL_PROVENANCE_INDEX_SCHEMA_VERSION",
            3,
        ):
            self.assertEqual(
                persisted_tool_provenance_errors(data, payload),
                [],
            )

    def test_coordinate_id_alias_expires_after_explicit_id_writer(self):
        call = self.connector_call()
        call["tool_call_id"] = "explicit-id"
        data = {
            "cycle_id": "cycle-coordinate-cutoff",
            "host_input_schema_version": 3,
            "research": [{
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [call],
            }],
            "cognitive_stages": [],
        }
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
        )
        payload["calls"][0]["tool_call_id"] = "research:0:0"

        self.assertEqual(
            persisted_tool_provenance_errors(
                data,
                payload,
                recorded_at="2026-09-18T18:00:00Z",
            ),
            [],
        )
        self.assertIn(
            "call_0:tool_call_id",
            persisted_tool_provenance_errors(
                data,
                payload,
                recorded_at="2026-09-18T21:03:00Z",
            ),
        )

    def test_v4_persisted_index_round_trip_and_request_tamper(self):
        data = {
            "host_input_schema_version": 4,
            "cycle_id": "cycle-v4-persisted",
            "research": [{
                "specialist_stage_id": "specialist",
                "tool_calls": [self.v4_call()],
            }],
            "cognitive_stages": [],
        }
        records = [{"prev_hash": None, "record_hash": "a" * 64}]
        specs = build_artifact_specs(data, records=records)
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs=specs,
        )
        self.assertEqual(
            persisted_tool_provenance_errors(
                data,
                payload,
                artifact_specs=specs,
            ),
            [],
        )
        changed = copy.deepcopy(data)
        changed["research"][0]["tool_calls"][0]["call"]["arguments"][
            "contract_id"
        ] = 2
        self.assertIn(
            "call_0:request_sha256",
            persisted_tool_provenance_errors(
                changed,
                payload,
                artifact_specs=specs,
            ),
        )
        self.assertIn(
            "call_0:artifact_spec",
            persisted_tool_provenance_errors(data, payload),
        )

    def test_v4_host_summary_round_trips_without_artifact(self):
        call = self.v4_call(
            result="Host interpretation of a located source.",
        )
        call["provenance"].update({
            "result_origin": "host_summary",
            "source_refs": [{
                "kind": "url",
                "value": "https://example.com/source",
            }],
            "capture": {
                "schema_version": 1,
                "representation": "host_summary_no_response",
                "redactions": [],
            },
            "web_sources": [{
                "url": "https://example.com/source",
                "title": "Example",
                "published_at": None,
                "retrieved_at": "2026-09-18T19:00:00Z",
            }],
        })
        data = {
            "host_input_schema_version": 4,
            "cycle_id": "cycle-v4-summary",
            "research": [{
                "specialist_stage_id": "specialist",
                "tool_calls": [call],
            }],
            "cognitive_stages": [],
        }
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs={},
        )
        self.assertEqual(
            persisted_tool_provenance_errors(data, payload),
            [],
        )

    def test_v4_missing_request_returns_a_diagnostic(self):
        data = {
            "host_input_schema_version": 4,
            "cycle_id": "cycle-v4-request",
            "research": [{
                "specialist_stage_id": "specialist",
                "tool_calls": [self.v4_call()],
            }],
            "cognitive_stages": [],
        }
        specs = build_artifact_specs(
            data,
            records=[{"prev_hash": None, "record_hash": "a" * 64}],
        )
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs=specs,
        )
        data["research"][0]["tool_calls"][0]["call"] = None
        self.assertIn(
            "call_0:request",
            persisted_tool_provenance_errors(
                data,
                payload,
                artifact_specs=specs,
            ),
        )

    def test_v4_requires_normalized_call_and_json_connector_result(self):
        now = datetime(2026, 9, 18, 19, 5, tzinfo=timezone.utc)
        self.assertEqual(
            validate_tool_call_provenance(
                self.v4_call(),
                cycle_as_of="2026-09-18T18:55:00Z",
                schema_version=4,
                validation_now=now,
            ),
            [],
        )
        invalid = self.v4_call(
            call="get_price_snapshot",
            result="{'last': 10}",
        )
        errors = validate_tool_call_provenance(
            invalid,
            cycle_as_of="2026-09-18T18:55:00Z",
            schema_version=4,
            validation_now=now,
        )
        self.assertIn("call_not_object", errors)
        self.assertIn("connector_result_string_invalid", errors)

    def test_v4_observation_cannot_be_in_the_future(self):
        call = self.v4_call()
        call["provenance"]["observed_at"] = "2026-09-18T19:06:00Z"
        self.assertIn(
            "observed_at_in_future",
            validate_tool_call_provenance(
                call,
                cycle_as_of="2026-09-18T18:55:00Z",
                schema_version=4,
                validation_now=datetime(
                    2026, 9, 18, 19, 5, tzinfo=timezone.utc,
                ),
            ),
        )

    def test_v4_web_metadata_is_explicit_and_rejects_secrets(self):
        call = self.v4_call()
        call["provenance"]["source_refs"] = [{
            "kind": "url",
            "value": "https://example.com/report",
        }]
        call["provenance"]["web_sources"] = [{
            "url": "https://example.com/report",
            "title": "Example report",
            "published_at": None,
            "retrieved_at": "2026-09-18T19:00:00Z",
        }]
        now = datetime(2026, 9, 18, 19, 5, tzinfo=timezone.utc)
        self.assertEqual(
            validate_tool_call_provenance(
                call,
                cycle_as_of="2026-09-18T18:55:00Z",
                schema_version=4,
                validation_now=now,
            ),
            [],
        )
        unsafe = "https://example.com/report?access_token=secret"
        call["provenance"]["source_refs"][0]["value"] = unsafe
        call["provenance"]["web_sources"][0]["url"] = unsafe
        self.assertIn(
            "web_source_0_url_sensitive_query",
            validate_tool_call_provenance(
                call,
                cycle_as_of="2026-09-18T18:55:00Z",
                schema_version=4,
                validation_now=now,
            ),
        )

    def test_v4_oversized_connector_response_is_refused(self):
        call = self.v4_call(result={"body": "x" * MAX_ARTIFACT_BYTES})
        self.assertIn(
            "capture_artifact_too_large",
            validate_tool_call_provenance(
                call,
                cycle_as_of="2026-09-18T18:55:00Z",
                schema_version=4,
                validation_now=datetime(
                    2026, 9, 18, 19, 5, tzinfo=timezone.utc,
                ),
            ),
        )

    def test_v4_feedback_exposes_metadata_not_private_bodies(self):
        call = self.v4_call()
        url = "https://example.com/report?page=1"
        call["provenance"]["source_refs"] = [{
            "kind": "url",
            "value": url,
        }]
        call["provenance"]["web_sources"] = [{
            "url": url,
            "title": "Example report",
            "published_at": None,
            "retrieved_at": "2026-09-18T19:00:00Z",
        }]
        data = {
            "host_input_schema_version": 4,
            "cycle_id": "cycle-v4-feedback",
            "research": [{
                "specialist_stage_id": "specialist",
                "finding": "Interpretation remains outside the artifact.",
                "tool_calls": [call],
            }],
            "cognitive_stages": [],
        }
        records = [{
            "record_hash": "a" * 64,
            "prev_hash": None,
        }]
        specs = build_artifact_specs(data, records=records)
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs=specs,
        )
        summary = latest_tool_provenance([{
            "record_id": "tool-provenance:cycle-v4-feedback",
            "record_type": "tool_provenance",
            "caused_by": ["cycle-receipt:cycle-v4-feedback"],
            "payload": payload,
        }])
        row = summary["rows"][0]
        self.assertNotIn("result", row)
        self.assertNotIn("call", row)
        self.assertNotIn("arguments", row)
        self.assertEqual(
            row["source_refs"][0]["value"],
            "https://example.com/report",
        )
        self.assertTrue(row["artifact_ref"].startswith("profile://"))

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
