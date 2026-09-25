"""Validate staged host cycles before publishing them to the executor."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .profile_paths import profile_root
from .host_feedback import (
    FEEDBACK_FILENAME,
    parse_reason,
    refresh_validation_feedback,
    write_validation_feedback,
)
from .host_publication import (
    content_sha256,
    load_policy,
    marker_path,
    verify_canonical_inputs,
)
from .host_input_validator import (
    DuplicateJsonKeyError,
    decode_json,
    diagnostic_decode_json,
    validate_path,
)
from .integrity import load_journal_records
from .input_artifacts import InputArtifactError, input_document_from_value
from .json_fragments import extract_top_level_field
from .refusal_audit import retry_lineage_errors
from .run_host_cycle import partition_validation_errors, validate_input
from .schedule_ledger import (
    load_schedule_contract,
    platform_run_id_errors,
    validate_schedule_context,
)
from .semantic_candidate import (
    BuiltSemanticCandidate,
    SemanticCandidateError,
    build_semantic_candidate,
    is_semantic_candidate,
    probe_semantic_candidate,
    translate_pointer,
)
from .semantic_patch import (
    MaterializedSemanticPatch,
    SemanticPatchError,
    materialize_semantic_patch,
)
from .tool_artifacts import _credential_paths

SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.json")
REJECTED_DIRECTORY = "rejected"
REJECTION_LEDGER = "REJECTIONS.jsonl"
SEMANTIC_MAX_LINE_CHARS = 1000
CANONICAL_RETRY_STATES = frozenset({
    "valid_evidence_call",
    "evidence_projection_bound",
    "consistent_tool_call_id",
    "valid_research_allocation_plan",
    "valid_research_allocation_variance",
})


class RejectedArchiveCollisionError(RuntimeError):
    pass


class StagingIntakeInfrastructureError(RuntimeError):
    def __init__(self, failures: Sequence[Mapping[str, str]]):
        self.failures = [dict(failure) for failure in failures]
        detail = "; ".join(
            f"{failure['input']}:{failure['error']}"
            for failure in self.failures
        )
        super().__init__(f"staging_intake_infrastructure_failure:{detail}")


def _full_refusal_code(entry: Mapping[str, Any]) -> str:
    code = str(entry.get("code", ""))
    detail = str(entry.get("detail", ""))
    if code == "malformed_json":
        return code
    return f"{code}:{detail}" if detail else code


def _rejection_codes(reason: str) -> list[str]:
    return [_full_refusal_code(entry) for entry in parse_reason(reason)]


def _load_rejection_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    history = []
    for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping) or not value.get("candidate_id"):
            raise ValueError(
                f"rejection_ledger_invalid:{path.name}:{line_number}")
        history.append(dict(value))
    return history


def _append_rejection_event(
    path: Path,
    event: Mapping[str, Any],
) -> list[dict[str, Any]]:
    history = _load_rejection_history(path)
    candidate_id = str(event["candidate_id"])
    if any(
        str(previous.get("candidate_id", "")) == candidate_id
        for previous in history
    ):
        return history
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), sort_keys=True) + "\n")
    return history + [dict(event)]


def _candidate_value(path: Path) -> Mapping[str, Any] | None:
    try:
        value = decode_json(path.read_text(encoding="utf-8"))
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        DuplicateJsonKeyError,
    ):
        return None
    return value if isinstance(value, Mapping) else None


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _memory_object_pointer(
    value: Mapping[str, Any],
    collection_name: str,
    identifier: str,
    field: str,
) -> str | None:
    distillation = value.get("memory_distillation")
    if not isinstance(distillation, Mapping):
        return None
    collection = distillation.get(collection_name)
    if not isinstance(collection, Sequence) or isinstance(
            collection, (str, bytes)):
        return None
    index = None
    for candidate_index, item in enumerate(collection):
        if (
            isinstance(item, Mapping)
            and str(item.get("memory_id", "")) == identifier
        ):
            index = candidate_index
            break
    if index is None and identifier.isdigit():
        numeric_index = int(identifier)
        if 0 <= numeric_index < len(collection):
            index = numeric_index
    if index is None:
        return None
    return (
        f"/memory_distillation/{_json_pointer_token(collection_name)}/"
        f"{index}/{_json_pointer_token(field)}"
    )


def _duplicate_tool_call_pointer(
    value: Mapping[str, Any],
    tool_call_id: str,
) -> str:
    paths: list[str] = []
    for index, wrapper in enumerate(value.get("evidence_calls") or ()):
        call = wrapper.get("call") if isinstance(wrapper, Mapping) else None
        if (
            isinstance(call, Mapping)
            and str(call.get("tool_call_id", "")).strip() == tool_call_id
        ):
            paths.append(f"/evidence_calls/{index}/call")
    for stage_index, stage in enumerate(value.get("cognitive_stages") or ()):
        if not isinstance(stage, Mapping) or stage.get("stage_id") != "market_scout":
            continue
        output = stage.get("output")
        report = (
            output.get("market_scout_report")
            if isinstance(output, Mapping)
            else None
        )
        calls = report.get("tool_calls") if isinstance(report, Mapping) else ()
        for call_index, call in enumerate(calls or ()):
            if (
                isinstance(call, Mapping)
                and str(call.get("tool_call_id", "")).strip()
                == tool_call_id
            ):
                paths.append(
                    f"/cognitive_stages/{stage_index}/output/"
                    f"market_scout_report/tool_calls/{call_index}"
                )
    for research_index, research in enumerate(value.get("research") or ()):
        calls = (
            research.get("tool_calls")
            if isinstance(research, Mapping)
            else ()
        )
        for call_index, call in enumerate(calls or ()):
            if (
                isinstance(call, Mapping)
                and str(call.get("tool_call_id", "")).strip()
                == tool_call_id
            ):
                paths.append(
                    f"/research/{research_index}/tool_calls/{call_index}"
                )
    return paths[1] if len(paths) > 1 else paths[0] if paths else "/"


def _correction_targets(
    reason: str,
    value: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    targets = []
    for entry in parse_reason(reason):
        code = str(entry["code"])
        detail = str(entry.get("detail", ""))
        pointer = ""
        required_state = ""
        if code.startswith("lesson_missing_") and detail.isdigit():
            field = code.removeprefix("lesson_missing_")
            pointer = f"/lessons/{detail}/{_json_pointer_token(field)}"
            required_state = (
                "non_empty"
                if field == "evidence"
                else "non_empty_string"
            )
        elif code == "host_input_schema_version_required":
            pointer = "/host_input_schema_version"
            required_state = "schema_version_4"
        elif code == "staged_host_input_schema_version_required":
            pointer = "/host_input_schema_version"
            required_state = "schema_version_4"
        elif code == "decision_repetition_review_field_required":
            pointer = "/decision/repetition_review"
            required_state = (
                "null_or_complete_decision_repetition_review"
            )
        elif code in {
            "decision_repetition_review_required",
            "decision_repetition_review_not_object",
            "decision_repetition_review_fields",
        }:
            pointer = "/decision/repetition_review"
            required_state = "complete_decision_repetition_review"
        elif code == "decision_repetition_prior_cycle_mismatch":
            pointer = "/decision/repetition_review/prior_cycle_id"
            required_state = "exact_prior_cycle_id"
        elif code == "decision_repetition_disposition_invalid":
            pointer = "/decision/repetition_review/disposition"
            required_state = "decision_repetition_disposition"
        elif code == "decision_repetition_review_unexpected":
            pointer = "/decision/repetition_review"
            required_state = "null"
        elif code in {
            "decision_repetition_evidence_delta_not_list",
            "decision_repetition_evidence_delta_required",
            "decision_repetition_evidence_ref_invalid",
        }:
            pointer = "/decision/repetition_review/evidence_delta"
            required_state = "current_cycle_evidence_refs"
        elif code in {
            "decision_repetition_unresolved_question_ids_not_list",
            "decision_repetition_unresolved_question_ids_required",
            "decision_repetition_unresolved_question_id_invalid",
            "decision_repetition_unresolved_question_id_unknown",
        }:
            pointer = (
                "/decision/repetition_review/unresolved_question_ids"
            )
            required_state = "non_empty_string_list"
        elif code == "decision_repetition_rationale_required":
            pointer = "/decision/repetition_review/rationale"
            required_state = "non_empty_string"
        elif code == "decision_stage_repetition_review_mismatch":
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ()
                )
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "decision"
            ), None) if value is not None else None
            if stage_index is not None:
                pointer = (
                    f"/cognitive_stages/{stage_index}/output/"
                    "repetition_review"
                )
                required_state = "matches_decision_repetition_review"
        elif code == "decision_stage_forecast_assessment_mismatch":
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ()
                )
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "decision"
            ), None) if value is not None else None
            if stage_index is not None:
                pointer = (
                    f"/cognitive_stages/{stage_index}/output/"
                    "forecast_assessment"
                )
                required_state = "matches_decision_forecast_assessment"
        elif code == "experiment_contract_required":
            pointer = "/decision/experiment"
            required_state = "complete_decision_experiment"
        elif code == "experiment_field_required":
            pointer = (
                "/decision/experiment/"
                f"{_json_pointer_token(detail)}"
            )
            required_state = "non_empty_string"
        elif code.startswith("retry_lineage_"):
            pointer = "/corrects_candidate_id"
            required_state = "non_empty_string"
        elif code == "schedule_context_required":
            pointer = "/schedule_context"
            required_state = "complete_schedule_context"
        elif code.startswith("schedule_context_"):
            pointer = "/schedule_context"
            required_state = "complete_schedule_context"
        elif code.startswith("worker_research_projection_"):
            pointer = "/worker_research_projection_id"
            required_state = "non_empty_string"
        elif code == "worker_research_disposition_missing":
            pointer = "/worker_research_dispositions"
            required_state = "worker_research_disposition_for_record"
        elif (
            code in {
                "worker_research_disposition_unexpected",
                "worker_research_disposition_duplicate",
            }
            and value is not None
        ):
            pointer = "/worker_research_dispositions"
            required_state = (
                "worker_research_disposition_absent"
                if code == "worker_research_disposition_unexpected"
                else "worker_research_disposition_unique"
            )
        elif code == "worker_research_record_authority_forbidden":
            pointer = detail if detail.startswith("/") else "/"
            required_state = "removed"
        elif code in {
            "worker_research_dispositions_required",
            "worker_research_dispositions_must_be_a_list",
        }:
            pointer = "/worker_research_dispositions"
            required_state = "worker_research_disposition_list"
        elif code == "worker_research_disposition_invalid":
            parts = detail.split(":")
            pointer = "/worker_research_dispositions"
            if parts and parts[0].isdigit():
                pointer += f"/{parts[0]}"
            required_state = "worker_research_disposition_row"
        elif code in {
            "learning_dispositions_required",
            "learning_disposition_missing_stage",
        }:
            pointer = "/learning_stage_dispositions"
            required_state = "three_required_stage_rows"
        elif code == "learning_disposition_invalid":
            parts = detail.split(":")
            if parts and parts[0].isdigit():
                index = parts[0]
                problem = parts[1] if len(parts) > 1 else ""
                field = {
                    "unknown_stage": "stage_id",
                    "duplicate_stage": "stage_id",
                    "disposition": "disposition",
                    "rationale_empty": "rationale",
                    "rationale_too_long": "rationale",
                }.get(problem)
                pointer = f"/learning_stage_dispositions/{index}"
                if field:
                    pointer += f"/{field}"
                required_state = "valid_learning_disposition"
        elif code == "learning_disposition_evidence_invalid":
            parts = detail.split(":")
            if parts and parts[0].isdigit():
                pointer = (
                    f"/learning_stage_dispositions/{parts[0]}/evidence"
                )
                required_state = "current_cycle_evidence_refs"
        elif code in {
            "learning_disposition_artifact_invalid",
            "learning_artifact_id_reused",
        }:
            parts = detail.split(":")
            if code == "learning_disposition_artifact_invalid" and (
                parts and parts[0].isdigit()
            ):
                pointer = (
                    f"/learning_stage_dispositions/{parts[0]}/artifact_refs"
                )
            else:
                pointer = "/learning_stage_dispositions"
            required_state = "same_cycle_unique_artifact_refs"
        elif code == "market_scout_required":
            pointer = "/cognitive_stages"
            required_state = "contains_market_scout_stage"
        elif code.startswith("market_scout_") and value is not None:
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ()
                )
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "market_scout"
            ), None)
            if stage_index is not None:
                pointer = (
                    f"/cognitive_stages/{stage_index}/output/"
                    "market_scout_report"
                )
                required_state = "valid_market_scout_report"
                if code == "market_scout_stage_invalid":
                    pointer = f"/cognitive_stages/{stage_index}"
                    required_state = "valid_market_scout_stage"
        elif code == "research_agenda_invalid" and value is not None:
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ())
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "research_director"
            ), None)
            if stage_index is not None:
                base = (
                    f"/cognitive_stages/{stage_index}/output/"
                    "research_agenda"
                )
                parts = detail.split(":")
                if detail == "missing":
                    pointer = base
                    required_state = "non_empty_object"
                elif detail == "drivers":
                    pointer = f"{base}/drivers"
                    required_state = "non_empty_list"
                elif detail == "candidates":
                    pointer = f"{base}/candidates"
                    required_state = "non_empty_list"
                elif detail == "rejected_alternative":
                    pointer = f"{base}/candidates"
                    required_state = "contains_rejected_candidate"
                elif detail == "selection_rationale":
                    pointer = f"{base}/selection_rationale"
                    required_state = "non_empty_string"
                elif len(parts) >= 3 and parts[0] in {
                    "driver", "candidate",
                } and parts[1].isdigit():
                    pointer = (
                        f"{base}/{parts[0]}s/{parts[1]}/"
                        f"{_json_pointer_token(parts[2])}"
                    )
                    required_state = "present"
                else:
                    pointer = base
                    required_state = "non_empty_object"
        elif code == "research_binding_invalid":
            parts = detail.split(":")
            if parts and parts[0].isdigit():
                pointer = (
                    f"/research/{parts[0]}/specialist_stage_id"
                )
                required_state = "non_empty_string"
        elif code == "evidence_call_invalid":
            parts = detail.split(":")
            if parts and parts[0].isdigit():
                base = f"/evidence_calls/{parts[0]}"
                problem = ":".join(parts[1:])
                pointer = base
                if problem == "producer":
                    pointer = f"{base}/producer"
                elif problem.startswith("projection"):
                    pointer = f"{base}/projection"
                elif problem.startswith("provenance:"):
                    provenance_problem = problem.removeprefix("provenance:")
                    pointer = f"{base}/call/provenance"
                    if (
                        "stable_ref" in provenance_problem
                        or provenance_problem.startswith("source_ref")
                    ):
                        pointer += "/source_refs"
                    elif provenance_problem.startswith("web_source"):
                        pointer += "/web_sources"
                    elif provenance_problem.startswith("observed_at"):
                        pointer += "/observed_at"
                    elif provenance_problem.startswith("capture"):
                        pointer += "/capture"
                elif problem in {"call", "tool", "tool_call_id"}:
                    pointer = (
                        f"{base}/call"
                        if problem == "call"
                        else f"{base}/call/{problem}"
                    )
                required_state = "valid_evidence_call"
        elif code == "evidence_projection_missing":
            pointer = "/evidence_calls"
            required_state = "evidence_projection_bound"
        elif code == "order_instruction_projection_conflict":
            pointer = "/snapshot/order_instructions"
            required_state = "matches_top_level_order_instructions"
        elif code == "tool_call_id_conflict" and value is not None:
            pointer = _duplicate_tool_call_pointer(value, detail)
            required_state = "consistent_tool_call_id"
        elif code in {"as_of_in_future", "as_of_without_timezone"}:
            pointer = "/as_of"
            required_state = "timezone_timestamp_not_future"
        elif code.startswith("research_allocation_") and value is not None:
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ()
                )
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "research_director"
            ), None)
            if stage_index is not None:
                base = (
                    f"/cognitive_stages/{stage_index}/output/"
                    "research_agenda"
                )
                parts = detail.split(":")
                if code == "research_allocation_candidate_invalid" and (
                    parts and parts[0].isdigit()
                ):
                    pointer = f"{base}/candidates/{parts[0]}"
                    required_state = "valid_research_allocation_candidate"
                elif code == "research_allocation_variance_invalid":
                    pointer = f"{base}/allocation_variance"
                    required_state = "valid_research_allocation_variance"
                else:
                    pointer = f"{base}/allocation_plan"
                    required_state = "valid_research_allocation_plan"
        elif (
            code == "research_direction_open_question_unaddressed"
            and value is not None
        ):
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ()
                )
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "research_director"
            ), None)
            if stage_index is not None:
                pointer = (
                    f"/cognitive_stages/{stage_index}/output/"
                    "research_agenda/candidates"
                )
                required_state = "candidate_with_opportunity_question_link"
        elif (
            code == "research_direction_committed_question_unaddressed"
            and value is not None
        ):
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ()
                )
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "research_director"
            ), None)
            if stage_index is not None:
                pointer = (
                    f"/cognitive_stages/{stage_index}/output/"
                    "research_agenda/candidates"
                )
                required_state = "addresses_committed_question"
        elif code in {
            "opportunity_agenda_link_invalid",
            "opportunity_agenda_revisit_required",
        } and value is not None:
            parts = detail.split(":")
            stage_index = next((
                index
                for index, stage in enumerate(
                    value.get("cognitive_stages") or ()
                )
                if isinstance(stage, Mapping)
                and stage.get("stage_id") == "research_director"
            ), None)
            if (
                parts
                and parts[0].isdigit()
                and stage_index is not None
            ):
                pointer = (
                    f"/cognitive_stages/{stage_index}/output/"
                    f"research_agenda/candidates/{parts[0]}"
                )
                required_state = "valid_opportunity_link"
        elif code.startswith("opportunity_"):
            parts = detail.split(":")
            if parts and parts[0].isdigit():
                pointer = f"/opportunity_updates/{parts[0]}"
                required_state = "valid_opportunity_update"
            else:
                pointer = "/opportunity_updates"
                required_state = "non_empty_list"
        elif (
            code == "forecast_assessment_required"
            or code.startswith("forecast_assessment_invalid")
        ):
            pointer = "/decision/forecast_assessment"
            required_state = "complete_decision_forecast_assessment"
        elif code.startswith("forecast_"):
            parts = detail.split(":")
            if code == "forecast_registrations_require_schema_v3":
                pointer = "/host_input_schema_version"
                required_state = "schema_version_4"
            elif code in {
                "forecast_registrations_must_be_a_list",
                "forecast_registrations_too_many",
            }:
                pointer = "/forecast_registrations"
                required_state = "forecast_registration_list"
            elif parts and parts[0].isdigit():
                if code.startswith("forecast_outcome_"):
                    pointer = f"/forecast_outcomes/{parts[0]}"
                    required_state = "valid_forecast_outcome"
                else:
                    pointer = f"/forecast_registrations/{parts[0]}"
                    required_state = "valid_forecast_registration"
            else:
                if code == "forecast_outcome_lookalike_key_unsupported":
                    pointer = "/forecast_outcomes"
                    required_state = "forecast_outcome_list"
                elif code.startswith("forecast_outcome"):
                    pointer = "/forecast_outcomes"
                    required_state = "forecast_outcome_list"
                else:
                    pointer = "/forecast_registrations"
                    required_state = "forecast_registration_list"
        elif code.startswith("instruction_reconciliation"):
            parts = detail.split(":")
            if code == "instruction_reconciliations_require_schema_v3":
                pointer = "/host_input_schema_version"
                required_state = "schema_version_4"
            elif code in {
                "instruction_reconciliations_must_be_a_list",
                "instruction_reconciliations_too_many",
            }:
                pointer = "/instruction_reconciliations"
                required_state = "instruction_reconciliation_list"
            elif parts and parts[0].isdigit():
                pointer = f"/instruction_reconciliations/{parts[0]}"
                required_state = "valid_instruction_reconciliation"
            else:
                pointer = "/instruction_reconciliations"
                required_state = "instruction_reconciliation_list"
        elif code.startswith("adversarial_dispute"):
            parts = detail.split(":")
            if code == "adversarial_disputes_require_schema_v3":
                pointer = "/host_input_schema_version"
                required_state = "schema_version_4"
            elif code in {
                "adversarial_disputes_must_be_a_list",
                "adversarial_disputes_too_many",
            }:
                pointer = "/adversarial_disputes"
                required_state = "adversarial_dispute_list"
            elif parts and parts[0].isdigit():
                pointer = f"/adversarial_disputes/{parts[0]}"
                required_state = "valid_adversarial_dispute"
            else:
                pointer = "/adversarial_disputes"
                required_state = "adversarial_dispute_list"
        elif code == "tool_provenance_invalid":
            parts = detail.split(":")
            if (
                len(parts) >= 3
                and parts[0].isdigit()
                and parts[1].isdigit()
            ):
                base = (
                    f"/research/{parts[0]}/tool_calls/{parts[1]}"
                )
                problem = ":".join(parts[2:])
                pointer = f"{base}/provenance"
                required_state = "non_empty_object"
                if problem.startswith("result_origin"):
                    pointer = f"{pointer}/result_origin"
                    required_state = "non_empty_string"
                elif problem.startswith("observed_at"):
                    pointer = f"{pointer}/observed_at"
                    required_state = "non_empty_string"
                elif (
                    problem.startswith("source_ref")
                    or "stable_ref" in problem
                ):
                    pointer = f"{pointer}/source_refs"
                    required_state = "non_empty_list"
                elif problem.startswith("host_summary_result"):
                    pointer = f"{base}/result"
                    required_state = "non_empty_string"
                elif (
                    problem.startswith("result_not")
                    or problem.startswith("connector_result")
                    or problem.startswith("capture_artifact")
                ):
                    pointer = f"{base}/result"
                    required_state = "present"
                elif problem.startswith("call_"):
                    pointer = f"{base}/call"
                    required_state = "normalized_action_and_arguments"
                elif problem.startswith("tool_call_id"):
                    pointer = f"{base}/tool_call_id"
                    required_state = "non_empty_string"
                elif problem.startswith("tool_call_kind"):
                    pointer = f"{base}/kind"
                    required_state = "non_empty_string"
                elif problem.startswith("capture_"):
                    pointer = f"{pointer}/capture"
                    required_state = "valid_capture_contract"
                elif problem.startswith("web_source"):
                    pointer = f"{pointer}/web_sources"
                    required_state = "valid_web_source_metadata"
        elif code == "goal_mode_invalid":
            parts = detail.split(":")
            if parts and parts[0].isdigit():
                index = parts[0]
                problem = parts[1] if len(parts) > 1 else ""
                if problem == "not_an_object":
                    pointer = f"/goal_observations/{index}"
                    required_state = "non_empty_object"
                else:
                    pointer = f"/goal_observations/{index}/mode"
                    required_state = "supported_goal_mode"
        elif code == "goal_creation_invalid":
            parts = detail.split(":")
            if detail == "multiple_creates":
                pointer = "/goal_observations"
                required_state = "single_or_empty_create"
            elif parts and parts[0].isdigit():
                index = parts[0]
                problem = parts[1] if len(parts) > 1 else ""
                field_by_problem = {
                    "missing_created_at": "created_at",
                    "empty_created_at": "created_at",
                    "created_at_invalid": "created_at",
                    "created_at_timezone_required": "created_at",
                    "created_at_must_equal_cycle_as_of": "created_at",
                    "missing_direction": "direction",
                    "empty_direction": "direction",
                    "direction_invalid": "direction",
                    "baseline_not_numeric": "baseline",
                    "partial_target_not_numeric": "partial_target",
                    "partial_target_not_between_baseline_and_target":
                        "partial_target",
                    "success_target_not_numeric": "success_target",
                    "metric_type_not_controllable": "metric_type",
                    "deadline_invalid": "deadline",
                    "deadline_timezone_required": "deadline",
                    "deadline_not_future": "deadline",
                    "caused_by_empty": "caused_by",
                    "caused_by_not_in_current_cycle": "caused_by",
                }
                if problem in {
                    "open_goal_exists",
                    "goal_id_conflict",
                    "duplicate_open_statement",
                    "duplicate_open_metric",
                }:
                    pointer = f"/goal_observations/{index}"
                    required_state = "removed"
                elif problem == "goal_not_an_object":
                    pointer = f"/goal_observations/{index}/goal"
                    required_state = "non_empty_object"
                elif problem == "mode_unknown":
                    pointer = f"/goal_observations/{index}/mode"
                    required_state = "non_empty_string"
                elif field := field_by_problem.get(problem):
                    pointer = (
                        f"/goal_observations/{index}/goal/"
                        f"{_json_pointer_token(field)}"
                    )
                    required_state = (
                        "numeric"
                        if field in {
                            "baseline", "partial_target", "success_target",
                        }
                        else "present"
                    )
                else:
                    pointer = f"/goal_observations/{index}"
                    required_state = "non_empty_object"
        elif code == "goal_progress_invalid":
            parts = detail.split(":")
            if parts and parts[0] == "multiple_progress":
                pointer = "/goal_observations"
                required_state = "single_or_empty_progress"
            elif parts and parts[0].isdigit():
                index = parts[0]
                problem = parts[1] if len(parts) > 1 else ""
                base = f"/goal_observations/{index}"
                if problem.startswith("unexpected_field_"):
                    field = problem.removeprefix("unexpected_field_")
                    pointer = f"{base}/{_json_pointer_token(field)}"
                    required_state = "removed"
                elif problem in {
                    "goal_not_open",
                    "goal_id_not_open",
                }:
                    pointer = base
                    required_state = "removed"
                elif problem.startswith("evidence_"):
                    pointer = f"{base}/evidence"
                    required_state = "non_empty_list"
                else:
                    field_by_problem = {
                        "goal_id_missing": "goal_id",
                        "observed_at_missing": "observed_at",
                        "observed_at_invalid": "observed_at",
                        "observed_at_timezone_required": "observed_at",
                        "observed_at_must_equal_cycle_as_of": "observed_at",
                        "observed_at_not_after_previous": "observed_at",
                        "previous_goal_time_invalid": "observed_at",
                        "observed_value_not_finite_number":
                            "observed_value",
                        "assessment_missing": "assessment",
                        "assessment_too_long": "assessment",
                        "evidence_empty": "evidence",
                        "evidence_too_many": "evidence",
                        "caused_by_empty": "caused_by",
                        "caused_by_not_in_current_cycle": "caused_by",
                    }
                    field = field_by_problem.get(problem)
                    pointer = (
                        f"{base}/{_json_pointer_token(field)}"
                        if field
                        else base
                    )
                    required_state = (
                        "numeric"
                        if field == "observed_value"
                        else "present"
                    )
        elif code == "goal_close_invalid":
            parts = detail.split(":")
            if parts and parts[0] in {
                "multiple_closes",
                "multiple_updates",
            }:
                pointer = "/goal_observations"
                required_state = "single_goal_update"
            elif parts and parts[0].isdigit():
                index = parts[0]
                problem = parts[1] if len(parts) > 1 else ""
                base = f"/goal_observations/{index}"
                if problem.startswith("unexpected_field_"):
                    field = problem.removeprefix("unexpected_field_")
                    pointer = f"{base}/{_json_pointer_token(field)}"
                    required_state = "removed"
                elif problem.startswith("analysis_"):
                    pointer = f"{base}/analysis"
                    required_state = "complete_goal_analysis"
                elif problem.startswith("evidence_"):
                    pointer = f"{base}/evidence"
                    required_state = "non_empty_list"
                elif problem in {
                    "goal_not_open",
                    "goal_creation_event_missing",
                    "measurement_before_deadline",
                    "goal_ungradable",
                    "legacy_mode_forbidden",
                }:
                    pointer = base
                    required_state = "removed"
                else:
                    field_by_problem = {
                        "goal_id_missing": "goal_id",
                        "goal_id_not_open": "goal_id",
                        "mode_unknown": "mode",
                        "closure_basis_invalid": "closure_basis",
                        "observed_at_missing": "observed_at",
                        "observed_at_invalid": "observed_at",
                        "observed_at_timezone_required": "observed_at",
                        "observed_at_must_equal_cycle_as_of": "observed_at",
                        "observed_at_not_after_previous": "observed_at",
                        "previous_goal_time_invalid": "observed_at",
                        "goal_deadline_invalid": "observed_at",
                        "observed_value_not_finite_number":
                            "observed_value",
                        "observed_value_forbidden_for_invalidation":
                            "observed_value",
                        "invalidation_reason_missing":
                            "invalidation_reason",
                        "invalidation_reason_too_long":
                            "invalidation_reason",
                        "invalidation_reason_forbidden":
                            "invalidation_reason",
                        "caused_by_empty": "caused_by",
                        "caused_by_not_in_current_cycle": "caused_by",
                        "analysis_not_object": "analysis",
                    }
                    field = field_by_problem.get(problem)
                    pointer = (
                        f"{base}/{_json_pointer_token(field)}"
                        if field
                        else base
                    )
                    required_state = (
                        "removed"
                        if problem in {
                            "observed_value_forbidden_for_invalidation",
                            "invalidation_reason_forbidden",
                        }
                        else "numeric"
                        if field == "observed_value"
                        else "present"
                    )
        elif code == "memory_object_invalid" and value is not None:
            parts = detail.split(":")
            if len(parts) >= 4 and parts[-2] in {"missing", "empty", "invalid"}:
                collection_name, identifier = parts[0], parts[1]
                field = parts[-1]
                pointer = _memory_object_pointer(
                    value,
                    collection_name,
                    identifier,
                    field,
                ) or ""
                required_state = (
                    "non_empty_list"
                    if field in {"claim_ids", "source_ids"}
                    else "present"
                )
        elif code == "tool_manifest_connectors_must_be_nonempty_list":
            pointer = "/tool_manifest_report/connectors"
            required_state = "non_empty_list"
        elif code == "memory_distillation_not_performed_object":
            pointer = "/memory_distillation"
            required_state = "null_or_complete_object"
        elif code == "tool_manifest_lookalike_key_unsupported":
            pointer = "/tool_manifest_report"
            required_state = "non_empty_object"
        elif code in {
            "known_instruction_recovery_required",
            "known_instruction_recovery_requires_schema_version",
            "known_instruction_recovery_source_cycle_mismatch",
            "known_instruction_recovery_source_hash_required",
            "known_instruction_recovery_decision_status_mismatch",
            "known_instruction_recovery_status_invalid",
        }:
            pointer = "/staged_order_instruction_recovery"
            required_state = "non_empty_object"
        elif code in {
            "known_instruction_recovery_create_missing",
            "known_instruction_recovery_create_mismatch",
        }:
            pointer = "/staged_order_instruction_recovery/create_activity"
            required_state = "non_empty_object"
        elif code.startswith("known_instruction_recovery_fresh_get_") or (
            code == "known_instruction_recovery_get_state_mismatch"
        ):
            pointer = "/staged_order_instruction_recovery/fresh_get"
            required_state = "non_empty_object"
        elif code in {
            "known_instruction_recovery_present_id_missing",
            "known_instruction_recovery_absent_id_present",
        }:
            pointer = "/staged_order_instruction_recovery/status"
            required_state = "non_empty_string"
        elif code in {
            "known_instruction_recovery_instruction_required",
            "known_instruction_recovery_instruction_identity",
            "known_instruction_recovery_instruction_mismatch",
        }:
            pointer = "/staged_order_instruction_recovery/instruction"
            required_state = "non_empty_object"
        elif code == "known_instruction_recovery_instruction_field":
            pointer = (
                "/staged_order_instruction_recovery/instruction/"
                f"{_json_pointer_token(detail)}"
            )
            required_state = "non_empty"
        elif code == "retry_target_unsatisfied" and "|" in detail:
            pointer, required_state = detail.rsplit("|", 1)
        if pointer and required_state:
            targets.append({
                "code": _full_refusal_code(entry),
                "json_pointer": pointer,
                "required_state": required_state,
            })
    return targets


_MISSING = object()


def _pointer_value(value: Mapping[str, Any], pointer: str) -> Any:
    current: Any = value
    for raw_token in pointer.strip("/").split("/"):
        if not raw_token:
            continue
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return _MISSING
            current = current[token]
        elif (
            isinstance(current, Sequence)
            and not isinstance(current, (str, bytes))
            and token.isdigit()
            and int(token) < len(current)
        ):
            current = current[int(token)]
        else:
            return _MISSING
    return current


def _target_satisfied(value: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    observed = _pointer_value(value, str(target["json_pointer"]))
    required_state = str(target["required_state"])
    if required_state == "non_empty_string":
        return isinstance(observed, str) and bool(observed.strip())
    if required_state == "non_empty":
        return observed is not _MISSING and bool(observed)
    if required_state == "non_empty_list":
        return isinstance(observed, list) and bool(observed)
    if required_state == "non_empty_object":
        return isinstance(observed, Mapping) and bool(observed)
    if required_state == "supported_goal_mode":
        return observed in {"create", "progress", "close"}
    if required_state == "null_or_complete_object":
        return observed is None or (
            isinstance(observed, Mapping)
            and observed.get("status") != "not_performed"
        )
    if required_state == "schema_version_2":
        return observed == 2
    if required_state == "schema_version_3":
        return observed == 3
    if required_state == "schema_version_4":
        return observed == 4
    if required_state == "complete_schedule_context":
        return (
            isinstance(observed, Mapping)
            and not platform_run_id_errors(observed)
            and all(
                isinstance(observed.get(field), str)
                and bool(observed[field].strip())
                for field in (
                    "task_id",
                    "expected_slot",
                    "started_at",
                    "source_observed_at",
                )
            )
            and observed.get("trigger") in {
                "scheduled",
                "manual",
                "recovery",
            }
            and observed.get("intervention") in {
                "none",
                "operator",
                "automation",
            }
        )
    if required_state == "normalized_action_and_arguments":
        return (
            isinstance(observed, Mapping)
            and set(observed) == {"action", "arguments"}
            and isinstance(observed.get("action"), str)
            and bool(observed["action"].strip())
            and (
                observed.get("arguments") is None
                or isinstance(observed.get("arguments"), Mapping)
            )
        )
    if required_state == "valid_capture_contract":
        return (
            isinstance(observed, Mapping)
            and observed.get("schema_version") == 1
            and observed.get("representation") in {
                "canonical_response",
                "redacted_canonical_response",
                "host_summary_no_response",
            }
            and isinstance(observed.get("redactions"), list)
        )
    if required_state == "valid_web_source_metadata":
        return isinstance(observed, list)
    if required_state == "valid_evidence_call":
        from .evidence_coverage import validate_evidence_coverage

        parts = str(target.get("code", "")).split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            return False
        prefix = f"evidence_call_invalid:{parts[1]}:"
        return not any(
            error.startswith(prefix)
            for error in validate_evidence_coverage(value)
        )
    if required_state == "evidence_projection_bound":
        from .evidence_coverage import validate_evidence_coverage

        code = str(target.get("code", ""))
        return code not in validate_evidence_coverage(value)
    if required_state == "matches_top_level_order_instructions":
        return (
            observed is not _MISSING
            and observed == value.get("order_instructions")
        )
    if required_state == "consistent_tool_call_id":
        from .tool_provenance import validate_tool_call_id_consistency

        code = str(target.get("code", ""))
        return code not in validate_tool_call_id_consistency(value)
    if required_state == "timezone_timestamp_not_future":
        from .timestamps import parse_iso_timestamp

        parsed = parse_iso_timestamp(observed)
        return (
            parsed is not None
            and parsed.tzinfo is not None
            and parsed.utcoffset() is not None
            and parsed <= datetime.now(timezone.utc) + timedelta(minutes=15)
        )
    if required_state == "three_required_stage_rows":
        if not isinstance(observed, list):
            return False
        stages = [
            str(row.get("stage_id", ""))
            for row in observed
            if isinstance(row, Mapping)
        ]
        return (
            len(observed) == 3
            and sorted(stages) == [
                "learning_audit",
                "meta_research",
                "self_improvement",
            ]
        )
    if required_state == "contains_market_scout_stage":
        return (
            isinstance(observed, list)
            and any(
                isinstance(row, Mapping)
                and row.get("stage_id") == "market_scout"
                for row in observed
            )
        )
    if required_state == "valid_market_scout_stage":
        return (
            isinstance(observed, Mapping)
            and observed.get("phase") == "discovery"
            and observed.get("depends_on") == ["portfolio"]
            and observed.get("required") is True
            and observed.get("status") == "completed"
        )
    if required_state == "valid_market_scout_report":
        return (
            isinstance(observed, Mapping)
            and isinstance(observed.get("scope"), Mapping)
            and isinstance(observed.get("budget"), Mapping)
            and isinstance(observed.get("tool_calls"), list)
            and bool(observed["tool_calls"])
            and isinstance(observed.get("candidates"), list)
            and "budget_variance" in observed
        )
    if required_state == "valid_learning_disposition":
        return (
            isinstance(observed, Mapping)
            and observed.get("stage_id") in {
                "learning_audit", "meta_research", "self_improvement",
            }
            and observed.get("disposition") in {"artifact", "no_change"}
            and isinstance(observed.get("rationale"), str)
            and 0 < len(observed["rationale"].strip()) <= 600
        )
    if required_state == "valid_opportunity_update":
        return (
            isinstance(observed, Mapping)
            and all(
                isinstance(observed.get(field), str)
                and bool(observed[field].strip())
                for field in (
                    "event_id",
                    "opportunity_id",
                    "to_state",
                    "thesis",
                    "rationale",
                )
            )
            and isinstance(observed.get("identity"), Mapping)
            and isinstance(observed.get("evidence"), list)
            and bool(observed["evidence"])
        )
    if required_state == "forecast_registration_list":
        return isinstance(observed, list)
    if required_state == "valid_forecast_registration":
        return (
            isinstance(observed, Mapping)
            and all(
                field in observed
                for field in (
                    "forecast_id",
                    "opportunity_id",
                    "supersedes_forecast_id",
                    "thesis",
                    "metric",
                    "horizon",
                    "expectation",
                    "confidence_probability",
                    "benchmark",
                    "entry_context",
                    "risk_assumptions",
                    "portfolio_context",
                    "invalidation_condition",
                    "evidence",
                )
            )
            and isinstance(observed.get("metric"), Mapping)
            and isinstance(observed.get("horizon"), Mapping)
            and isinstance(observed.get("expectation"), Mapping)
            and isinstance(observed.get("risk_assumptions"), list)
            and isinstance(observed.get("evidence"), list)
        )
    if required_state == "forecast_outcome_list":
        return isinstance(observed, list)
    if required_state == "valid_forecast_outcome":
        return (
            isinstance(observed, Mapping)
            and set(observed) == {
                "forecast_id",
                "tool_call_id",
                "invalidation_reason",
                "evidence",
            }
            and isinstance(observed.get("forecast_id"), str)
            and bool(observed["forecast_id"].strip())
            and isinstance(observed.get("tool_call_id"), str)
            and bool(observed["tool_call_id"].strip())
            and isinstance(observed.get("evidence"), list)
            and bool(observed["evidence"])
        )
    if required_state == "instruction_reconciliation_list":
        return isinstance(observed, list)
    if required_state == "valid_instruction_reconciliation":
        return (
            isinstance(observed, Mapping)
            and set(observed) == {
                "reconciliation_id",
                "recommendation_id",
                "instruction_id",
                "supersedes_reconciliation_id",
                "operator_observation",
                "account_orders_tool_call_id",
                "account_trades_tool_call_id",
                "evidence",
            }
            and isinstance(observed.get("operator_observation"), Mapping)
            and isinstance(observed.get("evidence"), list)
            and bool(observed["evidence"])
        )
    if required_state == "adversarial_dispute_list":
        return isinstance(observed, list)
    if required_state == "valid_adversarial_dispute":
        return (
            isinstance(observed, Mapping)
            and set(observed) == {
                "dispute_id",
                "opportunity_id",
                "emerging_position",
                "adversarial_position",
                "disputed_claims",
                "governance_resolution",
                "final_decision_changed",
                "evidence",
            }
            and isinstance(observed.get("disputed_claims"), list)
            and bool(observed["disputed_claims"])
            and isinstance(observed.get("evidence"), list)
            and bool(observed["evidence"])
        )
    if required_state == "valid_opportunity_link":
        return (
            isinstance(observed, Mapping)
            and (
                bool(str(observed.get("opportunity_id", "")).strip())
                or (
                    isinstance(
                        observed.get("distinct_from_opportunity_ids"),
                        list,
                    )
                    and bool(observed["distinct_from_opportunity_ids"])
                    and bool(str(
                        observed.get("distinctness_reason", "")
                    ).strip())
                )
            )
        )
    if required_state == "valid_research_allocation_candidate":
        factors = (
            observed.get("allocation_factors")
            if isinstance(observed, Mapping)
            else None
        )
        return (
            isinstance(observed, Mapping)
            and isinstance(factors, Mapping)
            and set(factors) == {
                "novelty",
                "portfolio_impact",
                "missing_information",
                "expected_information_gain",
            }
            and all(
                isinstance(factors.get(field), str)
                and bool(factors[field].strip())
                for field in factors
            )
            and all(
                observed.get(field) is None
                or (
                    isinstance(observed.get(field), str)
                    and bool(observed[field].strip())
                )
                for field in ("portfolio_risk_ref", "follow_up_ref")
            )
            and (
                observed.get("selected") is not True
                or bool(str(
                    observed.get("follow_up_ref")
                    or observed.get("portfolio_risk_ref")
                    or observed.get("opportunity_id")
                    or observed.get("scout_candidate_id")
                    or ""
                ).strip())
            )
            and (
                not str(
                    observed.get("target_missing_information_id", "")
                ).strip()
                or bool(str(observed.get("opportunity_id", "")).strip())
            )
        )
    if required_state == "candidate_with_opportunity_question_link":
        return (
            isinstance(observed, list)
            and any(
                isinstance(candidate, Mapping)
                and bool(str(
                    candidate.get("opportunity_id", "")
                ).strip())
                and bool(str(
                    candidate.get("target_missing_information_id", "")
                ).strip())
                for candidate in observed
            )
        )
    if required_state == "addresses_committed_question":
        detail = str(target.get("code", "")).split(":", 1)[-1]
        opportunity_id, separator, question_id = detail.partition(":")
        return (
            bool(separator)
            and isinstance(observed, list)
            and any(
                isinstance(candidate, Mapping)
                and str(candidate.get("opportunity_id", "")).strip()
                == opportunity_id
                and str(
                    candidate.get("target_missing_information_id", "")
                ).strip() == question_id
                for candidate in observed
            )
        )
    if required_state in {
        "valid_research_allocation_plan",
        "valid_research_allocation_variance",
    }:
        from .research_allocation import validate_research_allocation

        allocation_errors = validate_research_allocation(
            value,
            required=True,
        )
        if required_state == "valid_research_allocation_plan":
            return not any(
                error == "research_allocation_required"
                or error.startswith("research_allocation_plan_invalid:")
                for error in allocation_errors
            )
        if required_state == "valid_research_allocation_variance":
            return not any(
                error.startswith("research_allocation_variance_invalid:")
                for error in allocation_errors
            )
    if required_state == "current_cycle_evidence_refs":
        return (
            isinstance(observed, list)
            and bool(observed)
            and len(observed) <= 12
            and all(
                isinstance(ref, str)
                and (
                    ref.startswith("stage:")
                    or ref.startswith("finding:")
                )
                for ref in observed
            )
        )
    if required_state == "non_empty_string_list":
        return (
            isinstance(observed, list)
            and bool(observed)
            and all(
                isinstance(item, str) and bool(item.strip())
                for item in observed
            )
        )
    if required_state == "decision_repetition_disposition":
        return observed in {
            "new_evidence",
            "bounded_experiment",
            "deliberate_wait",
        }
    if required_state == "complete_decision_repetition_review":
        from .decision_repetition import REPETITION_REVIEW_FIELDS

        disposition = (
            observed.get("disposition")
            if isinstance(observed, Mapping)
            else None
        )
        evidence = (
            observed.get("evidence_delta")
            if isinstance(observed, Mapping)
            else None
        )
        unresolved = (
            observed.get("unresolved_question_ids")
            if isinstance(observed, Mapping)
            else None
        )
        return (
            isinstance(observed, Mapping)
            and set(observed) == REPETITION_REVIEW_FIELDS
            and disposition in {
                "new_evidence",
                "bounded_experiment",
                "deliberate_wait",
            }
            and isinstance(evidence, list)
            and all(
                isinstance(ref, str) and bool(ref.strip())
                for ref in evidence
            )
            and isinstance(unresolved, list)
            and all(
                isinstance(item, str) and bool(item.strip())
                for item in unresolved
            )
            and isinstance(observed.get("rationale"), str)
            and bool(observed["rationale"].strip())
            and (disposition != "new_evidence" or bool(evidence))
            and (disposition != "deliberate_wait" or bool(unresolved))
        )
    if required_state == "null_or_complete_decision_repetition_review":
        if observed is None:
            return True
        target = dict(target)
        target["required_state"] = "complete_decision_repetition_review"
        return _target_satisfied(value, target)
    if required_state == "exact_prior_cycle_id":
        detail = str(target.get("code", "")).split(":", 1)[-1]
        expected = detail.split(":actual=", 1)[0].removeprefix(
            "expected="
        )
        return isinstance(observed, str) and observed == expected
    if required_state == "complete_decision_experiment":
        from .decision_repetition import experiment_contract_errors

        return not experiment_contract_errors(observed)
    if required_state == "matches_decision_repetition_review":
        decision = value.get("decision")
        return (
            isinstance(decision, Mapping)
            and observed == decision.get("repetition_review")
        )
    if required_state == "complete_decision_forecast_assessment":
        return (
            isinstance(observed, Mapping)
            and set(observed) == {
                "status",
                "material_premise",
                "rationale",
                "forecast_ids",
            }
        )
    if required_state == "matches_decision_forecast_assessment":
        decision = value.get("decision")
        return (
            isinstance(decision, Mapping)
            and observed == decision.get("forecast_assessment")
        )
    if required_state == "null":
        return observed is None
    if required_state == "worker_research_disposition_list":
        return (
            isinstance(observed, list)
            and all(isinstance(row, Mapping) for row in observed)
        )
    if required_state == "worker_research_disposition_for_record":
        record_id = str(target.get("code", "")).split(":", 1)[-1]
        return (
            isinstance(observed, list)
            and any(
                isinstance(row, Mapping)
                and str(row.get("worker_record_id", "")).strip()
                == record_id
                for row in observed
            )
        )
    if required_state == "worker_research_disposition_absent":
        record_id = str(target.get("code", "")).split(":", 1)[-1]
        return (
            isinstance(observed, list)
            and all(
                not isinstance(row, Mapping)
                or str(row.get("worker_record_id", "")).strip()
                != record_id
                for row in observed
            )
        )
    if required_state == "worker_research_disposition_unique":
        record_id = str(target.get("code", "")).split(":", 1)[-1]
        return (
            isinstance(observed, list)
            and sum(
                1
                for row in observed
                if (
                    isinstance(row, Mapping)
                    and str(row.get("worker_record_id", "")).strip()
                    == record_id
                )
            ) <= 1
        )
    if required_state == "worker_research_disposition_row":
        if not isinstance(observed, Mapping):
            return False
        expected = {
            "worker_record_id",
            "disposition",
            "evidence",
            "rationale",
            "revisit_condition",
        }
        disposition = observed.get("disposition")
        revisit = observed.get("revisit_condition")
        return (
            set(observed) == expected
            and isinstance(observed.get("worker_record_id"), str)
            and bool(observed["worker_record_id"].strip())
            and disposition in {"used_as_lead", "rejected", "deferred"}
            and isinstance(observed.get("evidence"), list)
            and isinstance(observed.get("rationale"), str)
            and bool(observed["rationale"].strip())
            and (
                (
                    disposition == "deferred"
                    and isinstance(revisit, str)
                    and bool(revisit.strip())
                )
                or (
                    disposition != "deferred"
                    and revisit is None
                )
            )
        )
    if required_state == "same_cycle_unique_artifact_refs":
        return observed is _MISSING or (
            isinstance(observed, list)
            and bool(observed)
            and len(observed) <= 12
            and len(observed) == len(set(map(str, observed)))
        )
    if required_state == "contains_rejected_candidate":
        return (
            isinstance(observed, list)
            and any(
                isinstance(candidate, Mapping)
                and candidate.get("selected") is False
                and bool(str(
                    candidate.get("rejection_reason", "")
                ).strip())
                for candidate in observed
            )
        )
    if required_state == "removed":
        return observed is _MISSING
    if required_state == "numeric":
        return (
            isinstance(observed, (int, float))
            and not isinstance(observed, bool)
            and math.isfinite(float(observed))
        )
    if required_state == "single_or_empty_create":
        return (
            isinstance(observed, list)
            and sum(
                1 for request in observed
                if isinstance(request, Mapping)
                and request.get("mode") == "create"
            ) <= 1
        )
    if required_state == "single_or_empty_progress":
        goal_ids = [
            str(request.get("goal_id", "")).strip()
            for request in observed
            if isinstance(request, Mapping)
            and request.get("mode") == "progress"
            and str(request.get("goal_id", "")).strip()
        ] if isinstance(observed, list) else []
        return (
            isinstance(observed, list)
            and len(goal_ids) == len(set(goal_ids))
        )
    if required_state == "single_goal_update":
        if not isinstance(observed, list):
            return False
        modes_by_goal: dict[str, list[str]] = {}
        for request in observed:
            if not isinstance(request, Mapping):
                continue
            mode = str(request.get("mode", ""))
            goal_id = str(request.get("goal_id", "")).strip()
            if mode in {"progress", "close"} and goal_id:
                modes_by_goal.setdefault(goal_id, []).append(mode)
        return all(len(modes) <= 1 for modes in modes_by_goal.values())
    if required_state == "complete_goal_analysis":
        return (
            isinstance(observed, Mapping)
            and all(
                isinstance(observed.get(field), str)
                and bool(str(observed[field]).strip())
                for field in (
                    "causal_summary",
                    "counterfactual",
                    "next_change",
                )
            )
            and all(
                isinstance(observed.get(field), list)
                for field in ("worked", "failed")
            )
        )
    if required_state == "present":
        return observed is not _MISSING
    return False


def _canonical_retry_target(target: Mapping[str, Any]) -> bool:
    return bool(target.get("canonical_json_pointer")) or (
        target.get("required_state") in CANONICAL_RETRY_STATES
    )


def _journaled_retry_targets(
    event: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {
        str(row.get("candidate_id")): row
        for row in history
        if row.get("candidate_id")
    }

    def originals(
        target: Mapping[str, Any],
        row: Mapping[str, Any],
        visited: set[str],
    ) -> list[dict[str, Any]]:
        if not str(target.get("code", "")).startswith(
            "retry_target_unsatisfied:"
        ):
            return [dict(target)]
        parent_id = str(row.get("corrects_candidate_id", ""))
        parent = by_id.get(parent_id)
        if not parent_id or parent_id in visited or parent is None:
            return [dict(target)]
        matching = [
            prior
            for prior in parent.get("correction_targets", ())
            if isinstance(prior, Mapping)
            and prior.get("json_pointer") == target.get("json_pointer")
            and prior.get("required_state") == target.get("required_state")
        ]
        if not matching:
            return [dict(target)]
        return [
            original
            for prior in matching
            for original in originals(
                prior, parent, visited | {parent_id},
            )
        ]

    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for target in event.get("correction_targets", ()):
        if not isinstance(target, Mapping):
            continue
        for original in originals(
            target, event, {str(event.get("candidate_id", ""))},
        ):
            identity = (
                original.get("code"),
                original.get("json_pointer"),
                original.get("required_state"),
            )
            if identity not in seen:
                result.append(original)
                seen.add(identity)
    return result


def _retry_targets(
    history: Sequence[Mapping[str, Any]],
    *,
    input_name: str,
    cycle_id: str,
    corrects_candidate_id: str | None = None,
    lineage_declared: bool = False,
    staging_dir: Path | None = None,
    input_dir: Path | None = None,
    records: Sequence[Mapping[str, Any]] = (),
) -> list[Mapping[str, Any]]:
    event = None
    if lineage_declared:
        if corrects_candidate_id is None:
            return []
        event = next(
            (
                row for row in history
                if str(row.get("candidate_id", ""))
                == corrects_candidate_id
            ),
            None,
        )
    else:
        event = next(
            (
                row for row in reversed(history)
                if (
                    (
                        cycle_id
                        and str(row.get("cycle_id", "")) == cycle_id
                    )
                    or str(row.get("input", "")) == input_name
                )
            ),
            None,
        )
    if event is None:
        return []
    journaled = _journaled_retry_targets(event, history)
    if staging_dir is None or input_dir is None:
        return journaled
    archive = str(event.get("archive", "")).strip()
    prior_input = str(event.get("input", "")).strip()
    if not archive or not prior_input:
        return journaled
    archived_path = staging_dir / REJECTED_DIRECTORY / archive
    prior_value = _candidate_value(archived_path)
    if (
        prior_value is None
        or not is_semantic_candidate(prior_value, filename=prior_input)
    ):
        return journaled
    _built, reason, current_targets = _semantic_reason(
        Path(prior_input),
        prior_value,
        input_dir=input_dir,
        records=records,
    )
    if reason is None:
        return []
    if _built is None:
        for target in journaled:
            if not _canonical_retry_target(target):
                continue
            identity = (
                target.get("code"),
                target.get("json_pointer"),
                target.get("required_state"),
            )
            if not any(
                (
                    existing.get("code"),
                    existing.get("json_pointer"),
                    existing.get("required_state"),
                ) == identity
                for existing in current_targets
            ):
                current_targets.append({
                    **target,
                    "verification_status": "pending_builder",
                })
    return current_targets or journaled


def _pointer_overlap(left: str, right: str) -> bool:
    left = left.rstrip("/") or "/"
    right = right.rstrip("/") or "/"
    if "/" in {left, right}:
        return True
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def _semantic_builder_target_satisfied(
    target: Mapping[str, Any],
    current_targets: Sequence[Mapping[str, Any]],
    *,
    builder_succeeded: bool,
) -> bool:
    if not current_targets:
        return builder_succeeded
    target_code = str(target.get("code", "")).split(":", 1)[0]
    target_pointers = {
        str(target.get(field, "")).strip()
        for field in ("json_pointer", "canonical_json_pointer")
        if str(target.get(field, "")).strip()
    }
    for current in current_targets:
        current_code = str(current.get("code", "")).split(":", 1)[0]
        if target_code and current_code == target_code:
            return False
        current_pointers = {
            str(current.get(field, "")).strip()
            for field in ("json_pointer", "canonical_json_pointer")
            if str(current.get(field, "")).strip()
        }
        if any(
            _pointer_overlap(target_pointer, current_pointer)
            for target_pointer in target_pointers
            for current_pointer in current_pointers
        ):
            return False
    return True


def _retry_preflight_codes_for_value(
    value: Mapping[str, Any],
    *,
    input_name: str,
    history: Sequence[Mapping[str, Any]],
    candidate_id: str,
    canonical_value: Mapping[str, Any] | None = None,
    builder_succeeded: bool = True,
    current_semantic_targets: Sequence[Mapping[str, Any]] = (),
    staging_dir: Path | None = None,
    input_dir: Path | None = None,
    records: Sequence[Mapping[str, Any]] = (),
    deferred_targets: list[dict[str, Any]] | None = None,
) -> list[str]:
    cycle_id = str(value.get("cycle_id", "")).strip()
    lineage_codes = retry_lineage_errors(
        value,
        refusals=history,
        candidate_id=candidate_id,
    )
    lineage_declared = "corrects_candidate_id" in value
    raw_reference = value.get("corrects_candidate_id")
    corrects_candidate_id = (
        raw_reference
        if isinstance(raw_reference, str)
        and raw_reference
        and raw_reference == raw_reference.strip()
        else None
    )
    targets = _retry_targets(
        history,
        input_name=input_name,
        cycle_id=cycle_id,
        corrects_candidate_id=corrects_candidate_id,
        lineage_declared=lineage_declared,
        staging_dir=staging_dir,
        input_dir=input_dir,
        records=records,
    )
    codes = list(lineage_codes)
    for target in targets:
        required_state = str(target.get("required_state", ""))
        if not builder_succeeded and _canonical_retry_target(target):
            if deferred_targets is not None:
                deferred_targets.append({
                    **target,
                    "verification_status": "pending_builder",
                })
            continue
        if required_state == "semantic_builder_valid":
            satisfied = _semantic_builder_target_satisfied(
                target,
                current_semantic_targets,
                builder_succeeded=builder_succeeded,
            )
        else:
            pointer = str(
                target.get("canonical_json_pointer")
                or target.get("json_pointer")
                or ""
            )
            evaluation_target = dict(target)
            evaluation_target["json_pointer"] = pointer
            candidate = canonical_value or value
            satisfied = _target_satisfied(candidate, evaluation_target)
        if not satisfied:
            codes.append(
                "retry_target_unsatisfied:"
                f"{target['json_pointer']}|{required_state}"
            )
    return codes


def _retry_preflight_codes(
    path: Path,
    history: Sequence[Mapping[str, Any]],
    *,
    candidate_id: str,
) -> list[str]:
    value = _candidate_value(path)
    if value is None:
        return []
    return _retry_preflight_codes_for_value(
        value,
        input_name=path.name,
        history=history,
        candidate_id=candidate_id,
    )


def _with_additional_codes(
    reason: str | None,
    *,
    input_name: str,
    codes: Sequence[str],
) -> str | None:
    if not codes:
        return reason
    if reason is None:
        return (
            f"ValueError: invalid_host_input:{input_name}:"
            + ",".join(codes)
        )
    marker = f"invalid_host_input:{input_name}:"
    if marker in reason:
        return reason + "," + ",".join(codes)
    return reason


def candidate_paths(staging_dir: Path | str) -> list[Path]:
    staging_dir = Path(staging_dir)
    return [
        path for path in sorted(staging_dir.glob("*.json"))
        if path.name != FEEDBACK_FILENAME and not path.name.startswith(".")
    ]


def _semantic_longest_line(path: Path) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    return max((len(line) for line in lines), default=0)


def _semantic_format_reason(path: Path) -> str | None:
    longest = _semantic_longest_line(path)
    if longest is None:
        return None
    if longest > SEMANTIC_MAX_LINE_CHARS:
        return (
            "ValueError: invalid_host_input:"
            f"{path.name}:semantic_json_line_too_long:"
            f"{longest}>{SEMANTIC_MAX_LINE_CHARS}"
        )
    return None


def _predecode_semantic_targets(
    path: Path,
    reason: str,
    *,
    input_dir: Path | None = None,
    records: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    entries = list(parse_reason(reason))
    if not any(
        entry["code"] == "duplicate_json_key"
        for entry in entries
    ):
        try:
            decode_json(path.read_text(encoding="utf-8"))
        except DuplicateJsonKeyError as error:
            entries.append({
                "code": "duplicate_json_key",
                "detail": error.key,
            })
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    for entry in entries:
        if entry["code"] == "duplicate_json_key":
            targets.append({
                "code": "duplicate_json_key",
                "json_pointer": "/",
                "required_state": "semantic_builder_valid",
                "detail": str(entry.get("detail", "")),
            })
    format_reason = _semantic_format_reason(path)
    if format_reason is not None:
        targets.append({
            "code": "semantic_json_line_too_long",
            "json_pointer": "/",
            "required_state": "semantic_builder_valid",
            "detail": format_reason.rsplit(":", 1)[-1],
        })
    if input_dir is not None and any(
        target["code"] == "duplicate_json_key" for target in targets
    ):
        diagnostic = _diagnostic_semantic_targets(
            path,
            input_dir=input_dir,
            records=records,
        )
        for candidate in diagnostic:
            identity = (
                candidate.get("code"),
                candidate.get("json_pointer"),
                candidate.get("required_state"),
                candidate.get("detail"),
            )
            if not any(
                (
                    existing.get("code"),
                    existing.get("json_pointer"),
                    existing.get("required_state"),
                    existing.get("detail"),
                ) == identity
                for existing in targets
            ):
                targets.append(candidate)
    return targets


def _diagnostic_semantic_targets(
    path: Path,
    *,
    input_dir: Path,
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Probe latent semantic and schedule targets behind duplicate keys.

    Never authorises promotion. Uses :func:`diagnostic_decode_json` to build
    a lossless projection of the refused source solely so hidden semantic
    defects can appear alongside the duplicate-key target in one retry
    contract. Fails closed on conflicting scalars or objects.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        value, _merged = diagnostic_decode_json(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(value, Mapping):
        return []
    if not is_semantic_candidate(value, filename=path.name):
        return []
    if (
        path.name.endswith(".semantic.json")
        and value.get("semantic_input_schema_version") is None
    ):
        return []
    targets: list[dict[str, str]] = []
    try:
        probe_issues = probe_semantic_candidate(
            value,
            filename=path.name,
            records=records,
        )
    except Exception:
        probe_issues = ()
    for issue in probe_issues:
        row: dict[str, str] = {
            "code": issue.code,
            "json_pointer": issue.pointer,
            "required_state": "semantic_builder_valid",
        }
        if issue.detail:
            row["detail"] = issue.detail
        targets.append(row)
    try:
        schedule_errors = _semantic_schedule_errors(
            value,
            input_dir=input_dir,
        )
    except Exception:
        schedule_errors = []
    if schedule_errors:
        schedule_reason = (
            f"ValueError: invalid_host_input:{path.name}:"
            + ",".join(schedule_errors)
        )
        for target in _correction_targets(schedule_reason, value):
            identity = (
                target.get("code"),
                target.get("json_pointer"),
                target.get("required_state"),
                target.get("detail"),
            )
            if not any(
                (
                    existing.get("code"),
                    existing.get("json_pointer"),
                    existing.get("required_state"),
                    existing.get("detail"),
                ) == identity
                for existing in targets
            ):
                targets.append(target)
    return targets


def _reason_for(
    path: Path,
    *,
    input_dir: Path,
    records: Sequence[Mapping[str, Any]],
    seen_cycle_ids: set[str],
) -> str | None:
    if not SAFE_FILENAME.fullmatch(path.name):
        return f"ValueError: staged_input_filename_invalid:{path.name}"
    if (input_dir / path.name).exists():
        return f"ValueError: staged_input_filename_collision:{path.name}"
    refusal = validate_path(
        path,
        records=records,
        canonical_input_dir=input_dir,
    )
    if refusal is not None:
        return refusal["reason"]
    value = decode_json(path.read_text(encoding="utf-8"))
    cycle_id = str(value.get("cycle_id", "")).strip()
    if cycle_id and (
        cycle_id in seen_cycle_ids
        or any(
            record.get("record_id") == f"cycle-receipt:{cycle_id}"
            for record in records
        )
    ):
        return f"ValueError: staged_cycle_id_collision:{cycle_id}"
    if cycle_id:
        seen_cycle_ids.add(cycle_id)
    return None


def _canonical_cycle_ids(input_dir: Path) -> set[str]:
    cycle_ids = set()
    for path in input_dir.glob("*.json"):
        if path.name == FEEDBACK_FILENAME or path.name.startswith("."):
            continue
        try:
            value = decode_json(path.read_text(encoding="utf-8"))
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            DuplicateJsonKeyError,
        ):
            if path.stem.startswith("cycle-"):
                cycle_ids.add(path.stem)
            continue
        if not isinstance(value, Mapping):
            continue
        cycle_id = str(value.get("cycle_id", "")).strip()
        if cycle_id:
            cycle_ids.add(cycle_id)
    return cycle_ids


def _archive_rejected(path: Path, rejected_dir: Path) -> Path:
    rejected_dir.mkdir(parents=True, exist_ok=True)
    digest = content_sha256(path)
    target = rejected_dir / f"{path.stem}-{digest}{path.suffix}"
    if target.exists():
        if target.read_bytes() != path.read_bytes():
            raise RejectedArchiveCollisionError(
                f"rejected_archive_collision:{target.name}")
        path.unlink()
        return target
    path.replace(target)
    return target


def _expand_staged_patch(
    path: Path,
    *,
    scratch_dir: Path,
    rejected_dir: Path,
    history: Sequence[Mapping[str, Any]],
) -> tuple[Path, MaterializedSemanticPatch]:
    try:
        patch = decode_json(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError):
        raise SemanticPatchError("semantic_patch_invalid:decode") from None
    if not isinstance(patch, Mapping):
        raise SemanticPatchError("semantic_patch_invalid:schema")
    if _credential_paths(patch):
        raise SemanticPatchError(
            "semantic_patch_invalid:capture_unredacted_credential"
        )
    base_id = patch.get("base_candidate_id")
    refusal = next(
        (
            row for row in reversed(history)
            if row.get("candidate_id") == base_id
        ),
        None,
    )
    if refusal is None:
        raise SemanticPatchError("semantic_patch_invalid:base_not_refused")
    archive = refusal.get("archive")
    if (
        not isinstance(archive, str)
        or not archive
        or Path(archive).name != archive
        or refusal.get("erased")
    ):
        raise SemanticPatchError("semantic_patch_invalid:base_archive")
    source = rejected_dir / archive
    if not source.is_file():
        raise SemanticPatchError("semantic_patch_invalid:base_missing")
    materialized = materialize_semantic_patch(
        path.read_bytes(),
        archive_name=archive,
        archive_bytes=source.read_bytes(),
        refusal=refusal,
    )
    expanded = scratch_dir / materialized.output_name
    expanded.write_bytes(materialized.source_bytes)
    if _credential_paths(decode_json(materialized.source_bytes.decode())):
        raise SemanticPatchError(
            "semantic_patch_invalid:capture_unredacted_credential"
        )
    return expanded, materialized


def _semantic_schedule_errors(
    value: Mapping[str, Any],
    *,
    input_dir: Path,
) -> list[str]:
    try:
        contract = load_schedule_contract(input_dir.resolve().parent)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return validate_schedule_context(
        value.get("schedule_context"),
        contract=contract,
        candidate_as_of=value.get("as_of"),
        candidate_cycle_id=value.get("cycle_id"),
    )


def _semantic_reason(
        path: Path,
        value: Mapping[str, Any],
        *,
        input_dir: Path,
        records: Sequence[Mapping[str, Any]],
) -> tuple[
        BuiltSemanticCandidate | None,
        str | None,
        list[dict[str, str]],
]:
        schedule_errors = _semantic_schedule_errors(
            value,
            input_dir=input_dir,
        )
        schedule_reason = (
            f"ValueError: invalid_host_input:{path.name}:"
            + ",".join(schedule_errors)
            if schedule_errors
            else None
        )
        schedule_targets = (
            _correction_targets(schedule_reason, value)
            if schedule_reason is not None
            else []
        )
        probe_issues = probe_semantic_candidate(
            value,
            filename=path.name,
            records=records,
        )
        if probe_issues:
            targets = [{
                "code": issue.code,
                "json_pointer": issue.pointer,
                "required_state": "semantic_builder_valid",
                **({"detail": issue.detail} if issue.detail else {}),
            } for issue in probe_issues]
            targets.extend(schedule_targets)
            reason = (
                f"ValueError: invalid_host_input:{path.name}:"
                + ",".join(
                    f"semantic_candidate_invalid:{issue.code}|"
                    f"{issue.pointer}|{issue.detail}"
                    for issue in probe_issues
                )
                + (
                    "," + ",".join(schedule_errors)
                    if schedule_errors
                    else ""
                )
            )
            return None, reason, targets
        try:
            built = build_semantic_candidate(
                value,
                filename=path.name,
                records=records,
            )
        except SemanticCandidateError as error:
            targets = [{
                "code": issue.code,
                "json_pointer": issue.pointer,
                "required_state": "semantic_builder_valid",
                **({"detail": issue.detail} if issue.detail else {}),
            } for issue in error.issues]
            targets.extend(schedule_targets)
            reason = (
                f"ValueError: invalid_host_input:{path.name}:"
                + ",".join(
                    f"semantic_candidate_invalid:{issue.code}|"
                    f"{issue.pointer}|{issue.detail}"
                    for issue in error.issues
                )
                + (
                    "," + ",".join(schedule_errors)
                    if schedule_errors
                    else ""
                )
            )
            return None, reason, targets
        return _built_candidate_reason(
            path,
            built,
            input_dir=input_dir,
            records=records,
        )


def _built_candidate_reason(
        path: Path,
        built: BuiltSemanticCandidate,
        *,
        input_dir: Path,
        records: Sequence[Mapping[str, Any]],
) -> tuple[
            BuiltSemanticCandidate,
            str | None,
            list[dict[str, str]],
]:
        try:
            document = input_document_from_value(
                built.canonical,
                profile_root=input_dir.resolve().parent,
            )
        except InputArtifactError as error:
            return (
                built,
                f"ValueError: invalid_host_input:{path.name}:{error}",
                [],
            )
        errors = validate_input(
            document.hydrated,
            built.target_name,
            records=records,
            input_dir=input_dir,
            require_full_schema=True,
        )
        blocking_errors, _advisories = partition_validation_errors(
            built.canonical,
            errors,
        )
        if not blocking_errors:
            return built, None, []
        reason = (
            f"ValueError: invalid_host_input:{path.name}:"
            + ",".join(blocking_errors)
        )
        targets = _correction_targets(reason, built.canonical)
        for target in targets:
            canonical_pointer = str(target.get("json_pointer", ""))
            target["canonical_json_pointer"] = canonical_pointer
            target["json_pointer"] = translate_pointer(
                canonical_pointer,
                built.pointer_map,
            )
        return built, reason, targets


def _refresh_semantic_rejection_history(
    staging_dir: Path,
    input_dir: Path,
    history: Sequence[Mapping[str, Any]],
    *,
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    refreshed = []
    for event in history:
        row = dict(event)
        archive = str(row.get("archive", "")).strip()
        input_name = str(row.get("input", "")).strip()
        path = staging_dir / REJECTED_DIRECTORY / archive
        if not archive or not input_name or not path.is_file():
            refreshed.append(row)
            continue
        value = _candidate_value(path)
        if value is None and input_name.endswith(".semantic.json"):
            targets = _predecode_semantic_targets(
                path,
                str(row.get("reason", "")),
                input_dir=input_dir,
                records=records,
            )
            if targets:
                row["correction_targets"] = targets
            refreshed.append(row)
            continue
        if (
            not is_semantic_candidate(value, filename=input_name)
            or (
                input_name.endswith(".semantic.json")
                and value.get("semantic_input_schema_version") is None
            )
        ):
            refreshed.append(row)
            continue
        _built, reason, targets = _semantic_reason(
            Path(input_name),
            value,
            input_dir=input_dir,
            records=records,
        )
        if _built is None:
            for target in _journaled_retry_targets(event, history):
                if not _canonical_retry_target(target):
                    continue
                identity = (
                    target.get("code"),
                    target.get("json_pointer"),
                    target.get("required_state"),
                )
                if not any(
                    (
                        existing.get("code"),
                        existing.get("json_pointer"),
                        existing.get("required_state"),
                    ) == identity
                    for existing in targets
                ):
                    targets.append({
                        **target,
                        "verification_status": "pending_builder",
                    })
        lineage_codes = retry_lineage_errors(
            value,
            refusals=history,
            candidate_id=str(row.get("candidate_id", "")),
        )
        reason = _with_additional_codes(
            reason,
            input_name=input_name,
            codes=lineage_codes,
        )
        if reason is not None:
            for target in _correction_targets(reason, value):
                identity = (
                    target.get("code"),
                    target.get("json_pointer"),
                    target.get("required_state"),
                    target.get("detail"),
                )
                if not any(
                    (
                        existing.get("code"),
                        existing.get("json_pointer"),
                        existing.get("required_state"),
                        existing.get("detail"),
                    ) == identity
                    for existing in targets
                ):
                    targets.append(target)
            row["reason"] = reason
            row["codes"] = _rejection_codes(reason)
            row["correction_targets"] = targets
        refreshed.append(row)
    return refreshed


def _promote_semantic_candidate(
        path: Path,
        built: BuiltSemanticCandidate,
        *,
        input_dir: Path,
        source_digest: str,
        source_reformatted: bool = False,
        source_longest_line_chars: int | None = None,
        accepted_dir: Path | None = None,
        patch_provenance: Mapping[str, Any] | None = None,
) -> Path:
        target = input_dir / built.target_name
        if target.exists():
            raise FileExistsError(
                f"staged_input_filename_collision:{built.target_name}"
            )
        accepted = accepted_dir or (path.parent / "accepted_sources")
        accepted.mkdir(parents=True, exist_ok=True)
        source_target = (
            accepted
            / f"{path.stem}-{source_digest}{path.suffix}"
        )
        metadata_target = source_target.with_suffix(
            source_target.suffix + ".build.json"
        )
        if (
            source_target.exists()
            and source_target.read_bytes() != path.read_bytes()
        ):
            raise RejectedArchiveCollisionError(
                f"semantic_source_collision:{source_target.name}"
            )
        shutil.copyfile(path, source_target)
        metadata_target.write_text(
            json.dumps({
                "semantic_input_schema_version": 1,
                "builder_version": built.builder_version,
                "source_sha256": source_digest,
                "canonical_sha256": hashlib.sha256(
                    built.canonical_bytes
                ).hexdigest(),
                "canonical_filename": built.target_name,
                "source_reformatted": source_reformatted,
                "source_longest_line_chars": source_longest_line_chars,
                **(
                    {
                        "corrects_candidate_id":
                        built.canonical.get("corrects_candidate_id")
                    }
                    if "corrects_candidate_id" in built.canonical
                    else {}
                ),
                **(
                    {"patch_provenance": patch_provenance}
                    if patch_provenance is not None
                    else {}
                ),
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            handle.write(built.canonical_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        marker = marker_path(input_dir, target.name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(content_sha256(target) + "\n", encoding="utf-8")
        path.unlink()
        return target


def process_staging(
    staging_dir: Path | str,
    input_dir: Path | str,
    *,
    records: Sequence[Mapping[str, Any]],
    refresh_feedback: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    staging_dir = Path(staging_dir)
    input_dir = Path(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    try:
        policy = load_policy(input_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise StagingIntakeInfrastructureError([{
            "input": "host_input",
            "error": f"{type(error).__name__}: {error}",
        }]) from error
    if policy is None:
        refusals = [{
            "input": "host_input",
            "reason": "ValueError: host_promotion_policy_missing",
        }]
        write_validation_feedback(
            staging_dir,
            checked=[],
            refusals=refusals,
            refusal_history=_load_rejection_history(
                staging_dir / REJECTED_DIRECTORY / REJECTION_LEDGER
            ),
        )
        return [], refusals
    paths = candidate_paths(staging_dir)
    promoted: list[str] = []
    refusals: list[dict[str, str]] = []
    infrastructure_failures: list[dict[str, str]] = []
    rejected_dir = staging_dir / REJECTED_DIRECTORY
    ledger_path = rejected_dir / REJECTION_LEDGER
    rejection_history = _load_rejection_history(ledger_path)
    seen_cycle_ids = _canonical_cycle_ids(input_dir)
    seen_target_names = {
        path.name for path in input_dir.glob("*.json")
    }
    for path in paths:
        try:
            original_path = path
            scratch = None
            patch_result: MaterializedSemanticPatch | None = None
            patch_error: SemanticPatchError | None = None
            patch_base_id: str | None = None
            try:
                if path.name.endswith(".semantic-patch.json"):
                    # Extract base_candidate_id from patch before calling _expand_staged_patch
                    # since that call may move or delete the patch file
                    patch_value = _candidate_value(path)
                    if isinstance(patch_value, Mapping) and isinstance(
                        patch_value.get("base_candidate_id"), str
                    ):
                        patch_base_id = patch_value.get("base_candidate_id")
                    scratch = tempfile.TemporaryDirectory(
                        prefix=".semantic-patch-",
                        dir=staging_dir,
                    )
                    try:
                        path, patch_result = _expand_staged_patch(
                            path,
                            scratch_dir=Path(scratch.name),
                            rejected_dir=rejected_dir,
                            history=rejection_history,
                        )
                    except SemanticPatchError as error:
                        patch_error = error
            except Exception:
                pass
            digest = content_sha256(path)
            candidate_id = f"{path.name}@sha256:{digest}"
            value = _candidate_value(path)
            built = None
            semantic_targets: list[dict[str, str]] = []
            deferred_targets: list[dict[str, Any]] = []
            semantic = is_semantic_candidate(
                value,
                filename=path.name,
            )
            semantic_schema_missing = bool(
                semantic
                and value is not None
                and value.get("semantic_input_schema_version") is None
            )
            format_reason = _semantic_format_reason(path)
            source_longest_line_chars = _semantic_longest_line(path)
            source_reformatted = bool(
                semantic
                and value is not None
                and format_reason is not None
            )
            if patch_error is not None:
                reason = (
                    f"ValueError: invalid_host_input:{path.name}:"
                    f"{patch_error}"
                )
                semantic_targets = []
                retry_codes = []
                built = None
                value = None
            elif semantic and value is None:
                built = None
                reason = _reason_for(
                    path,
                    input_dir=input_dir,
                    records=records,
                    seen_cycle_ids=seen_cycle_ids,
                )
                semantic_targets = _predecode_semantic_targets(
                    path,
                    reason,
                    input_dir=input_dir,
                    records=records,
                )
                retry_codes = []
            elif semantic and value is not None:
                if semantic_schema_missing:
                    reason = (
                        f"ValueError: invalid_host_input:{path.name}:"
                        "semantic_input_schema_version_required"
                    )
                    semantic_targets = [{
                        "code": "semantic_input_schema_version_required",
                        "json_pointer": "/semantic_input_schema_version",
                        "required_state": "semantic_builder_valid",
                    }]
                    retry_codes = []
                else:
                    built, reason, semantic_targets = _semantic_reason(
                        path,
                        value,
                        input_dir=input_dir,
                        records=records,
                    )
                if built is not None:
                    if built.target_name in seen_target_names:
                        reason = (
                            "ValueError: staged_input_filename_collision:"
                            f"{built.target_name}"
                        )
                    cycle_id = str(
                        built.canonical.get("cycle_id", "")
                    ).strip()
                    if reason is None and cycle_id and (
                        cycle_id in seen_cycle_ids
                        or any(
                            record.get("record_id")
                            == f"cycle-receipt:{cycle_id}"
                            for record in records
                        )
                    ):
                        reason = (
                            "ValueError: staged_cycle_id_collision:"
                            f"{cycle_id}"
                        )
                if not semantic_schema_missing:
                    retry_codes = _retry_preflight_codes_for_value(
                        value,
                        input_name=path.name,
                        history=rejection_history,
                        candidate_id=candidate_id,
                        canonical_value=(
                            built.canonical if built is not None else None
                        ),
                        builder_succeeded=built is not None,
                        current_semantic_targets=semantic_targets,
                        staging_dir=staging_dir,
                        input_dir=input_dir,
                        records=records,
                        deferred_targets=deferred_targets,
                    )
            else:
                retry_codes = _retry_preflight_codes(
                    path,
                    rejection_history,
                    candidate_id=candidate_id,
                )
                reason = _reason_for(
                    path,
                    input_dir=input_dir,
                    records=records,
                    seen_cycle_ids=seen_cycle_ids,
                )
            reason = _with_additional_codes(
                reason,
                input_name=path.name,
                codes=retry_codes,
            )
            if reason is not None:
                recovered_cycle_id = (
                    extract_top_level_field(path, "cycle_id")
                    if value is None
                    else None
                )
                recovered_schedule_context = (
                    extract_top_level_field(path, "schedule_context")
                    if value is None
                    else None
                )
                targets = list(semantic_targets)
                for target in [
                    *deferred_targets,
                    *_correction_targets(reason, value),
                ]:
                    identity = (
                        target.get("json_pointer"),
                        target.get("required_state"),
                    )
                    if not any(
                        (
                            existing.get("json_pointer"),
                            existing.get("required_state"),
                        ) == identity
                        for existing in targets
                    ):
                        targets.append(target)
                privacy_erased = (
                    "capture_unredacted_credential" in reason
                    or patch_error is not None
                )
                archived = None
                if privacy_erased:
                    path.unlink()
                    if path != original_path:
                        original_path.unlink()
                else:
                    archived = _archive_rejected(path, rejected_dir)
                patch_provenance = None
                if patch_result is not None and not privacy_erased:
                    patch_archive = _archive_rejected(
                        original_path, rejected_dir / "patches"
                    )
                    patch_provenance = {
                        "input": original_path.name,
                        "sha256": patch_result.patch_sha256,
                        "archive": (
                            f"rejected/patches/{patch_archive.name}"
                        ),
                        "base_candidate_id": (
                            patch_result.base_candidate_id
                        ),
                    }
                event = {
                    "candidate_id": candidate_id,
                    "input": path.name,
                    "cycle_id": (
                        str(value.get("cycle_id", "")).strip()
                        if value is not None
                        else (
                            str(recovered_cycle_id).strip()
                            if isinstance(recovered_cycle_id, str)
                            else ""
                        )
                    ),
                    "sha256": digest,
                    "archive": archived.name if archived else None,
                    "erased": privacy_erased,
                    "erasure_reason": (
                        "unredacted_credential"
                        if privacy_erased
                        else None
                    ),
                    "refused_at": datetime.now(timezone.utc).isoformat(),
                    "codes": _rejection_codes(reason),
                    "correction_targets": targets,
                    "schedule_context": (
                        dict(value["schedule_context"])
                        if value is not None
                        and isinstance(
                            value.get("schedule_context"),
                            Mapping,
                        )
                        else (
                            dict(recovered_schedule_context)
                            if isinstance(
                                recovered_schedule_context,
                                Mapping,
                            )
                            else None
                        )
                    ),
                }
                if (
                    value is not None
                    and "corrects_candidate_id" in value
                ):
                    event["corrects_candidate_id"] = value.get(
                        "corrects_candidate_id"
                    )
                if patch_provenance is not None:
                    event["patch_provenance"] = patch_provenance
                if (
                    patch_error is not None
                    and patch_base_id is not None
                    and "capture_unredacted_credential" not in reason
                ):
                    event["base_candidate_id"] = patch_base_id
                rejection_history = _append_rejection_event(
                    ledger_path,
                    event,
                )
                refusals.append({
                    "input": path.name,
                    "reason": reason,
                    "candidate_id": candidate_id,
                    "archive": archived.name if archived else None,
                    "erased": privacy_erased,
                    "correction_targets": targets,
                    **(
                        {"patch_provenance": patch_provenance}
                        if patch_provenance is not None else {}
                    ),
                    **(
                        {"base_candidate_id": patch_base_id}
                        if (patch_error is not None and patch_base_id is not None) else {}
                    ),
                })
                continue
            if built is not None:
                patch_provenance = None
                if patch_result is not None:
                    patch_archive = _archive_rejected(
                        original_path, staging_dir / "accepted_patches"
                    )
                    patch_provenance = {
                        "input": original_path.name,
                        "sha256": patch_result.patch_sha256,
                        "archive": (
                            f"accepted_patches/{patch_archive.name}"
                        ),
                        "base_candidate_id": (
                            patch_result.base_candidate_id
                        ),
                    }
                target = _promote_semantic_candidate(
                    path,
                    built,
                    input_dir=input_dir,
                    source_digest=digest,
                    source_reformatted=source_reformatted,
                    source_longest_line_chars=source_longest_line_chars,
                    accepted_dir=(
                        staging_dir / "accepted_sources"
                        if patch_result is not None else None
                    ),
                    patch_provenance=patch_provenance,
                )
                promoted.append(target.name)
                seen_target_names.add(target.name)
                cycle_id = str(
                    built.canonical.get("cycle_id", "")
                ).strip()
                if cycle_id:
                    seen_cycle_ids.add(cycle_id)
                continue
            target = input_dir / path.name
            marker = marker_path(input_dir, target.name)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(content_sha256(path) + "\n", encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
            promoted.append(target.name)
        except (OSError, RejectedArchiveCollisionError) as error:
            infrastructure_failures.append({
                "input": path.name,
                "error": f"{type(error).__name__}: {error}",
            })
        finally:
            if scratch is not None:
                scratch.cleanup()

    direct_errors = verify_canonical_inputs(input_dir)
    refusals.extend({
        "input": (
            error.split(":", 1)[-1]
            if ":" in error
            else "host_input"
        ),
        "reason": f"ValueError: {error}",
    } for error in direct_errors)

    if paths or direct_errors or infrastructure_failures:
        feedback_path = write_validation_feedback(
            staging_dir,
            checked=[path.name for path in paths],
            refusals=refusals,
            refusal_history=rejection_history,
        )
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        feedback["staging_intake"] = {
            "promoted": promoted,
            "rejected": [row["input"] for row in refusals],
            "infrastructure_failures": infrastructure_failures,
        }
        feedback_path.write_text(
            json.dumps(feedback, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
    elif refresh_feedback:
        rejection_history = _refresh_semantic_rejection_history(
            staging_dir,
            input_dir,
            rejection_history,
            records=records,
        )
        refresh_validation_feedback(
            staging_dir,
            refusal_history=rejection_history,
        )
    if infrastructure_failures:
        raise StagingIntakeInfrastructureError(infrastructure_failures)
    return promoted, refusals


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir",
                        default=str(profile_root() / "host_staging"))
    parser.add_argument("--input-dir",
                        default=str(profile_root() / "host_input"))
    parser.add_argument("--verify-canonical", action="store_true")
    parser.add_argument(
        "--refresh-feedback",
        action="store_true",
        help=(
            "Refresh schema-derived staging feedback even when no candidate "
            "is pending."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.verify_canonical:
        errors = verify_canonical_inputs(args.input_dir)
        for error in errors:
            print(error)
        return 1 if errors else 0

    try:
        promoted, refusals = process_staging(
            args.staging_dir,
            args.input_dir,
            records=load_journal_records(),
            refresh_feedback=args.refresh_feedback,
        )
    except StagingIntakeInfrastructureError as error:
        for failure in error.failures:
            print(
                f"{failure['input']}: INFRASTRUCTURE FAILURE "
                f"{failure['error']}",
                file=sys.stderr,
            )
        return 1
    for name in promoted:
        print(f"{name}: promoted")
    for refusal in refusals:
        print(f"{refusal['input']}: REFUSED {refusal['reason']}")
    print(f"promoted {len(promoted)}, refused {len(refusals)}")
    # Refused input was handled: exact bytes were archived and feedback was
    # published. Exit 1 remains reserved for unhandled process failures.
    return 2 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
