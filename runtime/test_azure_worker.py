import json
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
        self.feedback = self.root / "FEEDBACK.json"
        self.outbox = self.root / "outbox"

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
            "REPLACE_WITH_SUBSCRIPTION",
            "REPLACE_WITH_ENDPOINT",
            "REPLACE_WITH_DEPLOYMENT",
            "REPLACE_WITH_MINUTE",
            "REPLACE_WITH_AUTH_MODE",
            "REPLACE_WITH_RESOURCE_GROUP",
            "REPLACE_WITH_ACCOUNT_NAME",
        ):
            self.assertIn(placeholder, text)
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("SOVEREIGN_AZURE_A_MINUTE:-20", installer)
        self.assertIn("SOVEREIGN_AZURE_B_MINUTE:-40", installer)
        self.assertIn('"azure-a"', installer)
        self.assertIn('"azure-b"', installer)
        self.assertIn("publish_research_inbox.py", RUNNER.read_text())


if __name__ == "__main__":
    unittest.main()
