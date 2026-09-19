"""Serialize profile checkout mutations across independent local workers."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Iterator, Sequence


class ProfileLockTimeout(TimeoutError):
    pass


@contextmanager
def profile_lock(
    path: Path | str,
    *,
    timeout_seconds: float = 540.0,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProfileLockTimeout(
                        f"profile_lock_timeout:{lock_path}"
                    )
                time.sleep(poll_seconds)
        handle.seek(0)
        handle.truncate()
        handle.write(
            f"pid={os.getpid()} acquired_at={time.time()}\n"
        )
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def run_locked(
    command: Sequence[str],
    *,
    lock_path: Path | str,
    timeout_seconds: float = 540.0,
) -> int:
    if not command:
        raise ValueError("profile_lock_command_required")
    with profile_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
    ):
        env = dict(os.environ)
        env["SOVEREIGN_PROFILE_LOCK_HELD"] = "1"
        return subprocess.run(
            list(command),
            check=False,
            env=env,
        ).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", required=True)
    parser.add_argument("--timeout", type=float, default=540.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        return run_locked(
            command,
            lock_path=args.lock,
            timeout_seconds=args.timeout,
        )
    except (ProfileLockTimeout, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
