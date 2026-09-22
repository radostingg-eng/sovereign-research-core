import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.git_sync import (
    GitSyncError,
    MAIN_REFSPEC,
    ensure_no_rebase_in_progress,
    push_main,
    sync_main,
)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="git-sync-"))
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.worker = self.root / "worker"
        self.peer = self.root / "peer"
        subprocess.run(
            [
                "git",
                "init",
                "--bare",
                "--initial-branch=main",
                str(self.remote),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.seed)],
            check=True,
            capture_output=True,
        )
        self._configure(self.seed)
        (self.seed / "README.md").write_text("initial\n")
        git(self.seed, "add", "README.md")
        git(self.seed, "commit", "-m", "initial")
        git(self.seed, "push", "-u", "origin", "main")
        for path in (self.worker, self.peer):
            subprocess.run(
                ["git", "clone", str(self.remote), str(path)],
                check=True,
                capture_output=True,
            )
            self._configure(path)

    def _configure(self, repo):
        git(repo, "config", "user.name", "test")
        git(repo, "config", "user.email", "test@example.com")

    def test_explicit_refspec_updates_main_despite_fetch_config(self):
        git(self.worker, "config", "--unset-all", "remote.origin.fetch")
        git(
            self.worker,
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/heads/other:refs/remotes/origin/other",
        )
        (self.peer / "REMOTE.md").write_text("remote\n")
        git(self.peer, "add", "REMOTE.md")
        git(self.peer, "commit", "-m", "remote")
        git(self.peer, "push", "origin", "main")

        fetched = sync_main(
            self.worker,
            retry_delay_seconds=0,
        )

        self.assertEqual(
            fetched,
            git(self.peer, "rev-parse", "HEAD").stdout.strip(),
        )
        self.assertEqual(
            git(self.worker, "rev-parse", "HEAD").stdout.strip(),
            fetched,
        )
        self.assertEqual(MAIN_REFSPEC, (
            "+refs/heads/main:refs/remotes/origin/main"
        ))

    def test_conflicted_rebase_is_aborted(self):
        (self.worker / "README.md").write_text("worker\n")
        git(self.worker, "add", "README.md")
        git(self.worker, "commit", "-m", "worker")
        local_head = git(
            self.worker,
            "rev-parse",
            "HEAD",
        ).stdout.strip()
        (self.peer / "README.md").write_text("peer\n")
        git(self.peer, "add", "README.md")
        git(self.peer, "commit", "-m", "peer")
        git(self.peer, "push", "origin", "main")

        with self.assertRaisesRegex(
            GitSyncError,
            "git_rebase_main_failed",
        ):
            sync_main(self.worker, retry_delay_seconds=0)

        ensure_no_rebase_in_progress(self.worker)
        self.assertEqual(
            git(self.worker, "rev-parse", "HEAD").stdout.strip(),
            local_head,
        )
        self.assertEqual(
            git(self.worker, "status", "--porcelain").stdout,
            "",
        )

    def test_fetch_retries_before_succeeding(self):
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="a" * 40 + "\n",
            stderr="",
        )
        failed = subprocess.CompletedProcess(
            args=["git"],
            returncode=1,
            stdout="",
            stderr="cannot lock ref",
        )
        calls = []

        def fake_git(_repo, *args, check=True):
            calls.append(args)
            if args[0] == "fetch" and sum(
                call[0] == "fetch" for call in calls
            ) < 3:
                return failed
            return completed

        with patch("ops.git_sync._git", side_effect=fake_git):
            sync_main(
                self.worker,
                fetch_attempts=3,
                retry_delay_seconds=0,
            )

        self.assertEqual(
            sum(call[0] == "fetch" for call in calls),
            3,
        )

    def test_existing_rebase_state_fails_closed(self):
        rebase_dir = self.worker / ".git" / "rebase-merge"
        rebase_dir.mkdir()

        with self.assertRaisesRegex(
            GitSyncError,
            "git_rebase_state_present:rebase-merge",
        ):
            ensure_no_rebase_in_progress(self.worker)

    def test_push_rebases_remote_advance_then_retries(self):
        (self.worker / "LOCAL.md").write_text("local\n")
        git(self.worker, "add", "LOCAL.md")
        git(self.worker, "commit", "-m", "local")
        (self.peer / "REMOTE.md").write_text("remote\n")
        git(self.peer, "add", "REMOTE.md")
        git(self.peer, "commit", "-m", "remote")
        git(self.peer, "push", "origin", "main")

        push_main(self.worker, retry_delay_seconds=0)

        remote_head = git(
            self.worker,
            "ls-remote",
            "origin",
            "refs/heads/main",
        ).stdout.split()[0]
        self.assertEqual(
            git(self.worker, "rev-parse", "HEAD").stdout.strip(),
            remote_head,
        )
        self.assertTrue((self.worker / "LOCAL.md").is_file())
        self.assertTrue((self.worker / "REMOTE.md").is_file())

    def test_push_timeout_is_reported_after_bounded_retry(self):
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="a" * 40 + "\n",
            stderr="",
        )
        push_calls = 0

        def fake_git(_repo, *args, check=True):
            nonlocal push_calls
            if args[0] == "push":
                push_calls += 1
                raise subprocess.TimeoutExpired(
                    cmd=["git", *args],
                    timeout=120,
                )
            return completed

        with patch("ops.git_sync._git", side_effect=fake_git):
            with self.assertRaisesRegex(
                GitSyncError,
                "git_push_main_failed:timeout_after_120s",
            ):
                push_main(
                    self.worker,
                    retry_delay_seconds=0,
                )

        self.assertEqual(push_calls, 2)


if __name__ == "__main__":
    unittest.main()
