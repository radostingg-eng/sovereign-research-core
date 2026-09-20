import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ops.azure_worker import (
    build_request,
    run_worker,
    select_target,
)
from runtime.research_inbox import load_inbox_record

ROOT = Path(__file__).resolve().parent.parent
PLIST = ROOT / "ops" / "com.sovereign.azureworker.plist"
INSTALLER = ROOT / "ops" / "install_azure_workers.sh"
RUNNER = ROOT / "ops" / "run_azure_worker.sh"


def feedback(*, not_shown=0):
    return {
        "opportunity_ledger": {
            "not_shown": not_shown,
            "items": [{
                "opportunity_id": "opportunity-b",
                "identity_fingerprint": "fingerprint-b",
                "state": "researching",
                "research_state": {
                    "missing_information": [{
                        "id": "question-b",
                        "question": "Question B?",
                        "status": "open",
                        "why_it_matters": "It changes valuation.",
                    }],
                },
            }, {
                "opportunity_id": "opportunity-a",
                "identity_fingerprint": "fingerprint-a",
                "state": "new",
                "research_state": {
                    "missing_information": [{
                        "id": "question-a",
                        "question": "Question A?",
                        "status": "open",
                        "why_it_matters": "It changes the thesis.",
                    }],
                },
            }],
        },
    }


class AzureWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="azure-worker-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.feedback = self.root / "FEEDBACK.json"
        self.outbox = self.root / "outbox"

    def installer_env(self, name, **overrides):
        root = self.root / name
        fake_bin = root / "bin"
        fake_bin.mkdir(parents=True)
        launchctl_log = root / "launchctl.log"
        launchctl = fake_bin / "launchctl"
        launchctl.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SOVEREIGN_TEST_LAUNCHCTL_LOG"\n',
            encoding="utf-8",
        )
        launchctl.chmod(0o755)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("SOVEREIGN_AZURE_")
        }
        env.update({
            "HOME": str(root / "home"),
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "PYTHON_BIN": sys.executable,
            "SOVEREIGN_AZURE_CLI_BIN": "/usr/bin/true",
            "SOVEREIGN_EXECUTOR_REPO": str(root / "profile"),
            "SOVEREIGN_TEST_LAUNCHCTL_LOG": str(launchctl_log),
            "SOVEREIGN_AZURE_A_SUBSCRIPTION": "sub-a",
            "SOVEREIGN_AZURE_A_ENDPOINT": "https://a.example.test",
            "SOVEREIGN_AZURE_A_DEPLOYMENT": "model-a",
        })
        env.update(overrides)
        return env, root / "home" / "Library" / "LaunchAgents", launchctl_log

    def run_installer(self, env):
        return subprocess.run(
            [str(INSTALLER), str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_selection_is_stable_and_not_a_rank(self):
        selected = select_target(feedback())

        self.assertEqual(selected["question_id"], "question-a")
        self.assertEqual(selected["candidate_count"], 2)
        self.assertIn("lexicographically", selected["selection_rule"])

    def test_incomplete_bounded_projection_produces_no_target(self):
        self.assertIsNone(select_target(feedback(not_shown=1)))

    def test_request_contains_no_account_state(self):
        request = build_request(select_target(feedback()))
        encoded = json.dumps(request).casefold()

        for forbidden in (
            "net_liquidation_value",
            "\"cash\"",
            "\"positions\"",
            "\"quantity\"",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_completed_record_is_worker_attested(self):
        self.feedback.write_text(json.dumps(feedback()), encoding="utf-8")

        def caller(**_kwargs):
            return (
                {
                    "summary": "A bounded research frame.",
                    "hypotheses": [],
                    "evidence_needed": ["Fresh filings"],
                    "counterevidence": [],
                    "uncertainties": [],
                    "suggested_next_question": "What do filings show?",
                },
                {
                    "id": "response-1",
                    "model": "gpt-test",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            )

        path = run_worker(
            feedback_path=self.feedback,
            outbox_dir=self.outbox,
            worker_id="azure-a",
            endpoint="https://example.openai.azure.com",
            deployment="gpt-test",
            subscription_id="sub-test",
            now=datetime(2026, 9, 19, 22, tzinfo=timezone.utc),
            caller=caller,
        )
        value = load_inbox_record(path)

        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["origin"], "worker_attested")
        self.assertEqual(value["target"]["question_id"], "question-a")

    def test_auth_failure_writes_marker_and_does_not_raise(self):
        self.feedback.write_text(json.dumps(feedback()), encoding="utf-8")

        def caller(**_kwargs):
            raise PermissionError("azure_token_failed:test")

        path = run_worker(
            feedback_path=self.feedback,
            outbox_dir=self.outbox,
            worker_id="azure-a",
            endpoint="https://example.openai.azure.com",
            deployment="gpt-test",
            subscription_id="sub-test",
            now=datetime(2026, 9, 19, 22, tzinfo=timezone.utc),
            caller=caller,
        )
        value = load_inbox_record(path)

        self.assertEqual(value["status"], "auth_error")
        self.assertEqual(value["origin"], "worker_attested")

    def test_worker_has_no_broker_runtime_dependency(self):
        text = (ROOT / "ops" / "azure_worker.py").read_text().casefold()

        self.assertNotIn("interactive brokers", text)
        self.assertNotIn("ibkr", text)
        self.assertNotIn("order_instruction", text)

    def test_plist_supports_independent_worker_configs(self):
        text = PLIST.read_text(encoding="utf-8")

        for placeholder in (
            "REPLACE_WITH_WORKER_ID",
            "REPLACE_WITH_AZURE_CLI",
            "REPLACE_WITH_SUBSCRIPTION",
            "REPLACE_WITH_ENDPOINT",
            "REPLACE_WITH_DEPLOYMENT",
            "REPLACE_WITH_MINUTE",
            "REPLACE_WITH_AUTH_MODE",
            "REPLACE_WITH_RESOURCE_GROUP",
            "REPLACE_WITH_ACCOUNT_NAME",
            "REPLACE_WITH_KEY_VAULT_NAME",
            "REPLACE_WITH_KEY_SECRET_NAME",
            "REPLACE_WITH_KEY_VAULT_SUBSCRIPTION",
        ):
            self.assertIn(placeholder, text)
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("SOVEREIGN_AZURE_A_MINUTE:-20", installer)
        self.assertIn("SOVEREIGN_AZURE_B_MINUTE:-40", installer)
        self.assertIn('"azure-a"', installer)
        self.assertIn('"azure-b"', installer)
        self.assertIn("publish_research_inbox.py", RUNNER.read_text())

    def test_missing_auth_mode_fails_before_launchd_install(self):
        env, launch_agents, launchctl_log = self.installer_env("missing-a")

        result = self.run_installer(env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SOVEREIGN_AZURE_A_AUTH_MODE", result.stderr)
        self.assertFalse(launch_agents.exists())
        self.assertFalse(launchctl_log.exists())

    def test_configured_b_requires_auth_mode_before_any_install(self):
        env, launch_agents, launchctl_log = self.installer_env(
            "missing-b",
            SOVEREIGN_AZURE_A_AUTH_MODE="entra",
            SOVEREIGN_AZURE_B_SUBSCRIPTION="sub-b",
            SOVEREIGN_AZURE_B_ENDPOINT="https://b.example.test",
            SOVEREIGN_AZURE_B_DEPLOYMENT="model-b",
        )

        result = self.run_installer(env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SOVEREIGN_AZURE_B_AUTH_MODE", result.stderr)
        self.assertFalse(launch_agents.exists())
        self.assertFalse(launchctl_log.exists())

    def test_explicit_supported_auth_modes_render(self):
        for mode in ("entra", "azure_cli_key", "key_vault"):
            with self.subTest(mode=mode):
                env, launch_agents, launchctl_log = self.installer_env(
                    f"mode-{mode}",
                    SOVEREIGN_AZURE_A_AUTH_MODE=mode,
                    SOVEREIGN_AZURE_A_RESOURCE_GROUP="group-a",
                    SOVEREIGN_AZURE_A_ACCOUNT_NAME="account-a",
                    SOVEREIGN_AZURE_A_KEY_VAULT_NAME="vault-a",
                    SOVEREIGN_AZURE_A_KEY_SECRET_NAME="secret-a",
                )

                result = self.run_installer(env)

                self.assertEqual(
                    result.returncode,
                    0,
                    result.stdout + result.stderr,
                )
                plist = (
                    launch_agents
                    / "com.sovereign.azureworker.azure.a.plist"
                ).read_text(encoding="utf-8")
                self.assertIn(
                    "<key>SOVEREIGN_AZURE_AUTH_MODE</key>\n"
                    f"    <string>{mode}</string>",
                    plist,
                )
                self.assertIn("bootstrap", launchctl_log.read_text())

    def test_explicit_b_auth_mode_renders_independently(self):
        env, launch_agents, _launchctl_log = self.installer_env(
            "mode-b",
            SOVEREIGN_AZURE_A_AUTH_MODE="azure_cli_key",
            SOVEREIGN_AZURE_B_SUBSCRIPTION="sub-b",
            SOVEREIGN_AZURE_B_ENDPOINT="https://b.example.test",
            SOVEREIGN_AZURE_B_DEPLOYMENT="model-b",
            SOVEREIGN_AZURE_B_AUTH_MODE="key_vault",
        )

        result = self.run_installer(env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        azure_a = (
            launch_agents / "com.sovereign.azureworker.azure.a.plist"
        ).read_text(encoding="utf-8")
        azure_b = (
            launch_agents / "com.sovereign.azureworker.azure.b.plist"
        ).read_text(encoding="utf-8")
        self.assertIn("<string>azure_cli_key</string>", azure_a)
        self.assertIn("<string>key_vault</string>", azure_b)


if __name__ == "__main__":
    unittest.main()
