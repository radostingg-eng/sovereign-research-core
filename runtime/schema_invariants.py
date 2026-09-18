"""Keep the host's worked example readable and executable.

The example is copied into refusal feedback, so an ignored field is not inert
documentation: it teaches the host a contract the runtime does not implement.
Formatting is also part of the safety boundary. Dense, semantically equivalent
JSON made object and array boundaries harder to review in the same cycle that
r34 emitted a malformed nested array.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .profile_paths import code_root, profile_root

ROOT = code_root()
# The filename is retained to avoid a broad rename across committed feedback
# and historical guidance. Its contents are the canonical v4 staged contract.
CANONICAL_EXAMPLE_PATH = ROOT / "schemas" / "host_input_v2.example.json"
CANONICAL_EXAMPLE_SCHEMA_VERSION = 4
HOST_INPUT_CONSUMER_PATH = Path(__file__).resolve().parent / "run_host_cycle.py"
STAGING_FEEDBACK_PATH = profile_root() / "host_staging" / "FEEDBACK.json"


def canonical_json(value: Any) -> str:
    """The sole committed representation for JSON examples."""
    return json.dumps(
        value,
        indent=2,
        ensure_ascii=True,
        sort_keys=False,
    ) + "\n"


def runtime_read_field_names(
    consumer_path: Path | str = HOST_INPUT_CONSUMER_PATH,
) -> set[str]:
    """String field names read by the production host-input consumer."""
    path = Path(consumer_path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            names.add(node.slice.value)
    return names


def unconsumed_top_level_fields(
    example: Mapping[str, Any],
    consumer_path: Path | str = HOST_INPUT_CONSUMER_PATH,
) -> list[str]:
    """Example fields no production host-input path reads."""
    consumed = runtime_read_field_names(consumer_path)
    return sorted(str(key) for key in example if str(key) not in consumed)


def inspect_example(
    path: Path | str = CANONICAL_EXAMPLE_PATH,
    consumer_path: Path | str = HOST_INPUT_CONSUMER_PATH,
) -> tuple[dict[str, Any], list[str]]:
    """Return parsed example plus deterministic contract violations."""
    schema_path = Path(path)
    try:
        text = schema_path.read_text(encoding="utf-8")
    except OSError as error:
        return {}, [f"canonical_schema_unreadable:{type(error).__name__}:{error}"]
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return {}, [
            "canonical_schema_invalid_json:"
            f"line={error.lineno}:column={error.colno}:char={error.pos}"
        ]
    if not isinstance(value, Mapping):
        return {}, ["canonical_schema_root_must_be_object"]

    example = dict(value)
    violations = []
    if text != canonical_json(example):
        violations.append("canonical_schema_not_pretty_printed")
    if (
        "host_input_schema_version" in example
        and example.get("host_input_schema_version")
        != CANONICAL_EXAMPLE_SCHEMA_VERSION
    ):
        violations.append(
            "canonical_schema_version_mismatch:"
            f"expected={CANONICAL_EXAMPLE_SCHEMA_VERSION}:"
            f"actual={example.get('host_input_schema_version')}"
        )
    violations.extend(
        f"canonical_schema_field_not_consumed:{field}"
        for field in unconsumed_top_level_fields(example, consumer_path)
    )
    return example, violations


def feedback_safe_example(
    example: Mapping[str, Any],
    consumer_path: Path | str = HOST_INPUT_CONSUMER_PATH,
) -> dict[str, Any]:
    """Remove unread top-level fields before teaching the shape to the host."""
    ignored = set(unconsumed_top_level_fields(example, consumer_path))
    return {
        str(key): value
        for key, value in example.items()
        if str(key) not in ignored
    }


def canonical_schema_status(
    path: Path | str = CANONICAL_EXAMPLE_PATH,
    consumer_path: Path | str = HOST_INPUT_CONSUMER_PATH,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The safe example and status projected into host feedback."""
    schema_path = Path(path)
    example, violations = inspect_example(schema_path, consumer_path)
    try:
        display_path = str(schema_path.relative_to(ROOT))
    except ValueError:
        display_path = str(schema_path)
    return feedback_safe_example(example, consumer_path), {
        "path": display_path,
        "violations": violations,
    }


def inspect_staging_feedback(
    feedback_path: Path | str = STAGING_FEEDBACK_PATH,
    schema_path: Path | str = CANONICAL_EXAMPLE_PATH,
    consumer_path: Path | str = HOST_INPUT_CONSUMER_PATH,
) -> list[str]:
    """Detect committed feedback that teaches a stale schema."""
    path = Path(feedback_path)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [
            "staging_feedback_unreadable:"
            f"{type(error).__name__}:{error}"
        ]
    if not isinstance(value, Mapping):
        return ["staging_feedback_root_must_be_object"]

    expected_shape, expected_status = canonical_schema_status(
        schema_path,
        consumer_path,
    )
    violations = []
    if (
        value.get("expected_input_shape") is not None
        and value.get("expected_input_shape") != expected_shape
    ):
        violations.append("staging_feedback_expected_input_shape_stale")
    if value.get("canonical_schema") != expected_status:
        violations.append("staging_feedback_schema_status_stale")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    _, violations = inspect_example()
    violations.extend(inspect_staging_feedback())
    if not violations:
        print(
            f"{CANONICAL_EXAMPLE_PATH.relative_to(ROOT)}: canonical and "
            "all top-level fields are consumed; staging feedback is current"
        )
        return 0
    print(f"{CANONICAL_EXAMPLE_PATH.relative_to(ROOT)}: invalid:")
    for violation in violations:
        print(f"  {violation}")
    print(
        "\nFormat with json.dumps(value, indent=2, ensure_ascii=True, "
        "sort_keys=False) plus one trailing newline. Remove explanatory "
        "fields the runtime does not read."
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
