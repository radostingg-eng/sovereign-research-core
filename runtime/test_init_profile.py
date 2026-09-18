"""A new operator can reach a working profile from nothing."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .audit_store import AuditJournal
from .init_profile import (
    CORE_REPO,
    PROFILE_WORKFLOW_VERSION,
    PROFILE_DIRECTORIES,
    existing_journal,
    init_profile,
    main,
    repair_profile,
)
from .profile_health import check_profile


class AFreshProfileIsUsableTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sovereign-init-"))

    def test_every_directory_a_cycle_writes_to_exists(self):
        init_profile(self.root)
        for relative in PROFILE_DIRECTORIES:
            with self.subTest(relative=relative):
                self.assertTrue((self.root / relative).is_dir(), relative)

    def test_the_journal_starts_a_chain_rather_than_being_empty(self):
        init_profile(self.root, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
        journal = existing_journal(self.root)
        self.assertIsNotNone(journal)
        record = json.loads(journal.read_text().strip())
        self.assertEqual(record["record_type"], "profile_genesis")
        self.assertIsNone(record["prev_hash"])
        self.assertEqual(record["agent"], "profile-initializer")
        self.assertEqual(
            record["created_at"],
            "2026-01-02T00:00:00+00:00",
        )
        self.assertTrue(record["record_hash"])
        self.assertTrue(AuditJournal(journal).validate()["valid"])

    def test_static_github_genesis_template_is_a_valid_chain(self):
        template = (
            Path(__file__).resolve().parent.parent
            / "profile_templates"
            / "audit"
            / "genesis.jsonl"
        )
        result = AuditJournal(template).validate()
        self.assertEqual(result["records"], 1)
        self.assertTrue(result["valid"], result)

    def test_core_lock_pins_the_commit_it_was_given(self):
        init_profile(self.root, core_commit="b" * 40)
        lock = json.loads((self.root / "core.lock").read_text())
        self.assertEqual(lock["commit"], "b" * 40)
        self.assertEqual(lock["core_repo"], CORE_REPO)
        self.assertEqual(
            lock["profile_workflow_version"],
            PROFILE_WORKFLOW_VERSION,
        )

    def test_an_unpinned_profile_says_so_rather_than_guessing(self):
        init_profile(self.root)
        lock = json.loads((self.root / "core.lock").read_text())
        self.assertEqual(lock["commit"], "UNPINNED")

    def test_preferences_are_a_template_not_a_position(self):
        init_profile(self.root)
        text = (self.root / "OPERATOR_PREFERENCES.md").read_text()
        self.assertIn("No explicit operator preferences recorded yet", text)
        self.assertIn("does not block setup or research", text)
        self.assertIn("without turning setup", text)
        # A starting portfolio would be someone else's posture.
        for leak in ("MSFT", "WHR", "$"):
            self.assertNotIn(leak, text)

    def test_feedback_is_versioned_and_only_caches_are_ignored(self):
        init_profile(self.root)
        ignored = (self.root / ".gitignore").read_text()
        self.assertIn("var/", ignored)
        self.assertIn(".core/", ignored)
        self.assertNotIn("FEEDBACK.json", ignored)

    def test_admission_policy_and_initial_feedback_are_installed(self):
        init_profile(self.root, core_commit="a" * 40)
        policy = json.loads(
            (
                self.root / "host_input" / ".promotion_policy.json"
            ).read_text()
        )
        self.assertEqual(
            policy,
            {"schema_version": 1, "legacy_files": []},
        )
        for relative in (
            "host_input/FEEDBACK.json",
            "host_staging/FEEDBACK.json",
        ):
            value = json.loads((self.root / relative).read_text())
            self.assertIsInstance(value, dict)
        self.assertEqual(check_profile(self.root), [])

    def test_two_fresh_profiles_have_unique_genesis_namespaces(self):
        other = Path(tempfile.mkdtemp(prefix="sovereign-init-other-"))
        init_profile(
            self.root,
            core_commit="a" * 40,
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        init_profile(
            other,
            core_commit="a" * 40,
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        first = json.loads(existing_journal(self.root).read_text())
        second = json.loads(existing_journal(other).read_text())
        self.assertNotEqual(
            first["payload"]["profile_id"],
            second["payload"]["profile_id"],
        )
        self.assertNotEqual(first["record_hash"], second["record_hash"])

    def test_actions_bootstrap_does_not_rewrite_preinstalled_workflows(self):
        workflow = self.root / ".github" / "workflows" / "host-cycle.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("preinstalled\n", encoding="utf-8")
        init_profile(
            self.root,
            core_commit="a" * 40,
            install_workflow=False,
        )
        self.assertEqual(workflow.read_text(), "preinstalled\n")

    def test_profile_cycle_workflow_is_installed(self):
        init_profile(self.root)
        workflow = self.root / ".github" / "workflows" / "host-cycle.yml"
        self.assertTrue(workflow.is_file())
        self.assertEqual(
            workflow.read_bytes(),
            (
                Path(__file__).resolve().parent.parent
                / "profile_templates"
                / ".github"
                / "workflows"
                / "host-cycle.yml"
            ).read_bytes(),
        )

    def test_empty_archive_has_a_valid_manifest(self):
        init_profile(self.root)
        from .archive_integrity import verify_archive

        self.assertEqual(
            verify_archive(self.root / "audit_archive"),
            [],
        )


class ExistingStateIsNeverOverwrittenTests(unittest.TestCase):
    """The one unrecoverable mistake here is replacing real history."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sovereign-init-"))

    def test_re_initialising_a_real_profile_is_refused(self):
        init_profile(self.root)
        with self.assertRaises(FileExistsError):
            init_profile(self.root)

    def test_the_refusal_names_the_journal_it_protected(self):
        init_profile(self.root)
        with self.assertRaises(FileExistsError) as caught:
            init_profile(self.root)
        self.assertIn("genesis", str(caught.exception))

    def test_an_existing_journal_is_left_byte_identical(self):
        init_profile(self.root)
        journal = existing_journal(self.root)
        before = journal.read_bytes()
        with self.assertRaises(FileExistsError):
            init_profile(self.root)
        self.assertEqual(journal.read_bytes(), before)

    def test_the_cli_reports_refusal_without_raising(self):
        init_profile(self.root)
        self.assertEqual(main([str(self.root)]), 1)

    def test_repair_installs_controls_without_rewriting_operator_state(self):
        init_profile(self.root, core_commit="a" * 40)
        protected = {
            "audit": existing_journal(self.root),
            "preferences": self.root / "OPERATOR_PREFERENCES.md",
            "state": self.root / "STATE.json",
            "parameters": self.root / "PARAMETERS.json",
            "lock": self.root / "core.lock",
        }
        protected["preferences"].write_text(
            "private preference\n",
            encoding="utf-8",
        )
        protected["state"].write_text(
            '{"schema_version":1,"private":"state"}\n',
            encoding="utf-8",
        )
        protected["parameters"].write_text(
            '{"schema_version":1,"private":"parameter"}\n',
            encoding="utf-8",
        )
        before = {
            name: path.read_bytes()
            for name, path in protected.items()
        }
        (self.root / ".github" / "workflows" / "host-cycle.yml").unlink()
        (self.root / "host_input" / ".promotion_policy.json").unlink()
        (self.root / "host_input" / "FEEDBACK.json").unlink()
        (self.root / "host_staging" / "FEEDBACK.json").unlink()

        changed = repair_profile(
            self.root,
            core_commit="b" * 40,
            upgrade_core=False,
        )

        self.assertIn(".github/workflows/host-cycle.yml", changed)
        self.assertIn("host_input/.promotion_policy.json", changed)
        self.assertEqual(
            {
                name: path.read_bytes()
                for name, path in protected.items()
            },
            before,
        )
        self.assertEqual(check_profile(self.root), [])

    def test_core_pin_changes_only_during_explicit_upgrade(self):
        init_profile(self.root, core_commit="a" * 40)
        repair_profile(
            self.root,
            core_commit="b" * 40,
            upgrade_core=False,
        )
        self.assertEqual(
            json.loads((self.root / "core.lock").read_text())["commit"],
            "a" * 40,
        )
        repair_profile(
            self.root,
            core_commit="b" * 40,
            upgrade_core=True,
        )
        self.assertEqual(
            json.loads((self.root / "core.lock").read_text())["commit"],
            "b" * 40,
        )

    def test_invalid_existing_journal_is_never_rebuilt(self):
        init_profile(self.root, core_commit="a" * 40)
        journal = existing_journal(self.root)
        original = journal.read_text()
        value = json.loads(original)
        value["record_hash"] = "0" * 64
        journal.write_text(json.dumps(value) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "profile_journal_invalid"):
            repair_profile(self.root, core_commit="b" * 40)
        self.assertEqual(journal.read_text(), json.dumps(value) + "\n")

    def test_corrupt_policy_is_not_silently_replaced(self):
        init_profile(self.root, core_commit="a" * 40)
        policy = self.root / "host_input" / ".promotion_policy.json"
        policy.write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            repair_profile(self.root, core_commit="b" * 40)
        self.assertEqual(policy.read_text(), "{broken")


if __name__ == "__main__":
    unittest.main()
