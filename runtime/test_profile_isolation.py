import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .tool_artifacts import (
    build_artifact_specs,
    materialize_artifacts,
    profile_root_for_journal,
    verify_artifact_records,
)
from .tool_provenance import (
    build_tool_provenance_index,
    latest_tool_provenance,
)
from .test_tool_artifacts import v4_call, v4_data


class ProfileIsolationAcceptanceTests(unittest.TestCase):
    def profile(self, name):
        root = Path(tempfile.mkdtemp(prefix=f"profile-{name}-"))
        journal = AuditJournal(root / "audit" / "journal.jsonl")
        journal.append(
            record_id=f"genesis-{name}",
            record_type="profile_genesis",
            agent="test",
            payload={"profile_id": name},
        )
        return root, journal

    def persisted_artifact(self, name):
        root, journal = self.profile(name)
        data = v4_data(v4_call(call_id=f"call-{name}"))
        specs = build_artifact_specs(data, records=journal.read())
        materialize_artifacts(specs, profile_root=root)
        payload = build_tool_provenance_index(
            data,
            cycle_id=f"cycle-{name}",
            artifact_specs=specs,
        )
        journal.append(
            record_id=f"tool-provenance:cycle-{name}",
            record_type="tool_provenance",
            agent="test",
            payload=payload,
        )
        return root, journal, specs

    def test_configured_profile_rejects_foreign_journal(self):
        first, _ = self.profile("one")
        second, journal = self.profile("two")

        with patch.dict(
            os.environ,
            {"SOVEREIGN_PROFILE_DIR": str(first)},
        ):
            with self.assertRaisesRegex(
                ValueError,
                "tool_artifact_journal_outside_profile",
            ):
                profile_root_for_journal(journal.path)
        self.assertNotEqual(first, second)

    def test_one_profile_cannot_satisfy_another_profiles_artifact(self):
        first, journal, _ = self.persisted_artifact("one")
        second, _ = self.profile("two")

        self.assertEqual(
            verify_artifact_records(
                journal.read(),
                profile_root=first,
            ),
            [],
        )
        errors = verify_artifact_records(
            journal.read(),
            profile_root=second,
        )
        self.assertTrue(any(
            error.endswith(":artifact_missing")
            for error in errors
        ))

    def test_concurrent_identical_artifact_writers_are_idempotent(self):
        root, journal = self.profile("concurrent")
        data = v4_data(v4_call(call_id="same-call"))
        specs = build_artifact_specs(data, records=journal.read())
        errors = []

        def write():
            try:
                materialize_artifacts(specs, profile_root=root)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=write) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(
            len(list((root / "tool_artifacts").rglob("*.json"))),
            1,
        )

    def test_feedback_projection_is_bounded_and_body_free(self):
        calls = []
        for index in range(25):
            calls.append({
                "research_index": index,
                "call_index": 0,
                "specialist_stage_id": "specialist",
                "tool": "tool",
                "result_origin": "connector_response",
                "observed_at": "2026-09-19T03:00:00Z",
                "source_refs": [{
                    "kind": "response_id",
                    "value": "x" * 1000,
                }],
                "result_sha256": "a" * 64,
            })
        summary = latest_tool_provenance([{
            "record_id": "tool-provenance:cycle-bounded",
            "record_type": "tool_provenance",
            "caused_by": ["cycle-receipt:cycle-bounded"],
            "payload": {"cycle_id": "cycle-bounded", "calls": calls},
        }])
        encoded = json.dumps(summary)

        self.assertEqual(len(summary["rows"]), 10)
        self.assertEqual(summary["not_shown"], 15)
        self.assertNotIn('"result":', encoded)
        self.assertLess(len(encoded), 20_000)


if __name__ == "__main__":
    unittest.main()
