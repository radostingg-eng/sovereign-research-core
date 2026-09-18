import copy
import unittest

from .research_allocation import (
    derived_research_allocation_coverage,
    derived_research_allocation_usage,
    primary_allocation_category,
    validate_research_allocation,
)
from .run_host_cycle import validate_input
from .strategy_coverage import research_agenda_summary
from .test_learning_dispositions import v3_input
from .test_run_host_cycle import add_market_scout, post_effective_full_cycle


def _dispositions():
    return [
        {
            "stage_id": stage_id,
            "disposition": "no_change",
            "rationale": f"No durable change supported for {stage_id}.",
            "evidence": [f"stage:{stage_id}"],
        }
        for stage_id in (
            "learning_audit",
            "meta_research",
            "self_improvement",
        )
    ]


def valid_input():
    return add_market_scout(post_effective_full_cycle(
        host_input_schema_version=3,
        learning_stage_dispositions=_dispositions(),
    ))


def agenda(data):
    director = next(
        row for row in data["cognitive_stages"]
        if row["stage_id"] == "research_director"
    )
    return director["output"]["research_agenda"]


class ResearchAllocationValidationTests(unittest.TestCase):
    def test_new_staged_v3_requires_allocation_contract(self):
        data = valid_input()
        value = agenda(data)
        del value["allocation_plan"]
        del value["allocation_variance"]
        for candidate in value["candidates"]:
            candidate.pop("portfolio_risk_ref")
            candidate.pop("follow_up_ref")
            candidate.pop("allocation_factors")

        self.assertIn(
            "research_allocation_required",
            validate_input(
                data,
                "new-v3.json",
                require_full_schema=True,
            ),
        )

    def test_historical_v3_without_allocation_remains_replayable(self):
        data = v3_input()
        self.assertEqual(
            validate_research_allocation(data, required=False),
            [],
        )

    def test_canonical_staged_fixture_is_valid(self):
        self.assertEqual(
            validate_input(
                valid_input(),
                "new-v3.json",
                require_full_schema=True,
            ),
            [],
        )

    def test_plan_uses_current_market_session_context(self):
        data = valid_input()
        agenda(data)["allocation_plan"][
            "market_session_context"
        ] = "none_open"
        self.assertIn(
            "research_allocation_plan_invalid:market_session_context",
            validate_research_allocation(data, required=True),
        )

    def test_plan_is_a_ceiling_within_scout_specialist_budget(self):
        data = valid_input()
        agenda(data)["allocation_plan"]["new_opportunity"] = 1
        self.assertIn(
            "research_allocation_plan_invalid:"
            "exceeds_specialist_investigations",
            validate_research_allocation(data, required=True),
        )

    def test_usage_and_coverage_are_derived_from_candidate_links(self):
        data = valid_input()
        candidates = agenda(data)["candidates"]
        selected = candidates[0]
        selected["portfolio_risk_ref"] = None
        selected["opportunity_id"] = "opportunity-existing"
        plan = agenda(data)["allocation_plan"]
        plan["portfolio_risk"] = 0
        plan["existing_opportunity"] = 1

        self.assertEqual(
            primary_allocation_category(selected),
            "existing_opportunity",
        )
        self.assertEqual(
            derived_research_allocation_usage(data),
            {
                "new_opportunity": 0,
                "existing_opportunity": 1,
                "portfolio_risk": 0,
                "follow_up": 0,
            },
        )
        self.assertEqual(
            derived_research_allocation_coverage(data),
            {
                "new_opportunity": 1,
                "existing_opportunity": 1,
                "portfolio_risk": 0,
                "follow_up": 0,
            },
        )
        self.assertEqual(
            validate_research_allocation(data, required=True),
            [],
        )

    def test_primary_category_has_documented_link_precedence(self):
        candidate = {
            "opportunity_id": "opportunity-one",
            "portfolio_risk_ref": "portfolio:msft",
            "follow_up_ref": "instruction:101",
        }
        self.assertEqual(
            primary_allocation_category(candidate),
            "follow_up",
        )
        candidate["follow_up_ref"] = None
        self.assertEqual(
            primary_allocation_category(candidate),
            "portfolio_risk",
        )

    def test_selected_new_work_requires_a_scout_link(self):
        data = valid_input()
        selected = agenda(data)["candidates"][0]
        selected["portfolio_risk_ref"] = None
        selected.pop("scout_candidate_id")
        plan = agenda(data)["allocation_plan"]
        plan["portfolio_risk"] = 0
        plan["new_opportunity"] = 1

        self.assertIn(
            "research_allocation_candidate_invalid:"
            "0:selected_new_requires_scout_candidate_id",
            validate_research_allocation(data, required=True),
        )

    def test_fake_portfolio_risk_reference_cannot_bypass_scout_link(self):
        data = valid_input()
        selected = agenda(data)["candidates"][0]
        selected.pop("scout_candidate_id")
        selected["portfolio_risk_ref"] = "position:not-a-real-position"

        self.assertIn(
            "research_allocation_candidate_invalid:"
            "0:portfolio_risk_ref_unresolved",
            validate_research_allocation(data, required=True),
        )

    def test_follow_up_reference_resolves_to_current_instruction(self):
        data = valid_input()
        data["order_instructions"] = [{"id": "101"}]
        selected = agenda(data)["candidates"][0]
        selected["portfolio_risk_ref"] = None
        selected["follow_up_ref"] = "instruction:101"
        plan = agenda(data)["allocation_plan"]
        plan["portfolio_risk"] = 0
        plan["follow_up"] = 1

        self.assertEqual(
            validate_research_allocation(data, required=True),
            [],
        )

    def test_portfolio_risk_reference_resolves_to_current_position(self):
        data = valid_input()
        data["snapshot"]["positions"] = [{"symbol": "MSFT"}]
        selected = agenda(data)["candidates"][0]
        selected["portfolio_risk_ref"] = "position:MSFT"

        self.assertEqual(
            validate_research_allocation(data, required=True),
            [],
        )

    def test_follow_up_reference_resolves_to_prior_candidate(self):
        data = valid_input()
        selected = agenda(data)["candidates"][0]
        selected["portfolio_risk_ref"] = None
        selected["follow_up_ref"] = "candidate:prior-specialist"
        plan = agenda(data)["allocation_plan"]
        plan["portfolio_risk"] = 0
        plan["follow_up"] = 1
        records = [{
            "record_type": "cycle_stage",
            "payload": {
                "agent_id": "research_director",
                "output": {
                    "research_agenda": {
                        "candidates": [{
                            "candidate_id": "prior-specialist",
                        }],
                    },
                },
            },
        }]

        self.assertEqual(
            validate_research_allocation(
                data,
                required=True,
                records=records,
            ),
            [],
        )

    def test_staged_follow_up_must_be_after_prior_candidate_record(self):
        data = valid_input()
        data["as_of"] = "2026-09-17T20:23:00Z"
        selected = agenda(data)["candidates"][0]
        selected["portfolio_risk_ref"] = None
        selected["follow_up_ref"] = "candidate:prior-specialist"
        plan = agenda(data)["allocation_plan"]
        plan["portfolio_risk"] = 0
        plan["follow_up"] = 1
        records = [{
            "record_type": "cycle_stage",
            "created_at": "2026-09-17T22:10:20Z",
            "payload": {
                "agent_id": "research_director",
                "output": {
                    "research_agenda": {
                        "candidates": [{
                            "candidate_id": "prior-specialist",
                        }],
                    },
                },
            },
        }]

        self.assertIn(
            "research_allocation_candidate_invalid:"
            "0:follow_up_ref_not_prior",
            validate_research_allocation(
                data,
                required=True,
                records=records,
            ),
        )
        self.assertNotIn(
            "research_allocation_candidate_invalid:"
            "0:follow_up_ref_not_prior",
            validate_research_allocation(
                data,
                required=False,
                records=records,
            ),
        )

    def test_staged_follow_up_after_prior_candidate_is_valid(self):
        data = valid_input()
        data["as_of"] = "2026-09-17T22:11:00Z"
        selected = agenda(data)["candidates"][0]
        selected["portfolio_risk_ref"] = None
        selected["follow_up_ref"] = "candidate:prior-specialist"
        plan = agenda(data)["allocation_plan"]
        plan["portfolio_risk"] = 0
        plan["follow_up"] = 1
        records = [{
            "record_type": "cycle_stage",
            "created_at": "2026-09-17T22:10:20Z",
            "payload": {
                "agent_id": "research_director",
                "output": {
                    "research_agenda": {
                        "candidates": [{
                            "candidate_id": "prior-specialist",
                        }],
                    },
                },
            },
        }]

        self.assertEqual(
            validate_research_allocation(
                data,
                required=True,
                records=records,
            ),
            [],
        )

    def test_unknown_follow_up_reference_is_refused(self):
        data = valid_input()
        selected = agenda(data)["candidates"][0]
        selected["portfolio_risk_ref"] = None
        selected["follow_up_ref"] = "instruction:missing"

        self.assertIn(
            "research_allocation_candidate_invalid:"
            "0:follow_up_ref_unresolved",
            validate_research_allocation(data, required=True),
        )

    def test_rejected_new_alternative_does_not_require_scout_evidence(self):
        data = valid_input()
        rejected = agenda(data)["candidates"][1]
        self.assertIsNone(rejected.get("scout_candidate_id"))
        self.assertEqual(
            validate_research_allocation(data, required=True),
            [],
        )

    def test_every_candidate_explains_four_allocation_factors(self):
        data = valid_input()
        del agenda(data)["candidates"][1]["allocation_factors"][
            "expected_information_gain"
        ]
        self.assertIn(
            "research_allocation_candidate_invalid:"
            "1:allocation_factors_fields",
            validate_research_allocation(data, required=True),
        )

    def test_bucket_overrun_requires_exact_variance(self):
        data = valid_input()
        plan = agenda(data)["allocation_plan"]
        plan["portfolio_risk"] = 0
        plan["new_opportunity"] = 1
        self.assertIn(
            "research_allocation_variance_invalid:required",
            validate_research_allocation(data, required=True),
        )

        agenda(data)["allocation_variance"] = {
            "exceeded": ["portfolio_risk"],
            "rationale": (
                "Fresh portfolio evidence redirected the planned new-idea "
                "slot into an existing exposure question."
            ),
        }
        self.assertEqual(
            validate_research_allocation(data, required=True),
            [],
        )

    def test_selected_references_cannot_be_double_counted(self):
        data = valid_input()
        duplicate = copy.deepcopy(agenda(data)["candidates"][0])
        duplicate["candidate_id"] = "second-specialist"
        agenda(data)["candidates"].append(duplicate)
        agenda(data)["allocation_plan"]["portfolio_risk"] = 2
        self.assertIn(
            "research_allocation_candidate_invalid:"
            "2:duplicate_selected_reference:portfolio:account",
            validate_research_allocation(data, required=True),
        )

    def test_feedback_counts_only_allocation_contract_cycles(self):
        data = valid_input()
        summary = research_agenda_summary([data])
        self.assertEqual(summary["allocation_cycles_examined"], 1)
        self.assertEqual(summary["legacy_cycles_excluded"], 0)
        self.assertEqual(
            summary["allocation_selection_counts"]["portfolio_risk"],
            1,
        )
        selected = summary["recent"][0]["selected"][0]
        self.assertEqual(
            selected["allocation_reference"],
            "portfolio:account",
        )
        self.assertLessEqual(
            len(selected["allocation_factors"]["novelty"]),
            160,
        )


if __name__ == "__main__":
    unittest.main()
