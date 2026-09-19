"""Publish validated external worker records into a private profile repo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.profile_lock import profile_lock
from runtime.research_inbox import load_inbox_record


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


def _load_outbox(path: Path) -> tuple[dict[str, Any], bytes]:
    content = path.read_bytes()
    return load_inbox_record(path), content


def _unexpected_dirty_paths(profile_root: Path) -> list[str]:
    result = _git(
        profile_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    paths = []
    for line in result.stdout.splitlines():
        path = line[3:]
        if path and not path.startswith("research_inbox/"):
            paths.append(path)
    return paths


def publish_outbox(
    *,
    profile_root: Path | str,
    outbox_dir: Path | str,
    lock_path: Path | str | None = None,
) -> list[str]:
    profile = Path(profile_root).resolve()
    outbox = Path(outbox_dir).resolve()
    paths = sorted(outbox.glob("*.json")) if outbox.is_dir() else []
    if not paths:
        return []
    loaded = [(path, *_load_outbox(path)) for path in paths]
    lock = (
        Path(lock_path)
        if lock_path is not None
        else profile / ".git" / "sovereign-profile.lock"
    )
    published = []
    with profile_lock(lock):
        unexpected = _unexpected_dirty_paths(profile)
        if unexpected:
            raise RuntimeError(
                "research_inbox_profile_dirty:"
                + "|".join(unexpected)
            )
        _git(profile, "switch", "--quiet", "main")
        _git(profile, "pull", "--rebase", "--quiet", "origin", "main")
        for source, value, content in loaded:
            worker_id = str(value["worker_id"])
            target = (
                profile
                / "research_inbox"
                / worker_id
                / source.name
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() != content:
                    raise RuntimeError(
                        f"research_inbox_collision:{target.name}"
                    )
            else:
                target.write_bytes(content)
            published.append(str(target.relative_to(profile)))
        _git(profile, "add", "-A", "research_inbox")
        staged = _git(
            profile,
            "diff",
            "--cached",
            "--quiet",
            check=False,
        )
        if staged.returncode not in {0, 1}:
            raise RuntimeError("research_inbox_git_diff_failed")
        if staged.returncode == 1:
            workers = sorted({
                str(value["worker_id"])
                for _source, value, _content in loaded
            })
            _git(
                profile,
                "commit",
                "-m",
                "research inbox: publish "
                + ",".join(workers)
                + f" ({len(loaded)})",
            )
            pushed = _git(
                profile,
                "push",
                "--quiet",
                "origin",
                "main",
                check=False,
            )
            if pushed.returncode != 0:
                _git(
                    profile,
                    "pull",
                    "--rebase",
                    "--quiet",
                    "origin",
                    "main",
                )
                _git(profile, "push", "--quiet", "origin", "main")
        for source, _value, _content in loaded:
            source.unlink()
    return published


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile-root", required=True)
    parser.add_argument("--outbox-dir", required=True)
    parser.add_argument("--lock-path")
    args = parser.parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    try:
        published = publish_outbox(
            profile_root=args.profile_root,
            outbox_dir=args.outbox_dir,
            lock_path=args.lock_path,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({
        "published": published,
        "count": len(published),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
