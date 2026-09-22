"""Synchronize the dedicated profile checkout without relying on FETCH_HEAD."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time
from typing import Sequence

MAIN_REFSPEC = "+refs/heads/main:refs/remotes/origin/main"


class GitSyncError(RuntimeError):
    """Raised when the profile checkout cannot be synchronized safely."""


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _git_path(repo: Path, name: str) -> Path:
    result = _git(repo, "rev-parse", "--git-path", name)
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def ensure_no_rebase_in_progress(repo: Path | str) -> None:
    root = Path(repo).resolve()
    active = [
        name
        for name in ("rebase-merge", "rebase-apply")
        if _git_path(root, name).exists()
    ]
    if active:
        raise GitSyncError(
            "git_rebase_state_present:" + "|".join(active)
        )


def _abort_rebase(repo: Path) -> None:
    aborted = _git(repo, "rebase", "--abort", check=False)
    if aborted.returncode == 0:
        return
    _git(repo, "rebase", "--quit", check=False)


def sync_main(
    repo: Path | str,
    *,
    fetch_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> str:
    root = Path(repo).resolve()
    ensure_no_rebase_in_progress(root)
    if fetch_attempts < 1:
        raise ValueError("fetch_attempts_must_be_positive")

    failure = ""
    for attempt in range(fetch_attempts):
        fetched = _git(
            root,
            "fetch",
            "--quiet",
            "origin",
            MAIN_REFSPEC,
            check=False,
        )
        if fetched.returncode == 0:
            break
        failure = (fetched.stderr or fetched.stdout).strip()
        if attempt + 1 < fetch_attempts:
            time.sleep(retry_delay_seconds)
    else:
        raise GitSyncError(
            "git_fetch_main_failed:" + (failure or "unknown")
        )

    tracking = _git(
        root,
        "rev-parse",
        "--verify",
        "refs/remotes/origin/main",
        check=False,
    )
    if tracking.returncode != 0:
        raise GitSyncError("git_origin_main_missing_after_fetch")

    rebased = _git(
        root,
        "rebase",
        "--quiet",
        "refs/remotes/origin/main",
        check=False,
    )
    if rebased.returncode != 0:
        detail = (rebased.stderr or rebased.stdout).strip()
        _abort_rebase(root)
        raise GitSyncError(
            "git_rebase_main_failed:" + (detail or "unknown")
        )
    return tracking.stdout.strip()


def push_main(
    repo: Path | str,
    *,
    push_attempts: int = 2,
    retry_delay_seconds: float = 1.0,
) -> None:
    """Push main with bounded commands and one sync-before-retry path."""
    root = Path(repo).resolve()
    ensure_no_rebase_in_progress(root)
    if push_attempts < 1:
        raise ValueError("push_attempts_must_be_positive")

    failure = ""
    for attempt in range(push_attempts):
        try:
            pushed = _git(
                root,
                "push",
                "--quiet",
                "origin",
                "main",
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            failure = f"timeout_after_{error.timeout}s"
        else:
            if pushed.returncode == 0:
                return
            failure = (pushed.stderr or pushed.stdout).strip()
        if attempt + 1 < push_attempts:
            sync_main(
                root,
                retry_delay_seconds=retry_delay_seconds,
            )
    raise GitSyncError(
        "git_push_main_failed:" + (failure or "unknown")
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--push-only", action="store_true")
    args = parser.parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    try:
        ensure_no_rebase_in_progress(args.repo)
        if args.check_only and args.push_only:
            raise ValueError("git_sync_mode_conflict")
        if args.push_only:
            push_main(args.repo)
        elif not args.check_only:
            sync_main(args.repo)
    except (GitSyncError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
