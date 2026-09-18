"""The runtime has to answer the host, not just refuse it."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .host_feedback import (
    CANONICAL_EXAMPLE,
    FEEDBACK_FILENAME,
    REFUSAL_GUIDANCE,
    explain,
    parse_reason,
    refusal_pattern_summary,
    write_feedback,
)
from .run_host_cycle import validate_input


class EveryRefusalCanExplainItselfTests(unittest.TestCase):
    """A code with no guidance is a refusal the host cannot act on.

    The host cannot run anything here and cannot read a terminal. If the
    runtime refuses an input and says only "missing_research", acting on it
    means guessing the shape, and the next scheduled run repeats the mistake.
    So a new validator error must not be able to ship as an unexplained one.
    """

    def emitted_codes(self):
        """Every code validate_input can produce, read off the source."""
        import re
        source = "\n".join(
            Path(__file__).with_name(name).read_text(encoding="utf-8")
            for name in (
                "run_host_cycle.py",
                "market_sessions.py",
                "learning_dispositions.py",
                "opportunity_ledger.py",
                "research_allocation.py",
                "forecasts.py",
                "forecast_outcomes.py",
                "instruction_reconciliation.py",
                "calibration_dataset.py",
                "research_value.py",
                "tool_inventory.py",
                "tool_provenance.py",
            )
        )
        codes = set()
        for match in re.finditer(r'errors\.append\(\s*f?"([a-z_]+)', source):
            codes.add(match.group(1))
        for match in re.finditer(r'errors\.append\(\s*\n\s*f?"([a-z_]+)', source):
            codes.add(match.group(1))
        # A trailing underscore means the name is completed by interpolation,
        # e.g. f"contradictory_{field}"; those are covered by name below.
        return {c for c in codes if not c.endswith("_")}

    def test_every_emitted_code_has_guidance(self):
        missing = sorted(self.emitted_codes() - set(REFUSAL_GUIDANCE))
        self.assertEqual(missing, [], f"refusal codes with no guidance: {missing}")

    def test_contradiction_codes_are_covered(self):
        """These are built by interpolation, so the scan above cannot see them."""
        for field in ("source", "as_of", "order_submission_used"):
            self.assertIn(f"contradictory_{field}", REFUSAL_GUIDANCE)

    def test_lesson_codes_are_covered(self):
        for code in (
            "lessons_must_be_a_list",
            "lesson_not_an_object",
            "lesson_missing_lesson",
            "lesson_missing_evidence",
            "lesson_missing_falsified_if",
        ):
            self.assertIn(code, REFUSAL_GUIDANCE)

    def test_guidance_says_what_to_do_not_only_what_broke(self):
        for code, entry in REFUSAL_GUIDANCE.items():
            with self.subTest(code=code):
                self.assertTrue(entry["means"].strip())
                self.assertTrue(entry["fix"].strip())
                self.assertGreater(len(entry["fix"]), 20)

    def test_an_unknown_code_is_reported_as_a_runtime_defect(self):
        """Never silently drop a refusal the runtime cannot explain."""
        entry = explain("some_code_nobody_wrote_guidance_for")
        self.assertIn("defect", entry["means"] + entry["fix"])

    def test_the_detail_after_a_code_is_preserved(self):
        entry = explain("research_without_tool_calls:Where is the risk?")
        self.assertEqual(entry["code"], "research_without_tool_calls")
        self.assertEqual(entry["detail"], "Where is the risk?")


class TheCanonicalExampleIsActuallyValidTests(unittest.TestCase):
    """The example is the shape the host copies.

    An example that would itself be refused is worse than none: it teaches
    the drift it was written to correct.
    """

    def test_the_worked_example_passes_validation(self):
        self.assertEqual(validate_input(CANONICAL_EXAMPLE, "example"), [])

    def test_the_worked_example_is_the_current_v3_contract(self):
        self.assertEqual(CANONICAL_EXAMPLE["host_input_schema_version"], 3)
        stage_ids = {
            row["stage_id"] for row in CANONICAL_EXAMPLE["cognitive_stages"]
        }
        self.assertIn("evidence_arbitration", stage_ids)
        self.assertIn("governance_review", stage_ids)
        self.assertIn("learning_audit", stage_ids)
        self.assertIn("meta_research", stage_ids)
        self.assertEqual(
            {
                row["stage_id"]
                for row in CANONICAL_EXAMPLE[
                    "learning_stage_dispositions"
                ]
            },
            {"learning_audit", "meta_research", "self_improvement"},
        )

    def test_the_worked_example_is_loaded_from_the_committed_template(self):
        from .host_feedback import CANONICAL_EXAMPLE_PATH

        self.assertEqual(
            json.loads(CANONICAL_EXAMPLE_PATH.read_text(encoding="utf-8")),
            CANONICAL_EXAMPLE,
        )

    def test_the_worked_example_contains_no_runtime_ignored_top_level_field(self):
        from .schema_invariants import unconsumed_top_level_fields

        self.assertEqual(unconsumed_top_level_fields(CANONICAL_EXAMPLE), [])


class FeedbackIsWrittenForTheHostTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sovereign-feedback-"))

    def read(self, **kwargs):
        kwargs.setdefault("accepted", [])
        kwargs.setdefault("refusals", [])
        kwargs.setdefault("skipped", [])
        path = write_feedback(self.dir, **kwargs)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_a_refusal_carries_the_fix_not_just_the_code(self):
        payload = self.read(refusals=[{
            "input": "c.json",
            "reason": "ValueError: invalid_host_input:c.json:missing_research,missing_decision"}])
        fixes = payload["refused"][0]["what_to_fix"]
        self.assertEqual([f["code"] for f in fixes],
                         ["missing_research", "missing_decision"])
        self.assertIn("status", fixes[1]["fix"])

    def test_research_agenda_history_is_exposed(self):
        agenda = {
            "cycles_examined": 1,
            "recent": [{"cycle_id": "cycle-one"}],
            "selection_counts": {"ADBE|quality_at_discount": 1},
        }
        payload = self.read(research_agenda=agenda)
        self.assertEqual(payload["research_agenda"], agenda)

    def test_market_scout_summary_is_exposed(self):
        summary = {
            "available": True,
            "cycle_id": "cycle-one",
            "budget": {"external_searches": 1},
            "usage": {"external_searches": 1},
            "candidates": [],
        }
        payload = self.read(market_scout=summary)
        self.assertEqual(payload["market_scout"], summary)

    def test_learning_dispositions_are_exposed(self):
        summary = {
            "count": 1,
            "recent": [{
                "record_id": "learning-disposition:c1:learning_audit",
                "stage_id": "learning_audit",
                "disposition": "no_change",
                "rationale": "No durable change was supported.",
                "evidence": ["stage:learning_audit"],
                "artifact_refs": [],
            }],
            "not_shown": 0,
        }
        payload = self.read(learning_dispositions=summary)
        self.assertEqual(payload["learning_dispositions"], summary)

    def test_opportunity_ledger_is_exposed(self):
        summary = {
            "total": 1,
            "counts_by_state": {"new": 1},
            "items": [{"opportunity_id": "vrt-special-situation"}],
            "not_shown": 0,
            "soft_identity_collisions": [],
        }
        payload = self.read(opportunity_ledger=summary)
        self.assertEqual(payload["opportunity_ledger"], summary)

    def test_forecast_ledger_is_exposed(self):
        summary = {
            "total": 1,
            "open_count": 1,
            "measured_count": 0,
            "overdue_count": 0,
            "superseded_count": 0,
            "items": [{
                "forecast_id": "forecast-one",
                "measurement_status": "open",
            }],
            "not_shown": 0,
        }
        payload = self.read(forecast_ledger=summary)
        self.assertEqual(payload["forecast_ledger"], summary)

    def test_forecast_outcomes_are_exposed(self):
        summary = {
            "measured_count": 1,
            "overdue_count": 0,
            "open_count": 0,
            "matured_count": 1,
            "measurement_coverage": 1.0,
            "invalidated_count": 0,
            "outcome_counts": {
                "resolved_true": 1,
                "resolved_false": 0,
            },
            "calibration": {
                "source": "persisted_forecast_outcomes",
                "n": 1,
                "status": "insufficient_data",
            },
            "recent": [{"forecast_id": "forecast-one"}],
        }
        payload = self.read(forecast_outcomes=summary)
        self.assertEqual(payload["forecast_outcomes"], summary)

    def test_instruction_reconciliation_is_exposed(self):
        summary = {
            "total_records": 1,
            "active_count": 1,
            "counts_by_status": {"deleted_saved_only": 1},
            "items": [{
                "instruction_id": "102",
                "status": "deleted_saved_only",
            }],
            "not_shown": 0,
        }
        payload = self.read(instruction_reconciliation=summary)
        self.assertEqual(payload["instruction_reconciliation"], summary)

    def test_empirical_calibration_is_exposed(self):
        summary = {
            "forecast_rows": 1,
            "exact_forecast_reconciliation_matches": 0,
            "forecast_outcome_calibration": {
                "source": "persisted_forecast_outcomes",
                "n": 0,
                "status": "insufficient_data",
            },
            "unresolved": {"open_forecasts": 1},
        }
        payload = self.read(empirical_calibration=summary)
        self.assertEqual(payload["empirical_calibration"], summary)

    def test_tool_provenance_is_exposed_with_stable_zero_state(self):
        zero = self.read()["tool_provenance"]
        self.assertEqual(zero["call_count"], 0)
        self.assertEqual(zero["rows"], [])
        provenance = {
            "cycle_id": "cycle-one",
            "call_count": 1,
            "connector_response_count": 1,
            "host_summary_count": 0,
            "rows": [{"result_sha256": "a" * 64}],
            "not_shown": 0,
            "what_this_means": "Hashes do not prove capture truth.",
        }
        payload = self.read(tool_provenance=provenance)
        self.assertEqual(payload["tool_provenance"], provenance)

    def test_open_goals_are_exposed(self):
        goals = {
            "open_count": 1,
            "expired_count": 0,
            "open": [{"goal_id": "goal-one", "status": "open"}],
        }
        payload = self.read(goals=goals)
        self.assertEqual(payload["goals"], goals)

    def test_closed_goal_analysis_is_exposed(self):
        goals = {
            "open_count": 0,
            "expired_count": 0,
            "open": [],
            "closed_count": 1,
            "invalidated_count": 0,
            "recent_closed": [{
                "goal_id": "goal-one",
                "status": "closed",
                "terminal_status": "missed",
                "analysis": {
                    "causal_summary": "The evidence arrived too late.",
                },
            }],
        }
        payload = self.read(goals=goals)
        self.assertEqual(
            payload["goals"]["recent_closed"][0]["terminal_status"],
            "missed",
        )

    def test_goal_attribution_is_exposed(self):
        attribution = {
            "sample_count": 1,
            "outcome_counts": {
                "met": 1,
                "partially_met": 0,
                "missed": 0,
                "invalidated": 0,
            },
        }
        payload = self.read(goal_attribution=attribution)
        self.assertEqual(payload["goal_attribution"], attribution)

    def test_the_expected_shape_is_included_when_something_was_refused(self):
        """The host should not have to find the schema elsewhere -- when it
        needs it. Sending a 670-byte example every cycle to a host that has
        been committing valid input for hours is pure cost, and the feedback
        file had grown to the point of competing with the instructions."""
        payload = self.read(refusals=[{"input": "c.json",
                                       "reason": "ValueError: invalid_host_input:c.json:missing_research"}])
        self.assertIn("research", payload["expected_input_shape"])
        self.assertIn("decision", payload["expected_input_shape"])
        self.assertEqual(
            payload["expected_input_shape"]["host_input_schema_version"], 3)
        self.assertIn(
            "cognitive_stages", payload["expected_input_shape"])
        self.assertIn(
            "experiment", payload["expected_input_shape"]["decision"])
        self.assertEqual(payload["canonical_schema"]["violations"], [])

    def test_ignored_schema_fields_are_not_sent_back_to_the_host(self):
        poisoned = dict(CANONICAL_EXAMPLE)
        poisoned["guidance_the_runtime_ignores"] = {"wrong": True}
        with patch(
            "runtime.host_feedback.canonical_schema_status",
            return_value=(
                {
                    key: value
                    for key, value in poisoned.items()
                    if key != "guidance_the_runtime_ignores"
                },
                {
                    "path": "schemas/host_input_v2.example.json",
                    "violations": [
                        "canonical_schema_field_not_consumed:"
                        "guidance_the_runtime_ignores",
                    ],
                },
            ),
        ):
            payload = self.read(refusals=[{
                "input": "c.json",
                "reason": (
                    "ValueError: invalid_host_input:c.json:missing_research"
                ),
            }])
        self.assertNotIn(
            "guidance_the_runtime_ignores",
            payload["expected_input_shape"],
        )
        self.assertEqual(
            payload["canonical_schema"]["violations"],
            [
                "canonical_schema_field_not_consumed:"
                "guidance_the_runtime_ignores",
            ],
        )

    def test_malformed_schema_does_not_block_actionable_feedback(self):
        with patch(
            "runtime.host_feedback.canonical_schema_status",
            return_value=(
                {},
                {
                    "path": "schemas/host_input_v2.example.json",
                    "violations": [
                        "canonical_schema_invalid_json:"
                        "line=2:column=3:char=4",
                    ],
                },
            ),
        ):
            payload = self.read(refusals=[{
                "input": "c.json",
                "reason": (
                    "ValueError: invalid_host_input:c.json:missing_research"
                ),
            }])
        self.assertEqual(payload["expected_input_shape"], {})
        self.assertEqual(
            payload["canonical_schema"]["violations"],
            ["canonical_schema_invalid_json:line=2:column=3:char=4"],
        )
        self.assertEqual(
            payload["refused"][0]["what_to_fix"][0]["code"],
            "missing_research",
        )

    def test_the_schema_is_omitted_on_a_clean_pass(self):
        self.assertIsNone(self.read()["expected_input_shape"])

    def test_only_the_most_recent_refusals_are_sent(self):
        """Six stale refusals were re-sent every cycle after the host had
        already corrected all of them. A refusal it has superseded is
        history, not feedback."""
        many = [{"input": f"c{i}.json",
                 "reason": f"ValueError: invalid_host_input:c{i}.json:missing_research"}
                for i in range(6)]
        payload = self.read(refusals=many)
        self.assertEqual(len(payload["refused"]), 3)
        self.assertEqual(payload["older_refusals_not_shown"], 3)
        self.assertEqual(payload["last_pass"]["refused"], 6)

    def test_recorded_refusals_are_history_not_current_failures(self):
        known = [{
            "input": "old.json",
            "reason": (
                "ValueError: invalid_host_input:old.json:missing_research"
            ),
            "first_seen_this_pass": False,
        }]

        payload = self.read(refusals=known)

        self.assertEqual(payload["last_pass"]["refused"], 0)
        self.assertEqual(payload["refused"], [])
        self.assertEqual(payload["older_refusals_not_shown"], 0)
        self.assertIsNone(payload["expected_input_shape"])
        self.assertEqual(payload["refusal_patterns"]["cycles_analyzed"], 1)
        self.assertEqual(payload["refusal_patterns"]["current_cycles"], 0)
        self.assertEqual(payload["refusal_patterns"]["historical_cycles"], 1)

    def test_mixed_pass_exposes_only_the_new_refusal(self):
        reason = "ValueError: invalid_host_input:{name}:missing_research"
        mixed = [
            {
                "input": "old.json",
                "reason": reason.format(name="old.json"),
                "first_seen_this_pass": False,
            },
            {
                "input": "new.json",
                "reason": reason.format(name="new.json"),
                "first_seen_this_pass": True,
            },
        ]

        payload = self.read(refusals=mixed)

        self.assertEqual(payload["last_pass"]["refused"], 1)
        self.assertEqual(
            [row["input"] for row in payload["refused"]],
            ["new.json"],
        )
        self.assertIsNotNone(payload["expected_input_shape"])
        self.assertEqual(payload["refusal_patterns"]["cycles_analyzed"], 2)
        self.assertEqual(payload["refusal_patterns"]["current_cycles"], 1)
        self.assertEqual(payload["refusal_patterns"]["historical_cycles"], 1)
        self.assertEqual(
            payload["refusal_patterns"]["counts_by_code"][
                "missing_research"
            ],
            2,
        )

    def test_refusal_patterns_count_cycles_not_duplicate_fields(self):
        repeated = (
            "ValueError: invalid_host_input:c.json:"
            "tool_manifest_actions_must_be_objects:one,"
            "tool_manifest_actions_must_be_objects:two"
        )
        summary = refusal_pattern_summary([
            {"input": "a.json", "reason": repeated},
            {"input": "b.json", "reason": repeated},
        ])
        self.assertEqual(
            summary["counts_by_code"]["tool_manifest_actions_must_be_objects"],
            2,
        )
        self.assertEqual(
            summary["repeated"][0]["latest_input"],
            "b.json",
        )
        self.assertEqual(summary["current_cycles"], 2)
        self.assertEqual(summary["historical_cycles"], 0)

    def test_it_is_written_even_when_nothing_was_refused(self):
        """A file that appears only on failure cannot be told from a stale one."""
        payload = self.read(accepted=[{"input": "a.json", "cycle_id": "c",
                                       "status": "completed",
                                       "decision_status": "wait"}])
        self.assertEqual(payload["last_pass"]["refused"], 0)
        self.assertEqual(payload["accepted"][0]["cycle_id"], "c")

    def test_the_feedback_file_is_not_itself_an_input(self):
        """The runner globs this directory; its own reply is not a cycle."""
        from .run_host_cycle import main
        write_feedback(self.dir, accepted=[], refusals=[], skipped=[])
        journal = self.dir / "audit.jsonl"
        code = main(["--input-dir", str(self.dir), "--journal", str(journal)])
        payload = json.loads((self.dir / FEEDBACK_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(payload["last_pass"]["refused"], 0)
        self.assertEqual(code, 0)


class ParsingARefusalReasonTests(unittest.TestCase):

    def test_a_validation_reason_splits_into_its_codes(self):
        entries = parse_reason(
            "ValueError: invalid_host_input:x.json:missing_as_of,source_must_be_ibkr")
        self.assertEqual([e["code"] for e in entries],
                         ["missing_as_of", "source_must_be_ibkr"])

    def test_a_non_validation_reason_still_explains(self):
        entries = parse_reason(
            "ValueError: cannot_verify_input_unchanged:legacy.json: cycle was persisted")
        self.assertEqual(entries[0]["code"], "cannot_verify_input_unchanged")
        self.assertIn("NEW file", entries[0]["fix"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheValidatorEnforcesWhatItClaimsTests(unittest.TestCase):
    """The comment promised the conclusion carries the evidence that produced it.

    Only the presence of a non-empty list was ever checked, so a row with no
    question, no finding and the integer 1 as its sole tool call passed, then
    crashed the research handler mid-cycle after the portfolio stage had
    already been journaled.
    """

    def input_with(self, **over):
        import copy
        data = copy.deepcopy(CANONICAL_EXAMPLE)
        for key, value in over.items():
            data[key] = value
        return data

    def codes(self, data):
        return {c.split(":", 1)[0] for c in validate_input(data, "t.json")}

    def test_the_baseline_is_valid(self):
        self.assertEqual(validate_input(self.input_with(), "t.json"), [])

    def test_a_research_row_must_say_what_was_asked(self):
        data = self.input_with()
        data["research"][0].pop("question")
        self.assertIn("research_without_question", self.codes(data))

    def test_a_research_row_must_say_what_was_concluded(self):
        data = self.input_with()
        data["research"][0].pop("finding")
        self.assertIn("research_without_finding", self.codes(data))

    def test_a_tool_call_must_be_an_object(self):
        data = self.input_with()
        data["research"][0]["tool_calls"] = [1]
        self.assertIn("tool_call_not_an_object", self.codes(data))

    def test_a_tool_call_must_name_its_tool(self):
        data = self.input_with()
        data["research"][0]["tool_calls"][0].pop("tool")
        self.assertIn("tool_call_without_tool", self.codes(data))

    def test_a_tool_call_must_carry_a_result(self):
        """A call that returned nothing is not evidence."""
        data = self.input_with()
        data["research"][0]["tool_calls"][0].pop("result")
        self.assertIn("tool_call_without_result", self.codes(data))

    def test_an_empty_result_is_still_a_result(self):
        """{"orders": []} is a real answer and must not be refused."""
        data = self.input_with()
        data["research"][0]["tool_calls"][0]["result"] = {"orders": []}
        self.assertEqual(validate_input(data, "t.json"), [])

    def test_a_naive_timestamp_is_refused(self):
        """The 14:01 cycle executed with "2026-09-16 14:01:25" and no offset.

        datetime.fromisoformat accepts it, so the receipt is anchored to a
        moment that is ambiguous by the local offset. An observation time
        that cannot be placed is not an observation time.
        """
        data = self.input_with(as_of="2026-09-16 14:01:25")
        self.assertIn("as_of_without_timezone", self.codes(data))

    def test_an_offset_other_than_utc_is_accepted(self):
        """The requirement is that it can be placed, not that it is UTC."""
        data = self.input_with(as_of="2026-09-17T00:00:00+02:00")
        self.assertEqual(validate_input(data, "t.json"), [])

    def test_a_bare_date_is_still_refused_as_unparseable_or_naive(self):
        data = self.input_with(as_of="2026-09-16")
        self.assertTrue(
            {"as_of_without_timezone", "unparseable_as_of"} & self.codes(data))

    def test_an_unrecognised_decision_status_is_refused(self):
        """The executor silently rewrites an unknown status to "blocked", so
        accepting one records a decision the host did not make."""
        data = self.input_with()
        data["decision"]["status"] = "sell_everything"
        self.assertIn("decision_status_not_allowed", self.codes(data))

    def test_a_decision_must_carry_its_reasoning(self):
        data = self.input_with()
        data["decision"].pop("rationale")
        self.assertIn("decision_without_rationale", self.codes(data))

    def test_the_drifted_object_shaped_research_is_refused(self):
        """One shared tool_calls bag cannot show which evidence produced which
        finding, so it is refused rather than normalised."""
        data = self.input_with(research={"question": "x", "tool_calls": [
            {"tool": "IBKR", "call": "c", "result": {}}]})
        self.assertIn("missing_research", self.codes(data))

    def test_the_drifted_decision_field_names_are_refused(self):
        data = self.input_with(decision={"decision_status": "wait",
                                         "reasoning": "because"})
        self.assertIn("missing_decision", self.codes(data))


class SelfImprovementIsReachableFromTheCycleTests(unittest.TestCase):
    """The receipt reported {"status": "none"} no matter what the host sent.

    evaluate_mutation had no production caller at all, so the sandbox,
    measurement and evidence gates were exercised only by their own tests.
    A cycle where the host DID propose a mutation also reported "none", which
    is not a summary of the stage, it is a false one.
    """

    def proposal(self, **over):
        from .run_host_cycle import _MUTATION_FIELDS
        mutation = {field: f"value-for-{field}" for field in _MUTATION_FIELDS}
        mutation.update({"mutation_id": "m-1", "targets": ["runtime/x.py"],
                         "failure_ids": ["f-1"], "counter_metrics": ["cost"],
                         "sample_requirement": 30})
        mutation.update(over)
        return mutation

    def state(self, mutation, **kwargs):
        from .run_host_cycle import self_improvement_state
        return self_improvement_state({"mutation": mutation}, **kwargs)

    def test_no_proposal_is_still_none(self):
        self.assertEqual(self.state(None)["status"], "none")

    def test_a_proposal_is_not_reported_as_none(self):
        state = self.state(self.proposal())
        self.assertNotEqual(state["status"], "none")
        self.assertEqual(state["mutation_ids"], ["m-1"])

    def test_without_the_opt_in_no_verdict_is_implied(self):
        """Withheld must read as withheld, never as a pass."""
        state = self.state(self.proposal())
        self.assertEqual(state["status"], "proposed_not_evaluated")
        self.assertEqual(state["gates"]["candidate_execution"], "not_enabled")
        self.assertNotIn(state["status"], {"eligible", "promoted", "passed"})

    def test_the_opt_in_is_off_by_default(self):
        """Running model-proposed patches is the operator's decision.

        The sandbox is isolated, but measurement executes the candidate to
        compare it against baseline, so an unattended hourly loop must not
        acquire that behaviour merely because the stage was wired up.
        """
        import inspect
        from .run_host_cycle import self_improvement_state
        signature = inspect.signature(self_improvement_state)
        self.assertIs(signature.parameters["allow_execution"].default, False)

    def test_a_partial_proposal_is_refused(self):
        from .run_host_cycle import validate_mutation_block
        mutation = self.proposal()
        mutation.pop("rollback_condition")
        codes = validate_mutation_block({"mutation": mutation})
        self.assertIn("mutation_missing_field:rollback_condition", codes)

    def test_a_non_object_proposal_is_refused(self):
        from .run_host_cycle import validate_mutation_block
        self.assertEqual(validate_mutation_block({"mutation": "a patch"}),
                         ["mutation_not_an_object"])

    def test_omitting_a_proposal_is_not_an_error(self):
        from .run_host_cycle import validate_mutation_block
        self.assertEqual(validate_mutation_block({}), [])

    def test_the_receipt_actually_carries_the_proposal(self):
        """Testing the function is not testing the wiring.

        self_improvement_state can be perfectly correct while the receipt
        still hardcodes "none", which is the bug this closed.
        """
        import copy
        import tempfile
        from .run_host_cycle import main
        directory = Path(tempfile.mkdtemp(prefix="sovereign-si-"))
        data = copy.deepcopy(CANONICAL_EXAMPLE)
        data["mutation"] = self.proposal()
        data["cycle_id"] = "cycle-si-wired"
        (directory / "c.json").write_text(json.dumps(data), encoding="utf-8")
        journal_path = directory / "audit.jsonl"
        self.assertEqual(main(["--input-dir", str(directory),
                               "--journal", str(journal_path)]), 0)
        receipts = [json.loads(line) for line in
                    journal_path.read_text(encoding="utf-8").splitlines()]
        receipt = next(r for r in receipts if r["record_type"] == "cycle_receipt")
        state = receipt["payload"]["self_improvement"]
        self.assertEqual(state["status"], "proposed_not_evaluated")
        self.assertEqual(state["mutation_ids"], ["m-1"])

    def test_validate_input_refuses_a_malformed_proposal(self):
        """The block check has to be reachable from the real entry point."""
        import copy
        data = copy.deepcopy(CANONICAL_EXAMPLE)
        data["mutation"] = "not an object"
        self.assertIn("mutation_not_an_object", validate_input(data, "t.json"))


class ARepeatedRefusalIsNotANewAlarmTests(unittest.TestCase):
    """A file the host cannot or will not fix sits in the directory forever.

    Counting it on every hourly pass made the exit code a permanent 1, which
    is the same as no alarm at all: nothing could distinguish "one old file is
    still bad" from "this cycle just broke". The refusal is still printed and
    still in the journal; it simply stops re-raising the alert.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sovereign-repeat-"))
        self.journal = self.dir / "audit.jsonl"

    def write(self, name, **over):
        import copy
        data = copy.deepcopy(CANONICAL_EXAMPLE)
        data.update(over)
        (self.dir / name).write_text(json.dumps(data), encoding="utf-8")

    def run_pass(self):
        from .run_host_cycle import main
        return main(["--input-dir", str(self.dir), "--journal", str(self.journal)])

    def test_a_new_refusal_raises_the_alarm(self):
        self.write("bad.json", decision={"status": "nonsense"})
        self.assertEqual(self.run_pass(), 1)

    def test_the_same_refusal_next_pass_does_not(self):
        self.write("bad.json", decision={"status": "nonsense"})
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.run_pass(), 0)

    def test_a_changed_file_that_is_still_bad_is_news_again(self):
        """Different content is a different refusal, even under one name."""
        self.write("bad.json", decision={"status": "nonsense"})
        self.assertEqual(self.run_pass(), 1)
        self.write("bad.json", decision={"status": "also nonsense"})
        self.assertEqual(self.run_pass(), 1)

    def test_runtime_bookkeeping_is_not_mistaken_for_an_input(self):
        """pathlib's glob matches dotfiles, unlike a shell's.

        The truncation mark lives in the audit directory, which is the input
        directory in some configurations, and an unfiltered glob handed the
        executor its own state file and refused it as a malformed cycle.
        """
        (self.dir / ".high_water.json").write_text('{"records": 1}',
                                                   encoding="utf-8")
        self.write("good.json", cycle_id="cycle-not-confused")
        self.assertEqual(self.run_pass(), 0)

    def test_a_good_file_still_runs_alongside_a_known_bad_one(self):
        self.write("bad.json", decision={"status": "nonsense"})
        self.run_pass()
        self.write("good.json", cycle_id="cycle-good-one")
        self.assertEqual(self.run_pass(), 0)
        from .audit_store import AuditJournal
        from .run_host_cycle import already_persisted
        self.assertTrue(already_persisted(AuditJournal(self.journal),
                                          "cycle-good-one"))


class AnIdlePassSaysNothingNewTests(unittest.TestCase):
    """The scheduler committed a one-line change on every idle run.

    generated_at always differs, so rewriting the file unconditionally made
    the tree dirty every pass and the scheduler pushed a junk commit each
    time it ran and found nothing to do. That buries the real receipts, and
    it gets worse the more often the executor polls, which is exactly what
    you want to do when the producer's cadence is unknown.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sovereign-quiet-"))

    def write(self, **kwargs):
        kwargs.setdefault("accepted", [])
        kwargs.setdefault("refusals", [])
        kwargs.setdefault("skipped", [])
        return write_feedback(self.dir, **kwargs)

    def test_an_unchanged_message_is_not_rewritten(self):
        path = self.write(skipped=["a.json"])
        first = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime_ns
        self.write(skipped=["a.json"])
        self.assertEqual(path.read_text(encoding="utf-8"), first)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_json_equivalent_tuple_does_not_rewrite_feedback(self):
        probes = {
            "contracts": {
                "instruction": {
                    "required_operations": ("create", "get"),
                },
            },
        }
        path = self.write(delivery_probes=probes)
        first = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime_ns
        self.write(delivery_probes=probes)
        self.assertEqual(path.read_text(encoding="utf-8"), first)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_a_real_change_still_lands(self):
        path = self.write(skipped=["a.json"])
        first = path.read_text(encoding="utf-8")
        self.write(accepted=[{"input": "b.json", "cycle_id": "c",
                              "status": "completed", "decision_status": "wait"}])
        self.assertNotEqual(path.read_text(encoding="utf-8"), first)

    def test_a_corrupt_existing_file_is_replaced_rather_than_trusted(self):
        path = self.dir / FEEDBACK_FILENAME
        path.write_text("{not json", encoding="utf-8")
        self.write(skipped=["a.json"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))
                         ["last_pass"]["already_persisted"], 1)


class TheFallbackLeavesFreshInputsAloneTests(unittest.TestCase):
    """Two executors, one append-only hash chain.

    Two writers picking the same parent is how this journal forks
    irreversibly, and it has happened here before. The fallback therefore
    only ever touches inputs the primary executor has already had its chance
    at: a minimum age means the local agent, which polls every ten minutes,
    always gets first refusal, and the fallback sees only what the laptop was
    asleep for.
    """

    def repo_with_input(self, name="c.json", *, age_days=0):
        import copy
        import subprocess
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="sovereign-age-"))
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
        inputs = root / "host_input"
        inputs.mkdir()
        data = copy.deepcopy(CANONICAL_EXAMPLE)
        data["cycle_id"] = "cycle-age-test"
        (inputs / name).write_text(json.dumps(data), encoding="utf-8")
        env = None
        if age_days:
            from datetime import datetime, timedelta, timezone
            when = (datetime.now(timezone.utc)
                    - timedelta(days=age_days)).isoformat()
            env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "input"], cwd=root,
                       check=True, env={**__import__("os").environ, **(env or {})})
        return root, inputs

    def run_pass(self, inputs, journal, min_age):
        from .run_host_cycle import main
        return main(["--input-dir", str(inputs), "--journal", str(journal),
                     "--min-input-age-minutes", str(min_age)])

    def test_a_fresh_input_is_left_to_the_primary_executor(self):
        root, inputs = self.repo_with_input()
        journal = root / "audit.jsonl"
        self.run_pass(inputs, journal, 20)
        from .audit_store import AuditJournal
        from .run_host_cycle import already_persisted
        self.assertFalse(already_persisted(AuditJournal(journal),
                                           "cycle-age-test"))

    def test_an_input_the_primary_never_took_is_executed(self):
        root, inputs = self.repo_with_input(age_days=1)
        journal = root / "audit.jsonl"
        self.run_pass(inputs, journal, 20)
        from .audit_store import AuditJournal
        from .run_host_cycle import already_persisted
        self.assertTrue(already_persisted(AuditJournal(journal),
                                          "cycle-age-test"))

    def test_the_gate_is_off_by_default(self):
        """The primary executor must not defer to anyone."""
        root, inputs = self.repo_with_input()
        journal = root / "audit.jsonl"
        self.run_pass(inputs, journal, 0)
        from .audit_store import AuditJournal
        from .run_host_cycle import already_persisted
        self.assertTrue(already_persisted(AuditJournal(journal),
                                          "cycle-age-test"))

    def test_an_unmeasurable_age_does_not_strand_the_input(self):
        """Outside a git repo there is no commit time.

        Refusing to execute on the strength of an age nobody could measure
        would leave the input undrained forever, which is worse than the race
        the gate exists to prevent.
        """
        import tempfile
        from .run_host_cycle import input_age_minutes
        loose = Path(tempfile.mkdtemp(prefix="sovereign-nogit-")) / "x.json"
        loose.write_text("{}", encoding="utf-8")
        self.assertIsNone(input_age_minutes(loose))


class TheOptInPathIsActuallyExercisedTests(unittest.TestCase):
    """This shipped raising TypeError on the first line of real work.

    Every test asserted the DEFAULT (allow_execution=False), so the branch
    that does the work was never once executed: run_candidate was called
    without repo_root, measure_candidate without repo_root, load_tasks
    without a path, and evaluate_mutation without sandbox_passed. Four
    signature errors behind one untested flag.

    Testing a function and testing the wiring are different things, which is
    the same lesson that put self_improvement_state here in the first place.
    """

    PATCH = ("--- a/runtime/scratch_probe.py\n"
             "+++ b/runtime/scratch_probe.py\n"
             "@@ -0,0 +1 @@\n"
             "+PROBE = 1\n")

    def proposal(self, **over):
        from .run_host_cycle import _MUTATION_FIELDS
        mutation = {field: f"v-{field}" for field in _MUTATION_FIELDS}
        mutation.update({
            "mutation_id": "m-optin", "targets": ["runtime/scratch_probe.py"],
            "failure_ids": ["f1"], "counter_metrics": ["cost"],
            "sample_requirement": 30, "patch": self.PATCH,
            "created_at": "2026-09-16T00:00:00Z"})
        mutation.update(over)
        return mutation

    def state(self, **over):
        from .run_host_cycle import self_improvement_state
        return self_improvement_state({"mutation": self.proposal(**over)},
                                      allow_execution=True)

    def test_the_opt_in_path_does_not_crash(self):
        state = self.state()
        self.assertNotEqual(state["status"], "evaluation_failed",
                            f"opt-in path errored: {state.get('note')}")

    def test_it_reaches_a_real_verdict_with_a_real_reason(self):
        state = self.state()
        self.assertIn(state["status"],
                      {"eligible", "testing", "rejected"})
        self.assertTrue(str(state["note"]).strip())
        self.assertEqual(state["mutation_ids"], ["m-optin"])

    def test_the_sandbox_actually_ran(self):
        """The gates must report a real sandbox outcome, not a placeholder."""
        self.assertIn(self.state()["gates"].get("sandbox"),
                      {"passed", "failed"})

    def test_a_bad_patch_does_not_take_the_cycle_down(self):
        """A proposal is optional extra work attached to a cycle.

        The sandbox refusing it says something about the proposal and nothing
        about the portfolio observation or the decision, so letting it
        propagate would refuse the whole input over a bad patch.
        """
        state = self.state(patch="")
        self.assertEqual(state["status"], "evaluation_failed")
        self.assertIn("empty_patch", state["note"])
        self.assertEqual(state["gates"]["candidate_execution"], "errored")


class AMalformedFileGetsActionableFeedbackTests(unittest.TestCase):
    """The host was told its own bad JSON was a runtime defect to report.

    parse_reason only understood validation codes, so an exception string
    fell through to the unknown-code branch. That is both wrong and
    unactionable on the single most likely thing to go wrong: the host emits
    JSON by hand, and a trailing comma is the classic way to get this.
    """

    REASON = ("JSONDecodeError: Expecting property name enclosed in double "
              "quotes: line 116 column 9 (char 11619)")

    def test_it_is_named_as_malformed_json(self):
        self.assertEqual(parse_reason(self.REASON)[0]["code"], "malformed_json")

    def test_it_does_not_blame_the_runtime(self):
        entry = parse_reason(self.REASON)[0]
        self.assertNotIn("runtime defect", entry["fix"])

    def test_it_names_the_usual_causes(self):
        fix = parse_reason(self.REASON)[0]["fix"]
        self.assertIn("trailing comma", fix)

    def test_the_parser_location_is_preserved(self):
        """Without the line and column the host cannot find the problem."""
        self.assertIn("line 116", parse_reason(self.REASON)[0]["detail"])

    def test_validation_reasons_still_parse_normally(self):
        entries = parse_reason(
            "ValueError: invalid_host_input:x.json:missing_as_of")
        self.assertEqual([e["code"] for e in entries], ["missing_as_of"])
