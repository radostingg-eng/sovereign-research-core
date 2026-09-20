import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "ops" / "run_hostcycle.sh"
INSTALLER = ROOT / "ops" / "install_hostcycle.sh"
PLIST = ROOT / "ops" / "com.sovereign.hostcycle.plist"


def run(*args, cwd=None, check=True, env=None):
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        env=env,
    )


class DedicatedExecutorTemplateTests(unittest.TestCase):
    def test_plist_runs_the_dedicated_runner(self):
        text = PLIST.read_text(encoding="utf-8")
        self.assertIn(
            "REPLACE_WITH_EXECUTOR_REPO_PATH/ops/run_hostcycle.sh",
            text,
        )
        self.assertIn("REPLACE_WITH_PYTHON_BIN", text)
        self.assertNotIn("git pull --rebase --quiet ||", text)

    def test_installer_defaults_outside_the_developer_checkout(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn(
            "$HOME/.local/share/sovereign-research-executor",
            text,
        )
        self.assertIn("git clone", text)
        self.assertIn("sys.executable", text)
        self.assertIn("REPLACE_WITH_PYTHON_BIN", text)


class HostcycleRunnerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = pathlib.Path(tempfile.mkdtemp(prefix="hostcycle-runner-"))
        self.addCleanup(shutil.rmtree, self.tempdir)
        self.remote = self.tempdir / "remote.git"
        self.worker = self.tempdir / "worker"
        run("git", "init", "--bare", "--initial-branch=main", str(self.remote))
        run("git", "clone", str(self.remote), str(self.worker))
        run("git", "config", "user.name", "test", cwd=self.worker)
        run("git", "config", "user.email", "test@example.com", cwd=self.worker)
        (self.worker / "audit").mkdir()
        (self.worker / "host_input").mkdir()
        (self.worker / "audit" / ".gitkeep").write_text("", encoding="utf-8")
        (self.worker / "host_input" / ".gitkeep").write_text(
            "", encoding="utf-8")
        run("git", "add", ".", cwd=self.worker)
        run("git", "commit", "-m", "initial", cwd=self.worker)
        run("git", "push", "-u", "origin", "main", cwd=self.worker)
        self.env = {
            **os.environ,
            "HOSTCYCLE_MAX_JITTER_SECONDS": "0",
            "PYTHON_BIN": "/usr/bin/true",
            "SOVEREIGN_PROFILE_LOCK_HELD": "1",
        }

    def test_pending_receipt_is_committed_and_pushed_before_pull(self):
        pending = self.worker / "audit" / "pending.jsonl"
        pending.write_text('{"receipt":"preserved"}\n', encoding="utf-8")

        result = run(
            str(RUNNER),
            str(self.worker),
            check=False,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            run("git", "status", "--porcelain", cwd=self.worker).stdout,
            "",
        )
        log = run(
            "git", "--git-dir", str(self.remote), "log",
            "--format=%s", "-1",
        ).stdout.strip()
        self.assertEqual(
            log,
            "audit: recover pending cycle result and host feedback",
        )

    def test_pending_tool_artifact_is_published_with_receipt(self):
        pending = self.worker / "audit" / "pending.jsonl"
        pending.write_text('{"receipt":"preserved"}\n', encoding="utf-8")
        artifact = (
            self.worker
            / "tool_artifacts"
            / "sha256"
            / "aa"
            / ("a" * 64 + ".json")
        )
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"value":1}\n', encoding="utf-8")

        result = run(
            str(RUNNER),
            str(self.worker),
            check=False,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tree = run(
            "git",
            "--git-dir",
            str(self.remote),
            "ls-tree",
            "-r",
            "--name-only",
            "main",
        ).stdout
        self.assertIn(
            "tool_artifacts/sha256/aa/" + "a" * 64 + ".json",
            tree,
        )

    def test_unrelated_dirty_file_fails_without_discarding_it(self):
        unexpected = self.worker / "README.md"
        unexpected.write_text("developer work\n", encoding="utf-8")

        result = run(
            str(RUNNER),
            str(self.worker),
            check=False,
            env=self.env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected dirty files", result.stdout)
        self.assertEqual(
            unexpected.read_text(encoding="utf-8"),
            "developer work\n",
        )

    def test_pending_research_inbox_is_published(self):
        pending = (
            self.worker
            / "research_inbox"
            / "azure-a"
            / "worker-record.json"
        )
        pending.parent.mkdir(parents=True)
        pending.write_text('{"status":"completed"}\n', encoding="utf-8")

        result = run(
            str(RUNNER),
            str(self.worker),
            check=False,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tree = run(
            "git",
            "--git-dir",
            str(self.remote),
            "ls-tree",
            "-r",
            "--name-only",
            "main",
        ).stdout
        self.assertIn(
            "research_inbox/azure-a/worker-record.json",
            tree,
        )

    def test_schedule_watchdog_lock_does_not_jam_pending_ledger(self):
        ledger = self.worker / "runs" / "SCHEDULE_EVENTS.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"record_id":"seed"}\n', encoding="utf-8")
        run("git", "add", "runs/SCHEDULE_EVENTS.jsonl", cwd=self.worker)
        run("git", "commit", "-m", "seed ledger", cwd=self.worker)
        run("git", "push", "--quiet", "origin", "main", cwd=self.worker)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write('{"record_id":"watchdog-heartbeat-1"}\n')
        ledger.with_name(ledger.name + ".lock").touch()

        result = run(
            str(RUNNER),
            str(self.worker),
            check=False,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            run("git", "status", "--porcelain", cwd=self.worker).stdout,
            "?? runs/SCHEDULE_EVENTS.jsonl.lock\n",
        )
        ledger_on_main = run(
            "git", "--git-dir", str(self.remote), "show",
            "main:runs/SCHEDULE_EVENTS.jsonl",
        ).stdout
        self.assertIn("watchdog-heartbeat-1", ledger_on_main)

    def test_unexpected_schedule_contract_change_still_refused(self):
        contract = self.worker / "runs" / "SCHEDULE.json"
        contract.parent.mkdir(parents=True, exist_ok=True)
        contract.write_text('{"cadence_minutes":60}\n', encoding="utf-8")
        run("git", "add", "runs/SCHEDULE.json", cwd=self.worker)
        run("git", "commit", "-m", "seed schedule contract", cwd=self.worker)
        run("git", "push", "--quiet", "origin", "main", cwd=self.worker)
        contract.write_text('{"cadence_minutes":30}\n', encoding="utf-8")

        result = run(
            str(RUNNER),
            str(self.worker),
            check=False,
            env=self.env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected dirty files", result.stdout)
        self.assertIn("runs/SCHEDULE.json", result.stdout)


if __name__ == "__main__":
    unittest.main()
