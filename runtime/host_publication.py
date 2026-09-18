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
    legacy_files = value.get("legacy_files")
    if not isinstance(legacy_files, list) or any(
        not isinstance(name, str) for name in legacy_files
    ):
        raise ValueError("host_promotion_policy_legacy_files_invalid")
    return value


def marker_path(input_dir: Path | str, filename: str) -> Path:
    return Path(input_dir) / MARKER_DIRECTORY / f"{filename}.sha256"


def is_promoted_input(path: Path, policy: dict[str, Any] | None = None) -> bool:
    policy = load_policy(path.parent) if policy is None else policy
    if policy is None:
        return True
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
        return ["host_promotion_policy_missing"]
    errors = []
    for path in sorted(input_dir.glob("*.json")):
        if path.name == "FEEDBACK.json" or path.name.startswith("."):
            continue
        if not is_promoted_input(path, policy):
            errors.append(f"host_input_not_promoted:{path.name}")
    return errors
