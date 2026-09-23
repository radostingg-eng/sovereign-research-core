import json
import pathlib
import tempfile
import unittest

from .accepted_inputs import input_fingerprint
from .host_input_validator import (
    UnsafeDuplicateJsonKeyError,
    diagnostic_decode_json,
    exclude_persisted_inputs,
    select_input_paths,
    validate_path,
    validate_paths,
)


class DiagnosticDecodeJsonTests(unittest.TestCase):
    def test_identical_duplicate_scalars_collapse_and_are_reported(self):
        value, merged = diagnostic_decode_json(
            '{"cycle_id":"cycle-A","cycle_id":"cycle-A"}'
        )
        self.assertEqual(value, {"cycle_id": "cycle-A"})
        self.assertEqual(merged, ["cycle_id"])

    def test_duplicate_lists_concatenate_without_dropping_authored_items(self):
        value, merged = diagnostic_decode_json(
            '{"findings":[{"id":"a"}],"findings":[{"id":"b"}]}'
        )
        self.assertEqual(
            value,
            {"findings": [{"id": "a"}, {"id": "b"}]},
        )
        self.assertEqual(merged, ["findings"])

    def test_conflicting_scalar_duplicates_are_refused_not_merged(self):
        with self.assertRaises(UnsafeDuplicateJsonKeyError) as ctx:
            diagnostic_decode_json(
                '{"cycle_id":"first","cycle_id":"second"}'
            )
        self.assertEqual(ctx.exception.key, "cycle_id")
        self.assertEqual(
            ctx.exception.reason,
            "conflicting_scalar_or_object",
        )

    def test_conflicting_objects_are_refused_not_merged(self):
        with self.assertRaises(UnsafeDuplicateJsonKeyError):
            diagnostic_decode_json(
                '{"snapshot":{"a":1},"snapshot":{"b":2}}'
            )

    def test_malformed_json_raises_decode_error(self):
        with self.assertRaises(json.JSONDecodeError):
            diagnostic_decode_json('{"broken":}')


class ChangedInputSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = pathlib.Path(tempfile.mkdtemp(prefix="validator-"))
        (self.directory / "old.json").write_text("{}", encoding="utf-8")
        (self.directory / "new.json").write_text("{}", encoding="utf-8")
        (self.directory / "FEEDBACK.json").write_text("{}", encoding="utf-8")
        (self.directory / ".high_water.json").write_text("{}", encoding="utf-8")

    def test_only_changed_inputs_are_selected(self):
        paths = select_input_paths(
            self.directory,
            changed_names=[str(self.directory / "new.json")],
        )
        self.assertEqual([path.name for path in paths], ["new.json"])

    def test_feedback_and_dotfiles_are_never_inputs(self):
        paths = select_input_paths(
            self.directory,
            changed_names=[
                str(self.directory / "FEEDBACK.json"),
                str(self.directory / ".high_water.json"),
            ],
        )
        self.assertEqual(paths, [])

    def test_manual_validation_selects_only_the_newest_name(self):
        paths = select_input_paths(self.directory)
        self.assertEqual([path.name for path in paths], ["old.json"])

    def test_manual_validation_skips_an_immutable_persisted_cycle(self):
        path = self.directory / "old.json"
        value = {"cycle_id": "cycle-old"}
        path.write_text(json.dumps(value), encoding="utf-8")
        selected = exclude_persisted_inputs(
            [path],
            [{
                "record_type": "cycle_receipt",
                "record_id": "cycle-receipt:cycle-old",
                "payload": {
                    "snapshot_id": f"old:{input_fingerprint(value)}",
                },
            }],
        )
        self.assertEqual(selected, [])

    def test_manual_validation_keeps_a_rewritten_persisted_cycle(self):
        path = self.directory / "old.json"
        path.write_text(
            json.dumps({"cycle_id": "cycle-old", "changed": True}),
            encoding="utf-8",
        )
        selected = exclude_persisted_inputs(
            [path],
            [{
                "record_type": "cycle_receipt",
                "record_id": "cycle-receipt:cycle-old",
                "payload": {"snapshot_id": "old:different-fingerprint"},
            }],
        )
        self.assertEqual(selected, [path])


class ValidationFeedbackPreservesExecutorDataTests(unittest.TestCase):
    def test_updating_a_refusal_preserves_rich_feedback(self):
        from .host_feedback import write_validation_feedback

        directory = pathlib.Path(tempfile.mkdtemp(prefix="feedback-merge-"))
        path = directory / "FEEDBACK.json"
        path.write_text(json.dumps({
            "strategy_coverage": {"families_used": {"hedging": 1}},
            "delivery_probes": {"missing": ["active_memory"]},
        }), encoding="utf-8")
        write_validation_feedback(
            directory,
            checked=["bad.json"],
            refusals=[{
                "input": "bad.json",
                "reason": "ValueError: invalid_host_input:bad.json:missing_as_of",
            }],
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("strategy_coverage", value)
        self.assertIn("delivery_probes", value)
        self.assertEqual(value["last_validation"]["checked"], ["bad.json"])

    def test_historical_invalid_files_are_not_revalidated(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="historical-"))
        old = directory / "old.json"
        new = directory / "new.json"
        old.write_text("{bad", encoding="utf-8")
        new.write_text(json.dumps({"not": "a cycle"}), encoding="utf-8")
        selected = select_input_paths(
            directory, changed_names=[str(new)])
        refusals = validate_paths(selected)
        self.assertEqual([row["input"] for row in refusals], ["new.json"])
        self.assertNotIn("old.json", json.dumps(refusals))

    def test_duplicate_keys_are_not_silently_overwritten(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="duplicates-"))
        path = directory / "duplicate.json"
        path.write_text(
            '{"cycle_id":"first","cycle_id":"second"}',
            encoding="utf-8",
        )
        refusal = validate_path(path)
        self.assertIsNotNone(refusal)
        self.assertIn("duplicate_json_key:cycle_id", refusal["reason"])

    def test_staging_refusal_recurrence_survives_feedback_updates(self):
        from .host_feedback import write_validation_feedback

        directory = pathlib.Path(tempfile.mkdtemp(prefix="recurrence-"))
        for name in ("first.json", "second.json"):
            write_validation_feedback(
                directory,
                checked=[name],
                refusals=[{
                    "input": name,
                    "reason": (
                        "ValueError: invalid_host_input:"
                        f"{name}:missing_as_of"
                    ),
                }],
            )
        value = json.loads(
            (directory / "FEEDBACK.json").read_text(encoding="utf-8"))
        recurrence = value["refusal_recurrence"]
        self.assertEqual(
            recurrence["counts_by_code"]["missing_as_of"],
            2,
        )
        self.assertEqual(
            recurrence["repeated"][0]["latest_input"],
            "second.json",
        )
        self.assertEqual(
            recurrence["inputs_by_code"]["missing_as_of"],
            ["first.json", "second.json"],
        )

    def test_staging_refusal_recurrence_is_idempotent_per_input(self):
        from .host_feedback import write_validation_feedback

        directory = pathlib.Path(tempfile.mkdtemp(prefix="recurrence-id-"))
        refusal = {
            "input": "same.json",
            "reason": (
                "ValueError: invalid_host_input:"
                "same.json:missing_as_of"
            ),
        }
        write_validation_feedback(
            directory,
            checked=["same.json"],
            refusals=[refusal],
        )
        write_validation_feedback(
            directory,
            checked=["same.json"],
            refusals=[refusal],
        )
        value = json.loads(
            (directory / "FEEDBACK.json").read_text(encoding="utf-8"))
        recurrence = value["refusal_recurrence"]
        self.assertEqual(
            recurrence["counts_by_code"]["missing_as_of"],
            1,
        )
        self.assertEqual(recurrence["repeated"], [])

    def test_staging_refusal_recurrence_repairs_count_only_feedback(self):
        from .host_feedback import write_validation_feedback

        directory = pathlib.Path(tempfile.mkdtemp(prefix="recurrence-old-"))
        feedback = directory / "FEEDBACK.json"
        feedback.write_text(json.dumps({
            "refusal_recurrence": {
                "counts_by_code": {"missing_as_of": 2},
                "repeated": [{
                    "code": "missing_as_of",
                    "occurrences": 2,
                    "latest_input": "same.json",
                }],
            },
        }), encoding="utf-8")
        refusal = {
            "input": "same.json",
            "reason": (
                "ValueError: invalid_host_input:"
                "same.json:missing_as_of"
            ),
        }
        write_validation_feedback(
            directory,
            checked=["same.json"],
            refusals=[refusal],
        )
        value = json.loads(feedback.read_text(encoding="utf-8"))
        recurrence = value["refusal_recurrence"]
        self.assertEqual(
            recurrence["inputs_by_code"]["missing_as_of"],
            ["same.json"],
        )
        self.assertEqual(
            recurrence["counts_by_code"]["missing_as_of"],
            1,
        )
        self.assertEqual(recurrence["repeated"], [])

    def test_r32_collapses_to_three_root_refusals(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        paths = list((root / "host_staging" / "rejected").glob(
            "cycle-20260917T060030Z-r32x1-*.json"))
        self.assertEqual(len(paths), 1)
        refusal = validate_path(paths[0])
        self.assertIsNotNone(refusal)
        reason = refusal["reason"]
        self.assertIn("lesson_missing_evidence:0", reason)
        self.assertIn("memory_distillation_not_performed_object", reason)
        self.assertIn(
            "tool_manifest_lookalike_key_unsupported:tool_manifest",
            reason,
        )
        preview = next(
            code for code in reason.split(",")
            if code.startswith(
                "tool_manifest_lookalike_contract_preview:tool_manifest:")
        )
        self.assertIn("missing_fields=observed_at", preview)
        self.assertIn("missing_known_ibkr=create alert", preview)
        self.assertNotIn("memory_distillation_missing:", reason)


if __name__ == "__main__":
    unittest.main()
