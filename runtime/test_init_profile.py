"""A new operator can reach a working profile from nothing."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .audit_store import AuditJournal
from .init_profile import (
    CORE_REPO,
    PROFILE_DIRECTORIES,
    existing_journal,
    init_profile,
    main,
)


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

    def test_derived_files_are_ignored_by_git(self):
        init_profile(self.root)
        ignored = (self.root / ".gitignore").read_text()
        self.assertIn("FEEDBACK.json", ignored)
        self.assertIn("var/", ignored)

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


if __name__ == "__main__":
    unittest.main()
