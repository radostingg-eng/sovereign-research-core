"""Fail-closed publication markers for canonical host inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

POLICY_FILENAME = ".promotion_policy.json"
MARKER_DIRECTORY = ".promoted"


def content_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_policy(input_dir: Path | str) -> dict[str, Any] | None:
    path = Path(input_dir) / POLICY_FILENAME
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("host_promotion_policy_not_an_object")
    if value.get("schema_version") != 1:
        raise ValueError("host_promotion_policy_schema_invalid")
    legacy_files = value.get("legacy_files")
    if not isinstance(legacy_files, list) or any(
        not isinstance(name, str) for name in legacy_files
    ):
        raise ValueError("host_promotion_policy_legacy_files_invalid")
    recovery_sources = value.get("required_recovery_sources", [])
    if not isinstance(recovery_sources, list) or any(
        not isinstance(source, dict)
        or set(source) != {"file", "sha256"}
        or not isinstance(source["file"], str)
        or not source["file"]
        or not isinstance(source["sha256"], str)
        or len(source["sha256"]) != 64
        for source in recovery_sources
    ):
        raise ValueError("host_promotion_policy_recovery_sources_invalid")
    return value


def marker_path(input_dir: Path | str, filename: str) -> Path:
    return Path(input_dir) / MARKER_DIRECTORY / f"{filename}.sha256"


def _is_pre_policy_legacy_input(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    version = value.get("host_input_schema_version")
    return version is None or version in {2, 3}


def is_promoted_input(path: Path, policy: dict[str, Any] | None = None) -> bool:
    policy = load_policy(path.parent) if policy is None else policy
    if policy is None:
        return _is_pre_policy_legacy_input(path)
    if path.name in set(policy["legacy_files"]):
        return True
    marker = marker_path(path.parent, path.name)
    if not marker.exists():
        return False
    return marker.read_text(encoding="utf-8").strip() == content_sha256(path)


def verify_canonical_inputs(input_dir: Path | str) -> list[str]:
    input_dir = Path(input_dir)
    policy = load_policy(input_dir)
    if policy is None:
        return [
            "host_promotion_policy_missing"
            for path in sorted(input_dir.glob("*.json"))
            if path.name != "FEEDBACK.json"
            and not path.name.startswith(".")
            and not _is_pre_policy_legacy_input(path)
        ][:1]
    errors = []
    for path in sorted(input_dir.glob("*.json")):
        if path.name == "FEEDBACK.json" or path.name.startswith("."):
            continue
        if not is_promoted_input(path, policy):
            errors.append(f"host_input_not_promoted:{path.name}")
    return errors
