"""Create an empty profile so a new operator can start from nothing.

The first operator never needed this: their profile grew inside the code
checkout one directory at a time, and by the time the split happened it
already existed. A second operator has only the shared code, and the
runtime refuses to invent state -- an absent journal is a real absence,
not something to be silently created mid-cycle.

So the profile is created once, deliberately, before the first cycle:

    python3 -m runtime.init_profile ~/sovereign-data

What it writes is a skeleton, not a starting position. Every file is
either empty, a template with placeholders, or the genesis record of a
journal with nothing in it yet. In particular it does NOT write a
portfolio, preferences, theses or goals: those are the operator's, and a
runtime that guessed them would be handing a new user someone else's
investment posture.

It is idempotent in the sense that matters: it refuses to touch a
directory that already has a journal, because the one unrecoverable
mistake here is overwriting a real operator's history with a fresh
skeleton.
"""

from __future__ import annotations

import argparse
import json
import sys
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .audit_store import AuditJournal
from .engine import canonical_json, make_record
from .host_feedback import write_feedback, write_validation_feedback
from .host_publication import load_policy
from .profile_paths import code_root

# Directories a cycle needs to exist before it can write anything.
PROFILE_DIRECTORIES: tuple[str, ...] = (
    ".github/workflows",
    "audit",
    "audit_archive",
    "bootstrap",
    "coordination",
    "experiments",
    "feedback_signals/pending",
    "feedback_signals/shared",
    "feedback_staging",
    "goals",
    "host_input",
    "host_staging",
    "host_staging/rejected",
    "portfolio",
    "recommendations",
    "reviews",
    "runs",
    "strategies",
    "theses",
    "tool_artifacts",
    "var",
)

PREFERENCES_TEMPLATE = """# Operator preferences

No explicit operator preferences recorded yet.

This is valid state and does not block setup or research. Never infer a
preference from holdings, past trades, or instruments already present in the
account.

When the operator naturally states a durable preference in conversation,
append it here without turning setup or ordinary conversation into an intake
interview. Record the exact meaning and what would change it. No preference
here is shared with anyone else.

## Explicit preferences

<!-- Empty until the operator volunteers one. -->
"""

GITIGNORE = """# Local caches and the read-only pinned core checkout.
var/
.core/

__pycache__/
*.pyc
.DS_Store
"""

README_TEMPLATE = """# Sovereign Research profile

This directory is one operator's private state: journal, preferences,
theses, goals, portfolio snapshots and host cycles. It is not shareable
and it is not interchangeable with anyone else's.

The code lives elsewhere. Point it here:

    export SOVEREIGN_PROFILE_DIR={root}

Nothing in this directory should ever be copied into the shared code
repository, pasted into a public issue, or attached to a pull request.
To report a bug upstream, use `runtime.contribution`, which builds a
report from placeholder values and refuses to publish private evidence.
"""


def _genesis_record(created_at: str) -> dict:
    """The first journal record: an empty chain with a known root."""
    return make_record(
        record_id="profile-genesis",
        record_type="profile_genesis",
        agent="profile-initializer",
        created_at=created_at,
        payload={
            "schema_version": 1,
            "note": (
                "Empty profile created by runtime.init_profile. No "
                "portfolio, preferences, theses or goals are implied."
            ),
            "profile_id": uuid4().hex,
        },
    )


def existing_journal(root: Path) -> Path | None:
    """The journal already in this directory, if any."""
    audit = root / "audit"
    if not audit.is_dir():
        return None
    return next(iter(sorted(audit.glob("*.jsonl"))), None)


CORE_REPO = "radostingg-eng/sovereign-research-core"
PROFILE_WORKFLOW_VERSION = 1
PROMOTION_POLICY = {
    "schema_version": 1,
    "legacy_files": [],
}


def _core_lock(commit: str | None) -> str:
    """The exact core commit this profile runs.

    Pinned rather than tracking a branch: a shared repository that many
    people can open pull requests against is also a repository whose main
    can change between one cycle and the next. The operator moves when
    they have looked at what changed.
    """
    return json.dumps(
        {
            "core_repo": CORE_REPO,
            "commit": commit or "UNPINNED",
            "profile_workflow_version": PROFILE_WORKFLOW_VERSION,
        },
        indent=2,
    ) + "\n"


def _merge_gitignore(existing: str = "") -> str:
    removed = {
        "FEEDBACK.json",
        "/FEEDBACK.json",
        "host_input/FEEDBACK.json",
        "/host_input/FEEDBACK.json",
        "host_staging/FEEDBACK.json",
        "/host_staging/FEEDBACK.json",
    }
    lines = [
        line
        for line in existing.splitlines()
        if line.strip() not in removed
    ]
    required = [
        "var/",
        ".core/",
        "__pycache__/",
        "*.pyc",
        ".DS_Store",
    ]
    if lines and lines[-1].strip():
        lines.append("")
    for line in required:
        if line not in lines:
            lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _write_if_missing(
    root: Path,
    name: str,
    body: str,
    written: list[str],
) -> None:
    path = root / name
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    written.append(name)


def _copy_if_changed(
    source: Path,
    target: Path,
    root: Path,
    written: list[str],
) -> None:
    if target.exists() and target.read_bytes() == source.read_bytes():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    written.append(str(target.relative_to(root)))


def _write_initial_feedback(root: Path, written: list[str]) -> None:
    host_feedback = root / "host_input" / "FEEDBACK.json"
    if not host_feedback.exists():
        write_feedback(
            root / "host_input",
            accepted=[],
            refusals=[],
            skipped=[],
        )
        written.append("host_input/FEEDBACK.json")
    staging_feedback = root / "host_staging" / "FEEDBACK.json"
    if not staging_feedback.exists():
        write_validation_feedback(
            root / "host_staging",
            checked=[],
            refusals=[],
            refusal_history=[],
        )
        written.append("host_staging/FEEDBACK.json")


def _write_empty_directory_markers(root: Path, written: list[str]) -> None:
    for relative in PROFILE_DIRECTORIES:
        directory = root / relative
        if any(directory.iterdir()):
            continue
        marker = directory / ".gitkeep"
        marker.write_text("", encoding="utf-8")
        written.append(str(marker.relative_to(root)))


def init_profile(
    root: Path | str,
    *,
    core_commit: str | None = None,
    now: datetime | None = None,
    install_workflow: bool = True,
) -> list[str]:
    """Create the skeleton. Returns what it wrote, in order.

    Refuses a directory that already holds a journal rather than merging
    into it: a half-initialised profile is harder to diagnose than one
    that never started.
    """
    root = Path(root).expanduser().resolve()
    occupied = existing_journal(root)
    if occupied is not None:
        raise FileExistsError(
            f"{root} already has a journal at {occupied.name}; refusing to "
            f"re-initialise an existing profile"
        )

    created_at = (now or datetime.now(timezone.utc)).isoformat()
    written: list[str] = []

    root.mkdir(parents=True, exist_ok=True)
    for relative in PROFILE_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
        written.append(f"{relative}/")

    journal = root / "audit" / f"{created_at[:10]}-genesis.jsonl"
    journal.write_text(
        canonical_json(_genesis_record(created_at)) + "\n",
        encoding="utf-8",
    )
    written.append(str(journal.relative_to(root)))

    if install_workflow:
        workflow_source = (
            code_root()
            / "profile_templates"
            / ".github"
            / "workflows"
            / "host-cycle.yml"
        )
        workflow_target = root / ".github" / "workflows" / "host-cycle.yml"
        shutil.copyfile(workflow_source, workflow_target)
        written.append(str(workflow_target.relative_to(root)))

    archive_manifest_source = (
        code_root()
        / "profile_templates"
        / "audit_archive"
        / "manifest.json"
    )
    archive_manifest_target = root / "audit_archive" / "manifest.json"
    shutil.copyfile(archive_manifest_source, archive_manifest_target)
    written.append(str(archive_manifest_target.relative_to(root)))

    files = {
        "OPERATOR_PREFERENCES.md": PREFERENCES_TEMPLATE,
        ".gitignore": _merge_gitignore(GITIGNORE),
        "README.md": README_TEMPLATE.format(root=root),
        "core.lock": _core_lock(core_commit),
        "host_input/.promotion_policy.json": (
            json.dumps(PROMOTION_POLICY, indent=2) + "\n"
        ),
        "PARAMETERS.json": json.dumps({"schema_version": 1}, indent=2) + "\n",
        "STATE.json": json.dumps(
            {"schema_version": 1, "created_at": created_at}, indent=2,
        ) + "\n",
    }
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
        written.append(name)

    _write_initial_feedback(root, written)
    _write_empty_directory_markers(root, written)
    return written


def repair_profile(
    root: Path | str,
    *,
    core_commit: str | None = None,
    upgrade_core: bool = False,
    install_workflow: bool = True,
) -> list[str]:
    """Repair control-plane files without rewriting operator state."""
    root = Path(root).expanduser().resolve()
    journal_path = existing_journal(root)
    if journal_path is None:
        return init_profile(
            root,
            core_commit=core_commit,
            install_workflow=install_workflow,
        )
    journal_result = AuditJournal(journal_path).validate()
    if not journal_result["valid"]:
        raise ValueError(
            "profile_journal_invalid:"
            + json.dumps(journal_result, sort_keys=True)
        )

    written: list[str] = []
    for relative in PROFILE_DIRECTORIES:
        path = root / relative
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            written.append(f"{relative}/")

    policy_path = root / "host_input" / ".promotion_policy.json"
    if policy_path.exists():
        load_policy(policy_path.parent)
    else:
        policy_path.write_text(
            json.dumps(PROMOTION_POLICY, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append("host_input/.promotion_policy.json")

    archive = root / "audit_archive"
    archive_manifest = archive / "manifest.json"
    if not archive_manifest.exists():
        if any(archive.glob("*.jsonl")):
            raise ValueError(
                "profile_archive_manifest_missing_with_records"
            )
        _copy_if_changed(
            code_root()
            / "profile_templates"
            / "audit_archive"
            / "manifest.json",
            archive_manifest,
            root,
            written,
        )

    if install_workflow:
        _copy_if_changed(
            code_root()
            / "profile_templates"
            / ".github"
            / "workflows"
            / "host-cycle.yml",
            root / ".github" / "workflows" / "host-cycle.yml",
            root,
            written,
        )

    _write_if_missing(
        root,
        "OPERATOR_PREFERENCES.md",
        PREFERENCES_TEMPLATE,
        written,
    )
    _write_if_missing(
        root,
        "README.md",
        README_TEMPLATE.format(root=root),
        written,
    )
    _write_if_missing(
        root,
        "PARAMETERS.json",
        json.dumps({"schema_version": 1}, indent=2) + "\n",
        written,
    )
    _write_if_missing(
        root,
        "STATE.json",
        json.dumps({"schema_version": 1}, indent=2) + "\n",
        written,
    )

    lock_path = root / "core.lock"
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("profile_core_lock_invalid") from error
        if not isinstance(lock, dict):
            raise ValueError("profile_core_lock_invalid")
        if upgrade_core:
            if core_commit is None:
                raise ValueError("profile_core_upgrade_commit_required")
            body = _core_lock(core_commit)
            if lock_path.read_text(encoding="utf-8") != body:
                lock_path.write_text(body, encoding="utf-8")
                written.append("core.lock")
    else:
        lock_path.write_text(_core_lock(core_commit), encoding="utf-8")
        written.append("core.lock")

    gitignore = root / ".gitignore"
    existing_ignore = (
        gitignore.read_text(encoding="utf-8")
        if gitignore.exists()
        else ""
    )
    merged_ignore = _merge_gitignore(existing_ignore)
    if merged_ignore != existing_ignore:
        gitignore.write_text(merged_ignore, encoding="utf-8")
        written.append(".gitignore")

    _write_initial_feedback(root, written)
    _write_empty_directory_markers(root, written)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="where this operator's state will live")
    parser.add_argument(
        "--core-commit",
        help="full SHA of the core commit this profile runs; written to "
             "core.lock. Left UNPINNED if omitted, which every later cycle "
             "will report as unresolved.",
    )
    parser.add_argument(
        "--no-workflow",
        action="store_true",
        help="initialize state without modifying .github/workflows",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        written = init_profile(
            args.root,
            core_commit=args.core_commit,
            install_workflow=not args.no_workflow,
        )
    except FileExistsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    root = Path(args.root).expanduser().resolve()
    print(f"created {len(written)} entries in {root}")
    print()
    print("Next:")
    print(f"  export SOVEREIGN_PROFILE_DIR={root}")
    print(f"  edit {root / 'OPERATOR_PREFERENCES.md'}")
    if not args.core_commit:
        print(f"  pin a core commit in {root / 'core.lock'}")
    print("  then run one host cycle")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
