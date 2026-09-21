import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ops.azure_worker import build_request, run_worker, select_target
from runtime.research_inbox import (
    load_inbox_record,
    research_inbox_summary,
)
from runtime.worker_role_contracts import (
    ROLE_OUTPUT_CONTRACT_VERSION,
    ROLE_SPECIFIC_FIELDS,
    role_result_digest,
    role_result_schema,
    role_result_validation_errors,
)


ROLE_RESULTS = {
    "primary_frame": {
        "summary": "Frame the decision.",
        "hypotheses": ["Demand remains durable."],
        "evidence_needed": ["Primary filing evidence."],
        "counterevidence": ["Margins may compress."],
        "uncertainties": ["Timing remains uncertain."],
        "suggested_next_question": "What evidence changes conviction?",
    },
    "evidence_map": {
        "summary": "Map the evidence.",
        "claims_to_verify": ["Demand remains durable."],
        "primary_sources": ["Company filing."],
        "evidence_gaps": ["No FY27 capex disclosure."],
        "conflict_checks": ["Reconcile guidance and cash flow."],
        "uncertainties": ["Disclosure timing."],
        "suggested_next_question": "Which filing resolves the gap?",
    },
    "adversarial_challenge": {
        "summary": "Challenge the leading thesis.",
        "challenged_claims": ["Demand remains durable."],
        "disconfirming_evidence_needed": ["Customer concentration trend."],
        "failure_modes": ["Capex outruns operating cash flow."],
        "alternative_explanations": ["Revenue growth is pull-forward."],
        "uncertainties": ["Customer mix is incomplete."],
        "suggested_next_question": "What would falsify durability?",
    },
    "independent_synthesis": {
        "summary": "Synthesize independently.",
        "agreements": ["Demand evidence is incomplete."],
        "disagreements": ["Valuation impact remains disputed."],
        "independent_conclusion": "Wait for primary evidence.",
        "arbitration_questions": ["Which cash-flow datapoint resolves this?"],
        "uncertainties": ["Timing remains uncertain."],
        "suggested_next_question": "What should be arbitrated first?",
    },
}


def feedback():
    return {
        "opportunity_ledger": {
            "not_shown": 0,
            "items": [{
                "opportunity_id": "opportunity-a",
                "identity_fingerprint": "fingerprint-a",
                "state": "researching",
                "research_state": {
                    "missing_information": [{
                        "id": "question-a",
                        "question": "What changes the decision?",
                        "status": "open",
                        "why_it_matters": "It changes conviction.",
                    }],
                },
            }],
        },
    }


class WorkerRoleContractTests(unittest.TestCase):
    def test_each_role_has_exact_strict_schema(self):
        for role, result in ROLE_RESULTS.items():
            schema = role_result_schema(role)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(result))
            self.assertEqual(
                set(schema["properties"]),
                set(result),
            )

    def test_cross_role_output_is_rejected(self):
        evidence_map = ROLE_RESULTS["evidence_map"]
        self.assertEqual(
            role_result_validation_errors(
                evidence_map,
                role="evidence_map",
            ),
            [],
        )
        self.assertIn(
            "fields",
            role_result_validation_errors(
                evidence_map,
                role="adversarial_challenge",
            ),
        )

    def test_request_uses_role_specific_schema(self):
        target = select_target(feedback())
        assert target is not None
        schemas = {
            role: build_request(target, role=role)["text"]["format"]
            for role in ROLE_RESULTS
        }
        for role, result in ROLE_RESULTS.items():
            self.assertEqual(
                schemas[role]["name"],
                f"sovereign_research_{role}",
            )
            self.assertEqual(
                set(schemas[role]["schema"]["required"]),
                set(result),
            )
        self.assertNotEqual(
            schemas["evidence_map"]["schema"],
            schemas["adversarial_challenge"]["schema"],
        )

    def test_role_digest_preserves_role_and_normalizes_common_fields(self):
        digest = role_result_digest(
            ROLE_RESULTS["adversarial_challenge"],
            role="adversarial_challenge",
        )
        assert digest is not None
        self.assertEqual(digest["role"], "adversarial_challenge")
        self.assertEqual(
            digest["hypotheses"],
            ["Demand remains durable."],
        )
        self.assertEqual(
            digest["evidence_needed"],
            ["Customer concentration trend."],
        )
        self.assertEqual(
            set(digest["role_output"]),
            ROLE_SPECIFIC_FIELDS["adversarial_challenge"],
        )

    def test_worker_persists_role_contract_and_projects_digest(self):
        root = Path(tempfile.mkdtemp(prefix="worker-role-"))
        feedback_path = root / "FEEDBACK.json"
        feedback_path.write_text(json.dumps(feedback()), encoding="utf-8")
        outbox = root / "research_inbox" / "azure-a-gpt5-mini"

        def caller(**kwargs):
            self.assertEqual(kwargs["role"], "evidence_map")
            return ROLE_RESULTS["evidence_map"], {
                "id": "response-1",
                "status": "completed",
                "model": "gpt-test",
            }

        path = run_worker(
            feedback_path=feedback_path,
            outbox_dir=outbox,
            worker_id="azure-a-gpt5-mini",
            endpoint="https://example.openai.azure.com",
            deployment="gpt-test",
            subscription_id="sub-test",
            role="evidence_map",
            now=datetime(2026, 9, 21, 10, tzinfo=timezone.utc),
            caller=caller,
        )
        record = load_inbox_record(path)
        self.assertEqual(record["request"]["output_contract"], {
            "schema_version": ROLE_OUTPUT_CONTRACT_VERSION,
            "role": "evidence_map",
        })
        summary = research_inbox_summary(
            root,
            now=datetime(2026, 9, 21, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["incomplete_result_count"], 0)
        self.assertEqual(summary["items"][0]["result"]["role"], "evidence_map")
        self.assertEqual(
            summary["items"][0]["result"]["role_output"]["evidence_gaps"],
            ["No FY27 capex disclosure."],
        )

    def test_wrong_role_shape_becomes_model_error(self):
        root = Path(tempfile.mkdtemp(prefix="worker-role-bad-"))
        feedback_path = root / "FEEDBACK.json"
        feedback_path.write_text(json.dumps(feedback()), encoding="utf-8")

        def caller(**_kwargs):
            return ROLE_RESULTS["primary_frame"], {
                "id": "response-1",
                "status": "completed",
                "model": "gpt-test",
            }

        path = run_worker(
            feedback_path=feedback_path,
            outbox_dir=root / "outbox",
            worker_id="azure-a-gpt5-mini",
            endpoint="https://example.openai.azure.com",
            deployment="gpt-test",
            subscription_id="sub-test",
            role="evidence_map",
            now=datetime(2026, 9, 21, 10, tzinfo=timezone.utc),
            caller=caller,
        )
        record = load_inbox_record(path)
        self.assertEqual(record["status"], "model_error")
        self.assertEqual(
            record["error"]["detail_code"],
            "invalid_structured_output",
        )
        self.assertEqual(record["quality"]["attempt_count"], 2)
