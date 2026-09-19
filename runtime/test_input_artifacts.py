import json
import tempfile
import unittest
from pathlib import Path

from .accepted_inputs import input_fingerprint
from .audit_store import AuditJournal
from .host_input_validator import validate_path
from .integrity import check_input_artifact_orphans
from .input_artifacts import (
    InputArtifactError,
    OUT_OF_LINE_RESULT_THRESHOLD_BYTES,
    input_document_from_value,
    load_input_data,
)
from .run_host_cycle import run_one
from .test_run_host_cycle import v4_post_effective_full_cycle
from .tool_artifacts import canonical_json_bytes, content_sha256


def reference(content):
    encoded = canonical_json_bytes(content)
    return {
        "$artifact": {
            "schema_version": 1,
            "sha256": content_sha256(encoded),
            "byte_length": len(encoded),
            "media_type": "application/json",
        },
    }


class InputArtifactReferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="input-artifacts-"))
        self.input_dir = self.root / "host_input"
        self.input_dir.mkdir()

    def store(self, content):
        encoded = canonical_json_bytes(content)
        digest = content_sha256(encoded)
        path = (
            self.root
            / "tool_artifacts"
            / "sha256"
            / digest[:2]
            / f"{digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        return path, reference(content)

    def data(self, result, *, version=4):
        return {
            "host_input_schema_version": version,
            "cycle_id": "cycle-large-reference",
            "research": [{
                "specialist_stage_id": "specialist",
                "tool_calls": [{
                    "tool_call_id": "large-call",
                    "kind": "connector_lookup",
                    "tool": "Test Connector",
                    "call": {"action": "lookup", "arguments": {}},
                    "result": result,
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
                }],
            }],
            "cognitive_stages": [],
        }

    def test_reference_hydrates_but_identity_stays_reference_only(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        _path, ref = self.store(body)
        raw = self.data(ref)

        document = input_document_from_value(
            raw,
            profile_root=self.root,
        )

        hydrated_result = document.hydrated["research"][0][
            "tool_calls"
        ][0]["result"]
        normalized_result = document.normalized["research"][0][
            "tool_calls"
        ][0]["result"]
        self.assertEqual(hydrated_result, body)
        self.assertEqual(normalized_result, ref)
        self.assertEqual(
            input_fingerprint(document.hydrated),
            input_fingerprint(document.normalized),
        )

    def test_large_inline_result_is_refused_before_receipt(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}

        with self.assertRaisesRegex(
            InputArtifactError,
            "large_tool_result_requires_artifact_ref",
        ):
            input_document_from_value(
                self.data(body),
                profile_root=self.root,
            )

    def test_reference_requires_v4(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        _path, ref = self.store(body)

        with self.assertRaisesRegex(
            InputArtifactError,
            "input_artifact_requires_schema_v4",
        ):
            input_document_from_value(
                self.data(ref, version=3),
                profile_root=self.root,
            )

    def test_missing_hash_size_and_noncanonical_files_fail(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        path, ref = self.store(body)
        raw = self.data(ref)

        path.unlink()
        with self.assertRaisesRegex(
            InputArtifactError,
            "input_artifact_missing",
        ):
            input_document_from_value(raw, profile_root=self.root)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes({"different": True}))
        with self.assertRaisesRegex(
            InputArtifactError,
            "input_artifact_size_mismatch|input_artifact_hash_mismatch",
        ):
            input_document_from_value(raw, profile_root=self.root)

        encoded = json.dumps(body, indent=2).encode("utf-8")
        ref["$artifact"]["sha256"] = content_sha256(encoded)
        ref["$artifact"]["byte_length"] = len(encoded)
        noncanonical = (
            self.root
            / "tool_artifacts"
            / "sha256"
            / ref["$artifact"]["sha256"][:2]
            / f"{ref['$artifact']['sha256']}.json"
        )
        noncanonical.parent.mkdir(parents=True, exist_ok=True)
        noncanonical.write_bytes(encoded)
        with self.assertRaisesRegex(
            InputArtifactError,
            "input_artifact_not_canonical_json",
        ):
            input_document_from_value(raw, profile_root=self.root)

    def test_symlink_and_recursive_reference_fail(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        path, ref = self.store(body)
        actual = path.with_suffix(".actual")
        path.replace(actual)
        path.symlink_to(actual)
        with self.assertRaisesRegex(
            InputArtifactError,
            "input_artifact_symlink_forbidden",
        ):
            input_document_from_value(
                self.data(ref),
                profile_root=self.root,
            )

        recursive = reference({"value": 1})
        recursive_body = {"$artifact": recursive["$artifact"]}
        recursive_path, recursive_ref = self.store(recursive_body)
        self.assertTrue(recursive_path.exists())
        with self.assertRaisesRegex(
            InputArtifactError,
            "input_artifact_recursive_reference",
        ):
            input_document_from_value(
                self.data(recursive_ref),
                profile_root=self.root,
            )

    def test_staged_validation_fails_loudly_when_artifact_is_missing(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        ref = reference(body)
        path = self.root / "host_staging" / "cycle.json"
        path.parent.mkdir()
        path.write_text(json.dumps(self.data(ref)), encoding="utf-8")

        refusal = validate_path(
            path,
            records=[],
            canonical_input_dir=self.input_dir,
        )

        self.assertIn("input_artifact_missing", refusal["reason"])

    def test_shared_loader_does_not_silently_skip_tampering(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        path, ref = self.store(body)
        input_path = self.input_dir / "cycle.json"
        input_path.write_text(json.dumps(self.data(ref)), encoding="utf-8")
        path.write_bytes(b"{}\n")

        with self.assertRaises(InputArtifactError):
            load_input_data(input_path)

    def test_orphan_artifact_is_reported_and_rejected_archive_preserves_it(self):
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        path, ref = self.store(body)

        failures = check_input_artifact_orphans([], self.root)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].defect, "orphan")

        rejected = self.root / "host_staging" / "rejected"
        rejected.mkdir(parents=True)
        (rejected / "candidate.json").write_text(
            json.dumps(self.data(ref)),
            encoding="utf-8",
        )
        self.assertTrue(path.is_file())
        self.assertEqual(
            check_input_artifact_orphans([], self.root),
            [],
        )

    def test_host_input_readers_use_shared_artifact_loader(self):
        root = Path(__file__).resolve().parent
        modules = (
            "forecasts.py",
            "forecast_outcomes.py",
            "instruction_reconciliation.py",
            "opportunity_ledger.py",
            "research_value.py",
            "strategy_coverage.py",
        )
        for name in modules:
            with self.subTest(module=name):
                text = (root / name).read_text(encoding="utf-8")
                self.assertIn("load_input_data", text)
                self.assertNotIn(
                    "json.loads(path.read_text",
                    text,
                )

    def test_reference_cycle_keeps_large_body_out_of_journal(self):
        data = v4_post_effective_full_cycle(
            cycle_id="cycle-large-journal",
        )
        body = {"body": "x" * OUT_OF_LINE_RESULT_THRESHOLD_BYTES}
        _path, ref = self.store(body)
        data["research"][0]["tool_calls"][0]["result"] = ref
        input_path = self.input_dir / "cycle.json"
        input_path.write_text(json.dumps(data), encoding="utf-8")
        journal = AuditJournal(self.root / "audit" / "journal.jsonl")
        journal.append(
            record_id="profile-genesis",
            record_type="profile_genesis",
            agent="test",
            payload={"schema_version": 1},
        )

        run_one(input_path, journal)

        journal_bytes = journal.path.read_bytes()
        self.assertNotIn(b"x" * 1000, journal_bytes)
        self.assertTrue(any(
            row.get("record_type") == "cycle_finalization"
            for row in journal.read()
        ))

    def test_existing_input_corpus_fingerprints_are_unchanged(self):
        root = Path(__file__).resolve().parent.parent
        paths = sorted((root / "host_input").glob("*.json"))
        if not paths:
            self.skipTest("profile input corpus unavailable")
        checked = 0
        malformed = 0
        for path in paths:
            if path.name == "FEEDBACK.json":
                continue
            with self.subTest(path=path.name):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                document = input_document_from_value(
                    raw,
                    profile_root=root,
                )
                self.assertEqual(
                    input_fingerprint(raw),
                    input_fingerprint(document.hydrated),
                )
                checked += 1
        self.assertGreater(checked, 0)
        self.assertGreaterEqual(malformed, 1)


if __name__ == "__main__":
    unittest.main()
