import copy
import unittest

from .decision_repetition import (
    decision_repetition_feedback,
    decision_repetition_receipt_context,
    latest_prior_finalized_decision,
    validate_decision_repetition_review,
)
from .host_feedback import CANONICAL_EXAMPLE
from .run_host_cycle import validate_input


def finalized_decision_records(
    cycle_id: str,
    status: str,
    *,
    review=None,
    completed_at="2026-09-21T01:00:00Z",
):
    receipt = {
        "cycle_id": cycle_id,
        "decision_status": status,
        "completed_at": completed_at,
    }
    if review is not None:
        receipt["decision_repetition"] = {
            "review_required": True,
            "prior_cycle_id": review["prior_cycle_id"],
            "prior_decision_status": status,
            "review": copy.deepcopy(review),
            "selection_advisories": [],
        }
    return [
        {
            "record_id": f"cycle-receipt:{cycle_id}",
            "record_type": "cycle_receipt",
            "payload": receipt,
        },
        {
            "record_id": f"cycle-finalization:{cycle_id}",
            "record_type": "cycle_finalization",
            "caused_by": [f"cycle-receipt:{cycle_id}"],
            "payload": {
                "schema_version": 1,
                "cycle_id": cycle_id,
                "input": {"canonical_sha256": "a" * 64},
                "receipt": {
                    "record_id": f"cycle-receipt:{cycle_id}",
                },
            },
        },
    ]


def opportunity_record(question_id: str, *, status="open", state="watch"):
    return {
        "record_id": "opportunity-event:current",
        "record_type": "opportunity_event",
        "payload": {
            "cycle_id": "cycle-opportunity",
            "opportunity_id": "opportunity-one",
            "identity_fingerprint": "fingerprint-one",
            "to_state": state,
            "research_state": {
                "missing_information": [{
                    "id": question_id,
                    "question": "What evidence would resolve this?",
                    "why_it_matters": "It changes the decision.",
                    "status": status,
                }],
                "uncertainties": [],
                "review_triggers": [],
                "next_question_id": question_id,
            },
        },
    }


def repeated_candidate(disposition: str):
    data = copy.deepcopy(CANONICAL_EXAMPLE)
    data["cycle_id"] = "cycle-current"
    review = {
        "prior_cycle_id": "cycle-prior",
        "disposition": disposition,
        "evidence_delta": [],
        "unresolved_question_ids": [],
        "rationale": "The prior decision was reviewed against current evidence.",
    }
    data["decision"]["repetition_review"] = review
    decision_stage = next(
        row for row in data["cognitive_stages"]
        if row["stage_id"] == "decision"
    )
    decision_stage["output"]["repetition_review"] = copy.deepcopy(review)
    return data


class DecisionRepetitionReviewTests(unittest.TestCase):
    def setUp(self):
        self.records = finalized_decision_records("cycle-prior", "wait")

    def repetition_codes(self, data):
        return {
            error.split(":", 1)[0]
            for error in validate_decision_repetition_review(
                data,
                records=self.records,
                required=True,
            )
        }

    def test_new_full_schema_requires_explicit_null_marker(self):
        data = repeated_candidate("deliberate_wait")
        data["decision"].pop("repetition_review")
        self.records = []
        self.assertEqual(
            self.repetition_codes(data),
            {"decision_repetition_review_field_required"},
        )

    def test_repeated_status_requires_review(self):
        data = repeated_candidate("deliberate_wait")
        data["decision"]["repetition_review"] = None
        self.assertIn(
            "decision_repetition_review_required",
            self.repetition_codes(data),
        )

    def test_new_evidence_requires_current_cycle_refs(self):
        data = repeated_candidate("new_evidence")
        data["decision"]["repetition_review"]["evidence_delta"] = [
            "stage:evidence_arbitration"
        ]
        self.assertEqual(self.repetition_codes(data), set())

        data["decision"]["repetition_review"]["evidence_delta"] = [
            "finding:prior-cycle"
        ]
        self.assertIn(
            "decision_repetition_evidence_ref_invalid",
            self.repetition_codes(data),
        )

    def test_bounded_experiment_requires_complete_experiment(self):
        data = repeated_candidate("bounded_experiment")
        self.assertIn(
            "experiment_contract_required",
            self.repetition_codes(data),
        )
        data["decision"]["experiment"] = {
            "hypothesis": "A bounded observation resolves the uncertainty.",
            "mechanism": "Observe the named evidence source.",
            "measurement": "Compare the new observation with the prior one.",
            "counter_metric": "Record evidence that contradicts the hypothesis.",
            "evaluation_window": "One scheduled cycle.",
            "rollback_condition": "Stop if the evidence source is unavailable.",
        }
        self.assertEqual(self.repetition_codes(data), set())

    def test_deliberate_wait_with_unresolved_question_is_valid(self):
        self.records.append(opportunity_record("question-valuation-bridge"))
        data = repeated_candidate("deliberate_wait")
        data["decision"]["repetition_review"][
            "unresolved_question_ids"
        ] = ["question-valuation-bridge"]
        self.assertEqual(self.repetition_codes(data), set())

    def test_deliberate_wait_rejects_invented_question_id(self):
        self.records.append(opportunity_record("question-real"))
        data = repeated_candidate("deliberate_wait")
        data["decision"]["repetition_review"][
            "unresolved_question_ids"
        ] = ["question-real", "question-invented"]
        self.assertIn(
            "decision_repetition_unresolved_question_id_unknown",
            self.repetition_codes(data),
        )

    def test_non_null_review_is_rejected_without_prior(self):
        self.records = []
        data = repeated_candidate("new_evidence")
        data["decision"]["repetition_review"]["evidence_delta"] = [
            "stage:evidence_arbitration"
        ]
        self.assertEqual(
            self.repetition_codes(data),
            {"decision_repetition_review_unexpected"},
        )

    def test_prior_cycle_must_match_latest_finalized_cycle(self):
        data = repeated_candidate("deliberate_wait")
        self.records.append(opportunity_record("question-valuation-bridge"))
        self.records.extend(
            finalized_decision_records(
                "cycle-latest",
                "wait",
                completed_at="2026-09-21T02:00:00Z",
            )
        )
        data["decision"]["repetition_review"]["prior_cycle_id"] = (
            "cycle-prior"
        )
        data["decision"]["repetition_review"][
            "unresolved_question_ids"
        ] = ["question-valuation-bridge"]
        self.assertIn(
            "decision_repetition_prior_cycle_mismatch",
            self.repetition_codes(data),
        )

    def test_unfinalized_newer_receipt_does_not_replace_latest_finalized(self):
        self.records.append(opportunity_record("question-valuation-bridge"))
        self.records.append({
            "record_id": "cycle-receipt:cycle-unfinalized",
            "record_type": "cycle_receipt",
            "payload": {
                "cycle_id": "cycle-unfinalized",
                "decision_status": "recommended",
                "completed_at": "2026-09-21T02:40:00Z",
            },
        })
        data = repeated_candidate("deliberate_wait")
        data["decision"]["repetition_review"][
            "unresolved_question_ids"
        ] = ["question-valuation-bridge"]
        self.assertEqual(self.repetition_codes(data), set())

    def test_changed_status_does_not_require_review(self):
        data = repeated_candidate("deliberate_wait")
        data["decision"]["status"] = "researching"
        data["decision"]["repetition_review"] = None
        self.assertEqual(self.repetition_codes(data), set())

    def test_non_null_review_is_rejected_when_status_changed(self):
        data = repeated_candidate("deliberate_wait")
        data["decision"]["status"] = "researching"
        self.assertEqual(
            self.repetition_codes(data),
            {"decision_repetition_review_unexpected"},
        )

    def test_historical_replay_does_not_require_review(self):
        data = repeated_candidate("deliberate_wait")
        data["decision"].pop("repetition_review")
        decision_stage = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "decision"
        )
        decision_stage["output"].pop("repetition_review")
        errors = validate_input(data, "historical-v4.json", records=self.records)
        self.assertFalse(any(
            error.startswith("decision_repetition_")
            for error in errors
        ))

    def test_finalized_cycle_is_exempt_even_in_full_validation(self):
        data = repeated_candidate("deliberate_wait")
        data["decision"].pop("repetition_review")
        decision_stage = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "decision"
        )
        decision_stage["output"].pop("repetition_review")
        records = [
            *self.records,
            *finalized_decision_records(
                "cycle-current",
                "wait",
                completed_at="2026-09-21T02:00:00Z",
            ),
        ]
        errors = validate_input(
            data,
            "historical-v4.json",
            records=records,
            require_full_schema=True,
        )
        self.assertFalse(any(
            error.startswith("decision_repetition_")
            for error in errors
        ))

    def test_receipt_time_beats_late_recovery_record_order(self):
        records = [
            *finalized_decision_records(
                "cycle-newer",
                "wait",
                completed_at="2026-09-21T02:30:00Z",
            ),
            *finalized_decision_records(
                "cycle-recovered-older",
                "recommended",
                completed_at="2026-09-21T02:00:00Z",
            ),
        ]
        self.assertEqual(
            latest_prior_finalized_decision(
                records,
                current_cycle_id="cycle-current",
            )["cycle_id"],
            "cycle-newer",
        )

    def test_record_order_breaks_equal_completed_at_tie(self):
        records = [
            *finalized_decision_records(
                "cycle-first",
                "wait",
                completed_at="2026-09-21T02:30:00Z",
            ),
            *finalized_decision_records(
                "cycle-second",
                "recommended",
                completed_at="2026-09-21T02:30:00Z",
            ),
        ]
        self.assertEqual(
            latest_prior_finalized_decision(
                records,
                current_cycle_id="cycle-current",
            )["cycle_id"],
            "cycle-second",
        )

    def test_missing_latest_status_is_advisory_not_older_fallback(self):
        records = [
            *self.records,
            *finalized_decision_records(
                "cycle-malformed",
                "",
                completed_at="2026-09-21T02:00:00Z",
            ),
        ]
        feedback = decision_repetition_feedback(records)
        self.assertEqual(
            feedback["latest_finalized_cycle_id"],
            "cycle-malformed",
        )
        self.assertIsNone(
            feedback["latest_finalized_decision_status"]
        )
        self.assertFalse(
            feedback["review_required_for_repeated_status"]
        )
        self.assertIn(
            "decision_repetition_latest_status_missing:cycle-malformed",
            feedback["advisories"],
        )

    def test_malformed_completed_at_is_advisory_not_order_fallback(self):
        records = [
            *self.records,
            *finalized_decision_records(
                "cycle-malformed-time",
                "recommended",
                completed_at="not-a-time",
            ),
        ]
        feedback = decision_repetition_feedback(records)
        self.assertFalse(
            feedback["review_required_for_repeated_status"]
        )
        self.assertIn(
            "decision_repetition_receipt_completed_at_invalid:"
            "cycle-malformed-time",
            feedback["advisories"],
        )

    def test_older_malformed_receipt_does_not_disable_newer_valid_gate(self):
        self.records = [
            *finalized_decision_records(
                "cycle-malformed-older",
                "recommended",
                completed_at="not-a-time",
            ),
            *finalized_decision_records(
                "cycle-newer",
                "wait",
                completed_at="2026-09-21T02:30:00Z",
            ),
            opportunity_record("question-valuation-bridge"),
        ]
        data = repeated_candidate("deliberate_wait")
        data["decision"]["repetition_review"]["prior_cycle_id"] = (
            "cycle-newer"
        )
        data["decision"]["repetition_review"][
            "unresolved_question_ids"
        ] = ["question-valuation-bridge"]

        self.assertEqual(self.repetition_codes(data), set())
        feedback = decision_repetition_feedback(self.records)
        self.assertEqual(
            feedback["latest_finalized_cycle_id"],
            "cycle-newer",
        )
        self.assertIn(
            "decision_repetition_receipt_completed_at_invalid:"
            "cycle-malformed-older",
            feedback["advisories"],
        )

    def test_late_finalized_invalid_receipt_blocks_older_valid_fallback(self):
        invalid = finalized_decision_records(
            "cycle-invalid",
            "recommended",
            completed_at="not-a-time",
        )
        valid = finalized_decision_records(
            "cycle-valid",
            "wait",
            completed_at="2026-09-21T02:30:00Z",
        )
        records = [
            invalid[0],
            valid[0],
            valid[1],
            invalid[1],
        ]

        self.assertIsNone(latest_prior_finalized_decision(
            records,
            current_cycle_id="cycle-current",
        ))
        feedback = decision_repetition_feedback(records)
        self.assertEqual(
            feedback["latest_finalized_cycle_id"],
            "cycle-invalid",
        )
        self.assertFalse(
            feedback["review_required_for_repeated_status"]
        )
        self.assertIn(
            "decision_repetition_latest_receipt_unusable:cycle-invalid",
            feedback["advisories"],
        )

    def test_receipt_context_records_requirement_and_prior_identity(self):
        self.records.append(opportunity_record("question-valuation-bridge"))
        data = repeated_candidate("deliberate_wait")
        data["decision"]["repetition_review"][
            "unresolved_question_ids"
        ] = ["question-valuation-bridge"]
        context = decision_repetition_receipt_context(
            data,
            records=self.records,
        )
        self.assertEqual(context["review_required"], True)
        self.assertEqual(context["prior_cycle_id"], "cycle-prior")
        self.assertEqual(context["prior_decision_status"], "wait")
        self.assertEqual(
            context["review"],
            data["decision"]["repetition_review"],
        )

    def test_changed_decision_receipt_context_keeps_prior_but_no_review(self):
        data = repeated_candidate("new_evidence")
        data["decision"]["status"] = "researching"
        data["decision"]["repetition_review"] = None
        context = decision_repetition_receipt_context(
            data,
            records=self.records,
        )
        self.assertEqual(context["review_required"], False)
        self.assertEqual(context["prior_cycle_id"], "cycle-prior")
        self.assertEqual(context["prior_decision_status"], "wait")
        self.assertIsNone(context["review"])

    def test_feedback_surfaces_latest_requirement_and_recent_reviews(self):
        review = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "deliberate_wait",
            "evidence_delta": [],
            "unresolved_question_ids": ["question-one"],
            "rationale": "The question remains unresolved.",
        }
        records = [
            *self.records,
            *finalized_decision_records(
                "cycle-current",
                "wait",
                review=review,
            ),
        ]
        feedback = decision_repetition_feedback(records)
        self.assertEqual(
            feedback["latest_finalized_cycle_id"],
            "cycle-current",
        )
        self.assertEqual(
            feedback["latest_finalized_decision_status"],
            "wait",
        )
        self.assertTrue(
            feedback["review_required_for_repeated_status"]
        )
        self.assertEqual(
            feedback["recent_reviews"][0]["repetition_review"],
            review,
        )


if __name__ == "__main__":
    unittest.main()
