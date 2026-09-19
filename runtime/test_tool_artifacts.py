import json
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .tool_artifacts import (
    REDACTION_SENTINEL,
    build_artifact_specs,
    canonical_json_bytes,
    materialize_artifacts,
    validate_capture,
    verify_artifact_records,
)
from .tool_provenance import (
    build_tool_provenance_index,
    resolve_tool_call,
)
from .integrity import check_tool_artifacts


def v4_call(
    *,
    call_id="call-one",
    result=None,
    origin="connector_response",
    representation="canonical_response",
    redactions=None,
):
    return {
        "tool_call_id": call_id,
        "kind": "connector_lookup",
        "tool": "Example Connector",
        "call": {
            "action": "lookup",
            "arguments": {"symbol": "XYZ"},
        },
        "result": {"value": 10} if result is None else result,
        "provenance": {
            "result_origin": origin,
            "observed_at": "2026-09-18T19:00:00Z",
            "source_refs": [],
            "capture": {
                "schema_version": 1,
                "representation": representation,
                "redactions": redactions or [],
            },
            "web_sources": [],
        },
    }


def v4_data(*calls):
    return {
        "host_input_schema_version": 4,
        "cycle_id": "cycle-artifacts",
        "research": [{
            "specialist_stage_id": "specialist",
            "tool_calls": list(calls),
        }],
        "cognitive_stages": [],
    }


class ToolArtifactTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tool-artifacts-"))
        self.journal = AuditJournal(self.root / "audit" / "journal.jsonl")
        self.journal.append(
            record_id="genesis",
            record_type="profile_genesis",
            agent="test",
            payload={"schema_version": 1},
        )

    def test_canonical_key_order_deduplicates_response_artifact(self):
        data = v4_data(
            v4_call(call_id="one", result={"b": 2, "a": 1}),
            v4_call(call_id="two", result={"a": 1, "b": 2}),
        )
        specs = build_artifact_specs(
            data,
            records=self.journal.read(),
        )
        first, second = specs.values()
        self.assertEqual(first["result_sha256"], second["result_sha256"])
        self.assertEqual(first["artifact_ref"], second["artifact_ref"])
        materialize_artifacts(specs, profile_root=self.root)
        self.assertEqual(
            len(list((self.root / "tool_artifacts").rglob("*.json"))),
            1,
        )

    def test_changed_response_produces_a_different_artifact(self):
        first = build_artifact_specs(
            v4_data(v4_call(result={"value": 1})),
            records=self.journal.read(),
        )
        second = build_artifact_specs(
            v4_data(v4_call(result={"value": 2})),
            records=self.journal.read(),
        )
        self.assertNotEqual(
            next(iter(first.values()))["result_sha256"],
            next(iter(second.values()))["result_sha256"],
        )

    def test_v4_index_resolver_and_artifact_use_one_result_hash(self):
        data = v4_data(v4_call(result={"b": 2, "a": 1}))
        specs = build_artifact_specs(
            data,
            records=self.journal.read(),
        )
        spec = next(iter(specs.values()))
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs=specs,
        )
        resolved, _call = resolve_tool_call(data, "call-one")

        self.assertEqual(
            {
                spec["result_sha256"],
                payload["calls"][0]["result_sha256"],
                resolved["result_sha256"],
            },
            {spec["result_sha256"]},
        )

    def test_large_response_is_stored_out_of_line(self):
        data = v4_data(v4_call(result={"body": "x" * 200_000}))
        specs = build_artifact_specs(
            data,
            records=self.journal.read(),
        )
        spec = next(iter(specs.values()))
        self.assertGreater(spec["artifact_byte_length"], 200_000)
        materialize_artifacts(specs, profile_root=self.root)
        self.assertEqual(
            (self.root / spec["artifact_path"]).stat().st_size,
            spec["artifact_byte_length"],
        )

    def test_redaction_is_explicit_and_cannot_hide_price(self):
        safe = {
            "account_number": REDACTION_SENTINEL,
            "price": 10,
        }
        capture = {
            "schema_version": 1,
            "representation": "redacted_canonical_response",
            "redactions": [{
                "path": "/account_number",
                "category": "account_identifier",
                "reason": "Private account identifier.",
            }],
        }
        self.assertEqual(
            validate_capture(
                safe,
                capture,
                result_origin="connector_response",
            ),
            [],
        )
        capture["redactions"][0]["path"] = "/price"
        safe["price"] = REDACTION_SENTINEL
        self.assertIn(
            "capture_redaction_0_investment_evidence_forbidden",
            validate_capture(
                safe,
                capture,
                result_origin="connector_response",
            ),
        )

    def test_redaction_rejects_investment_aliases_and_unknown_paths(self):
        for path in (
            "/net_liquidation_value",
            "/last_price",
            "/cash",
            "/buyingPower",
            "/unrealized_pnl",
        ):
            with self.subTest(path=path):
                errors = validate_capture(
                    {path.removeprefix("/"): REDACTION_SENTINEL},
                    {
                        "schema_version": 1,
                        "representation": "redacted_canonical_response",
                        "redactions": [{
                            "path": path,
                            "category": "account_identifier",
                            "reason": "Attempted hidden investment evidence.",
                        }],
                    },
                    result_origin="connector_response",
                )
                self.assertIn(
                    "capture_redaction_0_investment_evidence_forbidden",
                    errors,
                )

        for path in ("/foo", "/account"):
            with self.subTest(path=path):
                errors = validate_capture(
                    {path.removeprefix("/"): REDACTION_SENTINEL},
                    {
                        "schema_version": 1,
                        "representation": "redacted_canonical_response",
                        "redactions": [{
                            "path": path,
                            "category": "account_identifier",
                            "reason": "Unknown or parent path.",
                        }],
                    },
                    result_origin="connector_response",
                )
                self.assertIn(
                    "capture_redaction_0_path_not_allowed_for_category",
                    errors,
                )

    def test_nested_typed_redaction_and_array_leaf_are_allowed(self):
        for result, path in (
            (
                {"metadata": {"account_number": REDACTION_SENTINEL}},
                "/metadata/account_number",
            ),
            (
                {"account_numbers": [REDACTION_SENTINEL]},
                "/account_numbers/0",
            ),
            (
                {"auth": {"apiKey": REDACTION_SENTINEL}},
                "/auth/apiKey",
            ),
        ):
            category = (
                "credential" if "apiKey" in path else "account_identifier"
            )
            with self.subTest(path=path):
                self.assertEqual(
                    validate_capture(
                        result,
                        {
                            "schema_version": 1,
                            "representation":
                                "redacted_canonical_response",
                            "redactions": [{
                                "path": path,
                                "category": category,
                                "reason": "Private typed leaf.",
                            }],
                        },
                        result_origin="connector_response",
                    ),
                    [],
                )

    def test_capture_v2_detects_raw_result_and_request_credentials(self):
        capture = {
            "schema_version": 2,
            "representation": "canonical_response",
            "capture_origin": "direct_connector_response",
            "redactions": [],
            "request_redactions": [],
            "reconstruction_status": "exact_response",
        }
        errors = validate_capture(
            {"token": "ghp_abcdefghijklmnopqrstuvwxyz"},
            capture,
            result_origin="connector_response",
            request={
                "action": "lookup",
                "arguments": {
                    "api_key": "sk-abcdefghijklmnop",
                },
            },
        )
        self.assertTrue(any(
            error.startswith("capture_unredacted_credential:")
            for error in errors
        ))

    def test_capture_v2_accepts_declared_request_redaction(self):
        capture = {
            "schema_version": 2,
            "representation": "canonical_response",
            "capture_origin": "direct_connector_response",
            "redactions": [],
            "request_redactions": [{
                "path": "/arguments/api_key",
                "category": "credential",
                "reason": "Private API credential.",
            }],
            "reconstruction_status": "exact_response",
        }
        self.assertEqual(
            validate_capture(
                {"ok": True},
                capture,
                result_origin="connector_response",
                request={
                    "action": "lookup",
                    "arguments": {
                        "api_key": REDACTION_SENTINEL,
                    },
                },
            ),
            [],
        )

    def test_undeclared_redaction_marker_is_refused(self):
        errors = validate_capture(
            {"account_number": REDACTION_SENTINEL},
            {
                "schema_version": 1,
                "representation": "canonical_response",
                "redactions": [],
            },
            result_origin="connector_response",
        )
        self.assertIn("capture_redaction_markers_mismatch", errors)

    def test_host_summary_has_no_response_artifact(self):
        call = v4_call(
            origin="host_summary",
            result="The source reports a change.",
            representation="host_summary_no_response",
        )
        data = v4_data(call)
        specs = build_artifact_specs(
            data,
            records=self.journal.read(),
        )
        self.assertEqual(specs, {})
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs=specs,
        )
        self.assertIsNone(payload["calls"][0]["artifact_ref"])
        self.assertEqual(payload["calls"][0]["artifact_byte_length"], 0)

    def persisted_artifact_records(self):
        data = v4_data(v4_call())
        specs = build_artifact_specs(
            data,
            records=self.journal.read(),
        )
        materialize_artifacts(specs, profile_root=self.root)
        payload = build_tool_provenance_index(
            data,
            cycle_id=data["cycle_id"],
            artifact_specs=specs,
        )
        self.journal.append(
            record_id=f"tool-provenance:{data['cycle_id']}",
            record_type="tool_provenance",
            agent="test",
            caused_by=(),
            payload=payload,
        )
        return self.journal.read(), next(iter(specs.values()))

    def test_artifact_tampering_and_missing_files_are_detected(self):
        records, spec = self.persisted_artifact_records()
        self.assertEqual(
            verify_artifact_records(records, profile_root=self.root),
            [],
        )
        path = self.root / spec["artifact_path"]
        path.write_bytes(canonical_json_bytes({"value": 11}))
        self.assertTrue(any(
            error.endswith(":artifact_hash_mismatch")
            for error in verify_artifact_records(
                records,
                profile_root=self.root,
            )
        ))
        path.unlink()
        self.assertTrue(any(
            error.endswith(":artifact_missing")
            for error in verify_artifact_records(
                records,
                profile_root=self.root,
            )
        ))

    def test_runtime_integrity_includes_artifact_verification(self):
        records, spec = self.persisted_artifact_records()
        path = self.root / spec["artifact_path"]
        path.write_bytes(canonical_json_bytes({"value": 11}))
        failures = check_tool_artifacts(records, self.root)
        self.assertTrue(failures)
        self.assertEqual(failures[0].check, "tool_artifact")

    def test_v4_receipt_without_provenance_index_is_detected(self):
        self.journal.append(
            record_id="cycle-receipt:cycle-missing-index",
            record_type="cycle_receipt",
            agent="test",
            payload={
                "cycle_id": "cycle-missing-index",
                "host_input_schema_version": 4,
            },
        )

        errors = verify_artifact_records(
            self.journal.read(),
            profile_root=self.root,
        )

        self.assertIn(
            "tool-provenance:cycle-missing-index:index_missing",
            errors,
        )
        failures = check_tool_artifacts(
            self.journal.read(),
            self.root,
        )
        self.assertEqual(failures[0].check, "tool_provenance")
        self.assertEqual(failures[0].defect, "index_missing")

    def test_v4_index_cannot_strip_capture_and_artifact_fields(self):
        cycle_id = "cycle-stripped-index"
        self.journal.append(
            record_id=f"cycle-receipt:{cycle_id}",
            record_type="cycle_receipt",
            agent="test",
            payload={
                "cycle_id": cycle_id,
                "host_input_schema_version": 4,
            },
        )
        self.journal.append(
            record_id=f"tool-provenance:{cycle_id}",
            record_type="tool_provenance",
            agent="test",
            caused_by=(f"cycle-receipt:{cycle_id}",),
            payload={
                "schema_version": 2,
                "cycle_id": cycle_id,
                "calls": [{
                    "research_index": 0,
                    "call_index": 0,
                    "specialist_stage_id": "specialist",
                    "tool": "Example Connector",
                    "result_origin": "connector_response",
                    "observed_at": "2026-09-18T19:00:00Z",
                    "source_refs": [],
                    "result_sha256": "a" * 64,
                }],
            },
        )

        self.assertIn(
            f"tool-provenance:{cycle_id}:call_0:"
            "capture_fields_missing",
            verify_artifact_records(
                self.journal.read(),
                profile_root=self.root,
            ),
        )

    def test_artifact_reference_is_profile_scoped(self):
        records, _ = self.persisted_artifact_records()
        payload = records[-1]["payload"]
        payload["calls"][0]["artifact_ref"] = (
            "profile://0000000000000000/tool-artifacts/sha256/"
            + payload["calls"][0]["result_sha256"][:2]
            + "/"
            + payload["calls"][0]["result_sha256"]
            + ".json"
        )
        self.assertTrue(any(
            error.endswith(":artifact_ref_invalid")
            for error in verify_artifact_records(
                records,
                profile_root=self.root,
            )
        ))


if __name__ == "__main__":
    unittest.main()
