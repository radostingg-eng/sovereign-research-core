import hashlib
import json
import pathlib
import re
import tempfile
import unittest
from unittest.mock import patch

from .profile_paths import code_root
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
from .refusal_audit import retry_lineage_errors
from .staged_intake import (
    StagingIntakeInfrastructureError,
    _correction_targets,
    _retry_targets,
    _retry_preflight_codes_for_value,
    _target_satisfied,
    candidate_paths,
    main,
    process_staging,
)
from .semantic_candidate import (
    build_semantic_candidate,
    probe_semantic_candidate,
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
from .test_semantic_candidate import semantic_candidate


FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
SEMANTIC_PROBE_DIR = FIXTURES_DIR / "semantic_probe"
SEMANTIC_PROBE_EXPECTED = FIXTURES_DIR / "semantic_probe_expected.json"
MALFORMED_INTAKE_DIR = FIXTURES_DIR / "staged_intake_malformed"


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


def test_retry_schedule_target_accepts_honest_unavailable_run_id():
    target = {
        "json_pointer": "/schedule_context",
        "required_state": "complete_schedule_context",
    }
    context = {
        "schema_version": 2,
        "task_id": "task-hourly-1",
        "platform_run_id": None,
        "platform_run_id_status": "unavailable",
        "expected_slot": "2026-09-19T10:00:00Z",
        "started_at": "2026-09-19T10:00:00Z",
        "source_observed_at": "2026-09-19T10:00:00Z",
        "trigger": "scheduled",
        "intervention": "none",
    }
    assert _target_satisfied({"schedule_context": context}, target)
    assert not _target_satisfied(
        {"schedule_context": {**context, "platform_run_id": "invented"}},
        target,
    )


def sample_input(**over):
    data = full_cycle_input(
        host_input_schema_version=3,
        learning_stage_dispositions=_learning_dispositions(),
    )
    data.update(over)
    return upgrade_tool_calls_to_v4(add_market_scout(data))


def retry_union_candidate(*, additional_defect=False):
    value = sample_input(cycle_id="cycle-sample-retry")
    value["lessons"] = [{
        "lesson": "SAMPLE",
        "evidence": "SAMPLE",
        "falsified_if": "",
    }]
    memory = {
        "memory_id": "sample-memory",
        "layer": "active_brain",
        "status": "active",
        "as_of": "2000-01-01T00:00:00Z",
        "claim": "SAMPLE",
        "source_ids": ["sample-source"],
        "evidence_status": "verified",
        "confidence": 0.5,
        "reconstruction_status": "passed",
        "claim_ids": [],
    }
    value["memory_distillation"] = {
        "distillation_id": "sample-distillation",
        "source_ids": ["sample-source"],
        "source_time_bounds": {
            "from": "2000-01-01T00:00:00Z",
            "to": "2000-01-01T01:00:00Z",
        },
        "memory_objects": [memory],
        "claim_ids": ["sample-claim"],
        "contradiction_groups": {},
        "active_brain_proposals": [],
        "retirements": [],
        "reconstruction_spec": {
            "required_claim_ids": [],
            "distilled_claim_ids": [],
            "source_claim_ids": [],
            "distilled_contradiction_groups": {},
        },
        "compression_metrics": {
            "raw_units": 10,
            "distilled_units": 1,
        },
        "blockers": [],
        "ex_post_material": [],
        "brain_version": 1,
    }
    value["tool_manifest_report"] = {
        "observed_at": "2000-01-01T00:00:00Z",
        "complete_for_current_session": True,
        "connectors": [],
    }
    director = next(
        stage
        for stage in value["cognitive_stages"]
        if stage["stage_id"] == "research_director"
    )
    director["output"].pop("research_agenda")
    if additional_defect:
        value["learning_stage_dispositions"] = []
    return value


def seed_stale_semantic_retry(staging):
    parent = semantic_candidate()
    parent["research_agenda"]["candidates"][0]["candidate_id"] = (
        "scout-macro-specialist"
    )
    input_name = "cycle-parent.semantic.json"
    body = json.dumps(parent, indent=2) + "\n"
    digest = hashlib.sha256(body.encode()).hexdigest()
    archive = f"cycle-parent.semantic-{digest}.json"
    rejected = staging / "rejected"
    rejected.mkdir()
    (rejected / archive).write_text(body, encoding="utf-8")
    candidate_id = f"{input_name}@sha256:{digest}"
    event = {
        "candidate_id": candidate_id,
        "input": input_name,
        "cycle_id": parent["cycle_id"],
        "sha256": digest,
        "archive": archive,
        "refused_at": "2026-09-23T11:02:04Z",
        "codes": [
            "semantic_candidate_invalid:semantic_stage_output_missing",
            "retry_target_unsatisfied:/|semantic_builder_valid",
        ],
        "correction_targets": [
            {
                "code": "semantic_stage_output_missing",
                "json_pointer": "/stage_outputs/scout-macro-specialist",
                "required_state": "semantic_builder_valid",
            },
            {
                "code": "retry_target_unsatisfied:/|semantic_builder_valid",
                "json_pointer": "/",
                "required_state": "semantic_builder_valid",
            },
        ],
    }
    (rejected / "REJECTIONS.jsonl").write_text(
        json.dumps(event) + "\n",
        encoding="utf-8",
    )
    return candidate_id


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
    def test_forecast_assessment_refusal_targets_decision_assessment(self):
        targets = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "forecast_assessment_required",
            {"decision": {"status": "wait"}},
        )

        self.assertEqual(targets, [{
            "code": "forecast_assessment_required",
            "json_pointer": "/decision/forecast_assessment",
            "required_state": "complete_decision_forecast_assessment",
        }])

    def test_not_required_assessment_satisfies_retry_without_registrations(
        self,
    ):
        value = {
            "decision": {
                "forecast_assessment": {
                    "status": "not_required",
                    "material_premise": None,
                    "rationale": (
                        "No decision-material falsifiable premise exists."
                    ),
                    "forecast_ids": [],
                },
            },
        }
        target = {
            "code": "forecast_assessment_required",
            "json_pointer": "/decision/forecast_assessment",
            "required_state": "complete_decision_forecast_assessment",
        }

        self.assertTrue(_target_satisfied(value, target))

    def test_decision_stage_forecast_assessment_targets_stage_copy(self):
        assessment = {
            "status": "not_required",
            "material_premise": None,
            "rationale": "No material premise.",
            "forecast_ids": [],
        }
        value = {
            "decision": {"forecast_assessment": assessment},
            "cognitive_stages": [{
                "stage_id": "decision",
                "output": {"forecast_assessment": None},
            }],
        }
        targets = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "decision_stage_forecast_assessment_mismatch",
            value,
        )

        self.assertEqual(targets, [{
            "code": "decision_stage_forecast_assessment_mismatch",
            "json_pointer": (
                "/cognitive_stages/0/output/forecast_assessment"
            ),
            "required_state": "matches_decision_forecast_assessment",
        }])
        value["cognitive_stages"][0]["output"][
            "forecast_assessment"
        ] = assessment
        self.assertTrue(_target_satisfied(value, targets[0]))

    def test_repetition_review_refusal_has_actionable_target(self):
        value = {
            "decision": {
                "status": "wait",
            },
        }
        targets = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "decision_repetition_review_required:cycle-prior:wait",
            value,
        )
        self.assertEqual(targets, [{
            "code": (
                "decision_repetition_review_required:cycle-prior:wait"
            ),
            "json_pointer": "/decision/repetition_review",
            "required_state": "complete_decision_repetition_review",
        }])

    def test_v54_blocking_errors_all_have_actionable_targets(self):
        value = {
            "evidence_calls": [
                {"producer": "portfolio", "call": {"tool_call_id": "summary"}},
                {"producer": "portfolio", "call": {"tool_call_id": "positions"}},
                {"producer": "saved_instructions", "call": {
                    "tool_call_id": "instructions",
                }},
                {"producer": "account_orders", "call": {"tool_call_id": "orders"}},
                {"producer": "account_trades", "call": {"tool_call_id": "trades"}},
                {"producer": "market_sessions", "call": {"tool_call_id": "eu"}},
                {"producer": "market_sessions", "call": {"tool_call_id": "us"}},
                {"producer": "market_scout", "call": {
                    "tool_call_id": "scout-shared",
                }},
                {"producer": "specialist", "call": {
                    "tool_call_id": "research-shared",
                }},
            ],
            "cognitive_stages": [
                {
                    "stage_id": "market_scout",
                    "output": {
                        "market_scout_report": {
                            "tool_calls": [{
                                "tool_call_id": "scout-shared",
                            }],
                        },
                    },
                },
                {
                    "stage_id": "research_director",
                    "output": {
                        "research_agenda": {
                            "candidates": [],
                        },
                    },
                },
            ],
            "research": [{
                "tool_calls": [{
                    "tool_call_id": "research-shared",
                }],
            }],
            "snapshot": {"order_instructions": []},
            "order_instructions": [],
            "worker_research_dispositions": [],
        }
        reason = (
            "ValueError: invalid_host_input:cycle.json:"
            "evidence_call_invalid:1:projection,"
            "evidence_call_invalid:7:producer,"
            "evidence_call_invalid:7:provenance:"
            "host_summary_stable_ref_required,"
            "evidence_projection_missing:portfolio:/snapshot/positions,"
            "order_instruction_projection_conflict,"
            "research_direction_committed_question_unaddressed:"
            "opportunity-gnrc:gnrc-margin,"
            "research_direction_open_question_unaddressed,"
            "tool_call_id_conflict:scout-shared,"
            "tool_call_id_conflict:research-shared,"
            "worker_research_disposition_missing:azure-a-record"
        )

        targets = {
            target["code"]: target
            for target in _correction_targets(reason, value)
        }

        self.assertEqual(
            targets["evidence_call_invalid:1:projection"],
            {
                "code": "evidence_call_invalid:1:projection",
                "json_pointer": "/evidence_calls/1/projection",
                "required_state": "valid_evidence_call",
            },
        )
        self.assertEqual(
            targets["evidence_call_invalid:7:producer"]["json_pointer"],
            "/evidence_calls/7/producer",
        )
        self.assertEqual(
            targets[
                "evidence_call_invalid:7:provenance:"
                "host_summary_stable_ref_required"
            ]["json_pointer"],
            "/evidence_calls/7/call/provenance/source_refs",
        )
        self.assertEqual(
            targets[
                "evidence_projection_missing:portfolio:/snapshot/positions"
            ]["required_state"],
            "evidence_projection_bound",
        )
        self.assertEqual(
            targets["order_instruction_projection_conflict"],
            {
                "code": "order_instruction_projection_conflict",
                "json_pointer": "/snapshot/order_instructions",
                "required_state": "matches_top_level_order_instructions",
            },
        )
        self.assertEqual(
            targets[
                "research_direction_committed_question_unaddressed:"
                "opportunity-gnrc:gnrc-margin"
            ]["required_state"],
            "addresses_committed_question",
        )
        self.assertEqual(
            targets["research_direction_open_question_unaddressed"][
                "required_state"
            ],
            "candidate_with_opportunity_question_link",
        )
        self.assertEqual(
            targets["tool_call_id_conflict:scout-shared"]["json_pointer"],
            (
                "/cognitive_stages/0/output/market_scout_report/"
                "tool_calls/0"
            ),
        )
        self.assertEqual(
            targets["tool_call_id_conflict:research-shared"][
                "json_pointer"
            ],
            "/research/0/tool_calls/0",
        )
        self.assertEqual(
            targets[
                "worker_research_disposition_missing:azure-a-record"
            ]["required_state"],
            "worker_research_disposition_for_record",
        )

    def test_specific_retry_targets_require_the_actual_fix(self):
        value = {
            "research_agenda": {
                "candidates": [{
                    "opportunity_id": "opportunity-gnrc",
                    "target_missing_information_id": "gnrc-margin",
                }],
            },
            "snapshot": {"order_instructions": [{"id": "101"}]},
            "order_instructions": [{"id": "101"}],
            "worker_research_dispositions": [{
                "worker_record_id": "azure-a-record",
            }],
        }

        self.assertTrue(_target_satisfied(value, {
            "code": (
                "research_direction_committed_question_unaddressed:"
                "opportunity-gnrc:gnrc-margin"
            ),
            "json_pointer": "/research_agenda/candidates",
            "required_state": "addresses_committed_question",
        }))
        self.assertTrue(_target_satisfied(value, {
            "code": "worker_research_disposition_missing:azure-a-record",
            "json_pointer": "/worker_research_dispositions",
            "required_state": "worker_research_disposition_for_record",
        }))
        self.assertTrue(_target_satisfied(value, {
            "code": "order_instruction_projection_conflict",
            "json_pointer": "/snapshot/order_instructions",
            "required_state": "matches_top_level_order_instructions",
        }))

    def test_stale_worker_disposition_target_requires_row_removal(self):
        value = {
            "worker_research_dispositions": [
                {"worker_record_id": "stale-record"},
                {"worker_record_id": "current-record"},
            ],
        }
        target = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "worker_research_disposition_unexpected:stale-record",
            value,
        )[0]

        self.assertEqual(target, {
            "code": (
                "worker_research_disposition_unexpected:stale-record"
            ),
            "json_pointer": "/worker_research_dispositions",
            "required_state": "worker_research_disposition_absent",
        })
        self.assertFalse(_target_satisfied(value, target))
        value["worker_research_dispositions"].pop(0)
        self.assertTrue(_target_satisfied(value, target))

    def test_duplicate_worker_disposition_targets_second_row(self):
        value = {
            "worker_research_dispositions": [
                {"worker_record_id": "duplicate-record"},
                {"worker_record_id": "duplicate-record"},
            ],
        }
        target = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "worker_research_disposition_duplicate:duplicate-record",
            value,
        )[0]

        self.assertEqual(
            target["json_pointer"],
            "/worker_research_dispositions",
        )
        self.assertEqual(
            target["required_state"],
            "worker_research_disposition_unique",
        )
        self.assertFalse(_target_satisfied(value, target))
        value["worker_research_dispositions"].pop()
        self.assertTrue(_target_satisfied(value, target))

    def test_worker_authority_target_requires_reference_removal(self):
        value = {
            "decision": {
                "rests_on": ["worker-record-id"],
            },
        }
        target = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "worker_research_record_authority_forbidden:"
            "/decision/rests_on/0",
            value,
        )[0]

        self.assertEqual(target["json_pointer"], "/decision/rests_on/0")
        self.assertEqual(target["required_state"], "removed")

    def test_unexpected_repetition_review_targets_null(self):
        targets = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "decision_repetition_review_unexpected",
            {"decision": {"repetition_review": {"junk": True}}},
        )
        self.assertEqual(targets, [{
            "code": "decision_repetition_review_unexpected",
            "json_pointer": "/decision/repetition_review",
            "required_state": "null",
        }])

    def test_unknown_wait_question_targets_the_id_list(self):
        targets = _correction_targets(
            "ValueError: invalid_host_input:cycle.json:"
            "decision_repetition_unresolved_question_id_unknown:invented",
            {"decision": {"repetition_review": {
                "unresolved_question_ids": ["invented"],
            }}},
        )
        self.assertEqual(targets, [{
            "code": (
                "decision_repetition_unresolved_question_id_unknown:"
                "invented"
            ),
            "json_pointer": (
                "/decision/repetition_review/unresolved_question_ids"
            ),
            "required_state": "non_empty_string_list",
        }])

    def test_worker_disposition_row_satisfies_row_level_retry_target(self):
        value = {
            "worker_research_dispositions": [{
                "worker_record_id": "azure-a-sample",
                "disposition": "deferred",
                "evidence": ["stage:research_director"],
                "rationale": "Lower current decision value.",
                "revisit_condition": "Fresh primary evidence arrives.",
            }],
        }
        target = {
            "json_pointer": "/worker_research_dispositions/0",
            "required_state": "worker_research_disposition_row",
        }

        self.assertTrue(_target_satisfied(value, target))

    def test_synthetic_semantic_case_reports_all_targets_in_one_pass(self):
        manifest = json.loads(
            SEMANTIC_PROBE_EXPECTED.read_text(encoding="utf-8")
        )
        source_name, case = max(
            manifest["cases"].items(),
            key=lambda item: len(item[1]["defects"]),
        )
        self.assertGreaterEqual(len(case["defects"]), 30)
        source = SEMANTIC_PROBE_DIR / source_name
        value = json.loads(source.read_text(encoding="utf-8"))
        value["semantic_input_schema_version"] = 1
        self.write(case["source_filename"], value)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertGreaterEqual(
            len(refusals[0]["correction_targets"]),
            30,
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertGreaterEqual(
            len(feedback["retry_contract"]["targets"]),
            30,
        )
        self.assertEqual(
            feedback["retry_contract"]["patch_base"]["path"],
            "schemas/host_semantic_v1.example.json",
        )
        self.assertEqual(
            feedback["retry_contract"]["patch_base"]["source_kind"],
            "schema_exemplar",
        )
        self.assertTrue(feedback["retry_contract"]["refused_source"]["repair_only"])
        self.assertIn(
            "copy refused_source.path",
            feedback["retry_contract"]["instruction"],
        )
        self.assertNotIn(
            "patch that exact semantic source",
            feedback["retry_contract"]["instruction"],
        )
        self.assertIn(
            "Compare preservation_manifest before committing",
            feedback["retry_contract"]["instruction"],
        )
        manifest = feedback["retry_contract"]["preservation_manifest"]
        self.assertIn("findings", manifest["required_top_level_keys"])
        self.assertIn(
            "research_director",
            manifest["required_core_stage_output_ids"],
        )
        self.assertEqual(
            manifest["required_stage_output_fields"],
            [
                "blockers",
                "confidence",
                "evidence_status",
                "next_actions",
                "observations",
                "status",
                "tools_used",
            ],
        )
        self.assertEqual(
            manifest["required_learning_disposition_stage_ids"],
            ["learning_audit", "meta_research", "self_improvement"],
        )
        self.assertTrue(manifest["require_selected_stage_outputs"])
        self.assertEqual(manifest["evidence_call_shape"], "flat")
        self.assertTrue(manifest["forbid_nested_call_wrapper"])
        self.assertIn(
            "stage_outputs",
            manifest["patch_base_top_level_keys"],
        )

    def test_retry_uses_schema_exemplar_without_an_accepted_source(self):
        invalid = semantic_candidate()
        invalid["cycle_id"] = "cycle-invalid-first"
        del invalid["evidence_calls"]
        self.write("cycle-invalid-first.semantic.json", invalid)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertIsNone(feedback["last_accepted_semantic_source"])
        patch_base = feedback["retry_contract"]["patch_base"]
        self.assertEqual(
            patch_base["path"],
            "schemas/host_semantic_v1.example.json",
        )
        self.assertFalse(patch_base["accepted"])
        self.assertEqual(patch_base["source_kind"], "schema_exemplar")
        self.assertTrue(patch_base["structural_template_only"])

    def test_retry_has_no_patch_base_without_known_good_source(self):
        invalid = semantic_candidate()
        invalid["cycle_id"] = "cycle-invalid-no-safe-base"
        del invalid["evidence_calls"]
        self.write("cycle-invalid-no-safe-base.semantic.json", invalid)

        with patch(
            "runtime.host_feedback._schema_exemplar_patch_base",
            return_value=None,
        ):
            promoted, refusals = process_staging(
                self.staging,
                self.inputs,
                records=[],
            )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        self.assertTrue(refusals[0]["archive"])
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertIsNone(feedback["last_accepted_semantic_source"])
        self.assertIsNone(feedback["retry_contract"])

    def test_feedback_names_last_accepted_semantic_patch_base(self):
        self.write(
            "cycle-semantic.semantic.json",
            semantic_candidate(),
        )
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )
        self.assertEqual(promoted, ["cycle-semantic.json"])
        self.assertEqual(refusals, [])

        invalid = semantic_candidate()
        invalid["cycle_id"] = "cycle-invalid-next"
        del invalid["evidence_calls"]
        self.write("cycle-invalid-next.semantic.json", invalid)
        _, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )
        self.assertEqual(len(refusals), 1)

        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        source = feedback["last_accepted_semantic_source"]
        self.assertTrue(source["path"].startswith(
            "host_staging/accepted_sources/"
        ))
        self.assertEqual(source["cycle_id"], "cycle-semantic")
        self.assertEqual(len(source["sha256"]), 64)
        patch_base = feedback["retry_contract"]["patch_base"]
        self.assertEqual(patch_base["path"], source["path"])
        self.assertEqual(patch_base["sha256"], source["sha256"])
        self.assertTrue(patch_base["accepted"])
        self.assertEqual(
            patch_base["source_kind"],
            "accepted_semantic_source",
        )
        manifest = feedback["retry_contract"]["preservation_manifest"]
        self.assertIn(
            "evidence_calls",
            manifest["patch_base_top_level_keys"],
        )
        self.assertIn(
            "stage_outputs",
            manifest["required_top_level_keys"],
        )
        self.assertEqual(manifest["evidence_call_shape"], "nested")
        self.assertFalse(manifest["forbid_nested_call_wrapper"])

    def test_malformed_retry_uses_current_refusal_and_prior_comparison(self):
        self.write(
            "cycle-semantic.semantic.json",
            semantic_candidate(),
        )
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )
        self.assertEqual(promoted, ["cycle-semantic.json"])
        self.assertEqual(refusals, [])

        malformed = self.staging / "cycle-malformed.semantic.json"
        malformed.write_text(
            '{"semantic_input_schema_version":1,"cycle_id":',
            encoding="utf-8",
        )
        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        retry = feedback["retry_contract"]
        self.assertEqual(retry["targets"], [])
        self.assertEqual(retry["must_change_paths"], [])
        self.assertEqual(
            retry["patch_base"]["source_kind"],
            "accepted_semantic_source",
        )
        self.assertEqual(
            retry["refused_source"]["candidate_id"],
            refusals[0]["candidate_id"],
        )
        self.assertEqual(
            retry["semantic_patch"]["base_candidate_id"],
            refusals[0]["candidate_id"],
        )
        self.assertIn("strict parsing", retry["instruction"])

    def test_semantic_candidate_promotes_built_bytes_and_archives_source(self):
        semantic = semantic_candidate()
        source = self.write(
            "cycle-semantic.semantic.json",
            semantic,
        )
        source_bytes = source.read_bytes()
        expected_bytes = build_semantic_candidate(
            semantic,
            filename=source.name,
        ).canonical_bytes

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, ["cycle-semantic.json"])
        self.assertEqual(refusals, [])
        target = self.inputs / "cycle-semantic.json"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), expected_bytes)
        self.assertEqual(
            marker_path(self.inputs, target.name).read_text().strip(),
            content_sha256(target),
        )
        archived = list(
            path for path in (
                self.staging / "accepted_sources"
            ).glob("cycle-semantic.semantic-*.json")
            if not path.name.endswith(".build.json")
        )
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), source_bytes)
        metadata = json.loads(
            archived[0].with_suffix(
                archived[0].suffix + ".build.json"
            ).read_text()
        )
        self.assertEqual(metadata["canonical_filename"], target.name)
        self.assertEqual(metadata["builder_version"], 2)

    def test_semantic_candidate_never_overwrites_derived_target(self):
        target = self.inputs / "cycle-semantic.json"
        target.write_text("canonical", encoding="utf-8")
        self.write(
            "cycle-semantic.semantic.json",
            semantic_candidate(),
        )

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "staged_input_filename_collision:cycle-semantic.json",
            refusals[0]["reason"],
        )
        self.assertEqual(target.read_text(), "canonical")

    def test_canonical_v4_with_semantic_suffix_requires_semantic_v1(self):
        value = post_effective_full_cycle(
            cycle_id="cycle-canonical-alias",
        )
        self.write(
            "cycle-canonical-alias.semantic.json",
            value,
        )

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(
            refusals[0]["reason"],
            "ValueError: invalid_host_input:"
            "cycle-canonical-alias.semantic.json:"
            "semantic_input_schema_version_required",
        )
        expected_target = {
            "code": "semantic_input_schema_version_required",
            "json_pointer": "/semantic_input_schema_version",
            "required_state": "semantic_builder_valid",
        }
        self.assertEqual(
            refusals[0]["correction_targets"],
            [expected_target],
        )
        process_staging(
            self.staging,
            self.inputs,
            records=[],
            refresh_feedback=True,
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertEqual(
            feedback["retry_contract"]["targets"],
            [expected_target],
        )
        self.assertEqual(
            feedback["retry_contract"]["patch_base"]["path"],
            "schemas/host_semantic_v1.example.json",
        )
        self.assertEqual(
            feedback["retry_contract"]["patch_base"]["source_kind"],
            "schema_exemplar",
        )
        self.assertEqual(
            [row["code"] for row in feedback["refused"][0]["what_to_fix"]],
            ["semantic_input_schema_version_required"],
        )
        fix = feedback["refused"][0]["what_to_fix"][0]["fix"]
        self.assertIn("schemas/host_semantic_v1.example.json", fix)
        self.assertIn("Do not emit cognitive_stages", fix)

    def test_versionless_semantic_reports_downstream_errors_without_promoting(
        self,
    ):
        semantic = semantic_candidate()
        semantic["cycle_id"] = "cycle-versionless-semantic"
        semantic.pop("semantic_input_schema_version")
        semantic["market_scout_report"]["budget"]["external_searches"] = 0
        source = self.write("cycle-versionless.semantic.json", semantic)
        original = source.read_bytes()

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        reason = refusals[0]["reason"]
        self.assertIn("semantic_input_schema_version_required", reason)
        self.assertIn("market_scout_budget_variance_invalid:required", reason)
        self.assertFalse(
            (self.inputs / "cycle-versionless.json").exists()
        )
        archive = self.staging / "rejected" / refusals[0]["archive"]
        self.assertEqual(archive.read_bytes(), original)
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertIn(
            "/semantic_input_schema_version",
            feedback["retry_contract"]["must_change_paths"],
        )
        self.assertIn(
            "/market_scout_report",
            feedback["retry_contract"]["must_change_paths"],
        )

    def test_conflicting_research_call_refuses_without_dropping_observations(
        self,
    ):
        semantic = semantic_candidate()
        research_call = semantic["research"][0]["tool_calls"][0]
        semantic["evidence_calls"].append({
            "producer": "option_research",
            "tool_call_id": research_call["tool_call_id"],
            "action": research_call["action"],
            "arguments": research_call["arguments"],
            "result": {"observations": [{"status": "DELAYED"}]},
            "observed_at": research_call["observed_at"],
        })
        source = self.write("cycle-conflicting-call.semantic.json", semantic)
        original = source.read_bytes()

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        reason = refusals[0]["reason"]
        self.assertIn("semantic_tool_call_id_conflict", reason)
        self.assertIn("semantic_evidence_target_missing", reason)
        self.assertIn("semantic_tool_call_missing", reason)
        self.assertEqual(
            (
                self.staging / "rejected" / refusals[0]["archive"]
            ).read_bytes(),
            original,
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertIn(
            "/evidence_calls/6/tool_call_id",
            feedback["retry_contract"]["must_change_paths"],
        )
        self.assertIn(
            "Preserve all genuine observations",
            str(feedback["refused"][0]["what_to_fix"]),
        )

    def test_corrected_semantic_v1_retry_promotes(self):
        canonical = post_effective_full_cycle(
            cycle_id="cycle-canonical-alias",
        )
        self.write("first.semantic.json", canonical)
        _, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        corrected = semantic_candidate()
        corrected["cycle_id"] = "cycle-semantic-correction"
        corrected["corrects_candidate_id"] = refusals[0]["candidate_id"]
        self.write("second.semantic.json", corrected)
        promoted, second_refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, ["second.json"])
        self.assertEqual(second_refusals, [])

    def test_canonical_v4_without_semantic_suffix_stays_byte_for_byte(self):
        semantic = semantic_candidate()
        semantic["cycle_id"] = "cycle-canonical-v4"
        value = build_semantic_candidate(
            semantic,
            filename="cycle-canonical-v4.semantic.json",
        ).canonical
        source = self.write("cycle-canonical-v4.json", value)
        source_bytes = source.read_bytes()

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, ["cycle-canonical-v4.json"])
        self.assertEqual(refusals, [])
        self.assertEqual(
            (self.inputs / "cycle-canonical-v4.json").read_bytes(),
            source_bytes,
        )

    def test_semantic_builder_error_points_to_semantic_source(self):
        semantic = semantic_candidate()
        del semantic["stage_outputs"]["adversarial"]["observations"]
        self.write("cycle-semantic.semantic.json", semantic)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(
            refusals[0]["correction_targets"][0]["json_pointer"],
            "/stage_outputs/adversarial/observations",
        )

    def test_canonical_error_maps_back_to_semantic_agenda(self):
        semantic = semantic_candidate()
        del semantic["research_agenda"]["candidates"][0]["trigger"]
        self.write("cycle-semantic.semantic.json", semantic)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        pointers = {
            target["json_pointer"]
            for target in refusals[0]["correction_targets"]
        }
        self.assertIn(
            "/research_agenda/candidates/0/trigger",
            pointers,
        )

    def test_semantic_dense_line_is_reformatted_after_parse(self):
        semantic = semantic_candidate()
        source = self.staging / "cycle-dense.semantic.json"
        source.write_text(json.dumps(semantic), encoding="utf-8")
        source_bytes = source.read_bytes()

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, ["cycle-dense.json"])
        self.assertEqual(refusals, [])
        archived = next(
            path for path in (
                self.staging / "accepted_sources"
            ).glob("cycle-dense.semantic-*.json")
            if not path.name.endswith(".build.json")
        )
        self.assertEqual(archived.read_bytes(), source_bytes)
        metadata = json.loads(
            archived.with_suffix(
                archived.suffix + ".build.json"
            ).read_text()
        )
        self.assertTrue(metadata["source_reformatted"])
        self.assertGreater(metadata["source_longest_line_chars"], 1000)

    def test_malformed_semantic_dense_line_prioritizes_parser_error(self):
        source = self.staging / "cycle-dense.semantic.json"
        dense_prefix = '      "dense_observation": "'
        dense_suffix = '",'
        dense_line = (
            dense_prefix
            + ("x" * (3031 - len(dense_prefix) - len(dense_suffix)))
            + dense_suffix
        )
        lines = [
            "{",
            '  "semantic_input_schema_version": 1,',
            '  "research": [',
            "    {",
            dense_line,
            *[
                f'      "synthetic_{index:02d}": "'
                + ("x" * (110 if index < 65 else 141))
                + '",'
                for index in range(66)
            ],
            "    }",
        ]
        self.assertEqual(len(lines), 72)
        source.write_text(
            "\n".join(lines),
            encoding="utf-8",
        )

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        reason = refusals[0]["reason"]
        self.assertTrue(reason.startswith(
            "JSONDecodeError: Expecting property name enclosed "
            "in double quotes;"
        ))
        for detail in (
            "line=72",
            "column=5",
            "char=12105",
            "open_depth=3",
            "open_containers=",
            "context=",
        ):
            self.assertIn(detail, reason)
        self.assertNotIn("semantic_json_line_too_long", reason)
        line_target = {
            "code": "semantic_json_line_too_long",
            "json_pointer": "/",
            "required_state": "semantic_builder_valid",
            "detail": "3031>1000",
        }
        self.assertEqual(
            refusals[0]["correction_targets"],
            [line_target],
        )
        rejection = json.loads(
            (self.staging / "rejected" / "REJECTIONS.jsonl")
            .read_text()
            .splitlines()[-1]
        )
        self.assertEqual(rejection["codes"], ["malformed_json"])
        self.assertEqual(rejection["correction_targets"], [line_target])

        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        parser_fix = feedback["refused"][0]["what_to_fix"][0]
        self.assertEqual(parser_fix["code"], "malformed_json")
        for detail in (
            "line=72",
            "column=5",
            "char=12105",
            "open_depth=3",
            "open_containers=",
            "context=",
        ):
            self.assertIn(detail, parser_fix["detail"])
        self.assertEqual(
            feedback["retry_contract"]["targets"],
            [line_target],
        )
        self.assertEqual(
            feedback["retry_contract"]["patch_base"]["source_kind"],
            "schema_exemplar",
        )

    def test_duplicate_key_retry_preserves_all_predecode_targets(self):
        semantic = semantic_candidate()
        source = self.staging / "cycle-duplicate.semantic.json"
        compact = json.dumps(semantic, separators=(",", ":"))
        source.write_text(
            compact[:-1]
            + ',"findings":[{"id":"duplicate","statement":"conflict"}]}',
            encoding="utf-8",
        )

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "duplicate_json_key:findings",
            refusals[0]["reason"],
        )
        candidate_id = refusals[0]["candidate_id"]
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        targets = feedback["retry_contract"]["targets"]
        self.assertEqual(
            [target["code"] for target in targets],
            [
                "duplicate_json_key",
                "semantic_json_line_too_long",
            ],
        )
        self.assertTrue(all(
            target["json_pointer"] == "/"
            and target["required_state"] == "semantic_builder_valid"
            for target in targets
        ))
        self.assertEqual(targets[0]["detail"], "findings")

        process_staging(
            self.staging,
            self.inputs,
            records=[],
            refresh_feedback=True,
        )
        refreshed = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertEqual(
            refreshed["retry_contract"]["targets"],
            targets,
        )

        corrected = semantic_candidate()
        corrected["cycle_id"] = "cycle-duplicate-corrected"
        corrected["corrects_candidate_id"] = candidate_id
        self.write("cycle-duplicate-corrected.semantic.json", corrected)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, ["cycle-duplicate-corrected.json"])
        self.assertEqual(refusals, [])

    def test_bound_projection_supersedes_stale_worker_retry_targets(self):
        """v2r108: a retry bound to the new worker projection was refused
        because the parent's feedback demanded a disposition for a worker
        record the current projection no longer requires. Record-scoped
        worker targets outside the bound projection are superseded;
        targets for records still in the projection stay enforced.
        """
        parent_id = (
            "cycle-20260925T235546Z-v2r107.semantic.json@sha256:" + "a6" * 32
        )
        history = [{
            "candidate_id": parent_id,
            "input": "cycle-20260925T235546Z-v2r107.semantic.json",
            "cycle_id": "cycle-20260925T235546Z-v2r107",
            "correction_targets": [
                {
                    "code": (
                        "worker_research_disposition_missing:"
                        "azure-a-20260925T182004Z"
                    ),
                    "json_pointer": "/worker_research_dispositions",
                    "required_state": "worker_research_disposition_for_record",
                },
                {
                    "code": (
                        "worker_research_disposition_missing:"
                        "azure-b-20260925T234309Z"
                    ),
                    "json_pointer": "/worker_research_dispositions",
                    "required_state": "worker_research_disposition_for_record",
                },
            ],
        }]
        retry = {
            "cycle_id": "cycle-20260926T003300Z-v2r108",
            "corrects_candidate_id": parent_id,
            "worker_research_projection_id": (
                "worker-research-projection:v1:" + "c1" * 32
            ),
            "worker_research_dispositions": [],
        }
        kwargs = {
            "input_name": "cycle-20260926T003300Z-v2r108.semantic.json",
            "history": history,
            "candidate_id": (
                "cycle-20260926T003300Z-v2r108.semantic.json@sha256:"
                + "0" * 64
            ),
        }
        stale_code = (
            "retry_target_unsatisfied:/worker_research_dispositions|"
            "worker_research_disposition_for_record"
        )
        with patch(
            "runtime.staged_intake.load_worker_projection",
            return_value=[{"record_id": "azure-b-20260925T234309Z"}],
        ):
            codes = _retry_preflight_codes_for_value(retry, **kwargs)
            self.assertEqual(codes.count(stale_code), 1)
            retry["worker_research_dispositions"] = [
                {"worker_record_id": "azure-b-20260925T234309Z"},
            ]
            codes = _retry_preflight_codes_for_value(retry, **kwargs)
            self.assertNotIn(stale_code, codes)

        unbound = dict(retry)
        del unbound["worker_research_projection_id"]
        codes = _retry_preflight_codes_for_value(unbound, **kwargs)
        self.assertIn(stale_code, codes)

    def test_a_fresh_cycle_is_not_blocked_by_an_outstanding_refusal(self):
        """An unrepairable refusal must not wedge the producer forever.

        v2r93 was refused, the host read "recovery is mandatory", and then
        staged nothing for two days. Intake never actually required that
        repair: a fresh cycle carrying no corrects_candidate_id has no retry
        targets and no lineage edge. This pins that, so the escape added to
        the standing prompt stays truthful about what intake accepts.
        """
        history = [{
            "candidate_id": (
                "cycle-20260924T105901Z-v2r93.semantic.json@sha256:"
                + "0b" * 32
            ),
            "input": "cycle-20260924T105901Z-v2r93.semantic.json",
            "cycle_id": "cycle-20260924T105901Z-v2r93",
            "correction_targets": [{
                "code": "semantic_top_level_missing",
                "json_pointer": "/evidence_calls",
                "required_state": "semantic_builder_valid",
                "detail": "evidence_calls",
            }],
        }]
        fresh = {"cycle_id": "cycle-20260925T180000Z-v2r102"}
        self.assertNotIn("corrects_candidate_id", fresh)

        codes = _retry_preflight_codes_for_value(
            fresh,
            input_name="cycle-20260925T180000Z-v2r102.semantic.json",
            history=history,
            candidate_id=(
                "cycle-20260925T180000Z-v2r102.semantic.json@sha256:"
                + "0" * 64
            ),
            builder_succeeded=True,
        )
        self.assertEqual(codes, [])
        self.assertEqual(
            retry_lineage_errors(
                fresh,
                refusals=history,
                candidate_id=(
                    "cycle-20260925T180000Z-v2r102.semantic.json@sha256:"
                    + "0" * 64
                ),
            ),
            [],
        )

    def test_v78_duplicate_dispositions_repair_copies_immutable_archive(self):
        from .host_feedback import _retry_refused_source
        from .host_input_validator import (
            DuplicateJsonKeyError,
            decode_json,
        )

        semantic = semantic_candidate()
        semantic["worker_research_dispositions"] = []
        compact = json.dumps(semantic, separators=(",", ":"))
        source = self.staging / "cycle-v78.semantic.json"
        source.write_text(
            compact[:-1] + ',"worker_research_dispositions":[]}',
            encoding="utf-8",
        )

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "duplicate_json_key:worker_research_dispositions",
            refusals[0]["reason"],
        )
        candidate_id = refusals[0]["candidate_id"]
        archived = self.staging / "rejected" / refusals[0]["archive"]
        immutable_bytes = archived.read_bytes()
        with self.assertRaises(DuplicateJsonKeyError):
            decode_json(immutable_bytes.decode("utf-8"))
        feedback = json.loads((self.staging / "FEEDBACK.json").read_text())
        contract = feedback["retry_contract"]
        refused_source = contract["refused_source"]
        self.assertEqual(
            refused_source["sha256"],
            hashlib.sha256(immutable_bytes).hexdigest(),
        )
        self.assertFalse(refused_source["accepted"])
        self.assertTrue(refused_source["repair_only"])
        self.assertIn("copy refused_source.path", contract["instruction"])
        self.assertIn("feedback publisher verified", contract["instruction"])
        self.assertIn("no host-side SHA-256 tool", contract["instruction"])
        self.assertNotIn("Verify refused_source.sha256", contract["instruction"])
        self.assertIn("never edit the archive", contract["instruction"])
        self.assertNotIn(
            "patch that exact semantic source", contract["instruction"],
        )
        self.assertIn("copy", refused_source["instruction"].lower())
        self.assertNotIn("edit this file in place", refused_source["instruction"])

        prefix, separator, suffix = immutable_bytes.decode("utf-8").rpartition(
            ',"worker_research_dispositions":[]}'
        )
        self.assertTrue(separator)
        self.assertEqual(suffix, "")
        corrected = decode_json(prefix + "}")
        corrected["cycle_id"] = "cycle-v78-corrected"
        corrected["corrects_candidate_id"] = candidate_id
        self.write("cycle-v78-corrected.semantic.json", corrected)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[],
        )

        self.assertEqual(promoted, ["cycle-v78-corrected.json"])
        self.assertEqual(refusals, [])
        self.assertEqual(archived.read_bytes(), immutable_bytes)

        archived.write_bytes(immutable_bytes + b"\n")
        with self.assertRaisesRegex(
            ValueError, "refused_source_archive_digest_mismatch",
        ):
            _retry_refused_source(
                self.staging,
                {
                    "input": source.name,
                    "archive": archived.name,
                    "candidate_id": candidate_id,
                    "sha256": hashlib.sha256(immutable_bytes).hexdigest(),
                },
                [{"code": "duplicate_json_key"}],
            )

        tampered_retry = dict(corrected)
        tampered_retry["cycle_id"] = "cycle-v78-tampered-retry"
        self.write("cycle-v78-tampered-retry.semantic.json", tampered_retry)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[],
        )
        self.assertEqual(promoted, [])
        self.assertIn(
            "retry_lineage_archive_digest_mismatch",
            refusals[0]["reason"],
        )

    def test_parseable_v79_semantic_refusal_reuses_current_cycle_copy(self):
        from .host_input_validator import decode_json

        semantic = semantic_candidate()
        semantic["cycle_id"] = "cycle-v79-current"
        semantic["worker_research_dispositions"] = []
        semantic["market_scout_report"]["tool_calls"][0][
            "capture_origin"
        ] = "connector_response"
        source = self.staging / "cycle-v79.semantic.json"
        source.write_text(json.dumps(semantic, indent=2) + "\n", encoding="utf-8")

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[],
        )

        self.assertEqual(promoted, [])
        self.assertIn("semantic_capture_origin", refusals[0]["reason"])
        archive = self.staging / "rejected" / refusals[0]["archive"]
        original = archive.read_bytes()
        feedback = json.loads((self.staging / "FEEDBACK.json").read_text())
        contract = feedback["retry_contract"]
        self.assertEqual(
            contract["refused_source"]["sha256"],
            hashlib.sha256(original).hexdigest(),
        )
        self.assertIn("copy refused_source.path", contract["instruction"])
        self.assertIn("structural comparison", contract["instruction"])
        self.assertNotIn(
            "patch that exact semantic source", contract["instruction"],
        )

        corrected = decode_json(original.decode("utf-8"))
        corrected["cycle_id"] = "cycle-v79-corrected"
        corrected["corrects_candidate_id"] = refusals[0]["candidate_id"]
        corrected["market_scout_report"]["tool_calls"][0][
            "capture_origin"
        ] = "host_summary"
        self.write("cycle-v79-corrected.semantic.json", corrected)

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[],
        )

        self.assertEqual(promoted, ["cycle-v79-corrected.json"])
        self.assertEqual(refusals, [])
        self.assertEqual(archive.read_bytes(), original)

    def test_unparseable_semantic_refusal_exposes_verified_repair_source(self):
        source = self.staging / "cycle-broken.semantic.json"
        source.write_text('{"semantic_input_schema_version":1,', encoding="utf-8")

        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        feedback = json.loads((self.staging / "FEEDBACK.json").read_text())
        repair = feedback["retry_contract"]["refused_source"]
        self.assertEqual(
            repair["sha256"],
            hashlib.sha256(
                (self.root / repair["path"]).read_bytes()
            ).hexdigest(),
        )
        self.assertTrue(repair["repair_only"])
        self.assertIn(
            "lexical_edits_if_malformed",
            feedback["retry_contract"]["semantic_patch"],
        )

    def test_duplicate_key_predecode_surfaces_latent_semantic_targets(self):
        semantic = semantic_candidate()
        existing_call = dict(semantic["evidence_calls"][0]["call"])
        existing_call.pop("tool", None)
        semantic["evidence_calls"].append({
            "producer": "web_research_alpha",
            "call": dict(existing_call),
        })
        semantic["evidence_calls"].append({
            "producer": "web_research_beta",
            "call": dict(existing_call),
        })
        source = self.staging / "cycle-latent.semantic.json"
        compact = json.dumps(semantic, separators=(",", ":"))
        source.write_text(
            compact[:-1]
            + ',"findings":[{"id":"duplicate","statement":"conflict"}]}',
            encoding="utf-8",
        )

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "duplicate_json_key:findings",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        targets = feedback["retry_contract"]["targets"]
        codes_with_pointers = [
            (target["code"], target["json_pointer"])
            for target in targets
        ]
        self.assertIn(("duplicate_json_key", "/"), codes_with_pointers)
        self.assertIn(
            ("semantic_tool_call_missing", "/evidence_calls/6/call/tool"),
            codes_with_pointers,
        )
        self.assertIn(
            ("semantic_tool_call_missing", "/evidence_calls/7/call/tool"),
            codes_with_pointers,
        )
        refused_source = feedback["retry_contract"].get("refused_source")
        self.assertIsNotNone(refused_source)
        self.assertTrue(refused_source["path"].startswith(
            f"{self.staging.name}/rejected/"
        ))
        self.assertEqual(refused_source["accepted"], False)
        self.assertEqual(refused_source["repair_only"], True)
        archive_name = refused_source["path"].rsplit("/", 1)[1]
        archived_path = self.staging / "rejected" / archive_name
        self.assertTrue(archived_path.is_file())
        self.assertEqual(
            hashlib.sha256(archived_path.read_bytes()).hexdigest(),
            refused_source["sha256"],
        )

    def test_conflicting_duplicate_scalar_stays_unprobeable(self):
        semantic = semantic_candidate()
        source = self.staging / "cycle-conflict.semantic.json"
        compact = json.dumps(semantic, separators=(",", ":"))
        source.write_text(
            compact[:-1] + ',"cycle_id":"cycle-conflict-different"}',
            encoding="utf-8",
        )

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "duplicate_json_key:cycle_id",
            refusals[0]["reason"],
        )
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        targets = feedback["retry_contract"]["targets"]
        codes = {target["code"] for target in targets}
        self.assertIn("duplicate_json_key", codes)
        self.assertNotIn("semantic_tool_call_missing", codes)

    def test_malformed_semantic_recovers_top_level_schedule_context(self):
        semantic = semantic_candidate()
        semantic["cycle_id"] = "cycle-20260923T005700Z-context"
        semantic["schedule_context"] = {
            "schema_version": 1,
            "task_id": "Sovereign Research IBKR hourly v2",
            "platform_run_id": semantic["cycle_id"],
            "expected_slot": "2026-09-23T00:57:00+00:00",
            "started_at": "2026-09-23T00:57:00+00:00",
            "source_observed_at": "2026-09-23T00:57:00+00:00",
            "trigger": "scheduled",
            "intervention": "none",
        }
        source = self.staging / "cycle-context.semantic.json"
        text = json.dumps(semantic, indent=2)
        malformed = text[:-2] + ',\n  BROKEN\n}'
        source.write_text(malformed, encoding="utf-8")

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        rejection = json.loads(
            (self.staging / "rejected" / "REJECTIONS.jsonl")
            .read_text()
            .splitlines()[-1]
        )
        self.assertEqual(
            rejection["cycle_id"],
            "cycle-20260923T005700Z-context",
        )
        self.assertEqual(
            rejection["schedule_context"],
            semantic["schedule_context"],
        )
        archived = self.staging / "rejected" / rejection["archive"]
        self.assertEqual(
            archived.read_text(encoding="utf-8"),
            malformed,
        )

    def test_semantic_retry_lineage_survives_new_name_and_cycle_id(self):
        first = semantic_candidate()
        del first["research_agenda"]["candidates"][0]["trigger"]
        self.write("first.semantic.json", first)
        _, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )
        candidate_id = refusals[0]["candidate_id"]

        corrected = semantic_candidate()
        corrected["cycle_id"] = "cycle-semantic-correction"
        corrected["corrects_candidate_id"] = candidate_id
        self.write("second.semantic.json", corrected)
        promoted, second_refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, ["second.json"])
        self.assertEqual(second_refusals, [])
        canonical = json.loads(
            (self.inputs / "second.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            canonical["corrects_candidate_id"],
            candidate_id,
        )
        metadata = next(
            (
                path for path in (
                    self.staging / "accepted_sources"
                ).glob("second.semantic-*.json.build.json")
            ),
        )
        self.assertEqual(
            json.loads(metadata.read_text())["corrects_candidate_id"],
            candidate_id,
        )

    def test_retry_lineage_requires_an_existing_refusal(self):
        candidate = semantic_candidate()
        candidate["corrects_candidate_id"] = (
            "missing.semantic.json@sha256:" + "a" * 64
        )
        self.write("bad-reference.semantic.json", candidate)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertIn(
            "retry_lineage_reference_missing:",
            refusals[0]["reason"],
        )

    def test_retry_lineage_rejects_self_reference(self):
        candidate_id = "self.semantic.json@sha256:" + "a" * 64
        value = {"corrects_candidate_id": candidate_id}

        codes = retry_lineage_errors(
            value,
            refusals=[],
            candidate_id=candidate_id,
        )

        self.assertEqual(codes, ["retry_lineage_self_reference"])

    def test_retry_lineage_stops_at_combined_invalid_ancestor(self):
        parent = "parent.semantic.json@sha256:" + "a" * 64
        codes = retry_lineage_errors(
            {"corrects_candidate_id": parent},
            refusals=[{
                "candidate_id": parent,
                "codes": [
                    "semantic_candidate_invalid:semantic_capture_origin|"
                    "/research/0/tool_calls/0/capture_origin|bad,"
                    "retry_lineage_reference_missing:older",
                ],
            }],
            candidate_id=(
                "child.semantic.json@sha256:" + "b" * 64
            ),
        )

        self.assertEqual(codes, [])

    def test_retry_lineage_rejects_existing_cycle(self):
        first = "first.semantic.json@sha256:" + "a" * 64
        second = "second.semantic.json@sha256:" + "b" * 64
        history = [
            {
                "candidate_id": first,
                "corrects_candidate_id": second,
                "codes": [],
            },
            {
                "candidate_id": second,
                "corrects_candidate_id": first,
                "codes": [],
            },
        ]

        codes = retry_lineage_errors(
            {"corrects_candidate_id": first},
            refusals=history,
            candidate_id="third.semantic.json@sha256:" + "c" * 64,
        )

        self.assertEqual(codes, [f"retry_lineage_cycle:{first}"])

    def test_privacy_refusal_erases_candidate_but_keeps_ledger_digest(self):
        value = sample_input(cycle_id="cycle-secret-erasure")
        call = value["research"][0]["tool_calls"][0]
        call["provenance"]["capture"].update({
            "schema_version": 2,
            "capture_origin": "direct_connector_response",
            "request_redactions": [],
            "reconstruction_status": "exact_response",
        })
        call["call"]["arguments"] = {
            "api_key": "sk-abcdefghijklmnop",
        }
        source = self.write("cycle-secret.json", value)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        self.assertTrue(refusals[0]["erased"])
        self.assertFalse(source.exists())
        self.assertFalse(list(
            (self.staging / "rejected").glob("cycle-secret-*.json")
        ))
        ledger = [
            json.loads(line)
            for line in (
                self.staging / "rejected" / "REJECTIONS.jsonl"
            ).read_text().splitlines()
        ]
        self.assertTrue(ledger[-1]["erased"])
        self.assertEqual(
            ledger[-1]["erasure_reason"],
            "unredacted_credential",
        )

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

    def write_schedule_contract(self):
        runs = self.root / "runs"
        runs.mkdir()
        (runs / "SCHEDULE.json").write_text(
            json.dumps({
                "schema_version": 1,
                "enabled": True,
                "task_id": "Sovereign Research IBKR hourly v2",
                "task_name": "Sovereign Research IBKR hourly v2",
                "timezone": "Europe/Prague",
                "cadence_minutes": 60,
                "anchor_at": "2026-09-20T12:57:00Z",
                "reliability_gate_activation_at": (
                    "2026-09-23T14:57:00Z"
                ),
                "grace_minutes": 15,
                "source_max_age_minutes": 30,
                "accounting_window_hours": 48,
                "min_workflow_version": 2,
                "effective_core_commit": "a" * 40,
                "effective_prompt_sha256": "b" * 64,
                "effective_host_input_schema_version": 1,
            }),
            encoding="utf-8",
        )

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

    def test_refresh_reprobes_latest_archived_semantic_candidate(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        matches = list(
            (root / "host_staging" / "rejected").glob(
                "*r112x9*.json"
            )
        )
        if not matches:
            self.skipTest("profile rejection corpus not present")
        source = matches[0]
        rejected = self.staging / "rejected"
        rejected.mkdir()
        archive = source.name
        (rejected / archive).write_bytes(source.read_bytes())
        input_name = (
            source.name.split(".semantic-", 1)[0]
            + ".semantic.json"
        )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        candidate_id = f"{input_name}@sha256:{digest}"
        event = {
            "candidate_id": candidate_id,
            "input": input_name,
            "sha256": digest,
            "archive": archive,
            "refused_at": "2026-09-20T03:49:50Z",
            "codes": [
                "semantic_candidate_invalid:"
                "semantic_top_level_missing|"
                "/learning_stage_dispositions|"
                "learning_stage_dispositions"
            ],
            "correction_targets": [{
                "code": "semantic_top_level_missing",
                "json_pointer": "/learning_stage_dispositions",
                "required_state": "semantic_builder_valid",
            }],
        }
        (rejected / "REJECTIONS.jsonl").write_text(
            json.dumps(event) + "\n",
            encoding="utf-8",
        )
        (self.staging / "FEEDBACK.json").write_text(
            json.dumps({
                "refused": [{
                    "input": input_name,
                    "reason": (
                        "ValueError: invalid_host_input:"
                        f"{input_name}:semantic_candidate_invalid:"
                        "semantic_top_level_missing|"
                        "/learning_stage_dispositions|"
                        "learning_stage_dispositions"
                    ),
                    "candidate_id": candidate_id,
                    "archive": archive,
                    "what_to_fix": [],
                }],
                "retry_contract": {
                    "must_change_paths": [
                        "/learning_stage_dispositions"
                    ],
                },
            }),
            encoding="utf-8",
        )

        process_staging(
            self.staging,
            self.inputs,
            records=[],
            refresh_feedback=True,
        )

        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertGreaterEqual(
            len(feedback["retry_contract"]["targets"]),
            30,
        )

    def test_refresh_revalidates_built_archived_semantic_candidate(self):
        semantic = semantic_candidate()
        semantic["evidence_calls"].append({
            "producer": "market_scout",
            "tool_call_id": "scout-extra",
            "kind": "external_search",
            "tool": "web.search",
            "action": "web.search",
            "arguments": {"query": "fresh evidence"},
            "result": "Fresh evidence.",
            "observed_at": semantic["as_of"],
        })
        input_name = "cycle-refresh-built.semantic.json"
        body = json.dumps(semantic, indent=2) + "\n"
        digest = hashlib.sha256(body.encode()).hexdigest()
        archive = (
            "cycle-refresh-built.semantic-"
            f"{digest}.json"
        )
        rejected = self.staging / "rejected"
        rejected.mkdir()
        (rejected / archive).write_text(body, encoding="utf-8")
        candidate_id = f"{input_name}@sha256:{digest}"
        stale_target = {
            "code": "semantic_top_level_missing",
            "json_pointer": "/learning_stage_dispositions",
            "required_state": "semantic_builder_valid",
        }
        event = {
            "candidate_id": candidate_id,
            "input": input_name,
            "sha256": digest,
            "archive": archive,
            "refused_at": "2026-09-23T03:56:24Z",
            "codes": ["semantic_top_level_missing"],
            "correction_targets": [stale_target],
        }
        (rejected / "REJECTIONS.jsonl").write_text(
            json.dumps(event) + "\n",
            encoding="utf-8",
        )
        (self.staging / "FEEDBACK.json").write_text(
            json.dumps({
                "refused": [{
                    "input": input_name,
                    "reason": (
                        "ValueError: invalid_host_input:"
                        f"{input_name}:semantic_top_level_missing"
                    ),
                    "candidate_id": candidate_id,
                    "archive": archive,
                    "what_to_fix": [],
                }],
                "retry_contract": {
                    "targets": [stale_target],
                    "must_change_paths": [
                        "/learning_stage_dispositions"
                    ],
                },
            }),
            encoding="utf-8",
        )

        process_staging(
            self.staging,
            self.inputs,
            records=[],
            refresh_feedback=True,
        )

        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        target_codes = {
            target["code"]
            for target in feedback["retry_contract"]["targets"]
        }
        self.assertTrue(
            any(
                (
                    code.startswith("evidence_call_invalid:")
                    and code.endswith(":producer")
                )
                or code == "semantic_evidence_producer_invalid"
                for code in target_codes
            ),
            target_codes,
        )
        self.assertNotIn("semantic_top_level_missing", target_codes)

    def test_semantic_refusal_unions_schema_schedule_and_lineage_targets(self):
        self.write_schedule_contract()
        parent = semantic_candidate()
        parent["cycle_id"] = "cycle-20260923T145706Z-v2r66"
        parent["schedule_context"] = {
            "schema_version": 1,
            "task_id": "Sovereign Research IBKR hourly v2",
            "platform_run_id": parent["cycle_id"],
            "expected_slot": "2026-09-23T14:57:00Z",
            "started_at": "2026-09-23T14:57:06Z",
            "source_observed_at": "2026-09-23T14:57:06Z",
            "trigger": "scheduled",
            "intervention": "none",
        }
        del parent["learning_stage_dispositions"]
        parent_name = "cycle-20260923T145706Z-v2r66.semantic.json"
        self.write(parent_name, parent)
        _, parent_refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )
        self.assertEqual(len(parent_refusals), 1)

        child = semantic_candidate()
        child["cycle_id"] = "cycle-20260923T165706Z-v2r67"
        child["corrects_candidate_id"] = parent_name
        child["schedule_context"] = {
            "schema_version": 1,
            "task_id": "Sovereign Research IBKR hourly v2",
            "platform_run_id": child["cycle_id"],
            "expected_slot": "2026-09-23T16:57:00Z",
            "started_at": "2026-09-23T14:57:06Z",
            "source_observed_at": "2026-09-23T14:57:06Z",
            "trigger": "scheduled",
            "intervention": "none",
        }
        del child["learning_stage_dispositions"]
        child_name = "cycle-20260923T165706Z-v2r67.semantic.json"
        self.write(child_name, child)

        promoted, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        self.assertEqual(promoted, [])
        self.assertEqual(len(refusals), 1)
        reason = refusals[0]["reason"]
        for code in (
            "semantic_candidate_invalid:semantic_top_level_missing",
            "schedule_context_expected_slot_mismatch",
            f"retry_lineage_reference_missing:{parent_name}",
        ):
            self.assertIn(code, reason)
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        targets = {
            (
                target["code"].split(":", 1)[0],
                target["json_pointer"],
            )
            for target in feedback["retry_contract"]["targets"]
        }
        expected = {
            (
                "semantic_top_level_missing",
                "/learning_stage_dispositions",
            ),
            (
                "schedule_context_expected_slot_mismatch",
                "/schedule_context",
            ),
            (
                "retry_lineage_reference_missing",
                "/corrects_candidate_id",
            ),
        }
        self.assertTrue(expected <= targets, targets)
        lineage_fix = next(
            row
            for row in feedback["refused"][0]["what_to_fix"]
            if row["code"] == "retry_lineage_reference_missing"
        )
        self.assertIn("@sha256", lineage_fix["fix"])

        process_staging(
            self.staging,
            self.inputs,
            records=[],
            refresh_feedback=True,
        )
        refreshed = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        refreshed_targets = {
            (
                target["code"].split(":", 1)[0],
                target["json_pointer"],
            )
            for target in refreshed["retry_contract"]["targets"]
        }
        self.assertTrue(expected <= refreshed_targets, refreshed_targets)

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

    def test_retry_is_bound_to_all_previous_correction_targets(self):
        candidate = self.staging / "cycle-sample-retry.json"
        first = retry_union_candidate()
        candidate.write_text(json.dumps(first, indent=2), encoding="utf-8")
        process_staging(self.staging, self.inputs, records=[])
        second = retry_union_candidate(additional_defect=True)
        candidate.write_text(json.dumps(second, indent=2), encoding="utf-8")
        _, refusals = process_staging(self.staging, self.inputs, records=[])
        reason = refusals[0]["reason"]
        director_index = next(
            index
            for index, stage in enumerate(second["cognitive_stages"])
            if stage["stage_id"] == "research_director"
        )
        repeated_targets = {
            "/lessons/0/falsified_if",
            "/memory_distillation/memory_objects/0/claim_ids",
            "/tool_manifest_report/connectors",
            (
                f"/cognitive_stages/{director_index}/output/"
                "research_agenda"
            ),
        }
        for pointer in repeated_targets:
            self.assertIn(
                f"retry_target_unsatisfied:{pointer}|",
                reason,
            )

        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text())
        recurrence = feedback["refusal_recurrence"]
        for code in (
            "lesson_missing_falsified_if",
            "memory_object_invalid",
            "research_agenda_invalid",
            "tool_manifest_connectors_must_be_nonempty_list",
        ):
            self.assertEqual(recurrence["counts_by_code"][code], 2)
        self.assertEqual(
            set(feedback["retry_contract"]["must_change_paths"]),
            repeated_targets | {
                "/learning_stage_dispositions",
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

    def test_builder_blocker_does_not_falsely_fail_canonical_evidence(self):
        from .evidence_coverage import validate_evidence_coverage

        value = semantic_candidate()
        canonical = build_semantic_candidate(
            value, filename="cycle-valid.semantic.json",
        ).canonical
        self.assertFalse([
            code for code in validate_evidence_coverage(canonical)
            if code.startswith("evidence_call_invalid:4:")
        ])
        self.assertTrue(any(
            code.startswith("evidence_call_invalid:4:")
            for code in validate_evidence_coverage(value)
        ))
        selected = next(
            row["candidate_id"]
            for row in value["research_agenda"]["candidates"]
            if row["selected"] is True
        )
        del value["stage_outputs"][selected]
        parent = "cycle-parent.semantic.json@sha256:" + "a" * 64
        value["cycle_id"] = "cycle-child"
        value["corrects_candidate_id"] = parent
        target = {
            "code": (
                "evidence_call_invalid:4:provenance:"
                "host_summary_result_not_nonempty_string"
            ),
            "json_pointer": "/evidence_calls/4/call",
            "canonical_json_pointer": "/evidence_calls/4/call/provenance",
            "required_state": "valid_evidence_call",
        }
        current_targets = [{
            "code": issue.code,
            "json_pointer": issue.pointer,
            "required_state": "semantic_builder_valid",
        } for issue in probe_semantic_candidate(
            value, filename="cycle-child.semantic.json",
        )]
        self.assertIn(
            "/stage_outputs/" + selected,
            [row["json_pointer"] for row in current_targets],
        )
        history = [{
            "candidate_id": parent,
            "input": "cycle-parent.semantic.json",
            "correction_targets": [target],
        }]

        codes = _retry_preflight_codes_for_value(
            value,
            input_name="cycle-child.semantic.json",
            history=history,
            candidate_id="cycle-child.semantic.json@sha256:" + "b" * 64,
            builder_succeeded=False,
            current_semantic_targets=current_targets,
        )

        self.assertNotIn(
            "retry_target_unsatisfied:/evidence_calls/4/call|"
            "valid_evidence_call",
            codes,
        )

        bad = semantic_candidate()
        bad["cycle_id"] = "cycle-bad-evidence"
        bad["corrects_candidate_id"] = parent
        bad_call = bad["evidence_calls"][4]["call"]
        bad_call["result"] = ""
        bad_call["capture_origin"] = "host_summary"
        canonical_bad = build_semantic_candidate(
            bad, filename="cycle-bad-evidence.semantic.json",
        ).canonical
        bad_codes = _retry_preflight_codes_for_value(
            bad,
            input_name="cycle-bad-evidence.semantic.json",
            history=history,
            candidate_id="cycle-bad-evidence.semantic.json@sha256:" + "c" * 64,
            canonical_value=canonical_bad,
            builder_succeeded=True,
        )
        self.assertIn(
            "retry_target_unsatisfied:/evidence_calls/4/call|"
            "valid_evidence_call",
            bad_codes,
        )

    def test_retry_restores_distinct_original_targets_from_ancestor(self):
        parent = "cycle-parent.semantic.json@sha256:" + "a" * 64
        child = "cycle-child.semantic.json@sha256:" + "b" * 64
        pointer = "/research_agenda/candidates"
        required_state = "addresses_committed_question"
        original_targets = [{
            "code": (
                "research_direction_committed_question_unaddressed:"
                f"opportunity-{index}:question-{index}"
            ),
            "json_pointer": pointer,
            "required_state": required_state,
        } for index in (1, 2)]
        original_targets.append({
            "code": (
                "evidence_call_invalid:4:provenance:"
                "host_summary_result_not_nonempty_string"
            ),
            "json_pointer": "/evidence_calls/4/call",
            "canonical_json_pointer": "/evidence_calls/4/call/provenance",
            "required_state": "valid_evidence_call",
        })
        history = [{
            "candidate_id": parent,
            "input": "cycle-parent.semantic.json",
            "correction_targets": original_targets,
        }, {
            "candidate_id": child,
            "input": "cycle-child.semantic.json",
            "corrects_candidate_id": parent,
            "correction_targets": [{
                "code": (
                    f"retry_target_unsatisfied:{pointer}|{required_state}"
                ),
                "json_pointer": pointer,
                "required_state": required_state,
            }, {
                "code": (
                    "retry_target_unsatisfied:"
                    "/evidence_calls/4/call|valid_evidence_call"
                ),
                "json_pointer": "/evidence_calls/4/call",
                "required_state": "valid_evidence_call",
            }],
        }]

        targets = _retry_targets(
            history,
            input_name="cycle-next.semantic.json",
            cycle_id="cycle-next",
            corrects_candidate_id=child,
            lineage_declared=True,
        )

        self.assertEqual(
            {target["code"] for target in targets},
            {target["code"] for target in original_targets},
        )
        self.assertEqual(len(targets), 3)

    def test_pending_canonical_evidence_survives_reprobe_until_builder_recovers(
        self,
    ):
        parent = semantic_candidate()
        parent["cycle_id"] = "cycle-evidence-parent"
        call = parent["evidence_calls"][4]["call"]
        call["result"] = ""
        call["capture_origin"] = "host_summary"
        self.write("cycle-evidence-parent.semantic.json", parent)
        _, parent_refusals = process_staging(
            self.staging, self.inputs, records=[],
        )
        self.assertIn(
            "evidence_call_invalid:4:", parent_refusals[0]["reason"],
        )

        child = semantic_candidate()
        child["cycle_id"] = "cycle-builder-blocked"
        child["corrects_candidate_id"] = parent_refusals[0]["candidate_id"]
        selected = next(
            row["candidate_id"]
            for row in child["research_agenda"]["candidates"]
            if row["selected"] is True
        )
        del child["stage_outputs"][selected]
        self.write("cycle-builder-blocked.semantic.json", child)
        _, child_refusals = process_staging(
            self.staging, self.inputs, records=[],
        )
        self.assertIn(
            "semantic_stage_output_missing", child_refusals[0]["reason"],
        )
        self.assertNotIn(
            "retry_target_unsatisfied:/evidence_calls/4",
            child_refusals[0]["reason"],
        )
        feedback = json.loads((self.staging / "FEEDBACK.json").read_text())
        pending = [
            target for target in feedback["retry_contract"]["targets"]
            if target["code"].startswith("evidence_call_invalid:4:")
        ]
        self.assertTrue(pending)
        self.assertTrue(all(
            target.get("verification_status") == "pending_builder"
            for target in pending
        ))

        process_staging(
            self.staging, self.inputs, records=[], refresh_feedback=True,
        )
        refreshed = json.loads((self.staging / "FEEDBACK.json").read_text())
        self.assertTrue(any(
            target["code"].startswith("evidence_call_invalid:4:")
            and target.get("verification_status") == "pending_builder"
            for target in refreshed["retry_contract"]["targets"]
        ))

        corrected = semantic_candidate()
        corrected["cycle_id"] = "cycle-canonical-recovered"
        corrected["corrects_candidate_id"] = child_refusals[0]["candidate_id"]
        self.write("cycle-canonical-recovered.semantic.json", corrected)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[],
        )

        self.assertEqual(promoted, ["cycle-canonical-recovered.json"])
        self.assertEqual(refusals, [])

    def test_retry_revalidates_parent_targets_before_enforcement(self):
        parent_id = seed_stale_semantic_retry(self.staging)
        child = semantic_candidate()
        child["cycle_id"] = "cycle-corrected-with-new-defect"
        child["corrects_candidate_id"] = parent_id
        child["unchanged_from_prior"] = ["decision"]
        candidate = self.staging / "cycle-child.semantic.json"
        candidate.write_text(
            json.dumps(child, indent=2) + "\n",
            encoding="utf-8",
        )

        _, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        reason = refusals[0]["reason"]
        self.assertIn(
            "semantic_candidate_invalid:carry_forward_forbidden",
            reason,
        )
        self.assertNotIn("retry_target_unsatisfied", reason)
        feedback = json.loads(
            (self.staging / "FEEDBACK.json").read_text()
        )
        self.assertEqual(
            feedback["retry_contract"]["must_change_paths"],
            ["/unchanged_from_prior"],
        )
        ledger = (
            self.staging / "rejected" / "REJECTIONS.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        original = json.loads(ledger[0])
        self.assertEqual(
            original["correction_targets"][0]["json_pointer"],
            "/stage_outputs/scout-macro-specialist",
        )

    def test_retry_keeps_revalidated_target_when_still_invalid(self):
        parent_id = seed_stale_semantic_retry(self.staging)
        child = semantic_candidate()
        child["cycle_id"] = "cycle-still-mismatched"
        child["corrects_candidate_id"] = parent_id
        child["research_agenda"]["candidates"][0]["candidate_id"] = (
            "scout-macro-specialist"
        )
        candidate = self.staging / "cycle-child.semantic.json"
        candidate.write_text(
            json.dumps(child, indent=2) + "\n",
            encoding="utf-8",
        )

        _, refusals = process_staging(
            self.staging,
            self.inputs,
            records=[],
        )

        reason = refusals[0]["reason"]
        pointer = "/research_agenda/candidates/0/candidate_id"
        self.assertIn(
            "semantic_candidate_invalid:"
            "semantic_selected_specialist_mismatch",
            reason,
        )
        self.assertIn(
            f"retry_target_unsatisfied:{pointer}|"
            "semantic_builder_valid",
            reason,
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

    def test_synthetic_malformed_corpus_is_never_promoted(self):
        sources = sorted(MALFORMED_INTAKE_DIR.glob("cycle-sample-*.json"))
        self.assertEqual(len(sources), 7)
        originals = {}
        for source in sources:
            original = source.read_bytes()
            originals[source.name] = original
            (self.staging / source.name).write_bytes(original)
        promoted, refusals = process_staging(
            self.staging, self.inputs, records=[])
        self.assertEqual(promoted, [])
        self.assertEqual(
            {row["input"] for row in refusals},
            set(originals),
        )
        for name, original in originals.items():
            archived = list(
                (self.staging / "rejected").glob(
                    f"{pathlib.Path(name).stem}-*.json"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), original)

    def test_multiline_malformed_archive_is_invalid_and_byte_preserved(self):
        source = (
            MALFORMED_INTAKE_DIR
            / "cycle-sample-multiline-truncated.json"
        )
        original = source.read_bytes()
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(original)
        self.assertGreater(caught.exception.lineno, 1)

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

        with (
            patch.object(pathlib.Path, "replace", fail_one_candidate),
            patch(
                "runtime.staged_intake.load_journal_records",
                return_value=[],
            ),
        ):
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
