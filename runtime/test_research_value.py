import unittest

from .audit_store import AuditJournal
from .research_value import (
    persist_adversarial_disputes,
    research_value_census,
    validate_adversarial_disputes,
)
from .test_research_allocation import valid_input


def dispute():
    return {
        "dispute_id": "dispute-one",
        "opportunity_id": None,
        "emerging_position": "Current evidence supports continued research.",
        "adversarial_position": "The evidence may be theme-level repetition.",
        "disputed_claims": [{
            "claim_id": "claim-one",
            "emerging_claim": "The source adds incremental evidence.",
            "adversarial_claim": "The source repeats known evidence.",
        }],
        "governance_resolution": (
            "Keep researching but do not advance the opportunity state."
        ),
        "final_decision_changed": True,
        "evidence": [
            "stage:adversarial",
            "stage:governance_review",
        ],
    }


class AdversarialDisputeTests(unittest.TestCase):
    def test_valid_dispute_preserves_both_positions(self):
        data = valid_input()
        data["adversarial_disputes"] = [dispute()]
        self.assertEqual(
            validate_adversarial_disputes(
                data["adversarial_disputes"],
                data=data,
                records=[],
            ),
            [],
        )

    def test_unknown_opportunity_is_refused(self):
        data = valid_input()
        row = dispute()
        row["opportunity_id"] = "missing"
        self.assertIn(
            "adversarial_dispute_opportunity_invalid:0",
            validate_adversarial_disputes(
                [row], data=data, records=[]),
        )

    def test_persistence_is_idempotent_and_causal(self):
        data = valid_input()
        data["cycle_id"] = "cycle-dispute"
        data["adversarial_disputes"] = [dispute()]
        journal = AuditJournal("/tmp/test-research-value-dispute.jsonl")
        if journal.path.exists():
            journal.path.unlink()
        for stage in ("adversarial", "governance_review", "decision"):
            journal.append(
                record_id=f"cycle-stage:cycle-dispute:{stage}",
                record_type="cycle_stage",
                agent="test",
                payload={"cycle_id": "cycle-dispute", "agent_id": stage},
            )
        journal.append(
            record_id="cycle-receipt:cycle-dispute",
            record_type="cycle_receipt",
            agent="test",
            payload={"cycle_id": "cycle-dispute"},
        )
        self.assertEqual(
            persist_adversarial_disputes(
                data, journal, {"cycle_id": "cycle-dispute"}),
            1,
        )
        self.assertEqual(
            persist_adversarial_disputes(
                data, journal, {"cycle_id": "cycle-dispute"}),
            0,
        )
        record = journal.read()[-1]
        self.assertEqual(
            record["payload"]["sampling_independence"],
            "single_host_role_execution",
        )
        self.assertTrue(journal.validate()["valid"])
        journal.path.unlink()


class ResearchValueCensusTests(unittest.TestCase):
    def test_census_keeps_dimensions_separate(self):
        records = [
            {
                "record_type": "cycle_receipt",
                "payload": {"decision_status": "researching"},
            },
            {
                "record_type": "cycle_receipt",
                "payload": {"decision_status": "wait"},
            },
            {
                "record_type": "tool_provenance",
                "payload": {"calls": [
                    {
                        "tool": "IBKR",
                        "result_origin": "connector_response",
                        "result_sha256": "a" * 64,
                    },
                    {
                        "tool": "IBKR",
                        "result_origin": "connector_response",
                        "result_sha256": "a" * 64,
                    },
                ]},
            },
        ]
        census = research_value_census(records)
        self.assertEqual(
            census["result_novelty"]["repeated_result_observations"], 1)
        self.assertEqual(
            census["decision_statuses"]["consecutive_changes"], 1)
        self.assertEqual(
            census["dimensions"]["valuation_attractiveness"]["status"],
            "not_structured",
        )
        self.assertNotIn("score", str(census).lower())

    def test_dispute_rate_is_descriptive(self):
        records = [{
            "record_type": "adversarial_dispute",
            "payload": {
                "dispute_id": "d1",
                "final_decision_changed": True,
                "sampling_independence": "single_host_role_execution",
            },
        }]
        census = research_value_census(records)
        self.assertEqual(
            census["adversarial_disputes"]["decision_change_rate"], 1.0)
        self.assertEqual(
            census["adversarial_disputes"]["sampling_independence"],
            "single_host_role_execution",
        )


if __name__ == "__main__":
    unittest.main()
