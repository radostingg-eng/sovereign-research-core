"""Load host inputs with verified, content-addressed large response bodies."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .tool_artifacts import (
    MAX_ARTIFACT_BYTES,
    canonical_json_bytes,
    content_sha256,
    iter_tool_calls,
)

INPUT_ARTIFACT_SCHEMA_VERSION = 1
OUT_OF_LINE_RESULT_THRESHOLD_BYTES = 128 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class InputArtifactError(ValueError):
    """A referenced evidence body is missing, mutable, or inconsistent."""


@dataclass(frozen=True)
class InputDocument:
    raw: dict[str, Any]
    hydrated: dict[str, Any]
    normalized: dict[str, Any]
    references: dict[tuple[str, int, int], dict[str, Any]]


def _reference(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != {"$artifact"}:
        return None
    reference = value.get("$artifact")
    return reference if isinstance(reference, Mapping) else None


def _reference_value(content: bytes) -> dict[str, Any]:
    return {
        "$artifact": {
            "schema_version": INPUT_ARTIFACT_SCHEMA_VERSION,
            "sha256": content_sha256(content),
            "byte_length": len(content),
            "media_type": "application/json",
        },
    }


def _artifact_path(root: Path, digest: str) -> Path:
    return root / "tool_artifacts" / "sha256" / digest[:2] / f"{digest}.json"


def _assert_no_symlink(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise InputArtifactError(
                f"input_artifact_symlink_forbidden:{current.name}"
            )


def _load_reference(
    value: Mapping[str, Any],
    *,
    profile_root: Path,
) -> tuple[Any, dict[str, Any]]:
    if set(value) != {
        "schema_version",
        "sha256",
        "byte_length",
        "media_type",
    }:
        raise InputArtifactError("input_artifact_reference_fields")
    if value.get("schema_version") != INPUT_ARTIFACT_SCHEMA_VERSION:
        raise InputArtifactError("input_artifact_reference_schema")
    digest = value.get("sha256")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise InputArtifactError("input_artifact_reference_sha256")
    byte_length = value.get("byte_length")
    if (
        not isinstance(byte_length, int)
        or isinstance(byte_length, bool)
        or byte_length <= 0
        or byte_length > MAX_ARTIFACT_BYTES
    ):
        raise InputArtifactError("input_artifact_reference_byte_length")
    if value.get("media_type") != "application/json":
        raise InputArtifactError("input_artifact_reference_media_type")
    root = profile_root.resolve()
    target = _artifact_path(root, digest)
    _assert_no_symlink(root, target)
    if not target.is_file():
        raise InputArtifactError(
            f"input_artifact_missing:{digest}"
        )
    stat = target.stat()
    if stat.st_size > MAX_ARTIFACT_BYTES:
        raise InputArtifactError("input_artifact_file_too_large")
    if stat.st_size != byte_length:
        raise InputArtifactError("input_artifact_size_mismatch")
    content = target.read_bytes()
    if content_sha256(content) != digest:
        raise InputArtifactError("input_artifact_hash_mismatch")
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputArtifactError(
            "input_artifact_not_json"
        ) from error
    if canonical_json_bytes(parsed) != content:
        raise InputArtifactError("input_artifact_not_canonical_json")
    if _reference(parsed) is not None:
        raise InputArtifactError("input_artifact_recursive_reference")
    return parsed, dict(value)


def normalize_input_for_identity(
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace large v4 connector bodies with deterministic references."""
    normalized = deepcopy(dict(data))
    if normalized.get("host_input_schema_version") != 4:
        return normalized
    for descriptor in iter_tool_calls(normalized):
        call = descriptor["call"]
        provenance = call.get("provenance")
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("result_origin") != "connector_response"
        ):
            continue
        result = call.get("result")
        if _reference(result) is not None:
            continue
        try:
            content = canonical_json_bytes(result)
        except (TypeError, ValueError):
            continue
        if len(content) > OUT_OF_LINE_RESULT_THRESHOLD_BYTES:
            call["result"] = _reference_value(content)
    return normalized


def input_document_from_value(
    value: Mapping[str, Any],
    *,
    profile_root: Path | str,
) -> InputDocument:
    raw = deepcopy(dict(value))
    hydrated = deepcopy(raw)
    references: dict[tuple[str, int, int], dict[str, Any]] = {}
    version = raw.get("host_input_schema_version")
    for descriptor in iter_tool_calls(hydrated):
        call = descriptor["call"]
        result = call.get("result")
        reference = _reference(result)
        if reference is not None:
            if version != 4:
                raise InputArtifactError(
                    "input_artifact_requires_schema_v4"
                )
            parsed, metadata = _load_reference(
                reference,
                profile_root=Path(profile_root),
            )
            call["result"] = parsed
            references[(
                descriptor["scope"],
                descriptor["research_index"],
                descriptor["call_index"],
            )] = metadata
            continue
        if version != 4:
            continue
        provenance = call.get("provenance")
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("result_origin") != "connector_response"
        ):
            continue
        try:
            size = len(canonical_json_bytes(result))
        except (TypeError, ValueError):
            continue
        if size > OUT_OF_LINE_RESULT_THRESHOLD_BYTES:
            raise InputArtifactError(
                "large_tool_result_requires_artifact_ref"
            )
    normalized = normalize_input_for_identity(hydrated)
    return InputDocument(
        raw=raw,
        hydrated=hydrated,
        normalized=normalized,
        references=references,
    )


def load_input_document(
    path: Path | str,
    *,
    profile_root: Path | str | None = None,
) -> InputDocument:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"host_input_not_an_object:{source.name}")
    root = (
        Path(profile_root)
        if profile_root is not None
        else source.resolve().parent.parent
    )
    return input_document_from_value(value, profile_root=root)


def load_input_data(
    path: Path | str,
    *,
    profile_root: Path | str | None = None,
) -> dict[str, Any]:
    return load_input_document(
        path,
        profile_root=profile_root,
    ).hydrated


def orphan_input_artifacts(
    *,
    profile_root: Path | str,
    referenced_digests: set[str],
) -> list[str]:
    root = Path(profile_root).resolve()
    store = root / "tool_artifacts" / "sha256"
    if not store.exists():
        return []
    orphans = []
    for path in store.glob("*/*.json"):
        digest = path.stem
        if _DIGEST.fullmatch(digest) and digest not in referenced_digests:
            orphans.append(str(path.relative_to(root)))
    return sorted(orphans)


def referenced_artifact_digests(
    data: Mapping[str, Any],
) -> set[str]:
    digests = set()
    for descriptor in iter_tool_calls(data):
        reference = _reference(descriptor["call"].get("result"))
        digest = (
            reference.get("sha256")
            if isinstance(reference, Mapping)
            else None
        )
        if isinstance(digest, str) and _DIGEST.fullmatch(digest):
            digests.add(digest)
    return digests


def profile_input_artifact_references(
    profile_root: Path | str,
) -> set[str]:
    root = Path(profile_root)
    referenced = set()
    patterns = (
        "host_input/*.json",
        "host_staging/*.json",
        "host_staging/accepted_sources/*.json",
        "host_staging/rejected/*.json",
    )
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.name == "FEEDBACK.json":
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping):
                referenced.update(referenced_artifact_digests(value))
    return referenced


def journal_artifact_references(
    records: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> set[str]:
    referenced = set()
    for record in records:
        if record.get("record_type") != "tool_provenance":
            continue
        payload = record.get("payload")
        calls = (
            payload.get("calls")
            if isinstance(payload, Mapping)
            else None
        )
        for row in calls or ():
            if not isinstance(row, Mapping):
                continue
            ref = row.get("artifact_ref")
            if not isinstance(ref, str):
                continue
            digest = ref.rsplit("/", 1)[-1].removesuffix(".json")
            if _DIGEST.fullmatch(digest):
                referenced.add(digest)
    return referenced
