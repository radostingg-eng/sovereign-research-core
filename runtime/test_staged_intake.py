import json
import os
import pathlib
import re
import tempfile
import unittest
from unittest.mock import patch

from .profile_paths import code_root, profile_root
from .accepted_inputs import input_fingerprint
from .host_publication import (
    content_sha256,
    marker_path,
    verify_canonical_inputs,
)
from .opportunity_ledger import (
    identity_fingerprint,
    soft_identity_fingerprint,
)
from .staged_intake import (
    StagingIntakeInfrastructureError,
    candidate_paths,
    main,
    process_staging,
)
from .test_forecasts import forecast, forecast_input
from .test_forecast_outcomes import (
    forecast_record,
    outcome_input,
)
from .test_instruction_reconciliation import (
    proposal_record,
    reconciliation_input,
)
from .test_run_host_cycle import (
    add_market_scout,
    full_cycle_input,
    goal_creation,
    goal_close,
    goal_progress,
    open_goal_record,
    post_effective_full_cycle as _post_effective_full_cycle,
)
from .test_tool_provenance import upgrade_tool_calls_to_v4


def _learning_dispositions():
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


def sample_input(**over):
    data = full_cycle_input(
        host_input_schema_version=3,
        learning_stage_dispositions=_learning_dispositions(),
    )
    data.update(over)
    return upgrade_tool_calls_to_v4(add_market_scout(data))


def post_effective_full_cycle(**over):
    data = _post_effective_full_cycle(
        host_input_schema_version=3,
        learning_stage_dispositions=_learning_dispositions(),
    )
    data.update(over)
    return upgrade_tool_calls_to_v4(add_market_scout(data))


def opportunity_record():
    identity = {
        "instrument": "VRT",
        "instrument_type": "equity",
        "strategy_family": "special_situations",
        "direction": "long",
        "thesis_key": "data-center power acquisition",
    }
    return {
        "record_id": "opportunity-event:seed",
        "record_type": "opportunity_event",
        "payload": {
            "schema_version": 2,
            "cycle_id": "cycle-seed",
            "event_id": "vrt-seed",
            "opportunity_id": "vrt-special-situation",
            "identity": identity,
            "identity_fingerprint": identity_fingerprint(identity),
            "soft_identity_fingerprint": soft_identity_fingerprint(identity),
            "from_state": None,
            "to_state": "new",
            "research_state": {
                "missing_information": [{
                    "id": "valuation-bridge",
                    "question": "What cash-flow value is attributable?",
                    "why_it_matters": "It determines if repricing is valid.",
                    "status": "open",
                }],
                "uncertainties": [{
                    "id": "economics-uncertain",
                    "description": "Incremental economics are not measured.",
                    "status": "open",
                }],
                "review_triggers": [{
                    "id": "new-company-economics",
                    "condition": "Fresh economics evidence appears.",
                    "status": "active",
                }],
                "next_question_id": "valuation-bridge",
            },
        },
    }


class StagedHostIntakeTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="host-staging-"))
        self.staging = self.root / "host_staging"
        self.inputs = self.root / "host_input"
        self.staging.mkdir()
        self.inputs.mkdir()
        (self.inputs / ".promotion_policy.json").write_text(
            json.dumps({"schema_version": 1, "legacy_files": []}),
            encoding="utf-8",
        )

    def write(self, name, value):
        path = self.staging / name
        text = value if isinstance(value, str) else json.dumps(value, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_candidate_is_promoted_byte_for_byte(self):
        source = self.write(
            "cycle-good.json",
            sample_input(cycle_id="cycle-good"),
        )
        original = source.read_bytes()
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        target = self.inputs / "cycle-good.json"
        self.assertEqual(promoted, ["cycle-good.json"])
        self.assertEqual(refusals, [])
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(
            marker_path(self.inputs, target.name).read_text().strip(),
            content_sha256(target),
        )
        self.assertEqual(verify_canonical_inputs(self.inputs), [])

    def test_invalid_candidate_is_archived_and_never_promoted(self):
        self.write("cycle-bad.json", "PLACEHOLDER")
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn(
            "host_input_not_json_sentinel",
            refusals[0]["reason"],
        )
        rejected = list((self.staging / "rejected").glob("cycle-bad-*.json"))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].read_text(), "PLACEHOLDER")
        self.assertFalse((self.inputs / "cycle-bad.json").exists())
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(feedback["staging_intake"]["promoted"], [])
        self.assertEqual(
            feedback["staging_intake"]["rejected"], ["cycle-bad.json"])

    def test_valid_candidate_survives_beside_a_refused_sibling(self):
        valid = self.write(
            "cycle-valid.json",
            sample_input(cycle_id="cycle-valid"),
        )
        valid_bytes = valid.read_bytes()
        self.write("cycle-invalid.json", "PLACEHOLDER")

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, ["cycle-valid.json"])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(
            (self.inputs / "cycle-valid.json").read_bytes(),
            valid_bytes,
        )

    def test_missing_research_agenda_has_an_actionable_retry_target(self):
        value = sample_input(cycle_id="cycle-missing-agenda")
        director = next(
            row for row in value["cognitive_stages"]
            if row["stage_id"] == "research_director")
        del director["output"]["research_agenda"]
        self.write("cycle-missing-agenda.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn(
            "research_agenda_invalid:missing",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/cognitive_stages/2/output/research_agenda"],
        )
        fix = next(
            item
            for item in feedback["refused"][0]["what_to_fix"]
            if item["code"] == "research_agenda_invalid"
        )
        self.assertIn("rejected alternative", fix["fix"])

    def test_future_as_of_points_to_the_timestamp(self):
        value = sample_input(cycle_id="cycle-future-time")
        future = "2999-09-18T18:45:00Z"
        value["as_of"] = future
        value["snapshot"]["as_of"] = future
        self.write("cycle-future-time.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn("as_of_in_future", refusals[0]["reason"])
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertIn(
            "/as_of",
            feedback["retry_contract"]["must_change_paths"],
        )

    def test_missing_research_allocation_points_to_the_plan(self):
        value = sample_input(cycle_id="cycle-missing-allocation")
        director = next(
            row for row in value["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        del director["output"]["research_agenda"]["allocation_plan"]
        self.write("cycle-missing-allocation.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn(
            "research_allocation_required",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertIn(
            "/cognitive_stages/2/output/"
            "research_agenda/allocation_plan",
            feedback["retry_contract"]["must_change_paths"],
        )

    def test_invalid_forecast_points_to_the_registration(self):
        value = forecast_input(
            forecast(),
            cycle_id="cycle-invalid-forecast",
        )
        del value["forecast_registrations"][0]["metric"]["source"]["field"]
        self.write("cycle-invalid-forecast.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn(
            "forecast_source_invalid:0:metric:source:fields",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertIn(
            "/forecast_registrations/0",
            feedback["retry_contract"]["must_change_paths"],
        )

    def test_corrected_forecast_retry_can_promote(self):
        first = forecast_input(
            forecast(),
            cycle_id="cycle-forecast-retry",
        )
        del first["forecast_registrations"][0]["metric"]["source"]["field"]
        self.write("cycle-forecast-retry.json", first)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertTrue(refusals)

        corrected = forecast_input(
            forecast(),
            cycle_id="cycle-forecast-retry",
        )
        self.write("cycle-forecast-retry-fixed.json", corrected)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, ["cycle-forecast-retry-fixed.json"])
        self.assertEqual(refusals, [])

    def test_invalid_forecast_outcome_points_to_the_row(self):
        value = outcome_input(stable_ref="ibkr://price/OTHER")
        self.write("cycle-invalid-outcome.json", value)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[forecast_record()],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "forecast_outcome_tool_call_invalid:0:stable_ref",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertIn(
            "/forecast_outcomes/0",
            feedback["retry_contract"]["must_change_paths"],
        )

    def test_corrected_forecast_outcome_retry_can_promote(self):
        first = outcome_input(stable_ref="ibkr://price/OTHER")
        self.write("cycle-outcome-retry.json", first)
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[forecast_record()],
        )
        self.assertEqual(promoted, [])
        self.assertTrue(refusals)

        corrected = outcome_input()
        self.write("cycle-outcome-retry-fixed.json", corrected)
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[forecast_record()],
        )
        self.assertEqual(promoted, ["cycle-outcome-retry-fixed.json"])
        self.assertEqual(refusals, [])

    def test_invalid_instruction_reconciliation_points_to_row(self):
        value = reconciliation_input()
        value["instruction_reconciliations"][0]["operator_observation"][
            "quote"
        ] = ""
        self.write("cycle-invalid-reconciliation.json", value)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[proposal_record()],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "instruction_reconciliation_operator_invalid:0:quote",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertIn(
            "/instruction_reconciliations/0",
            feedback["retry_contract"]["must_change_paths"],
        )

    def test_corrected_instruction_reconciliation_can_promote(self):
        first = reconciliation_input()
        first["instruction_reconciliations"][0]["operator_observation"][
            "quote"
        ] = ""
        self.write("cycle-reconciliation-retry.json", first)
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[proposal_record()],
        )
        self.assertEqual(promoted, [])
        self.assertTrue(refusals)

        corrected = reconciliation_input()
        self.write("cycle-reconciliation-retry-fixed.json", corrected)
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[proposal_record()],
        )
        self.assertEqual(
            promoted,
            ["cycle-reconciliation-retry-fixed.json"],
        )
        self.assertEqual(refusals, [])

    def test_corrected_research_allocation_retry_can_promote(self):
        first = sample_input(cycle_id="cycle-allocation-retry")
        director = next(
            row for row in first["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        director["output"]["research_agenda"]["allocation_plan"][
            "rationale"
        ] = ""
        self.write("cycle-allocation-retry.json", first)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertTrue(refusals)

        corrected = sample_input(cycle_id="cycle-allocation-retry")
        self.write("cycle-allocation-retry-fixed.json", corrected)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, ["cycle-allocation-retry-fixed.json"])
        self.assertEqual(refusals, [])

    def test_corrected_candidate_retry_can_use_durable_follow_up_ref(self):
        first = sample_input(cycle_id="cycle-allocation-candidate-retry")
        director = next(
            row for row in first["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        del director["output"]["research_agenda"]["candidates"][0][
            "allocation_factors"
        ]
        self.write("cycle-allocation-candidate-retry.json", first)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertTrue(refusals)

        corrected = sample_input(
            cycle_id="cycle-allocation-candidate-retry",
        )
        director = next(
            row for row in corrected["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        agenda = director["output"]["research_agenda"]
        selected = agenda["candidates"][0]
        selected["portfolio_risk_ref"] = None
        selected["follow_up_ref"] = "candidate:prior-specialist"
        agenda["allocation_plan"]["portfolio_risk"] = 0
        agenda["allocation_plan"]["follow_up"] = 1
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
        self.write("cycle-allocation-candidate-retry-fixed.json", corrected)
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=records,
        )
        self.assertEqual(
            promoted,
            ["cycle-allocation-candidate-retry-fixed.json"],
        )
        self.assertEqual(refusals, [])

    def test_every_retry_required_state_has_a_satisfaction_handler(self):
        source = pathlib.Path(
            __file__
        ).with_name("staged_intake.py").read_text(encoding="utf-8")
        emitted = set(re.findall(
            r'required_state = "([a-z0-9_]+)"',
            source,
        ))
        handled = set(re.findall(
            r'required_state == "([a-z0-9_]+)"',
            source,
        ))
        handled.update(
            value
            for group in re.findall(
                r'required_state in \{([^}]+)\}',
                source,
                flags=re.DOTALL,
            )
            for value in re.findall(r'"([a-z0-9_]+)"', group)
        )
        self.assertEqual(
            emitted - handled,
            set(),
            f"retry states without handlers: {sorted(emitted - handled)}",
        )

    def test_missing_market_scout_has_an_actionable_retry_target(self):
        value = sample_input(cycle_id="cycle-missing-market-scout")
        value["cognitive_stages"] = [
            row for row in value["cognitive_stages"]
            if row["stage_id"] != "market_scout"
        ]
        director = next(
            row for row in value["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        director["depends_on"] = ["portfolio"]
        self.write("cycle-missing-market-scout.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn("market_scout_required", refusals[0]["reason"])
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/cognitive_stages"],
        )

    def test_missing_tool_provenance_points_to_the_call_envelope(self):
        value = post_effective_full_cycle(cycle_id="cycle-no-provenance")
        del value["research"][0]["tool_calls"][0]["provenance"]
        self.write("cycle-no-provenance.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn(
            "tool_provenance_invalid:0:0:provenance_missing",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/research/0/tool_calls/0/provenance"],
        )

    def test_invalid_opportunity_points_to_the_event(self):
        value = post_effective_full_cycle(
            cycle_id="cycle-invalid-opportunity",
            opportunity_updates=[{
                "event_id": "vrt-new",
                "opportunity_id": "vrt-special-situation",
                "from_state": None,
                "to_state": "new",
                "identity": {
                    "instrument": "VRT",
                    "instrument_type": "equity",
                    "strategy_family": "special_situations",
                    "direction": "long",
                },
                "thesis": "A corporate action may change earnings power.",
                "rationale": "Fresh evidence makes the idea worth retaining.",
                "evidence": ["stage:research_director"],
            }],
        )
        self.write("cycle-invalid-opportunity.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn(
            "opportunity_update_identity_invalid:0:missing_fields:thesis_key",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/opportunity_updates/0"],
        )
        self.assertEqual(
            feedback["retry_contract"]["targets"][0]["required_state"],
            "valid_opportunity_update",
        )

    def test_agenda_opportunity_error_points_to_candidate_path(self):
        value = post_effective_full_cycle(
            cycle_id="cycle-agenda-revisit-missing",
            opportunity_updates=[],
        )
        director = next(
            row for row in value["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        candidates = director["output"]["research_agenda"]["candidates"]
        candidates.insert(0, {
            "candidate_id": "rejected-first",
            "scout_candidate_id": None,
            "opportunity_id": None,
            "distinct_from_opportunity_ids": [],
            "distinctness_reason": None,
            "instrument": "ADBE",
            "strategy_family": "quality_at_discount",
            "trigger": "Fresh evidence rejected after comparison.",
            "selected": False,
            "selection_reason": "Lower expected information gain.",
            "rejection_reason": "No fresh valuation evidence.",
            "specialist_stage_id": "value_alternative",
        })
        selected = candidates[1]
        selected.update({
            "instrument": "VRT",
            "strategy_family": "special_situations",
            "opportunity_id": "vrt-special-situation",
        })
        scout = next(
            row for row in value["cognitive_stages"]
            if row["stage_id"] == "market_scout"
        )
        scout["output"]["market_scout_report"]["candidates"][0][
            "identity"
        ] = opportunity_record()["payload"]["identity"]
        self.write("cycle-agenda-revisit-missing.json", value)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[opportunity_record()],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "opportunity_agenda_revisit_required:"
            "1:vrt-special-situation",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertIn(
            "/cognitive_stages/2/output/research_agenda/candidates/1",
            feedback["retry_contract"]["must_change_paths"],
        )

    def test_invalid_goal_creation_points_to_the_bad_field(self):
        value = sample_input(
            cycle_id="cycle-invalid-goal",
            goal_observations=[goal_creation(baseline="unknown")],
        )
        self.write("cycle-invalid-goal.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn(
            "goal_creation_invalid:0:baseline_not_numeric",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/goal_observations/0/goal/baseline"],
        )

    def test_missing_goal_mode_points_to_supported_mode_field(self):
        value = sample_input(
            cycle_id="cycle-missing-goal-mode",
            goal_observations=[{"goal_id": "goal-one"}],
        )
        self.write("cycle-missing-goal-mode.json", value)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])

        self.assertEqual(promoted, [])
        self.assertIn(
            "goal_mode_invalid:0:missing",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/goal_observations/0/mode"],
        )
        target = feedback["retry_contract"]["targets"][0]
        self.assertEqual(target["required_state"], "supported_goal_mode")

    def test_invalid_goal_progress_points_to_terminal_claim(self):
        value = sample_input(
            cycle_id="cycle-invalid-progress",
            as_of="2026-09-16T15:00:00Z",
            goal_observations=[goal_progress(status="met")],
        )
        self.write("cycle-invalid-progress.json", value)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[open_goal_record()],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "goal_progress_invalid:0:unexpected_field_status",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/goal_observations/0/status"],
        )
        self.assertIn(
            "Do not send a goal snapshot",
            next(
                row["fix"]
                for row in feedback["refused"][0]["what_to_fix"]
                if row["code"] == "goal_progress_invalid"
            ),
        )

    def test_invalid_goal_close_points_to_host_authored_status(self):
        value = sample_input(
            cycle_id="cycle-invalid-close",
            as_of="2026-09-17T15:00:00Z",
            goal_observations=[goal_close(status="met")],
        )
        sessions = value["market_sessions"]
        sessions["observed_at"] = "2026-09-17T15:00:00Z"
        sessions["overlap"] = "both_open"
        director = next(
            row for row in value["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        director["output"]["research_agenda"]["allocation_plan"][
            "market_session_context"
        ] = "both_open"
        for market, local_time, next_close in (
            (
                sessions["markets"][0],
                "2026-09-17T17:00:00+02:00",
                "2026-09-17T17:30:00+02:00",
            ),
            (
                sessions["markets"][1],
                "2026-09-17T11:00:00-04:00",
                "2026-09-17T16:00:00-04:00",
            ),
        ):
            market["local_time"] = local_time
            market["status"] = "open"
            market["is_open"] = True
            market["next_close"] = next_close
        self.write("cycle-invalid-close.json", value)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[open_goal_record()],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "goal_close_invalid:0:unexpected_field_status",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/goal_observations/0/status"],
        )

    def test_same_rejected_bytes_count_once(self):
        for _ in range(2):
            self.write("cycle-repeat.json", "PLACEHOLDER")
            process_staging(self.staging, self.inputs, records=[])
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        recurrence = feedback["refusal_recurrence"]
        self.assertEqual(
            recurrence["counts_by_code"]["host_input_not_json_sentinel"],
            1,
        )
        ledger = (
            self.staging / "rejected" / "REJECTIONS.jsonl"
        ).read_text().splitlines()
        self.assertEqual(len(ledger), 1)

    def test_distinct_rejected_bytes_under_same_name_count_separately(self):
        for content in ("PLACEHOLDER", "TODO"):
            self.write("cycle-repeat.json", content)
            process_staging(self.staging, self.inputs, records=[])
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        recurrence = feedback["refusal_recurrence"]
        self.assertEqual(
            recurrence["counts_by_code"]["host_input_not_json_sentinel"],
            2,
        )
        self.assertEqual(
            recurrence["inputs_by_code"]["host_input_not_json_sentinel"],
            ["cycle-repeat.json"],
        )
        self.assertEqual(
            len(recurrence["candidate_ids_by_code"][
                "host_input_not_json_sentinel"
            ]),
            2,
        )

    def test_no_candidate_refresh_updates_only_derived_feedback(self):
        self.write("cycle-structured.json", sample_input(
            cycle_id="cycle-structured",
            lessons=[{
                "lesson_id": "lesson-1",
                "lesson": "A falsifiable lesson.",
                "evidence": ["source-1"],
            }],
        ))
        process_staging(self.staging, self.inputs, records=[])
        path = self.staging / "FEEDBACK.json"
        stale = json.loads(path.read_text(encoding="utf-8"))
        original_reason = stale["refused"][0]["reason"]
        original_recurrence = stale["refusal_recurrence"]
        original_retry = stale["retry_contract"]
        original_last_validation = stale["last_validation"]
        original_older = stale["older_refusals_not_shown"]
        original_staging = stale["staging_intake"]
        stale["read_this_first"] = "No staged submission has been checked yet."
        stale["canonical_schema"] = {"path": "stale", "violations": ["old"]}
        stale["expected_input_shape"] = {"stale": True}
        path.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")

        process_staging(
            self.staging,
            self.inputs,
            records=[],
            refresh_feedback=True,
        )
        refreshed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["refused"][0]["reason"], original_reason)
        self.assertEqual(refreshed["refusal_recurrence"], original_recurrence)
        self.assertEqual(refreshed["retry_contract"], original_retry)
        self.assertEqual(
            refreshed["last_validation"], original_last_validation)
        self.assertEqual(
            refreshed["older_refusals_not_shown"], original_older)
        self.assertEqual(refreshed["staging_intake"], original_staging)
        self.assertNotIn("stale", refreshed["expected_input_shape"])
        self.assertEqual(refreshed["canonical_schema"]["violations"], [])
        self.assertIn(
            "json_pointer",
            refreshed["refused"][0]["what_to_fix"][0],
        )

        first_refresh = path.read_bytes()
        process_staging(
            self.staging,
            self.inputs,
            records=[],
            refresh_feedback=True,
        )
        self.assertEqual(path.read_bytes(), first_refresh)

    def test_no_candidate_without_refresh_leaves_feedback_untouched(self):
        path = self.staging / "FEEDBACK.json"
        path.write_text('{"legacy":true}\n', encoding="utf-8")
        process_staging(self.staging, self.inputs, records=[])
        self.assertEqual(path.read_text(encoding="utf-8"), '{"legacy":true}\n')

    def test_refresh_refuses_malformed_existing_feedback(self):
        path = self.staging / "FEEDBACK.json"
        path.write_text('{"broken":', encoding="utf-8")
        with self.assertRaisesRegex(
            ValueError,
            "validation_feedback_unreadable",
        ):
            process_staging(
                self.staging,
                self.inputs,
                records=[],
                refresh_feedback=True,
            )

    @unittest.skipUnless(
        os.environ.get("SOVEREIGN_PROFILE_DIR"),
        "needs real rejected-candidate profile corpus",
    )
    def test_r33_retry_is_bound_to_all_previous_correction_targets(self):
        source_dir = (
            profile_root()
            / "host_staging"
            / "rejected"
        )
        first = source_dir / (
            "cycle-20260917T070143Z-r33x2-"
            "b7fcbc27ef33b2a61e97e8e2d4c7514966c4c8d889d4e4affc984c5d5304ec5b"
            ".json"
        )
        second = source_dir / (
            "cycle-20260917T070143Z-r33x2-"
            "982c7cd6a484919c2e5597c55ed87c57dd332980b0d3f0b0378d8eacc4ac70a5"
            ".json"
        )
        candidate = self.staging / "cycle-20260917T070143Z-r33x2.json"
        candidate.write_bytes(first.read_bytes())
        process_staging(self.staging, self.inputs, records=[])
        candidate.write_bytes(second.read_bytes())
        _, refusals = process_staging(self.staging, self.inputs, records=[])
        reason = refusals[0]["reason"]
        for pointer in (
            "/lessons/0/falsified_if|non_empty_string",
            "/memory_distillation/memory_objects/0/claim_ids|non_empty_list",
            "/tool_manifest_report/connectors|non_empty_list",
        ):
            self.assertIn(f"retry_target_unsatisfied:{pointer}", reason)

        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        recurrence = feedback["refusal_recurrence"]
        for code in (
            "lesson_missing_falsified_if",
            "memory_object_invalid",
            "tool_manifest_connectors_must_be_nonempty_list",
        ):
            self.assertEqual(recurrence["counts_by_code"][code], 2)
        self.assertEqual(
            set(feedback["retry_contract"]["must_change_paths"]),
            {
                "/lessons/0/falsified_if",
                "/memory_distillation/memory_objects/0/claim_ids",
                "/tool_manifest_report/connectors",
                "/cognitive_stages/1/output/research_agenda",
                "/host_input_schema_version",
            },
        )
        fixes = {
            row["code"]: row
            for row in feedback["refused"][0]["what_to_fix"]
        }
        self.assertEqual(
            fixes["lesson_missing_falsified_if"]["json_pointer"],
            "/lessons/0/falsified_if",
        )
        self.assertEqual(
            fixes["memory_object_invalid"]["json_pointer"],
            "/memory_distillation/memory_objects/0/claim_ids",
        )
        self.assertEqual(
            fixes["tool_manifest_connectors_must_be_nonempty_list"][
                "json_pointer"
            ],
            "/tool_manifest_report/connectors",
        )

    def test_long_valid_json_line_is_not_treated_as_a_syntax_error(self):
        value = sample_input(cycle_id="cycle-wide")
        value["research"][0]["finding"] = "x" * 12000
        source = self.write(
            "cycle-wide.json",
            json.dumps(value, separators=(",", ":")),
        )
        self.assertGreater(
            max(len(line) for line in source.read_text().splitlines()),
            10000,
        )
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, ["cycle-wide.json"])
        self.assertEqual(refusals, [])

    def test_cli_returns_two_for_handled_candidate_refusal(self):
        self.write("cycle-bad.json", "PLACEHOLDER")
        result = main([
            "--staging-dir", str(self.staging),
            "--input-dir", str(self.inputs),
        ])
        self.assertEqual(result, 2)

    def test_non_utf8_file_is_archived_without_blocking_valid_candidate(self):
        bad = self.staging / "cycle-bad.json"
        original = b'{"cycle_id":"cycle-bad","value":"\xff"}'
        bad.write_bytes(original)
        self.write("cycle-good.json", sample_input(cycle_id="cycle-good"))
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, ["cycle-good.json"])
        self.assertIn("host_input_not_utf8:", refusals[0]["reason"])
        rejected = list(
            (self.staging / "rejected").glob("cycle-bad-*.json"))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].read_bytes(), original)

    def test_duplicate_json_key_is_refused(self):
        value = sample_input(cycle_id="cycle-original")
        text = '{"cycle_id":"cycle-duplicate",' + json.dumps(value)[1:]
        self.write("cycle-duplicate.json", text)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertIn(
            "duplicate_json_key:cycle_id",
            refusals[0]["reason"],
        )

    def test_malformed_feedback_includes_precise_context(self):
        self.write(
            "cycle-context.json",
            '{"cycle_id":"cycle-context","snapshot":{"source":"ibkr"}',
        )
        _, refusals = process_staging(
            self.staging, self.inputs, records=[])
        reason = refusals[0]["reason"]
        self.assertIn("line=1", reason)
        self.assertIn("column=", reason)
        self.assertIn("char=", reason)
        self.assertIn("open_depth=", reason)
        self.assertIn("open_containers=", reason)
        self.assertIn("context=", reason)
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        detail = feedback["refused"][0]["what_to_fix"][0]["detail"]
        self.assertIn("context=", detail)

    @unittest.skipUnless(
        os.environ.get("SOVEREIGN_PROFILE_DIR"),
        "needs real rejected-candidate profile corpus",
    )
    def test_historical_malformed_corpus_is_never_promoted(self):
        source_dir = profile_root() / "host_input"
        names = [
            "cycle-20260916T160432Z-q7m3.json",
            "cycle-20260916T170449Z-m4x8.json",
            "cycle-20260916T225020Z-r23n0.json",
            "cycle-20260916T230205Z-r25p2.json",
            "cycle-20260917T040041Z-r30v6.json",
            "cycle-20260917T040500Z-r30v7.json",
            "cycle-20260917T045714Z-r31w0.json",
        ]
        originals = {}
        for name in names:
            original = (source_dir / name).read_bytes()
            originals[name] = original
            (self.staging / name).write_bytes(original)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertEqual(
            {row["input"] for row in refusals},
            set(names),
        )
        for name, original in originals.items():
            archived = list(
                (self.staging / "rejected").glob(
                    f"{pathlib.Path(name).stem}-*.json"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), original)

    @unittest.skipUnless(
        os.environ.get("SOVEREIGN_PROFILE_DIR"),
        "needs real rejected-candidate profile corpus",
    )
    def test_r34_malformed_archive_remains_invalid_and_byte_preserved(self):
        source = (
            profile_root()
            / "host_staging"
            / "rejected"
            / (
                "cycle-20260917T085921Z-r34y3-"
                "6f3dbf3d14228e57b0cc63c834a75f12"
                "a75b85479c8ae8a17081d49459dad7f0.json"
            )
        )
        original = source.read_bytes()
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(original)
        self.assertEqual(caught.exception.lineno, 188)

        candidate = self.staging / source.name
        candidate.write_bytes(original)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertEqual([row["input"] for row in refusals], [source.name])
        archived = list(
            (self.staging / "rejected").glob(f"{source.stem}-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), original)

    def test_marker_exists_before_candidate_moves_to_canonical(self):
        source = self.write(
            "cycle-atomic.json",
            sample_input(cycle_id="cycle-atomic"),
        )
        original_replace = pathlib.Path.replace

        def fail_candidate_move(path, target):
            if path == source:
                raise OSError("simulated move failure")
            return original_replace(path, target)

        with patch.object(pathlib.Path, "replace", fail_candidate_move):
            with self.assertRaisesRegex(
                StagingIntakeInfrastructureError,
                "simulated move failure",
            ):
                process_staging(self.staging, self.inputs, records=[])
        self.assertTrue(
            marker_path(self.inputs, source.name).exists(),
        )
        self.assertFalse((self.inputs / source.name).exists())
        self.assertEqual(verify_canonical_inputs(self.inputs), [])

    def test_promotion_failure_does_not_block_other_candidates(self):
        failed = self.write(
            "cycle-a-failed.json",
            sample_input(cycle_id="cycle-a-failed"),
        )
        self.write(
            "cycle-b-good.json",
            sample_input(cycle_id="cycle-b-good"),
        )
        original_replace = pathlib.Path.replace

        def fail_one_candidate(path, target):
            if path == failed:
                raise OSError("simulated promotion failure")
            return original_replace(path, target)

        with patch.object(pathlib.Path, "replace", fail_one_candidate):
            with self.assertRaisesRegex(
                StagingIntakeInfrastructureError,
                "simulated promotion failure",
            ):
                process_staging(self.staging, self.inputs, records=[])
        self.assertTrue(failed.exists())
        self.assertTrue((self.inputs / "cycle-b-good.json").exists())
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        self.assertEqual(
            feedback["staging_intake"]["promoted"],
            ["cycle-b-good.json"],
        )
        self.assertEqual(
            feedback["staging_intake"]["infrastructure_failures"][0]["input"],
            "cycle-a-failed.json",
        )

    def test_archive_failure_does_not_block_valid_candidate(self):
        failed = self.write("cycle-a-bad.json", "PLACEHOLDER")
        self.write(
            "cycle-b-good.json",
            sample_input(cycle_id="cycle-b-good"),
        )
        with patch(
            "runtime.staged_intake._archive_rejected",
            side_effect=OSError("simulated archive failure"),
        ):
            with self.assertRaisesRegex(
                StagingIntakeInfrastructureError,
                "simulated archive failure",
            ):
                process_staging(self.staging, self.inputs, records=[])
        self.assertTrue(failed.exists())
        self.assertTrue((self.inputs / "cycle-b-good.json").exists())

    def test_cli_returns_one_after_draining_an_io_failure(self):
        failed = self.write(
            "cycle-a-failed.json",
            sample_input(cycle_id="cycle-a-failed"),
        )
        self.write(
            "cycle-b-good.json",
            sample_input(cycle_id="cycle-b-good"),
        )
        original_replace = pathlib.Path.replace

        def fail_one_candidate(path, target):
            if path == failed:
                raise OSError("simulated promotion failure")
            return original_replace(path, target)

        with patch.object(pathlib.Path, "replace", fail_one_candidate):
            result = main([
                "--staging-dir", str(self.staging),
                "--input-dir", str(self.inputs),
            ])
        self.assertEqual(result, 1)
        self.assertTrue((self.inputs / "cycle-b-good.json").exists())

    def test_all_pending_candidates_are_drained(self):
        self.write("cycle-a.json", sample_input(cycle_id="cycle-a"))
        self.write("cycle-b.json", sample_input(cycle_id="cycle-b"))
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, ["cycle-a.json", "cycle-b.json"])
        self.assertEqual(refusals, [])
        self.assertEqual(candidate_paths(self.staging), [])

    def test_filename_collision_is_rejected_without_overwrite(self):
        target = self.inputs / "cycle-same.json"
        target.write_text("canonical", encoding="utf-8")
        self.write(
            "cycle-same.json",
            sample_input(cycle_id="cycle-new"),
        )
        _, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertIn(
            "staged_input_filename_collision:cycle-same.json",
            refusals[0]["reason"],
        )
        self.assertEqual(target.read_text(), "canonical")

    def test_persisted_cycle_id_collision_is_rejected(self):
        value = sample_input(cycle_id="cycle-existing")
        self.write("cycle-new-name.json", value)
        _, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[{
                "record_type": "cycle_receipt",
                "record_id": "cycle-receipt:cycle-existing",
                "payload": {
                    "snapshot_id": (
                        "old:" + input_fingerprint(value)
                    ),
                },
            }],
        )
        self.assertIn(
            "staged_cycle_id_collision:cycle-existing",
            refusals[0]["reason"],
        )

    def test_unexecuted_canonical_cycle_id_collision_is_rejected(self):
        (self.inputs / "existing.json").write_text(
            json.dumps(sample_input(cycle_id="cycle-existing")),
            encoding="utf-8",
        )
        (self.inputs / ".promotion_policy.json").write_text(
            json.dumps({
                "schema_version": 1,
                "legacy_files": ["existing.json"],
            }),
            encoding="utf-8",
        )
        self.write(
            "new-name.json",
            sample_input(cycle_id="cycle-existing"),
        )
        _, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertIn(
            "staged_cycle_id_collision:cycle-existing",
            refusals[0]["reason"],
        )

    def test_malformed_canonical_filename_still_reserves_cycle_id(self):
        (self.inputs / "cycle-existing.json").write_text(
            '{"cycle_id":"cycle-existing"',
            encoding="utf-8",
        )
        (self.inputs / ".promotion_policy.json").write_text(
            json.dumps({
                "schema_version": 1,
                "legacy_files": ["cycle-existing.json"],
            }),
            encoding="utf-8",
        )
        self.write(
            "new-name.json",
            sample_input(cycle_id="cycle-existing"),
        )
        _, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertIn(
            "staged_cycle_id_collision:cycle-existing",
            refusals[0]["reason"],
        )

    def test_direct_unpromoted_canonical_file_fails_verification(self):
        (self.inputs / "direct.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            verify_canonical_inputs(self.inputs),
            ["host_input_not_promoted:direct.json"],
        )

    def test_missing_policy_fails_closed(self):
        (self.inputs / ".promotion_policy.json").unlink()
        _, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(
            refusals,
            [{
                "input": "host_input",
                "reason": "ValueError: host_promotion_policy_missing",
            }],
        )

    def test_missing_policy_never_moves_a_valid_candidate(self):
        (self.inputs / ".promotion_policy.json").unlink()
        candidate = self.write(
            "cycle-policy-blocked.json",
            sample_input(cycle_id="cycle-policy-blocked"),
        )

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertTrue(candidate.is_file())
        self.assertFalse(
            (self.inputs / "cycle-policy-blocked.json").exists()
        )
        self.assertEqual(
            refusals[0]["reason"],
            "ValueError: host_promotion_policy_missing",
        )

    def test_missing_policy_does_not_mark_direct_input_promoted(self):
        (self.inputs / ".promotion_policy.json").unlink()
        direct = self.inputs / "direct.json"
        direct.write_text(
            json.dumps(sample_input(cycle_id="cycle-direct-policy")),
            encoding="utf-8",
        )
        from .host_publication import is_promoted_input

        self.assertFalse(is_promoted_input(direct))

    def test_missing_policy_keeps_pre_policy_legacy_replay_compatible(self):
        (self.inputs / ".promotion_policy.json").unlink()
        legacy = self.inputs / "legacy.json"
        legacy.write_text(
            json.dumps({"source": "legacy"}),
            encoding="utf-8",
        )
        from .host_publication import is_promoted_input

        self.assertTrue(is_promoted_input(legacy))


class StagedWorkflowContractTests(unittest.TestCase):
    def test_promoting_workflow_is_serial_and_fails_publication_errors(self):
        root = code_root()
        text = (
            root
            / "profile_templates"
            / ".github"
            / "workflows"
            / "host-cycle.yml"
        ).read_text()
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("--staging-dir host_staging", text)
        self.assertIn("--verify-canonical", text)
        self.assertIn("publication failed after 3 attempts", text)
        self.assertIn("steps.intake.outputs.rc == '2'", text)
        self.assertIn("Candidate refused safely", text)
        self.assertIn('if [ "$rc" != "0" ] && [ "$rc" != "2" ]', text)
        self.assertIn("python3 -P -m runtime.run_host_cycle", text)
        self.assertIn("AuditJournal", text)
        self.assertNotIn("Fail the run if an input was refused", text)
        self.assertNotIn("rebase conflicted; a later push already moved", text)
        self.assertIn("git add -A tool_artifacts/", text)
        self.assertIn("github.event.repository.private", text)
        self.assertIn("python3 -P -m runtime.profile_health", text)
        self.assertIn("profile code shadow present", text)
        self.assertIn("name: Execute or finalize accepted inputs", text)
        self.assertNotIn(
            "name: Execute accepted candidate\n"
            "        if: steps.intake.outputs.promoted != '0'",
            text,
        )
        self.assertIn("steps.intake.outputs.promoted != '0'", text)


if __name__ == "__main__":
    unittest.main()
