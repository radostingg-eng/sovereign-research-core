"""Materialize a hash-bound semantic correction without trusting its contents."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .host_input_validator import decode_json

_SAFE_SEMANTIC_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,110}\.semantic\.json"
)
_IMMUTABLE_FIELDS = frozenset({
    "corrects_candidate_id",
    "semantic_input_schema_version",
})
_LEXICAL_PUNCTUATION = frozenset("{}[],:")
_MAX_LEXICAL_EDITS = 16


class SemanticPatchError(ValueError):
    pass


@dataclass(frozen=True)
class MaterializedSemanticPatch:
    output_name: str
    source_bytes: bytes
    source_sha256: str
    patch_sha256: str
    base_candidate_id: str


def _fail(reason: str) -> None:
    raise SemanticPatchError(f"semantic_patch_invalid:{reason}")


def _tokens(pointer: Any) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        _fail("pointer")
    tokens = pointer[1:].split("/")
    if any(re.search(r"~(?![01])", token) for token in tokens):
        _fail("pointer_escape")
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in tokens
    ]


def _resolve(
    root: Any,
    pointer: str,
    *,
    absent_last: bool = False,
) -> tuple[Any, str, list[str]]:
    tokens = _tokens(pointer)
    current = root
    array_items: list[str] = []
    walked: list[str] = []
    for token in tokens[:-1]:
        if isinstance(current, list):
            if not token.isascii() or not token.isdecimal() or (
                len(token) > 1 and token.startswith("0")
            ):
                _fail(f"array_index:{pointer}")
            index = int(token)
            if index >= len(current):
                _fail(f"array_bounds:{pointer}")
            current = current[index]
            walked.append(token)
            array_items.append("/" + "/".join(walked))
        elif isinstance(current, dict) and token in current:
            current = current[token]
            walked.append(token.replace("~", "~0").replace("/", "~1"))
        else:
            _fail(f"missing_parent:{pointer}")
    last = tokens[-1]
    if isinstance(current, list):
        if absent_last:
            _fail(f"add_array_item:{pointer}")
        if not last.isascii() or not last.isdecimal() or (
            len(last) > 1 and last.startswith("0")
        ):
            _fail(f"array_index:{pointer}")
        index = int(last)
        if index >= len(current):
            _fail(f"array_bounds:{pointer}")
        array_items.append(pointer)
    elif not isinstance(current, dict):
        _fail(f"not_container:{pointer}")
    return current, last, array_items


def _same_json_value(actual: Any, expected: Any) -> bool:
    try:
        return json.dumps(
            actual, sort_keys=True, allow_nan=False
        ) == json.dumps(expected, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        _fail("non_json_preimage")


def _check_guards(
    base: Mapping[str, Any],
    pointer: str,
    array_items: list[str],
    guards: Any,
) -> None:
    if not isinstance(guards, list) or len(guards) != len(array_items):
        _fail(f"array_guards_required:{pointer}")
    seen: set[str] = set()
    for guard in guards:
        if not isinstance(guard, dict) or set(guard) != {
            "array_item_path", "identity_path", "expected",
        }:
            _fail(f"array_guard_shape:{pointer}")
        item_path = guard["array_item_path"]
        identity_path = guard["identity_path"]
        if (
            not isinstance(item_path, str)
            or item_path not in array_items
            or item_path in seen
            or not isinstance(identity_path, str)
            or not identity_path.startswith(item_path + "/")
            or _tokens(identity_path)[-1] not in ("id", "symbol", "ticker")
            and not _tokens(identity_path)[-1].endswith("_id")
        ):
            _fail(f"array_guard_identity:{pointer}")
        seen.add(item_path)
        holder, key, _ = _resolve(base, identity_path)
        if (
            not isinstance(holder, dict)
            or key not in holder
            or not isinstance(holder[key], str)
            or not holder[key].strip()
        ):
            _fail(f"array_guard_field:{identity_path}")
        if not _same_json_value(holder[key], guard["expected"]):
            _fail(f"array_guard_changed:{identity_path}")
        parent, last, _ = _resolve(
            base, item_path.rsplit("/", 1)[0]
        )
        siblings = parent[
            int(last) if isinstance(parent, list) else last
        ]
        relative = identity_path[len(item_path):]
        matches = 0
        for sibling in siblings:
            try:
                candidate, candidate_key, _ = _resolve(sibling, relative)
            except SemanticPatchError:
                continue
            if isinstance(candidate, dict) and candidate_key in candidate:
                matches += _same_json_value(
                    candidate[candidate_key], guard["expected"]
                )
        if matches != 1:
            _fail(f"array_guard_not_unique:{identity_path}")


def _assert_outside_json_string(source: bytes, offset: int) -> None:
    in_string = False
    escaped = False
    for byte in source[:offset]:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
        elif byte == ord('"'):
            in_string = True
    if in_string:
        _fail("lexical_inside_string")


def _apply_lexical_edits(source: bytes, edits: Any) -> bytes:
    if (
        not isinstance(edits, list)
        or not 1 <= len(edits) <= _MAX_LEXICAL_EDITS
    ):
        _fail("lexical_shape")
    changes: list[tuple[int, int, bytes]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            _fail("lexical_shape")
        kind = edit.get("op")
        required = (
            {"op", "offset", "expected", "before", "after"}
            if kind == "delete"
            else {"op", "offset", "value", "before", "after"}
            if kind == "insert"
            else set()
        )
        if not required or set(edit) not in (
            required, required - {"offset"},
        ):
            _fail("lexical_shape")
        punctuation = edit["expected" if kind == "delete" else "value"]
        before, after = edit["before"], edit["after"]
        if (
            not isinstance(punctuation, str)
            or punctuation not in _LEXICAL_PUNCTUATION
            or not isinstance(before, str)
            or not isinstance(after, str)
            or not 8 <= len(before) <= 64
            or not 8 <= len(after) <= 64
        ):
            _fail("lexical_shape")
        marker = punctuation.encode("ascii")
        prior = before.encode("utf-8")
        following = after.encode("utf-8")
        if "offset" in edit:
            offset = edit["offset"]
            if type(offset) is not int or not 0 <= offset <= len(source):
                _fail("lexical_shape")
        else:
            context = prior + (marker if kind == "delete" else b"") + following
            first = source.find(context)
            if first < 0 or source.find(context, first + 1) >= 0:
                _fail("lexical_context_not_unique")
            offset = first + len(prior)
        end = offset + (len(marker) if kind == "delete" else 0)
        if (
            end > len(source)
            or source[max(0, offset - len(prior)):offset] != prior
            or source[end:end + len(following)] != following
            or kind == "delete" and source[offset:end] != marker
        ):
            _fail("lexical_preimage")
        _assert_outside_json_string(source, offset)
        changes.append((offset, end, b"" if kind == "delete" else marker))
    ordered = sorted(changes)
    if any(
        current[0] <= previous[1]
        for previous, current in zip(ordered, ordered[1:])
    ):
        _fail("lexical_overlap")
    repaired = source
    for start, end, replacement in reversed(ordered):
        repaired = repaired[:start] + replacement + repaired[end:]
    return repaired


def _decode_base(
    source: bytes,
    lexical_edit: Any,
    lexical_edits: Any = None,
) -> Mapping[str, Any]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        _fail("base_utf8")
    try:
        base = decode_json(text)
    except json.JSONDecodeError:
        if lexical_edits is not None:
            repaired = _apply_lexical_edits(source, lexical_edits)
        else:
            if not isinstance(lexical_edit, dict) or set(lexical_edit) != {
                "offset", "insert", "before", "after",
            }:
                _fail("base_decode")
            offset = lexical_edit["offset"]
            insertion = lexical_edit["insert"]
            before = lexical_edit["before"]
            after = lexical_edit["after"]
            if (
                type(offset) is not int
                or offset < 0
                or offset > len(source)
                or not isinstance(insertion, str)
                or insertion not in _LEXICAL_PUNCTUATION
                or not isinstance(before, str)
                or not isinstance(after, str)
                or not 8 <= len(before) <= 64
                or not 8 <= len(after) <= 64
                or source[max(0, offset - len(before.encode())):offset]
                != before.encode()
                or source[offset:offset + len(after.encode())] != after.encode()
            ):
                _fail("lexical_preimage")
            _assert_outside_json_string(source, offset)
            repaired = source[:offset] + insertion.encode() + source[offset:]
        try:
            base = decode_json(repaired.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            _fail("lexical_output")
    except ValueError:
        _fail("base_decode")
    else:
        if lexical_edit is not None or lexical_edits is not None:
            _fail("lexical_not_needed")
    if not isinstance(base, dict) or base.get(
        "semantic_input_schema_version"
    ) != 1:
        _fail("base_schema")
    return base


def materialize_semantic_patch(
    patch_bytes: bytes,
    *,
    archive_name: str,
    archive_bytes: bytes,
    refusal: Mapping[str, Any],
) -> MaterializedSemanticPatch:
    """Expand a host edit against the exact immutable refused source.

    This only checks patch identity and pre-images. The caller must validate
    the resulting full document through the normal builder and intake gates.
    """
    try:
        patch = decode_json(patch_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        _fail(f"decode:{type(error).__name__}")
    if not isinstance(patch, dict):
        _fail("schema")
    fields = {
        "schema_version", "base_candidate_id", "output_filename", "operations",
    }
    version = patch.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        _fail("schema")
    lexical_key = "lexical_edit" if version == 1 else "lexical_edits"
    if set(patch) not in (fields, fields | {lexical_key}):
        _fail("schema")
    if lexical_key in patch and patch[lexical_key] is None:
        _fail("lexical_shape")
    output_name = patch["output_filename"]
    if not isinstance(output_name, str) or not _SAFE_SEMANTIC_NAME.fullmatch(
        output_name
    ):
        _fail("output_filename")
    base_id = patch["base_candidate_id"]
    digest = hashlib.sha256(archive_bytes).hexdigest()
    base_name = refusal.get("input")
    if (
        not isinstance(base_id, str)
        or not isinstance(base_name, str)
        or not _SAFE_SEMANTIC_NAME.fullmatch(base_name)
        or base_id != refusal.get("candidate_id")
        or base_id != f"{base_name}@sha256:{digest}"
        or digest != refusal.get("sha256")
        or refusal.get("erased")
        or refusal.get("archive") != archive_name
        or archive_name != base_name.removesuffix(".json") + f"-{digest}.json"
        or output_name == base_name
    ):
        _fail("base_identity")
    base = _decode_base(
        archive_bytes,
        patch.get("lexical_edit"),
        patch.get("lexical_edits"),
    )
    operations = patch["operations"]
    if not isinstance(operations, list) or (
        not operations and lexical_key not in patch
    ):
        _fail("operations")
    paths: list[str] = []
    for operation in operations:
        if not isinstance(operation, dict):
            _fail("operation_shape")
        kind = operation.get("op")
        fields = (
            {"op", "path", "expected", "value", "array_guards"}
            if kind == "replace"
            else {"op", "path", "value", "array_guards"}
            if kind == "add"
            else set()
        )
        if set(operation) != fields:
            _fail("operation_shape")
        pointer = operation["path"]
        tokens = _tokens(pointer)
        if tokens[0] in _IMMUTABLE_FIELDS:
            _fail(f"immutable_field:{pointer}")
        holder, key, array_items = _resolve(
            base, pointer, absent_last=kind == "add"
        )
        _check_guards(base, pointer, array_items, operation["array_guards"])
        if kind == "add":
            if key in holder:
                _fail(f"add_existing:{pointer}")
        elif (
            (isinstance(holder, dict) and key not in holder)
            or not _same_json_value(holder[
                int(key) if isinstance(holder, list) else key
            ], operation["expected"])
        ):
            _fail(f"preimage_changed:{pointer}")
        elif kind == "replace" and _same_json_value(
            operation["expected"], operation["value"]
        ):
            _fail(f"no_change:{pointer}")
        if any(
            pointer == prior
            or pointer.startswith(prior + "/")
            or prior.startswith(pointer + "/")
            for prior in paths
        ):
            _fail(f"overlapping_paths:{pointer}")
        paths.append(pointer)
    result = copy.deepcopy(base)
    for operation in operations:
        holder, key, _ = _resolve(result, operation["path"])
        holder[int(key) if isinstance(holder, list) else key] = copy.deepcopy(
            operation["value"]
        )
    result["corrects_candidate_id"] = base_id
    try:
        source = (
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        _fail(f"value:{type(error).__name__}")
    return MaterializedSemanticPatch(
        output_name=output_name,
        source_bytes=source,
        source_sha256=hashlib.sha256(source).hexdigest(),
        patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
        base_candidate_id=base_id,
    )
