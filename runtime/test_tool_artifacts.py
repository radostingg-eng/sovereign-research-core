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
from .tool_provenance import build_tool_provenance_index
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
