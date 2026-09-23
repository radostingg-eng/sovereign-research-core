"""Validate only the host inputs introduced by one commit."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import input_fingerprint
from .input_artifacts import (
    InputArtifactError,
    input_document_from_value,
)
from .host_feedback import FEEDBACK_FILENAME, write_validation_feedback
from .integrity import load_journal_records
from .profile_paths import profile_root
from .run_host_cycle import (
    partition_validation_errors,
    persisted_snapshot_id,
    validate_input,
)


class DuplicateJsonKeyError(ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


class UnsafeDuplicateJsonKeyError(ValueError):
    """A duplicate key whose values cannot be losslessly diagnosed."""

    def __init__(self, key: str, reason: str):
        super().__init__(f"{key}:{reason}")
        self.key = key
        self.reason = reason


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKeyError(key)
        value[key] = item
    return value


def decode_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def _diagnostic_merge_pairs(
    pairs: list[tuple[str, Any]],
    *,
    merged_keys: list[str],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key not in value:
            value[key] = item
            continue
        previous = value[key]
        if previous == item:
            merged_keys.append(key)
            continue
        if isinstance(previous, list) and isinstance(item, list):
            combined = list(previous)
            for element in item:
                if element not in combined:
                    combined.append(element)
            value[key] = combined
            merged_keys.append(key)
            continue
        raise UnsafeDuplicateJsonKeyError(
            key,
            "conflicting_scalar_or_object",
        )
    return value


def diagnostic_decode_json(text: str) -> tuple[Any, list[str]]:
    """Parse JSON while merging duplicate keys losslessly for diagnostics.

    Never use the returned value for promotion. Identical duplicate values
    collapse to one occurrence and duplicate lists concatenate without
    dropping authored elements. Conflicting scalars or objects raise
    :class:`UnsafeDuplicateJsonKeyError` so the caller cannot silently accept
    an ambiguous merge. The list contains the keys that required merging so
    callers can name them in refusal feedback.
    """
    merged_keys: list[str] = []
    value = json.loads(
        text,
        object_pairs_hook=lambda pairs: _diagnostic_merge_pairs(
            pairs,
            merged_keys=merged_keys,
        ),
    )
    return value, merged_keys


def _open_container_hint(text: str, end: int) -> tuple[int, str]:
    stack: list[tuple[str, int]] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for offset, character in enumerate(text[:end]):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append((character, offset))
        elif character in pairs and stack and stack[-1][0] == pairs[character]:
            stack.pop()
    hint = " > ".join(
        f"{character}@{offset}" for character, offset in stack[-8:])
    return len(stack), hint


def _decode_error_reason(error: json.JSONDecodeError, text: str) -> str:
    start = max(0, error.pos - 100)
    end = min(len(text), error.pos + 100)
    context = json.dumps(text[start:end], ensure_ascii=True)
    depth, containers = _open_container_hint(text, error.pos)
    return (
        f"JSONDecodeError: {error.msg}; line={error.lineno}; "
        f"column={error.colno}; char={error.pos}; open_depth={depth}; "
        f"open_containers={json.dumps(containers)}; context={context}"
    )


def select_input_paths(
    input_dir: Path,
    *,
    changed_names: Sequence[str] | None = None,
) -> list[Path]:
    """Select changed inputs, or the newest input for manual validation."""
    input_dir = input_dir.resolve()
    if changed_names is None:
        paths = [
            path for path in sorted(input_dir.glob("*.json"))
            if path.name != FEEDBACK_FILENAME
            and not path.name.startswith(".")
        ]
        return paths[-1:] if paths else []
    selected = []
    for name in changed_names:
        path = Path(name)
        if path.name == FEEDBACK_FILENAME or path.name.startswith("."):
            continue
        candidate = (
            path.resolve()
            if path.is_absolute()
            else (input_dir.parent / path).resolve()
        )
        if candidate.parent != input_dir or candidate.suffix != ".json":
            continue
        if candidate.exists():
            selected.append(candidate)
    return sorted(set(selected))


def changed_paths(commit: str, *, repo_root: Path) -> list[str]:
    result = subprocess.run(
        [
            "git", "-C", str(repo_root), "diff-tree", "--root",
            "--no-commit-id", "--name-only", "-r", commit, "--", "host_input",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git_diff_tree_failed:{result.stderr.strip()[:200]}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def exclude_persisted_inputs(
    paths: Sequence[Path],
    records: Sequence[Mapping[str, Any]],
) -> list[Path]:
    """Do not retro-validate immutable inputs that already have receipts."""
    selected = []
    for path in paths:
        try:
            value = decode_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, DuplicateJsonKeyError):
            selected.append(path)
            continue
        cycle_id = (
            str(value.get("cycle_id", "")).strip()
            if isinstance(value, Mapping)
            else ""
        )
        previous = (
            persisted_snapshot_id(records, cycle_id)
            if cycle_id
            else None
        )
        current = (
            f"{path.stem}:{input_fingerprint(value)}"
            if isinstance(value, Mapping)
            else None
        )
        if previous is not None and previous == current:
            continue
        selected.append(path)
    return selected


def validate_path(
    path: Path,
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    canonical_input_dir: Path | None = None,
) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        return {
            "input": path.name,
            "reason": (
                "ValueError: invalid_host_input:"
                f"{path.name}:host_input_not_utf8:{error.start}"
            ),
        }
    if text.strip().upper() in {"PLACEHOLDER", "TODO", "TBD"}:
        return {
            "input": path.name,
            "reason": (
                "ValueError: invalid_host_input:"
                f"{path.name}:host_input_not_json_sentinel"
            ),
        }
    try:
        value = decode_json(text)
    except json.JSONDecodeError as error:
        return {
            "input": path.name,
            "reason": _decode_error_reason(error, text),
        }
    except DuplicateJsonKeyError as error:
        return {
            "input": path.name,
            "reason": (
                "ValueError: invalid_host_input:"
                f"{path.name}:duplicate_json_key:{error.key}"
            ),
        }
    if not isinstance(value, Mapping):
        return {
            "input": path.name,
            "reason": f"ValueError: host_input_not_an_object:{path.name}",
        }
    try:
        document = input_document_from_value(
            value,
            profile_root=(
                canonical_input_dir.resolve().parent
                if canonical_input_dir is not None
                else path.resolve().parent.parent
            ),
        )
    except InputArtifactError as error:
        return {
            "input": path.name,
            "reason": (
                f"ValueError: invalid_host_input:{path.name}:{error}"
            ),
        }
    errors = validate_input(
        document.hydrated,
        path.name,
        records=records,
        input_dir=canonical_input_dir,
        require_full_schema=True,
    )
    blocking_errors, _advisories = partition_validation_errors(
        document.hydrated,
        errors,
    )
    if blocking_errors:
        return {
            "input": path.name,
            "reason": (
                f"ValueError: invalid_host_input:{path.name}:"
                + ",".join(blocking_errors)
            ),
        }
    return None


def validate_paths(
    paths: Sequence[Path],
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    canonical_input_dir: Path | None = None,
) -> list[dict[str, str]]:
    return [
        refusal
        for path in paths
        if (
            refusal := validate_path(
                path,
                records=records,
                canonical_input_dir=canonical_input_dir,
            )
        ) is not None
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input-dir",
        default=str(profile_root() / "host_input"),
    )
    parser.add_argument("--changed-at")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    input_dir = Path(args.input_dir)
    names = (
        changed_paths(args.changed_at, repo_root=profile_root())
        if args.changed_at
        else None
    )
    paths = select_input_paths(input_dir, changed_names=names)
    records = load_journal_records()
    if names is None:
        paths = exclude_persisted_inputs(paths, records)
    refusals = validate_paths(
        paths,
        records=records,
        canonical_input_dir=input_dir,
    )
    write_validation_feedback(
        input_dir,
        checked=[path.name for path in paths],
        refusals=refusals,
    )
    for path in paths:
        matching = [row for row in refusals if row["input"] == path.name]
        if matching:
            print(f"{path.name}: REFUSED {matching[0]['reason']}")
        else:
            print(f"{path.name}: valid")
    print(f"checked {len(paths)}, refused {len(refusals)}")
    return 1 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
