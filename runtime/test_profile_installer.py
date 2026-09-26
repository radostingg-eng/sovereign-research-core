import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .init_profile import init_profile
from .profile_health import check_profile
from .run_host_cycle import main as run_host_cycles
from .staged_intake import process_staging
from .test_staged_intake import sample_input

ROOT = Path(__file__).resolve().parent.parent
BUILD_TEMPLATE = ROOT / "ops" / "build_profile_template.py"
REMOTE_INSTALLER = ROOT / "ops" / "install_profile_repo.py"


def run(*args, cwd=None, check=True, env=None):
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        env=env,
        text=True,
        capture_output=True,
    )


class ProfileRepositoryTemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="profile-template-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.commit = "a" * 40

    def build(self, name):
        output = self.root / name
        run(
            "python3",
            str(BUILD_TEMPLATE),
            str(output),
            "--core-commit",
            self.commit,
        )
        return output

    def test_template_is_data_free_and_contains_static_workflows(self):
        output = self.build("template")
        self.assertTrue(
            (output / ".github/workflows/host-cycle.yml").is_file()
        )
        self.assertTrue(
            (output / ".github/workflows/profile-bootstrap.yml").is_file()
        )
        self.assertFalse((output / "audit").exists())
        self.assertFalse((output / "portfolio").exists())
        self.assertFalse((output / "OPERATOR_PREFERENCES.md").exists())
        lock = json.loads((output / "core.lock").read_text())
        self.assertEqual(lock["commit"], self.commit)
        self.assertIn("must be **private**", (output / "README.md").read_text())
        ignored = (output / ".gitignore").read_text()
        self.assertIn("runs/SCHEDULE_EVENTS.jsonl.lock", ignored)
        self.assertNotIn("*.lock", ignored)
        self.assertNotIn("\nruns/\n", "\n" + ignored)

    def test_template_bootstrap_creates_unique_profiles_without_workflow_writes(self):
        profiles = []
        for name in ("one", "two"):
            profile = self.build(name)
            workflows = {
                path.name: path.read_bytes()
                for path in (profile / ".github/workflows").glob("*.yml")
            }
            init_profile(
                profile,
                core_commit=self.commit,
                install_workflow=False,
            )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (profile / ".github/workflows").glob("*.yml")
                },
                workflows,
            )
            self.assertEqual(check_profile(profile), [])
            journal = next((profile / "audit").glob("*.jsonl"))
            profiles.append(json.loads(journal.read_text()))
        self.assertNotEqual(
            profiles[0]["record_hash"],
            profiles[1]["record_hash"],
        )

    def test_bootstrap_workflow_never_stages_workflow_files(self):
        text = (
            ROOT
            / "profile_templates"
            / ".github"
            / "workflows"
            / "profile-bootstrap.yml"
        ).read_text()
        self.assertIn("--no-workflow", text)
        self.assertIn("-m runtime.repair_profile", text)
        self.assertIn("PYTHONPATH: ${{ github.workspace }}/.core", text)
        self.assertIn("github.event.repository.private", text)
        self.assertNotIn("git add -A -- .github", text)
        self.assertIn("profile code shadow present", text)

    def test_schedule_accounting_does_not_run_on_profile_pushes(self):
        text = (
            ROOT
            / "profile_templates"
            / ".github"
            / "workflows"
            / "host-cycle.yml"
        ).read_text()
        self.assertIn("github.event_name == 'schedule'", text)
        self.assertIn("github.event_name == 'workflow_dispatch'", text)
        self.assertNotIn(
            "account-schedule:\n    if: always()\n",
            text,
        )

    def test_host_cycle_cadence_stays_inside_heartbeat_threshold(self):
        """Actions-minutes lever #1: the account-schedule cron must run
        often enough that its heartbeat never trips
        schedule_ledger.check_watchdog_heartbeat, even with a realistic
        GitHub cron dispatch delay.
        """
        from .schedule_ledger import check_watchdog_heartbeat

        threshold_hours = inspect.signature(
            check_watchdog_heartbeat
        ).parameters["max_age_hours"].default
        text = (
            ROOT
            / "profile_templates"
            / ".github"
            / "workflows"
            / "host-cycle.yml"
        ).read_text()
        match = re.search(r"cron: '(\d+) \*/(\d+) \* \* \*'", text)
        self.assertIsNotNone(
            match, "expected an every-N-hours 'MM */N * * *' cron expression"
        )
        cadence_hours = int(match.group(2))
        # Documented worst-case GitHub Actions scheduled-dispatch delay
        # assumption from the profile-minutes reduction plan. The 4.0h
        # threshold (raised from 3.0h once observed delays could exceed
        # 1h) keeps a real margin above cadence + this worst case instead
        # of sitting exactly at the alarm boundary.
        worst_case_delay_hours = 1.0
        self.assertLessEqual(
            cadence_hours, 2,
            "cadence regressed past the reviewed 2h ceiling",
        )
        self.assertLessEqual(
            cadence_hours + worst_case_delay_hours,
            threshold_hours,
            "cadence + worst-case delay must stay inside the watchdog "
            "heartbeat alarm threshold",
        )

    def test_host_cycle_checkouts_use_partial_clone_with_full_history(self):
        """Actions-minutes lever #2: both profile checkouts fetch blobs
        lazily but must keep full commit/tree history so `git rebase`
        and the git-log-based metadata lookups in runtime/ keep working.
        """
        text = (
            ROOT
            / "profile_templates"
            / ".github"
            / "workflows"
            / "host-cycle.yml"
        ).read_text()
        self.assertEqual(text.count("fetch-depth: 0"), 2)
        self.assertEqual(text.count("filter: blob:none"), 2)
        for depth_index, filter_index in zip(
            (m.start() for m in re.finditer("fetch-depth: 0", text)),
            (m.start() for m in re.finditer("filter: blob:none", text)),
        ):
            self.assertLess(
                abs(depth_index - filter_index), 40,
                "fetch-depth: 0 and filter: blob:none must be on the same "
                "checkout step",
            )

    def test_host_cycle_safety_gates_and_dedup_survive_minutes_changes(self):
        """The minutes-reduction pass must not weaken any check: private-
        repo refusal and code-shadow gates stay in both jobs, and the
        duplicated 'read pinned core' block collapses to one YAML anchor
        instead of two independent copies drifting apart.
        """
        text = (
            ROOT
            / "profile_templates"
            / ".github"
            / "workflows"
            / "host-cycle.yml"
        ).read_text()
        # The private-repo refusal and code-shadow gate live only in the
        # `cycle` job (account-schedule trusts a job it `needs`), so each
        # marker appears exactly once.
        self.assertEqual(
            text.count("github.event.repository.private"), 1
        )
        self.assertEqual(text.count("profile code shadow present"), 1)
        self.assertEqual(text.count("&read_pinned_core"), 1)
        self.assertEqual(text.count("*read_pinned_core"), 1)

    def test_bootstrap_repair_entrypoint_preserves_existing_state(self):
        profile = self.root / "existing"
        init_profile(profile, core_commit=self.commit)
        journal = next((profile / "audit").glob("*.jsonl"))
        preferences = profile / "OPERATOR_PREFERENCES.md"
        state = profile / "STATE.json"
        before = {
            "journal": journal.read_bytes(),
            "preferences": preferences.read_bytes(),
            "state": state.read_bytes(),
        }
        (profile / "host_input" / ".promotion_policy.json").unlink()
        (profile / "host_staging" / "FEEDBACK.json").unlink()

        result = run(
            "python3",
            "-m",
            "runtime.repair_profile",
            "--root",
            str(profile),
            "--core-commit",
            self.commit,
            "--no-workflow",
            cwd=ROOT,
        )

        self.assertIn("profile repair healthy", result.stdout)
        self.assertEqual(journal.read_bytes(), before["journal"])
        self.assertEqual(preferences.read_bytes(), before["preferences"])
        self.assertEqual(state.read_bytes(), before["state"])
        self.assertTrue(
            (profile / "host_input" / ".promotion_policy.json").is_file()
        )
        self.assertTrue(
            (profile / "host_staging" / "FEEDBACK.json").is_file()
        )


class ProfileCodeIsolationTests(unittest.TestCase):
    def test_python_safe_path_executes_pinned_core_not_profile_shadow(self):
        root = Path(tempfile.mkdtemp(prefix="profile-safe-path-"))
        self.addCleanup(shutil.rmtree, root)
        for base, value in (
            (root / "runtime", "PROFILE"),
            (root / ".core" / "runtime", "CORE"),
        ):
            base.mkdir(parents=True)
            (base / "__init__.py").write_text("", encoding="utf-8")
            (base / "probe.py").write_text(
                f"print({value!r})\n",
                encoding="utf-8",
            )
        result = run(
            "python3",
            "-P",
            "-m",
            "runtime.probe",
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / ".core")},
        )
        self.assertEqual(result.stdout.strip(), "CORE")

    def test_profile_health_refuses_code_shadowing(self):
        root = Path(tempfile.mkdtemp(prefix="profile-shadow-"))
        self.addCleanup(shutil.rmtree, root)
        init_profile(root, core_commit="a" * 40)
        (root / "runtime").mkdir()
        self.assertIn(
            "profile_code_shadow_present:runtime",
            check_profile(root),
        )


class ProfilePersistenceTests(unittest.TestCase):
    def test_initialized_controls_and_feedback_survive_a_clean_clone(self):
        root = Path(tempfile.mkdtemp(prefix="profile-persist-"))
        self.addCleanup(shutil.rmtree, root)
        source = root / "source"
        remote = root / "remote.git"
        clone = root / "clone"
        init_profile(source, core_commit="a" * 40)
        run("git", "init", "-q", "-b", "main", cwd=source)
        run("git", "config", "user.name", "test", cwd=source)
        run("git", "config", "user.email", "test@example.com", cwd=source)
        run("git", "add", "-A", cwd=source)
        run("git", "commit", "-q", "-m", "profile", cwd=source)
        run("git", "init", "--bare", "--initial-branch=main", str(remote))
        run("git", "remote", "add", "origin", str(remote), cwd=source)
        run("git", "push", "-q", "-u", "origin", "main", cwd=source)
        run("git", "clone", "-q", str(remote), str(clone))

        self.assertEqual(check_profile(clone), [])
        for relative in (
            "host_input/.promotion_policy.json",
            "host_input/FEEDBACK.json",
            "host_staging/FEEDBACK.json",
        ):
            self.assertTrue((clone / relative).is_file(), relative)

    def test_refusal_feedback_survives_push_and_clean_clone(self):
        root = Path(tempfile.mkdtemp(prefix="profile-refusal-"))
        self.addCleanup(shutil.rmtree, root)
        source = root / "source"
        remote = root / "remote.git"
        observer = root / "observer"
        init_profile(source, core_commit="a" * 40)
        run("git", "init", "-q", "-b", "main", cwd=source)
        run("git", "config", "user.name", "test", cwd=source)
        run("git", "config", "user.email", "test@example.com", cwd=source)
        run("git", "add", "-A", cwd=source)
        run("git", "commit", "-q", "-m", "profile", cwd=source)
        run("git", "init", "--bare", "--initial-branch=main", str(remote))
        run("git", "remote", "add", "origin", str(remote), cwd=source)
        run("git", "push", "-q", "-u", "origin", "main", cwd=source)

        candidate = source / "host_staging" / "cycle-invalid.json"
        candidate.write_text("PLACEHOLDER", encoding="utf-8")
        promoted, refusals = process_staging(
            source / "host_staging",
            source / "host_input",
            records=AuditJournal(
                next((source / "audit").glob("*.jsonl"))
            ).read(),
        )
        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        run("git", "add", "-A", cwd=source)
        run("git", "commit", "-q", "-m", "refusal", cwd=source)
        run("git", "push", "-q", cwd=source)
        run("git", "clone", "-q", str(remote), str(observer))

        feedback = json.loads(
            (observer / "host_staging" / "FEEDBACK.json").read_text()
        )
        self.assertEqual(
            feedback["refused"][0]["input"],
            "cycle-invalid.json",
        )
        self.assertTrue(feedback["refused"][0]["what_to_fix"])

    def test_mixed_batch_executes_the_valid_candidate(self):
        root = Path(tempfile.mkdtemp(prefix="profile-mixed-batch-"))
        self.addCleanup(shutil.rmtree, root)
        init_profile(root, core_commit="a" * 40)
        valid = root / "host_staging" / "cycle-valid.json"
        valid.write_text(
            json.dumps(sample_input(cycle_id="cycle-valid")),
            encoding="utf-8",
        )
        (root / "host_staging" / "cycle-invalid.json").write_text(
            "PLACEHOLDER",
            encoding="utf-8",
        )
        journal = AuditJournal(next((root / "audit").glob("*.jsonl")))
        promoted, refusals = process_staging(
            root / "host_staging",
            root / "host_input",
            records=journal.read(),
        )
        self.assertEqual(promoted, ["cycle-valid.json"])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(
            run_host_cycles([
                "--input-dir",
                str(root / "host_input"),
                "--journal",
                str(journal.path),
            ]),
            0,
        )
        self.assertTrue(any(
            record.get("record_id") == "cycle-receipt:cycle-valid"
            for record in journal.read()
        ))

    def test_remote_installer_requires_private_repo_and_workflow_scope(self):
        text = REMOTE_INSTALLER.read_text(encoding="utf-8")
        self.assertIn("target profile repository must be private", text)
        self.assertIn("gh auth refresh -h github.com -s workflow", text)
        self.assertIn("repair_profile", text)
        self.assertIn("profile main moved during install", text)


if __name__ == "__main__":
    unittest.main()
