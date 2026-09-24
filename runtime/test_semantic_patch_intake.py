import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from .host_input_validator import decode_json
from .staged_intake import process_staging
from .test_semantic_candidate import semantic_candidate


class SemanticPatchIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.staging = self.root / "host_staging"
        self.inputs = self.root / "host_input"
        self.staging.mkdir()
        self.inputs.mkdir()
        (self.inputs / ".promotion_policy.json").write_text(
            json.dumps({"schema_version": 1, "legacy_files": []}),
            encoding="utf-8",
        )

    def _refused_base(self):
        value = semantic_candidate()
        observations = value["stage_outputs"]["adversarial"].pop(
            "observations"
        )
        path = self.staging / "cycle-semantic.semantic.json"
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )
        self.assertEqual(promoted, [])
        self.assertEqual(len(refused), 1)
        return refused[0], observations

    def _patch(self, refusal, observations):
        patch = {
            "schema_version": 1,
            "base_candidate_id": refusal["candidate_id"],
            "output_filename": "cycle-semantic-fixed.semantic.json",
            "operations": [{
                "op": "add",
                "path": "/stage_outputs/adversarial/observations",
                "value": observations,
                "array_guards": [],
            }],
        }
        path = self.staging / "cycle-semantic-fix.semantic-patch.json"
        path.write_text(json.dumps(patch, indent=2), encoding="utf-8")
        return path

    def test_retry_contract_precedes_large_history_after_write_and_refresh(
        self,
    ):
        refusal, _observations = self._refused_base()
        path = self.staging / "FEEDBACK.json"
        first = path.read_text(encoding="utf-8")
        self.assertLess(first.index('\n  "retry_contract":'), 1024)
        self.assertLess(
            first.index('\n  "retry_contract":'),
            first.index('\n  "refused":'),
        )
        previous = json.loads(first)
        retry = previous.pop("retry_contract")
        previous["refusal_recurrence"] = {"large_history": "x" * 100000}
        previous["retry_contract"] = retry
        path.write_text(json.dumps(previous, indent=2), encoding="utf-8")

        process_staging(
            self.staging, self.inputs, records=[], refresh_feedback=True
        )

        refreshed_text = path.read_text(encoding="utf-8")
        self.assertLess(
            refreshed_text.index('\n  "retry_contract":'), 1024
        )
        self.assertLess(
            refreshed_text.index('\n  "retry_contract":'),
            refreshed_text.index('\n  "refusal_recurrence":'),
        )
        refreshed = json.loads(refreshed_text)
        self.assertEqual(
            refreshed["retry_contract"]["corrects_candidate_id"],
            refusal["candidate_id"],
        )
        self.assertEqual(
            len(refreshed["refusal_recurrence"]["large_history"]),
            100000,
        )
        self.assertIn("Read retry_contract", refreshed["read_this_first"])

    def test_accepted_source_is_full_and_patch_provenance_is_separate(self):
        refusal, observations = self._refused_base()
        raw_patch = self._patch(refusal, observations)
        patch_sha = hashlib.sha256(raw_patch.read_bytes()).hexdigest()

        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )

        self.assertEqual(refused, [])
        self.assertEqual(promoted, ["cycle-semantic-fixed.json"])
        accepted = list(
            (self.staging / "accepted_sources").glob(
                "cycle-semantic-fixed.semantic-*.json"
            )
        )
        accepted = [
            path for path in accepted
            if not path.name.endswith(".build.json")
        ]
        self.assertEqual(len(accepted), 1)
        full = decode_json(accepted[0].read_text(encoding="utf-8"))
        self.assertEqual(full["corrects_candidate_id"], refusal["candidate_id"])
        self.assertEqual(
            full["stage_outputs"]["adversarial"]["observations"],
            observations,
        )
        self.assertIn("evidence_calls", full)
        self.assertNotIn("operations", full)
        metadata = json.loads(
            accepted[0].with_suffix(".json.build.json").read_text()
        )
        proof = metadata["patch_provenance"]
        self.assertEqual(proof["sha256"], patch_sha)
        self.assertEqual(proof["base_candidate_id"], refusal["candidate_id"])
        archived = self.staging / proof["archive"]
        self.assertTrue(archived.is_file())
        self.assertFalse(raw_patch.exists())

    def test_downstream_refusal_archives_full_materialized_source(self):
        refusal, _observations = self._refused_base()
        raw_patch = self._patch(refusal, "not-a-list")
        patch_sha = hashlib.sha256(raw_patch.read_bytes()).hexdigest()

        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refused), 1)
        self.assertEqual(
            refused[0]["input"], "cycle-semantic-fixed.semantic.json"
        )
        self.assertIn(
            "cognitive_stage_output_not_list", refused[0]["reason"]
        )
        ledger = [
            json.loads(line) for line in (
                self.staging / "rejected" / "REJECTIONS.jsonl"
            ).read_text().splitlines()
        ]
        event = ledger[-1]
        full_archive = self.staging / "rejected" / event["archive"]
        self.assertEqual(
            event["candidate_id"],
            f"cycle-semantic-fixed.semantic.json@sha256:{event['sha256']}",
        )
        self.assertEqual(
            hashlib.sha256(full_archive.read_bytes()).hexdigest(),
            event["sha256"],
        )
        self.assertEqual(
            decode_json(full_archive.read_text())["corrects_candidate_id"],
            refusal["candidate_id"],
        )
        proof = event["patch_provenance"]
        self.assertEqual(proof["sha256"], patch_sha)
        self.assertEqual(
            hashlib.sha256(
                (self.staging / proof["archive"]).read_bytes()
            ).hexdigest(),
            patch_sha,
        )
        self.assertFalse(raw_patch.exists())

    def test_credential_in_patch_is_erased_without_temp_or_archive(self):
        refusal, _observations = self._refused_base()
        raw_patch = self._patch(
            refusal, {"api_key": "test-secret-not-a-real-key"}
        )

        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0]["erased"])
        self.assertFalse(raw_patch.exists())
        self.assertEqual(
            list(self.staging.glob(".semantic-patch-*")), []
        )
        self.assertEqual(
            list((self.staging / "rejected").glob("*patch*.json")), []
        )
        self.assertFalse(
            (self.inputs / "cycle-semantic-fixed.json").exists()
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        explanation = feedback["refused"][0]["what_to_fix"][0]
        self.assertEqual(explanation["code"], "semantic_patch_invalid")
        self.assertIn("pre-image", explanation["fix"])
        self.assertNotIn("runtime defect", explanation["means"])

    def test_malformed_base_needs_lexical_and_semantic_correction(self):
        value = semantic_candidate()
        observations = value["stage_outputs"]["adversarial"].pop(
            "observations"
        )
        raw = (json.dumps(value, indent=2) + "\n").encode()
        for offset in range(16, len(raw) - 16):
            if raw[offset:offset + 1] != b"}":
                continue
            try:
                decode_json((raw[:offset] + raw[offset + 1:]).decode())
            except json.JSONDecodeError:
                break
        else:
            self.fail("semantic fixture has no internal closing brace")
        malformed = raw[:offset] + raw[offset + 1:]
        (self.staging / "cycle-semantic.semantic.json").write_bytes(
            malformed
        )
        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )
        self.assertEqual(promoted, [])
        self.assertEqual(len(refused), 1)
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertEqual(
            feedback["retry_contract"]["refused_source"]["candidate_id"],
            refused[0]["candidate_id"],
        )
        self.assertEqual(
            feedback["retry_contract"]["semantic_patch"]["schema_version"],
            2,
        )
        self.assertIn(
            "lexical_edits_if_malformed",
            feedback["retry_contract"]["semantic_patch"],
        )
        process_staging(
            self.staging, self.inputs, records=[], refresh_feedback=True
        )
        refreshed = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertIn(
            "lexical_edits_if_malformed",
            refreshed["retry_contract"]["semantic_patch"],
        )
        self.assertEqual(
            feedback["retry_contract"]["semantic_patch"][
                "lexical_edits_if_malformed"
            ]["max_edits"],
            16,
        )
        patch = self._patch(refused[0], observations)
        document = json.loads(patch.read_text())
        document["lexical_edit"] = {
            "offset": offset,
            "insert": "}",
            "before": malformed[offset - 16:offset].decode(),
            "after": malformed[offset:offset + 16].decode(),
        }
        patch.write_text(json.dumps(document), encoding="utf-8")

        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )

        self.assertEqual(refused, [])
        self.assertEqual(promoted, ["cycle-semantic-fixed.json"])

    def test_six_trailing_commas_need_six_hash_bound_edits(self):
        source = (json.dumps(semantic_candidate(), indent=2) + "\n").encode()
        start = source.index(b'"evidence_calls": [')
        end = source.index(b"\n  ],", start)
        positions = [
            start + match.start()
            for match in re.finditer(
                rb"\n      }(?=\n    }(?:,|$))", source[start:end]
            )
        ]
        self.assertEqual(len(positions), 6)
        malformed = source
        offsets = []
        for position in positions:
            offset = position + len(offsets)
            malformed = malformed[:offset] + b"," + malformed[offset:]
            offsets.append(offset)
        (self.staging / "cycle-semantic.semantic.json").write_bytes(
            malformed
        )
        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )
        self.assertEqual(promoted, [])
        self.assertEqual(len(refused), 1)
        self.assertEqual(
            hashlib.sha256(
                (self.staging / "rejected" / refused[0]["archive"]).read_bytes()
            ).hexdigest(),
            refused[0]["candidate_id"].rsplit(":", 1)[-1],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertEqual(
            feedback["retry_contract"]["semantic_patch"]["schema_version"],
            2,
        )
        patch = {
            "schema_version": 2,
            "base_candidate_id": refused[0]["candidate_id"],
            "output_filename": "cycle-semantic-fixed.semantic.json",
            "lexical_edits": [{
                "op": "delete",
                "offset": offset,
                "expected": ",",
                "before": malformed[offset - 16:offset].decode(),
                "after": malformed[offset + 1:offset + 17].decode(),
            } for offset in offsets],
            "operations": [],
        }
        incomplete = {
            **patch,
            "lexical_edits": patch["lexical_edits"][:5],
        }
        (
            self.staging / "cycle-semantic-partial.semantic-patch.json"
        ).write_text(json.dumps(incomplete), encoding="utf-8")
        promoted, refused_partial = process_staging(
            self.staging, self.inputs, records=[]
        )
        self.assertEqual(promoted, [])
        self.assertTrue(refused_partial[0]["erased"])
        self.assertIn("lexical_output", refused_partial[0]["reason"])
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        retry = feedback["retry_contract"]
        self.assertEqual(
            retry["refused_source"]["candidate_id"],
            patch["base_candidate_id"],
        )
        self.assertEqual(
            retry["failed_patch_input"],
            "cycle-semantic-partial.semantic-patch.json",
        )
        self.assertEqual(retry["semantic_patch"]["schema_version"], 2)
        process_staging(
            self.staging, self.inputs, records=[], refresh_feedback=True
        )
        refreshed = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )["retry_contract"]
        self.assertEqual(
            refreshed["refused_source"]["candidate_id"],
            patch["base_candidate_id"],
        )
        self.assertEqual(
            refreshed["failed_patch_input"],
            "cycle-semantic-partial.semantic-patch.json",
        )
        (
            self.staging / "cycle-semantic-fixed.semantic-patch.json"
        ).write_text(json.dumps(patch), encoding="utf-8")

        promoted, refused_final = process_staging(
            self.staging, self.inputs, records=[]
        )

        self.assertEqual(refused_final, [])
        self.assertEqual(promoted, ["cycle-semantic-fixed.json"])
        accepted = list(
            (self.staging / "accepted_sources").glob(
                "cycle-semantic-fixed.semantic-*.json"
            )
        )
        accepted = [
            path for path in accepted
            if not path.name.endswith(".build.json")
        ]
        self.assertEqual(len(accepted), 1)
        full = decode_json(accepted[0].read_text())
        self.assertEqual(
            full["corrects_candidate_id"], patch["base_candidate_id"]
        )
        self.assertEqual(full["evidence_calls"],
                         decode_json(source.decode())["evidence_calls"])

    def test_invalid_patch_never_recovers_an_unverified_base(self):
        path = self.staging / "cycle-unknown.semantic-patch.json"
        path.write_text(json.dumps({
            "schema_version": 2,
            "base_candidate_id": (
                "cycle-missing.semantic.json@sha256:" + "0" * 64
            ),
            "output_filename": "cycle-retry.semantic.json",
            "operations": [],
        }), encoding="utf-8")

        promoted, refused = process_staging(
            self.staging, self.inputs, records=[]
        )

        self.assertEqual(promoted, [])
        self.assertTrue(refused[0]["erased"])
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertIsNone(feedback["retry_contract"])


if __name__ == "__main__":
    unittest.main()
