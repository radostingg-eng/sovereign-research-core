"""Verify that a private profile can safely admit and execute host cycles."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .archive_integrity import verify_archive
from .audit_store import AuditJournal
from .host_publication import load_policy
from .init_profile import (
    CORE_REPO,
    PROFILE_WORKFLOW_VERSION,
    existing_journal,
)
from .profile_paths import profile_root
from .schedule_ledger import load_schedule_contract

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
PROFILE_CODE_SHADOW_PATHS = (
    "runtime",
    "ops",
    "conftest.py",
)


def _json_object(path: Path, code: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, [f"{code}_missing"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, [f"{code}_invalid"]
    if not isinstance(value, dict):
        return None, [f"{code}_not_object"]
    return value, []


def check_profile(
    root: Path | str,
    *,
    require_workflow: bool = True,
) -> list[str]:
    root = Path(root).expanduser().resolve()
    errors: list[str] = []
    for relative in PROFILE_CODE_SHADOW_PATHS:
        if (root / relative).exists():
            errors.append(f"profile_code_shadow_present:{relative}")

    journal_path = existing_journal(root)
    if journal_path is None:
        errors.append("profile_journal_missing")
    else:
        result = AuditJournal(journal_path).validate()
        if not result["valid"]:
            errors.append("profile_journal_invalid")

    try:
        errors.extend(
            f"profile_archive_invalid:{error}"
            for error in verify_archive(root / "audit_archive")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("profile_archive_invalid")

    lock, lock_errors = _json_object(root / "core.lock", "profile_core_lock")
    errors.extend(lock_errors)
    if lock is not None:
        if lock.get("core_repo") != CORE_REPO:
            errors.append("profile_core_repo_invalid")
        commit = lock.get("commit")
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            errors.append("profile_core_commit_invalid")
        if (
            lock.get("profile_workflow_version")
            != PROFILE_WORKFLOW_VERSION
        ):
            errors.append("profile_workflow_version_mismatch")

    try:
        policy = load_policy(root / "host_input")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("profile_promotion_policy_invalid")
    else:
        if policy is None:
            errors.append("profile_promotion_policy_missing")

    try:
        load_schedule_contract(root)
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("profile_schedule_contract_invalid")

    for relative, code in (
        ("host_input/FEEDBACK.json", "profile_execution_feedback"),
        ("host_staging/FEEDBACK.json", "profile_validation_feedback"),
    ):
        _, feedback_errors = _json_object(root / relative, code)
        errors.extend(feedback_errors)

    if require_workflow:
        workflow = root / ".github" / "workflows" / "host-cycle.yml"
        if not workflow.is_file():
            errors.append("profile_host_workflow_missing")
        else:
            text = workflow.read_text(encoding="utf-8")
            marker = f"PROFILE_WORKFLOW_VERSION: '{PROFILE_WORKFLOW_VERSION}'"
            if marker not in text:
                errors.append("profile_host_workflow_version_mismatch")
            if "python3 -P -m runtime." not in text:
                errors.append("profile_host_workflow_safe_path_missing")
            if "github.event.repository.private" not in text:
                errors.append("profile_host_workflow_private_gate_missing")
            if "account-schedule:" not in text:
                errors.append("profile_schedule_watchdog_missing")
            if "--workflow-version 2" not in text:
                errors.append(
                    "profile_schedule_watchdog_version_mismatch"
                )
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(profile_root()),
        help="private profile root",
    )
    parser.add_argument("--no-workflow", action="store_true")
    args = parser.parse_args(argv)
    errors = check_profile(
        args.root,
        require_workflow=not args.no_workflow,
    )
    for error in errors:
        print(error)
    print("profile health: ok" if not errors else "profile health: FAILED")
    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
