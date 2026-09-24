import copy
import hashlib
import json
import unittest

from .host_input_validator import decode_json
from .semantic_patch import SemanticPatchError, materialize_semantic_patch


def _fixture():
    base = {
        "semantic_input_schema_version": 1,
        "cycle_id": "cycle-sample",
        "evidence_calls": [
            {
                "call": {"tool_call_id": "call-a", "result": {"ok": False}},
            },
            {
                "call": {"tool_call_id": "call-b", "result": {"ok": False}},
            },
        ],
        "stage_outputs": {},
    }
    source = (json.dumps(base, indent=2) + "\n").encode()
    digest = hashlib.sha256(source).hexdigest()
    name = "cycle-sample.semantic.json"
    archive = f"cycle-sample.semantic-{digest}.json"
    candidate_id = f"{name}@sha256:{digest}"
    refusal = {
        "candidate_id": candidate_id,
        "input": name,
        "sha256": digest,
        "archive": archive,
        "erased": False,
    }
    patch = {
        "schema_version": 1,
        "base_candidate_id": candidate_id,
        "output_filename": "cycle-sample-v2.semantic.json",
        "operations": [
            {
                "op": "replace",
                "path": "/evidence_calls/1/call/result/ok",
                "expected": False,
                "value": True,
                "array_guards": [{
                    "array_item_path": "/evidence_calls/1",
                    "identity_path": "/evidence_calls/1/call/tool_call_id",
                    "expected": "call-b",
                }],
            },
            {
                "op": "add",
                "path": "/stage_outputs/mcd",
                "value": {"status": "completed", "finding": "host-authored"},
                "array_guards": [],
            },
        ],
    }
    return source, archive, refusal, patch


def _materialize(source, archive, refusal, patch):
    return materialize_semantic_patch(
        json.dumps(patch).encode(),
        archive_name=archive,
        archive_bytes=source,
        refusal=refusal,
    )


def _multiple_trailing_comma_fixture():
    source, _archive, refusal, patch = _fixture()
    malformed = source.replace(b'"ok": false\n', b'"ok": false,\n')
    offsets = [
        index for index in range(len(malformed))
        if malformed[index:index + 1] == b","
        and malformed[index - 5:index] == b"false"
    ]
    assert len(offsets) == 2
    digest = hashlib.sha256(malformed).hexdigest()
    refusal = {
        **refusal,
        "sha256": digest,
        "candidate_id": f"{refusal['input']}@sha256:{digest}",
        "archive": f"cycle-sample.semantic-{digest}.json",
    }
    patch = {
        **patch,
        "schema_version": 2,
        "base_candidate_id": refusal["candidate_id"],
        "operations": [],
        "lexical_edits": [{
            "op": "delete",
            "offset": offset,
            "expected": ",",
            "before": malformed[offset - 16:offset].decode(),
            "after": malformed[offset + 1:offset + 17].decode(),
        } for offset in offsets],
    }
    return source, malformed, refusal, patch


class SemanticPatchTests(unittest.TestCase):
    def test_materializes_full_source_and_immediate_lineage(self):
        source, archive, refusal, patch = _fixture()
        result = _materialize(source, archive, refusal, patch)
        output = decode_json(result.source_bytes.decode())
        self.assertEqual(result.output_name, patch["output_filename"])
        self.assertEqual(
            result.source_sha256,
            hashlib.sha256(result.source_bytes).hexdigest(),
        )
        self.assertEqual(result.base_candidate_id, refusal["candidate_id"])
        self.assertEqual(output["corrects_candidate_id"], refusal["candidate_id"])
        self.assertFalse(output["evidence_calls"][0]["call"]["result"]["ok"])
        self.assertTrue(output["evidence_calls"][1]["call"]["result"]["ok"])
        self.assertEqual(
            output["stage_outputs"]["mcd"]["finding"], "host-authored"
        )
        self.assertNotIn("operations", output)
        self.assertEqual(decode_json(source.decode())["stage_outputs"], {})

    def test_archive_bytes_and_ledger_must_agree(self):
        source, archive, refusal, patch = _fixture()
        for change in (
            {"sha256": "0" * 64},
            {"candidate_id": "other@sha256:" + refusal["sha256"]},
            {"archive": "unrelated.json"},
            {"erased": True},
        ):
            with self.subTest(change=change):
                altered = {**refusal, **change}
                with self.assertRaisesRegex(
                    SemanticPatchError, "base_identity"
                ):
                    _materialize(source, archive, altered, patch)
        with self.assertRaisesRegex(SemanticPatchError, "base_identity"):
            _materialize(source + b" ", archive, refusal, patch)

    def test_unparseable_or_duplicate_key_base_is_not_repaired(self):
        source, archive, refusal, patch = _fixture()
        for corrupt in (
            b'{"semantic_input_schema_version":1,',
            b'{"semantic_input_schema_version":1,'
            b'"semantic_input_schema_version":1}',
        ):
            digest = hashlib.sha256(corrupt).hexdigest()
            changed = {
                **refusal,
                "sha256": digest,
                "candidate_id": f"{refusal['input']}@sha256:{digest}",
                "archive": f"cycle-sample.semantic-{digest}.json",
            }
            changed_patch = {
                **patch,
                "base_candidate_id": changed["candidate_id"],
            }
            with self.assertRaisesRegex(SemanticPatchError, "base_decode"):
                _materialize(
                    corrupt, changed["archive"], changed, changed_patch
                )
        self.assertEqual(source, _fixture()[0])

    def test_hash_bound_lexical_insert_still_requires_semantic_edits(self):
        source, archive, refusal, patch = _fixture()
        missing = source.replace(b'      }\n    }\n', b'      \n    }\n', 1)
        self.assertNotEqual(missing, source)
        offset = missing.index(b'      \n    }\n') + 6
        digest = hashlib.sha256(missing).hexdigest()
        refusal = {
            **refusal,
            "sha256": digest,
            "candidate_id": f"{refusal['input']}@sha256:{digest}",
            "archive": f"cycle-sample.semantic-{digest}.json",
        }
        patch["base_candidate_id"] = refusal["candidate_id"]
        patch["lexical_edit"] = {
            "offset": offset,
            "insert": "}",
            "before": missing[offset - 16:offset].decode(),
            "after": missing[offset:offset + 16].decode(),
        }
        result = _materialize(
            missing, refusal["archive"], refusal, patch
        )
        output = decode_json(result.source_bytes.decode())
        self.assertEqual(output["corrects_candidate_id"], refusal["candidate_id"])
        self.assertTrue(output["evidence_calls"][1]["call"]["result"]["ok"])
        self.assertEqual(output["stage_outputs"]["mcd"]["status"], "completed")
        lexical_only = {**patch, "operations": []}
        parsed = decode_json(_materialize(
            missing, refusal["archive"], refusal, lexical_only
        ).source_bytes.decode())
        self.assertFalse(
            parsed["evidence_calls"][1]["call"]["result"]["ok"]
        )
        self.assertEqual(
            parsed["corrects_candidate_id"], refusal["candidate_id"]
        )
        bad_patch = copy.deepcopy(patch)
        bad_patch["lexical_edit"]["before"] = "incorrect context"
        with self.assertRaisesRegex(SemanticPatchError, "lexical_preimage"):
            _materialize(missing, refusal["archive"], refusal, bad_patch)
        with self.assertRaisesRegex(SemanticPatchError, "lexical_not_needed"):
            _materialize(source, archive, _fixture()[2], {
                **patch,
                "base_candidate_id": _fixture()[2]["candidate_id"],
            })

    def test_v2_deletes_multiple_trailing_commas_against_original_bytes(self):
        source, malformed, refusal, patch = (
            _multiple_trailing_comma_fixture()
        )
        result = _materialize(
            malformed, refusal["archive"], refusal, patch
        )
        value = decode_json(result.source_bytes.decode())
        self.assertEqual(
            value["corrects_candidate_id"], refusal["candidate_id"]
        )
        expected = decode_json(source.decode())
        expected["corrects_candidate_id"] = refusal["candidate_id"]
        self.assertEqual(value, expected)
        reordered = {
            **patch,
            "lexical_edits": list(reversed(patch["lexical_edits"])),
        }
        self.assertEqual(
            _materialize(
                malformed, refusal["archive"], refusal, reordered
            ).source_bytes,
            result.source_bytes,
        )
        context_only = copy.deepcopy(patch)
        for edit in context_only["lexical_edits"]:
            offset = edit.pop("offset")
            edit["before"] = malformed[offset - 64:offset].decode()
            edit["after"] = malformed[offset + 1:offset + 65].decode()
        self.assertEqual(
            _materialize(
                malformed, refusal["archive"], refusal, context_only
            ).source_bytes,
            result.source_bytes,
        )

    def test_v2_rejects_partial_tampered_or_overlapping_lexical_edits(self):
        source, malformed, refusal, patch = (
            _multiple_trailing_comma_fixture()
        )
        for changed, error in (
            (
                {**patch, "lexical_edits": patch["lexical_edits"][:1]},
                "lexical_output",
            ),
            (
                {**patch, "lexical_edits": [
                    {**patch["lexical_edits"][0], "before": "wrong-context"},
                    patch["lexical_edits"][1],
                ]},
                "lexical_preimage",
            ),
            (
                {**patch, "lexical_edits": [
                    patch["lexical_edits"][0],
                    patch["lexical_edits"][0],
                ]},
                "lexical_overlap",
            ),
            (
                {**patch, "lexical_edits": [
                    {**patch["lexical_edits"][0], "offset": True},
                    patch["lexical_edits"][1],
                ]},
                "lexical_shape",
            ),
            (
                {**patch, "lexical_edits": [
                    {**patch["lexical_edits"][0], "expected": "}"},
                    patch["lexical_edits"][1],
                ]},
                "lexical_preimage",
            ),
            (
                {**patch, "lexical_edits": [
                    {
                        **{
                            key: value for key, value in edit.items()
                            if key != "offset"
                        },
                        "before": edit["before"][-8:],
                        "after": edit["after"][:8],
                    }
                    for edit in patch["lexical_edits"]
                ]},
                "lexical_context_not_unique",
            ),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(SemanticPatchError, error):
                    _materialize(
                        malformed, refusal["archive"], refusal, changed
                    )
        with self.assertRaisesRegex(SemanticPatchError, "lexical_not_needed"):
            _materialize(source, _fixture()[1], _fixture()[2], {
                **patch,
                "base_candidate_id": _fixture()[2]["candidate_id"],
            })
        with self.assertRaisesRegex(SemanticPatchError, "lexical_shape"):
            _materialize(
                malformed, refusal["archive"], refusal,
                {**patch, "lexical_edits": patch["lexical_edits"] * 9},
            )
        changed_source = malformed.replace(b"call-a", b"call,a", 1)
        digest = hashlib.sha256(changed_source).hexdigest()
        changed_refusal = {
            **refusal,
            "sha256": digest,
            "candidate_id": f"{refusal['input']}@sha256:{digest}",
            "archive": f"cycle-sample.semantic-{digest}.json",
        }
        inside = changed_source.index(b"call,a") + len(b"call")
        unsafe = {
            **patch,
            "base_candidate_id": changed_refusal["candidate_id"],
            "lexical_edits": [
                *patch["lexical_edits"],
                {
                    "op": "delete",
                    "offset": inside,
                    "expected": ",",
                    "before": changed_source[inside - 16:inside].decode(),
                    "after": changed_source[inside + 1:inside + 17].decode(),
                },
            ],
        }
        with self.assertRaisesRegex(
            SemanticPatchError, "lexical_inside_string"
        ):
            _materialize(
                changed_source,
                changed_refusal["archive"],
                changed_refusal,
                unsafe,
            )

    def test_array_guards_detect_reordered_or_changed_items(self):
        source, archive, refusal, patch = _fixture()
        for guards in (
            [],
            [{
                "array_item_path": "/evidence_calls/1",
                "identity_path": "/evidence_calls/1/call/tool_call_id",
                "expected": "call-a",
            }],
        ):
            changed = copy.deepcopy(patch)
            changed["operations"][0]["array_guards"] = guards
            with self.assertRaises(SemanticPatchError):
                _materialize(source, archive, refusal, changed)

    def test_preimage_and_presence_are_checked_before_any_edit(self):
        source, archive, refusal, patch = _fixture()
        for op, expected in (
            ({"path": "/evidence_calls/1/call/result/ok",
              "expected": 0}, "preimage_changed"),
            ({"path": "/evidence_calls/1/call/result/unknown",
              "expected": None}, "preimage_changed"),
        ):
            changed = copy.deepcopy(patch)
            changed["operations"][0].update(op)
            with self.assertRaisesRegex(SemanticPatchError, expected):
                _materialize(source, archive, refusal, changed)
        changed = copy.deepcopy(patch)
        changed["operations"][1]["path"] = "/stage_outputs"
        with self.assertRaisesRegex(SemanticPatchError, "add_existing"):
            _materialize(source, archive, refusal, changed)

    def test_no_unchecked_array_insertions_or_overlapping_edits(self):
        source, archive, refusal, patch = _fixture()
        changed = copy.deepcopy(patch)
        changed["operations"][1]["path"] = "/evidence_calls/2"
        with self.assertRaisesRegex(SemanticPatchError, "add_array_item"):
            _materialize(source, archive, refusal, changed)
        changed = copy.deepcopy(patch)
        changed["operations"].append(copy.deepcopy(changed["operations"][0]))
        with self.assertRaisesRegex(SemanticPatchError, "overlapping_paths"):
            _materialize(source, archive, refusal, changed)

    def test_rejects_lineage_edits_bad_pointers_and_unsafe_filenames(self):
        source, archive, refusal, patch = _fixture()
        for pointer in (
            "/corrects_candidate_id",
            "/semantic_input_schema_version",
        ):
            changed = copy.deepcopy(patch)
            changed["operations"][1]["path"] = pointer
            with self.assertRaisesRegex(SemanticPatchError, "immutable_field"):
                _materialize(source, archive, refusal, changed)
        changed = copy.deepcopy(patch)
        changed["operations"][1]["path"] = "/stage_outputs/~2bad"
        with self.assertRaisesRegex(SemanticPatchError, "pointer_escape"):
            _materialize(source, archive, refusal, changed)
        changed = {**patch, "output_filename": "../cycle-v2.semantic.json"}
        with self.assertRaisesRegex(SemanticPatchError, "output_filename"):
            _materialize(source, archive, refusal, changed)
        changed = {
            **patch,
            "output_filename": "x" * 112 + ".semantic.json",
        }
        with self.assertRaisesRegex(SemanticPatchError, "output_filename"):
            _materialize(source, archive, refusal, changed)

    def test_patch_document_rejects_duplicate_keys_and_unknown_fields(self):
        source, archive, refusal, patch = _fixture()
        for document in (
            b'{"schema_version":1,"schema_version":1}',
            json.dumps({**patch, "accepted": True}).encode(),
        ):
            with self.assertRaises(SemanticPatchError):
                materialize_semantic_patch(
                    document,
                    archive_name=archive,
                    archive_bytes=source,
                    refusal=refusal,
                )


if __name__ == "__main__":
    unittest.main()
