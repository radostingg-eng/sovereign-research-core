#!/usr/bin/env python3
"""Install or repair one existing private profile using owner-authorized gh."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime.init_profile import repair_profile  # noqa: E402
from runtime.profile_health import check_profile  # noqa: E402
from runtime.profile_paths import PROFILE_PATHS  # noqa: E402

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PERMISSIONS = {"ADMIN", "MAINTAIN", "WRITE"}


def _run(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def _verify_gh_authority(repo: str) -> dict:
    auth = _run("gh", "auth", "status", "-h", "github.com")
    if "workflow" not in auth.stdout + auth.stderr:
        raise RuntimeError(
            "gh workflow scope missing; run: "
            "gh auth refresh -h github.com -s workflow"
        )
    info = json.loads(_run(
        "gh", "repo", "view", repo,
        "--json", "nameWithOwner,visibility,viewerPermission,defaultBranchRef",
    ).stdout)
    if info.get("visibility") != "PRIVATE":
        raise RuntimeError("target profile repository must be private")
    if info.get("viewerPermission") not in _PERMISSIONS:
        raise RuntimeError(
            f"insufficient repository permission:{info.get('viewerPermission')}"
        )
    branch = (info.get("defaultBranchRef") or {}).get("name")
    if branch != "main":
        raise RuntimeError(f"profile default branch must be main:{branch}")
    return info


def _allowed_change(path: str) -> bool:
    if path == ".github/workflows/host-cycle.yml":
        return True
    if path in {".gitignore", "README.md", "core.lock"}:
        return True
    top = Path(path).parts[0] if Path(path).parts else ""
    return top in {Path(relative).parts[0] for relative in PROFILE_PATHS}


def install_repository(
    repo: str,
    *,
    core_commit: str,
    upgrade_core: bool,
    dry_run: bool = False,
) -> list[str]:
    if _COMMIT.fullmatch(core_commit) is None:
        raise ValueError("core_commit_must_be_full_sha")
    _verify_gh_authority(repo)
    with tempfile.TemporaryDirectory(prefix="sovereign-profile-install-") as tmp:
        checkout = Path(tmp) / "profile"
        _run("gh", "repo", "clone", repo, str(checkout), "--", "--quiet")
        lock_path = checkout / "core.lock"
        if lock_path.exists():
            try:
                current = json.loads(lock_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise RuntimeError("existing core.lock is invalid") from error
            if (
                current.get("commit") != core_commit
                and not upgrade_core
            ):
                raise RuntimeError(
                    "core pin differs; rerun with --upgrade-core after review"
                )
        repair_profile(
            checkout,
            core_commit=core_commit,
            upgrade_core=upgrade_core,
            install_workflow=True,
        )
        errors = check_profile(checkout)
        if errors:
            raise RuntimeError("profile health failed:" + ",".join(errors))
        status = _run(
            "git", "status", "--porcelain=v1",
            cwd=checkout,
        ).stdout.splitlines()
        changed = [line[3:] for line in status if len(line) >= 4]
        unexpected = [path for path in changed if not _allowed_change(path)]
        if unexpected:
            raise RuntimeError(
                "installer changed unexpected paths:" + ",".join(unexpected)
            )
        if dry_run or not changed:
            return changed
        _run(
            "git", "config", "user.name",
            "sovereign-profile-installer",
            cwd=checkout,
        )
        _run(
            "git", "config", "user.email",
            "noreply@github.com",
            cwd=checkout,
        )
        _run("git", "add", "-A", cwd=checkout)
        _run(
            "git", "commit", "-m",
            f"install: sovereign profile core {core_commit[:12]}",
            cwd=checkout,
        )
        _run("git", "fetch", "--quiet", "origin", "main", cwd=checkout)
        if _run(
            "git", "rev-parse", "origin/main", cwd=checkout,
        ).stdout.strip() != _run(
            "git", "rev-parse", "HEAD^", cwd=checkout,
        ).stdout.strip():
            raise RuntimeError(
                "profile main moved during install; rerun against the new head"
            )
        _run("git", "push", "origin", "HEAD:main", cwd=checkout)
        return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="owner/name private profile repository")
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--upgrade-core", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = install_repository(
            args.repo,
            core_commit=args.core_commit,
            upgrade_core=args.upgrade_core,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"install refused: {error}", file=sys.stderr)
        return 1
    print(
        "profile healthy; "
        + (f"{len(changed)} paths would change" if args.dry_run else
           f"{len(changed)} paths changed")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
