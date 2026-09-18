"""Verify the retired audit corpus without pretending its damage is repaired."""
from __future__ import annotations

from .profile_paths import code_root, profile_root
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .integrity import (
    check_causal_integrity, check_hash_format, check_record_validity,
    check_supersession_records, load_journal_records, order_chain,
    superseded_defects,
)

ROOT = code_root()
ARCHIVE_DIR = profile_root() / "audit_archive"
MANIFEST_PATH = ARCHIVE_DIR / "manifest.json"


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("archive_manifest_not_an_object")
    return dict(value)


def observed_defects(records: list[dict[str, Any]]) -> list[str]:
    failures = (
        check_hash_format(records)
        + check_record_validity(records)
        + check_causal_integrity(records)
        + check_supersession_records(records)
    )
    return sorted({failure.ident for failure in failures})


def verify_archive(
    archive_dir: Path = ARCHIVE_DIR,
    manifest_path: Path | None = None,
) -> list[str]:
    """File identity and known-damage signature must stay exact."""
    manifest_path = manifest_path or archive_dir / "manifest.json"
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return [f"archive_manifest_unreadable:{error}"]

    expected_files = manifest.get("files")
    if not isinstance(expected_files, Mapping):
        return ["archive_manifest_files_invalid"]
    actual_names = {path.name for path in archive_dir.glob("*.jsonl")}
    expected_names = set(expected_files)
    errors = [
        f"archive_file_missing:{name}"
        for name in sorted(expected_names - actual_names)
    ]
    errors.extend(
        f"archive_file_unexpected:{name}"
        for name in sorted(actual_names - expected_names)
    )
    for name in sorted(expected_names & actual_names):
        path = archive_dir / name
        expected = expected_files[name]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected.get("sha256"):
            errors.append(f"archive_file_hash_mismatch:{name}")
        count = sum(
            1 for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if count != expected.get("records"):
            errors.append(
                f"archive_file_record_count:{name}:"
                f"{count}!={expected.get('records')}")
    if errors:
        return errors

    try:
        records = load_journal_records(archive_dir)
    except ValueError as error:
        return [f"archive_records_unreadable:{error}"]
    ordered, _ = order_chain(records)
    facts = {
        "records": len(records),
        "reachable_records": len(ordered),
        "unreachable_records": len(records) - len(ordered),
        "valid_supersessions": len(superseded_defects(records)),
    }
    for name, observed in facts.items():
        if manifest.get(name) != observed:
            errors.append(
                f"archive_{name}_changed:"
                f"{observed}!={manifest.get(name)}")
    expected_defects = sorted(manifest.get("known_defects") or ())
    actual_defects = observed_defects(records)
    if actual_defects != expected_defects:
        missing = sorted(set(expected_defects) - set(actual_defects))
        added = sorted(set(actual_defects) - set(expected_defects))
        errors.extend(f"archive_known_defect_disappeared:{item}" for item in missing)
        errors.extend(f"archive_new_defect:{item}" for item in added)
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    archive_dir = Path(argv[0]).resolve() if argv else ARCHIVE_DIR
    errors = verify_archive(archive_dir)
    if errors:
        for error in errors:
            print(f"ARCHIVE INTEGRITY: {error}")
        return 1
    manifest = load_manifest(archive_dir / "manifest.json")
    print(
        "archive integrity: expected damaged corpus intact "
        f"({manifest['records']} records, "
        f"{manifest['unreachable_records']} unreachable)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
