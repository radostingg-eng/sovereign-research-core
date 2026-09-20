import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.publish_research_inbox import (
    publish_outbox,
)
from runtime.research_inbox import validate_inbox_record


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def worker_record():
    return {
        "schema_version": 1,
        "record_id": "azure-a-20260919t220000z",
        "worker_id": "azure-a",
        "status": "completed",
        "origin": "worker_attested",
        "observed_at": "2026-09-19T22:00:00Z",
        "expires_at": "2026-09-20T01:00:00Z",
        "deployment": {
            "endpoint": "https://example.openai.azure.com",
            "deployment": "gpt-test",
            "subscription_id": "sub-test",
        },
        "selection": {
            "rule": "stable",
            "candidate_count": 1,
            "projection_complete": True,
        },
        "target": {
            "opportunity_id": "opportunity-one",
            "identity_fingerprint": "identity-one",
            "question_id": "question-one",
            "question": "What would change the decision?",
        },
        "request": {"sha256": "a" * 64},
        "result": {"summary": "Bounded worker result."},
    }


class ResearchInboxPublisherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="inbox-publisher-"))
        self.remote = self.root / "remote.git"
        self.profile = self.root / "profile"
        self.outbox = self.root / "outbox"
        self.outbox.mkdir()
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
            ["git", "clone", str(self.remote), str(self.profile)],
            check=True,
            capture_output=True,
        )
        git(self.profile, "config", "user.name", "test")
        git(self.profile, "config", "user.email", "test@example.com")
        (self.profile / "README.md").write_text("profile\n")
        git(self.profile, "add", "README.md")
        git(self.profile, "commit", "-m", "initial")
        git(self.profile, "push", "-u", "origin", "main")

    def test_publisher_commits_pushes_and_clears_outbox(self):
        source = self.outbox / "record.json"
        source.write_text(
            json.dumps(worker_record(), indent=2) + "\n",
            encoding="utf-8",
        )

        published = publish_outbox(
            profile_root=self.profile,
            outbox_dir=self.outbox,
        )

        self.assertEqual(
            published,
            ["research_inbox/azure-a/record.json"],
        )
        self.assertFalse(source.exists())
        self.assertEqual(git(self.profile, "status", "--porcelain").stdout, "")
        tree = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "ls-tree",
                "-r",
                "--name-only",
                "main",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("research_inbox/azure-a/record.json", tree)

    def test_origin_must_be_worker_attested(self):
        value = worker_record()
        value["origin"] = "direct_connector_response"

        self.assertEqual(
            validate_inbox_record(value, byte_length=100),
            ["research_inbox_origin"],
        )

    def test_schedule_watchdog_lock_does_not_jam_pending_ledger(self):
        ledger = self.profile / "runs" / "SCHEDULE_EVENTS.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"record_id":"seed"}\n', encoding="utf-8")
        git(self.profile, "add", "runs/SCHEDULE_EVENTS.jsonl")
        git(self.profile, "commit", "-m", "seed ledger")
        git(self.profile, "push", "origin", "main")
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write('{"record_id":"watchdog-heartbeat-1"}\n')
        ledger.with_name(ledger.name + ".lock").touch()
        source = self.outbox / "record.json"
        source.write_text(json.dumps(worker_record()), encoding="utf-8")

        published = publish_outbox(
            profile_root=self.profile,
            outbox_dir=self.outbox,
        )

        self.assertEqual(
            published,
            ["research_inbox/azure-a/record.json"],
        )
        self.assertEqual(
            git(self.profile, "status", "--porcelain").stdout,
            "?? runs/SCHEDULE_EVENTS.jsonl.lock\n",
        )
        ledger_on_main = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "show",
                "main:runs/SCHEDULE_EVENTS.jsonl",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("watchdog-heartbeat-1", ledger_on_main)

    def test_schedule_contract_change_still_blocks_publish(self):
        contract = self.profile / "runs" / "SCHEDULE.json"
        contract.parent.mkdir(parents=True, exist_ok=True)
        contract.write_text('{"cadence_minutes":60}\n', encoding="utf-8")
        git(self.profile, "add", "runs/SCHEDULE.json")
        git(self.profile, "commit", "-m", "seed schedule contract")
        git(self.profile, "push", "origin", "main")
        contract.write_text('{"cadence_minutes":30}\n', encoding="utf-8")
        source = self.outbox / "record.json"
        source.write_text(json.dumps(worker_record()), encoding="utf-8")

        with self.assertRaisesRegex(
            RuntimeError,
            "research_inbox_profile_dirty:runs/SCHEDULE.json",
        ):
            publish_outbox(
                profile_root=self.profile,
                outbox_dir=self.outbox,
            )

        self.assertTrue(source.exists())

    def test_unrelated_dirty_profile_is_not_modified(self):
        source = self.outbox / "record.json"
        source.write_text(json.dumps(worker_record()), encoding="utf-8")
        (self.profile / "README.md").write_text("developer change\n")

        with self.assertRaisesRegex(
            RuntimeError,
            "research_inbox_profile_dirty:README.md",
        ):
            publish_outbox(
                profile_root=self.profile,
                outbox_dir=self.outbox,
            )

        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
