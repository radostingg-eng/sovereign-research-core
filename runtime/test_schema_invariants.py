import json
import tempfile
import unittest
from pathlib import Path

from .schema_invariants import (
    CANONICAL_EXAMPLE_PATH,
    canonical_json,
    feedback_safe_example,
    inspect_example,
    inspect_staging_feedback,
    runtime_read_field_names,
)


class CanonicalSchemaInvariantTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="schema-invariants-"))
        self.schema = self.root / "example.json"
        self.consumer = self.root / "consumer.py"
        self.consumer.write_text(
            'def consume(data):\n    return data.get("known")\n',
            encoding="utf-8",
        )

    def test_the_committed_example_is_canonical_and_consumed(self):
        _, violations = inspect_example()
        self.assertEqual(violations, [])

    def test_semantically_equal_minified_json_is_refused(self):
        self.schema.write_text('{"known":{"nested":true}}', encoding="utf-8")
        _, violations = inspect_example(self.schema, self.consumer)
        self.assertIn("canonical_schema_not_pretty_printed", violations)

    def test_an_ignored_top_level_field_is_refused_and_not_republished(self):
        value = {"known": 1, "guidance_the_runtime_ignores": {"wrong": True}}
        self.schema.write_text(canonical_json(value), encoding="utf-8")
        example, violations = inspect_example(self.schema, self.consumer)
        self.assertIn(
            "canonical_schema_field_not_consumed:"
            "guidance_the_runtime_ignores",
            violations,
        )
        self.assertEqual(
            feedback_safe_example(example, self.consumer),
            {"known": 1},
        )

    def test_malformed_json_is_reported_with_location(self):
        self.schema.write_text('{"known": [1, 2}', encoding="utf-8")
        example, violations = inspect_example(self.schema, self.consumer)
        self.assertEqual(example, {})
        self.assertRegex(
            violations[0],
            r"canonical_schema_invalid_json:line=1:column=\d+:char=\d+",
        )

    def test_runtime_field_names_come_from_ast_not_comments(self):
        self.consumer.write_text(
            '# data.get("comment_only")\n'
            'def consume(data):\n'
            '    return data["subscript"]\n',
            encoding="utf-8",
        )
        self.assertEqual(
            runtime_read_field_names(self.consumer),
            {"subscript"},
        )

    def test_committed_text_is_the_deterministic_serialization(self):
        value = json.loads(CANONICAL_EXAMPLE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            CANONICAL_EXAMPLE_PATH.read_text(encoding="utf-8"),
            canonical_json(value),
        )

    def test_stale_feedback_shape_and_status_are_refused(self):
        self.schema.write_text(
            canonical_json({"known": 1}),
            encoding="utf-8",
        )
        feedback = self.root / "FEEDBACK.json"
        feedback.write_text(json.dumps({
            "expected_input_shape": {"known": 1, "stale": True},
        }), encoding="utf-8")
        self.assertEqual(
            inspect_staging_feedback(
                feedback,
                self.schema,
                self.consumer,
            ),
            [
                "staging_feedback_expected_input_shape_stale",
                "staging_feedback_schema_status_stale",
            ],
        )

    def test_current_feedback_projection_passes(self):
        self.schema.write_text(
            canonical_json({"known": 1}),
            encoding="utf-8",
        )
        feedback = self.root / "FEEDBACK.json"
        feedback.write_text(json.dumps({
            "expected_input_shape": {"known": 1},
            "canonical_schema": {
                "path": str(self.schema),
                "violations": [],
            },
        }), encoding="utf-8")
        self.assertEqual(
            inspect_staging_feedback(
                feedback,
                self.schema,
                self.consumer,
            ),
            [],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
