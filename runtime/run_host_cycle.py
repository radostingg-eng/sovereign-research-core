"""Run one host cycle from a committed input file.

The point of this module is to remove the human from the loop. The host has
IBKR access and can write to the repository but cannot execute anything; an
executor with the repository can execute but has no IBKR. Neither can close
the loop alone, so the host commits what it observed and decided, and this
runs the cycle against it and persists a real receipt.

The receipt is produced by ProductionHostExecutor, not reconstructed. That is
the whole distinction: the previous cycle's reasoning existed only in a chat
transcript and had to be persisted as findings, because nothing had actually
executed stages. Here something does.

What this deliberately does NOT do is decide anything. Every judgement in the
input -- what to research, what it means, what to do -- came from the host.
This supplies handlers that return the host's own observations and let the
gates rule on them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .audit_store import AuditJournal
from .accepted_inputs import input_fingerprint
from .cycle_finalization import (
    finalization_record_id,
    persist_cycle_finalization,
)
from .cycle_receipt import ALLOWED_DECISIONS
from .effectiveness import build_ex_ante_snapshot, verify_snapshot_integrity
from .evidence_coverage import validate_evidence_coverage
from .forecasts import (
    backfill_forecast_registrations,
    forecast_ledger_summary,
    persist_forecast_registrations,
    validate_forecast_registrations,
)
from .forecast_outcomes import (
    backfill_forecast_outcomes,
    forecast_outcome_summary,
    persist_forecast_outcomes,
    validate_forecast_outcomes,
)
from .host_feedback import FEEDBACK_FILENAME, write_feedback
from .integrity import load_journal_records
from .instruction_reconciliation import (
    backfill_instruction_reconciliations,
    instruction_reconciliation_summary,
    persist_instruction_reconciliations,
    validate_instruction_reconciliations,
)
from .instruction_expiry import (
    instruction_expiry_record_ids,
    instruction_expiry_summary,
    persist_instruction_expiry_decisions,
    validate_instruction_expiry_decisions,
)
from .input_artifacts import (
    load_input_data,
    load_input_document,
    normalize_input_for_identity,
)
from .learning_dispositions import (
    LEARNING_DISPOSITION_SCHEMA_VERSIONS,
    LEARNING_STAGES,
    learning_disposition_summary,
    persist_learning_dispositions,
    validate_learning_dispositions,
)
from .schema_versions import (
    CANONICAL_STAGED_INPUT_VERSIONS,
    CURRENT_FULL_CYCLE_SCHEMA_VERSION,
    SUPPORTED_FULL_CYCLE_VERSIONS,
)
from .candidate_registry import (
    candidate_registry_summary,
    validate_rediscovery_candidates,
)
from .market_scout import market_scout_summary, validate_market_scout
from .orchestrator import AgentJob
from .opportunity_ledger import (
    backfill_opportunity_events,
    opportunity_ledger_summary,
    persist_opportunity_updates,
    validate_opportunity_updates,
)
from .research_value import (
    backfill_adversarial_disputes,
    persist_adversarial_disputes,
    research_value_census,
    validate_adversarial_disputes,
)
from .research_inbox import research_inbox_summary
from .refusal_audit import sync_rejection_ledger
from .research_allocation import validate_research_allocation
from .tool_provenance import (
    build_tool_provenance_index,
    latest_tool_provenance,
    persisted_tool_provenance_errors,
    provenance_required,
    validate_tool_call_id_consistency,
    validate_tool_call_provenance,
)
from .tool_artifacts import (
    build_artifact_specs,
    iter_tool_calls,
    materialize_artifacts,
    profile_root_for_journal,
)
from .tool_probation import (
    persist_tool_probations,
    probation_record_ids,
    tool_probation_summary,
    validate_tool_probations,
)
from .production_host import ProductionHostExecutor
from .profile_paths import code_root, profile_root
from .timestamps import effective_as_of, parse_iso_timestamp

# The journal follows the operator, not the working directory. Resolving
# it from cwd let a CLI run from the wrong directory write a second
# journal beside whatever happened to be there.
JOURNAL_DIR = profile_root() / "audit"
PROCESSED_MARKER = "cycle_id"
FULL_HOST_INPUT_SCHEMA_VERSION = CURRENT_FULL_CYCLE_SCHEMA_VERSION
MAX_STAGED_FUTURE_SKEW = timedelta(minutes=15)
GOAL_OBSERVATION_MODES = frozenset({"create", "progress", "close"})
LEGACY_UNTYPED_GOAL_INPUT_FINGERPRINTS = frozenset({
    "74c9e9974fa2248e",
})
ADVISORY_EVIDENCE_PROBLEMS = frozenset({
    "capture_missing",
    "provenance_v4_fields",
    "web_sources_not_list",
})
ADVISORY_WEB_SOURCE_PROBLEM = re.compile(
    r"web_source_\d+_(?:fields|reconstruction_status)$"
)
FULL_CYCLE_CORE_STAGES = frozenset({
    "portfolio",
    "market_scout",
    "research_director",
    "memory_retrieval",
    "evidence_arbitration",
    "portfolio_fit",
    "counterfactual",
    "adversarial",
    "governance_review",
    "decision",
    "learning_audit",
    "meta_research",
    "self_improvement",
})


def schema_version(data: Mapping[str, Any]) -> int | None:
    """Return the declared host-input schema version when it is integral."""
    value = data.get("host_input_schema_version")
    if value is None or isinstance(value, bool):
        return None
    try:
        version = int(value)
    except (TypeError, ValueError):
        return None
    return version


def is_full_cycle(data: Mapping[str, Any]) -> bool:
    """Whether the input uses any supported full-cycle schema."""
    return schema_version(data) in SUPPORTED_FULL_CYCLE_VERSIONS


def required_finalization_record_types(
    data: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, str]:
    """Closed set of post-receipt records required for this input."""
    cycle_id = str(receipt["cycle_id"])
    required: dict[str, str] = {}
    if receipt.get(
        "host_input_schema_version"
    ) in LEARNING_DISPOSITION_SCHEMA_VERSIONS:
        required.update({
            f"learning-disposition:{cycle_id}:{stage_id}":
                "learning_disposition"
            for stage_id in LEARNING_STAGES
        })
    as_of = effective_snapshot(data).get("as_of")
    if (
        receipt.get("evidence_completeness") != "partial"
        and is_full_cycle(data)
        and (
            data.get("host_input_schema_version") == 4
            or provenance_required(as_of)
        )
    ):
        required[f"tool-provenance:{cycle_id}"] = "tool_provenance"
    required.update(probation_record_ids(data, cycle_id=cycle_id))
    required.update(instruction_expiry_record_ids(
        data,
        cycle_id=cycle_id,
    ))
    return required


def _advisory_evidence_error(error: str) -> bool:
    parts = error.split(":")
    if len(parts) >= 4 and parts[0] == "evidence_call_invalid":
        if parts[2] != "provenance":
            return False
        problem = ":".join(parts[3:])
        return (
            problem in ADVISORY_EVIDENCE_PROBLEMS
            or ADVISORY_WEB_SOURCE_PROBLEM.fullmatch(problem) is not None
        )
    if (
        len(parts) >= 4
        and parts[0] == "tool_provenance_invalid"
    ):
        problem = ":".join(parts[3:])
        return ADVISORY_WEB_SOURCE_PROBLEM.fullmatch(problem) is not None
    if (
        len(parts) >= 3
        and parts[0] == "market_scout_tool_provenance_invalid"
    ):
        problem = ":".join(parts[2:])
        return ADVISORY_WEB_SOURCE_PROBLEM.fullmatch(problem) is not None
    return False


def partial_cycle_guard_errors(
    data: Mapping[str, Any],
) -> list[str]:
    errors = []
    if data.get("forecast_outcomes"):
        errors.append("partial_cycle_forecast_outcome_forbidden")
    if data.get("tool_probations"):
        errors.append("partial_cycle_tool_probation_forbidden")
    for index, row in enumerate(
        data.get("order_instruction_activity") or ()
    ):
        if (
            isinstance(row, Mapping)
            and str(row.get("operation", "")).strip().lower()
            in {"create", "delete"}
        ):
            errors.append(
                "partial_cycle_instruction_mutation_forbidden:"
                f"{index}"
            )
    for index, row in enumerate(
        data.get("instruction_lifecycle_updates") or ()
    ):
        if not isinstance(row, Mapping):
            continue
        if str(row.get("to_state", "")).strip().lower() in {
            "approved",
            "submitted",
            "executed",
            "deleted",
            "instruction_created",
        }:
            errors.append(
                "partial_cycle_instruction_lifecycle_forbidden:"
                f"{index}"
            )
    for index, row in enumerate(
        data.get("instruction_expiry_decisions") or ()
    ):
        if (
            isinstance(row, Mapping)
            and row.get("decision") in {"delete", "recreate"}
        ):
            errors.append(
                "partial_cycle_instruction_expiry_mutation_forbidden:"
                f"{index}"
            )
    return errors


def partition_validation_errors(
    data: Mapping[str, Any],
    errors: Sequence[str],
) -> tuple[list[str], list[str]]:
    advisory = sorted({
        error for error in errors
        if _advisory_evidence_error(error)
    })
    blocking = sorted({
        error for error in errors
        if error not in advisory
    })
    if advisory:
        blocking.extend(partial_cycle_guard_errors(data))
    return sorted(set(blocking)), advisory


def _memory_version_record_id(
    prefix: str,
    cycle_id: str,
    memory_id: str,
) -> str:
    identity = json.dumps(
        [cycle_id, memory_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _research_memory_payload(
    item: Mapping[str, Any],
    distillation: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(item)
    payload.update({
        "distillation_id": str(distillation.get("distillation_id", "")),
        "brain_version": (
            item.get("brain_version")
            or distillation.get("brain_version")
        ),
        "research_admitted": (
            evaluation.get(
                "research_admitted",
                evaluation.get("admitted"),
            ) is True),
        "distillation_admitted": evaluation.get("admitted") is True,
    })
    return payload


def _without_record_descendants(
    records: Sequence[Mapping[str, Any]],
    root_id: str,
) -> list[Mapping[str, Any]]:
    """Remove one prior partial write and everything causally below it."""
    removed = {root_id}
    changed = True
    while changed:
        changed = False
        for record in records:
            record_id = str(record.get("record_id", ""))
            if not record_id or record_id in removed:
                continue
            if removed.intersection(
                str(value) for value in record.get("caused_by") or ()
            ):
                removed.add(record_id)
                changed = True
    return [
        record for record in records
        if str(record.get("record_id", "")) not in removed
    ]


COGNITIVE_OUTPUT_FIELDS = frozenset({
    "observations", "evidence_status", "blockers", "confidence",
    "next_actions",
})
COGNITIVE_EVIDENCE_STATUSES = frozenset({
    "verified", "cross_checked", "partial", "unknown", "not_applicable",
})
ORDER_INSTRUCTION_OPERATIONS = frozenset({"get", "create", "delete"})
ORDER_INSTRUCTION_ID_KEYS = (
    "id", "instruction_id", "order_instruction_id", "order_id", "orderId",
)


def load_input(path: Path) -> dict[str, Any]:
    return load_input_data(path)


def effective_snapshot(data: Mapping[str, Any]) -> dict[str, Any]:
    """The snapshot the handlers will actually consume.

    Validation used to read top-level source/as_of/order_submission_used
    while the handlers read the NESTED snapshot, so an input whose top level
    said "ibkr" and whose snapshot said "guesswork", carried an invalid
    timestamp and declared order_submission_used=True was accepted and
    executed. The live-order denial was bypassed by disagreeing with itself.

    One object now, built once, validated and executed. A nested value WINS
    over the top level, because that is what the handler would have used and
    validating anything else validates a different input.
    """
    snapshot = dict(data.get("snapshot") or {})
    for field in ("source", "as_of", "order_submission_used"):
        if field not in snapshot and field in data:
            snapshot[field] = data[field]
    return snapshot


def effective_order_instructions(
    data: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> Any:
    """Return the authoritative post-operation IBKR instruction state."""
    snapshot = snapshot if snapshot is not None else effective_snapshot(data)
    instructions = data.get("order_instructions")
    if instructions is None:
        instructions = snapshot.get("order_instructions")
    if instructions is None:
        instructions = snapshot.get("saved_order_instructions")
    return instructions


def order_instruction_ids(instructions: Any) -> set[str]:
    """Extract connector instruction ids without assuming one field spelling."""
    if not isinstance(instructions, list):
        return set()
    ids = set()
    for instruction in instructions:
        if not isinstance(instruction, Mapping):
            continue
        for key in ORDER_INSTRUCTION_ID_KEYS:
            value = instruction.get(key)
            if value is not None and str(value).strip():
                ids.add(str(value))
                break
    return ids


def current_goal_cause_ids(data: Mapping[str, Any]) -> set[str]:
    """IDs this cycle actually supplied and a new goal may cite."""
    causes = {
        str(value)
        for value in (
            data.get("decision", {}).get("rests_on", ())
            if isinstance(data.get("decision"), Mapping)
            else ()
        )
        if str(value).strip()
    }
    for finding in data.get("findings") or ():
        if isinstance(finding, Mapping) and str(
                finding.get("id", "")).strip():
            causes.add(str(finding["id"]))
    for stage in data.get("cognitive_stages") or ():
        if not isinstance(stage, Mapping):
            continue
        stage_id = str(stage.get("stage_id", "")).strip()
        if stage_id:
            causes.add(stage_id)
    return causes


def validate_goal_observations(
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Refuse invalid goal creation, progress, or closure before execution."""
    from .governance import (
        created_goal_states,
        latest_goal_states,
        validate_goal_close,
        validate_goal_creation,
        validate_goal_progress,
    )

    requests = data.get("goal_observations")
    if not isinstance(requests, list):
        return []
    create_rows = [
        (index, row)
        for index, row in enumerate(requests)
        if isinstance(row, Mapping) and row.get("mode") == "create"
    ]
    errors = []
    if len(create_rows) > 1:
        errors.append("goal_creation_invalid:multiple_creates")
    latest_goals = latest_goal_states(records)
    created_goals = created_goal_states(records)
    open_goals = {
        goal_id: payload
        for goal_id, payload in latest_goals.items()
        if payload.get("status") == "open"
    }
    allowed_causes = current_goal_cause_ids(data)
    cycle_as_of = str(effective_snapshot(data).get("as_of", ""))
    for index, row in create_rows:
        problems = validate_goal_creation(
            row,
            cycle_as_of=cycle_as_of,
            allowed_causes=sorted(allowed_causes),
        )
        goal = row.get("goal")
        goal = goal if isinstance(goal, Mapping) else {}
        goal_id = str(goal.get("goal_id", "")).strip()
        if open_goals:
            problems.append("open_goal_exists")
        if goal_id and goal_id in latest_goals:
            problems.append("goal_id_conflict")
        statement = str(goal.get("statement", "")).strip().casefold()
        metric = str(goal.get("success_metric", "")).strip().casefold()
        for payload in open_goals.values():
            existing = payload.get("goal")
            if not isinstance(existing, Mapping):
                continue
            if statement and statement == str(
                    existing.get("statement", "")).strip().casefold():
                problems.append("duplicate_open_statement")
            if metric and metric == str(
                    existing.get("success_metric", "")).strip().casefold():
                problems.append("duplicate_open_metric")
        errors.extend(
            f"goal_creation_invalid:{index}:{problem}"
            for problem in sorted(set(problems))
        )
    progress_rows = [
        (index, row)
        for index, row in enumerate(requests)
        if isinstance(row, Mapping) and row.get("mode") == "progress"
    ]
    progress_goal_ids = [
        str(row.get("goal_id", "")).strip()
        for _, row in progress_rows
        if str(row.get("goal_id", "")).strip()
    ]
    for goal_id in sorted(set(progress_goal_ids)):
        if progress_goal_ids.count(goal_id) > 1:
            errors.append(
                f"goal_progress_invalid:multiple_progress:{goal_id}")
    for index, row in progress_rows:
        goal_id = str(row.get("goal_id", "")).strip()
        problems = validate_goal_progress(
            row,
            cycle_as_of=cycle_as_of,
            allowed_causes=sorted(allowed_causes),
            open_goal=open_goals.get(goal_id),
        )
        errors.extend(
            f"goal_progress_invalid:{index}:{problem}"
            for problem in sorted(set(problems))
        )
    close_rows = [
        (index, row)
        for index, row in enumerate(requests)
        if isinstance(row, Mapping) and row.get("mode") == "close"
    ]
    close_goal_ids = [
        str(row.get("goal_id", "")).strip()
        for _, row in close_rows
        if str(row.get("goal_id", "")).strip()
    ]
    for goal_id in sorted(set(close_goal_ids)):
        if close_goal_ids.count(goal_id) > 1:
            errors.append(
                f"goal_close_invalid:multiple_closes:{goal_id}")
        if goal_id in progress_goal_ids:
            errors.append(
                f"goal_close_invalid:multiple_updates:{goal_id}")
    for index, row in close_rows:
        goal_id = str(row.get("goal_id", "")).strip()
        problems = validate_goal_close(
            row,
            cycle_as_of=cycle_as_of,
            allowed_causes=sorted(allowed_causes),
            open_goal=open_goals.get(goal_id),
            created_goal=created_goals.get(goal_id),
        )
        errors.extend(
            f"goal_close_invalid:{index}:{problem}"
            for problem in sorted(set(problems))
        )
    full_cycle = is_full_cycle(data)
    legacy_untyped = (
        input_fingerprint(data) in LEGACY_UNTYPED_GOAL_INPUT_FINGERPRINTS
    )
    for index, row in enumerate(requests):
        if not isinstance(row, Mapping):
            errors.append(f"goal_mode_invalid:{index}:not_an_object")
            continue
        mode = row.get("mode")
        if full_cycle:
            if mode is None and legacy_untyped:
                continue
            if not isinstance(mode, str) or not mode.strip():
                errors.append(f"goal_mode_invalid:{index}:missing")
            elif mode == "grade":
                errors.append(f"goal_mode_invalid:{index}:legacy_grade")
            elif mode not in GOAL_OBSERVATION_MODES:
                errors.append(f"goal_mode_invalid:{index}:unknown")
        elif mode not in {
            None, "grade", *GOAL_OBSERVATION_MODES,
        }:
            errors.append(f"goal_close_invalid:{index}:mode_unknown")
    return errors


def _strict_json_equal(left: Any, right: Any) -> bool:
    return json.dumps(
        left, sort_keys=True, separators=(",", ":"),
    ) == json.dumps(
        right, sort_keys=True, separators=(",", ":"),
    )


def _timezone_value(value: Any) -> datetime | None:
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _known_recovery_is_armed(
    input_dir: Path,
    source_name: str,
    source_sha256: str,
) -> bool:
    from .host_publication import load_policy

    policy = load_policy(input_dir)
    if policy is None:
        return False
    recovery_sources = (
        policy.get("required_recovery_sources", ())
    )
    return isinstance(recovery_sources, list) and any(
        isinstance(source, Mapping)
        and source.get("file") == source_name
        and source.get("sha256") == source_sha256
        for source in recovery_sources
    )


def validate_known_instruction_recovery(
    data: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
    input_dir: Path,
) -> list[str]:
    """Require genuine r27 lineage until the staged-instruction proof lands."""
    from .delivery_acceptance import acceptance_status, live_proofs

    if live_proofs(records)["staged_order_instruction"]:
        return []
    contract = acceptance_status(records)["contracts"][
        "staged_order_instruction"
    ]["known_recovery_source"]
    instruction_id = str(contract["instruction_id"])
    source_name = Path(str(contract["source_file"])).name
    if not _known_recovery_is_armed(
        input_dir,
        source_name,
        str(contract["source_sha256"]),
    ):
        return []

    source_path = input_dir / source_name
    if not source_path.exists():
        return [f"known_instruction_recovery_source_missing:{source_name}"]
    try:
        source_bytes = source_path.read_bytes()
        source = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [f"known_instruction_recovery_source_invalid:{source_name}"]
    if hashlib.sha256(source_bytes).hexdigest() != contract["source_sha256"]:
        return [f"known_instruction_recovery_source_hash_mismatch:{source_name}"]
    if not isinstance(source, Mapping):
        return [f"known_instruction_recovery_source_invalid:{source_name}"]

    recovery = data.get("staged_order_instruction_recovery")
    if not isinstance(recovery, Mapping):
        return [f"known_instruction_recovery_required:{instruction_id}"]
    errors = []
    if not is_full_cycle(data):
        errors.append("known_instruction_recovery_requires_schema_version")
    if recovery.get("source_cycle_id") != contract["source_cycle_id"]:
        errors.append(
            f"known_instruction_recovery_source_cycle_mismatch:{instruction_id}")
    if recovery.get("source_sha256") != contract["source_sha256"]:
        errors.append(
            f"known_instruction_recovery_source_hash_required:{instruction_id}")

    source_decision = source.get("decision")
    source_decision = (
        source_decision if isinstance(source_decision, Mapping) else {}
    )
    if recovery.get("source_decision_status") != source_decision.get("status"):
        errors.append(
            f"known_instruction_recovery_decision_status_mismatch:"
            f"{instruction_id}")

    source_activity = source.get("order_instruction_activity")
    source_create = (
        source_activity[0]
        if isinstance(source_activity, list)
        and source_activity
        and isinstance(source_activity[0], Mapping)
        else None
    )
    create_activity = recovery.get("create_activity")
    if not isinstance(create_activity, Mapping):
        errors.append(
            f"known_instruction_recovery_create_missing:{instruction_id}")
    elif source_create is None or not _strict_json_equal(
            create_activity, source_create):
        errors.append(
            f"known_instruction_recovery_create_mismatch:{instruction_id}")

    fresh_get = recovery.get("fresh_get")
    current_as_of = _timezone_value(effective_snapshot(data).get("as_of"))
    if not isinstance(fresh_get, Mapping):
        errors.append(
            f"known_instruction_recovery_fresh_get_required:{instruction_id}")
        fresh_result = None
    else:
        fresh_result = fresh_get.get("result")
        if str(fresh_get.get("operation", "")).lower() != "get":
            errors.append(
                f"known_instruction_recovery_fresh_get_operation:"
                f"{instruction_id}")
        if not str(fresh_get.get("tool", "")).strip():
            errors.append(
                f"known_instruction_recovery_fresh_get_tool:{instruction_id}")
        observed_at = _timezone_value(fresh_get.get("observed_at"))
        if observed_at is None or current_as_of is None:
            errors.append(
                f"known_instruction_recovery_fresh_get_time_invalid:"
                f"{instruction_id}")
        elif observed_at != current_as_of:
            errors.append(
                f"known_instruction_recovery_fresh_get_not_current_cycle:"
                f"{instruction_id}")

    instructions = effective_order_instructions(data)
    fresh_instructions = (
        fresh_result.get("order_instructions")
        if isinstance(fresh_result, Mapping)
        else None
    )
    if not isinstance(fresh_instructions, list) or not _strict_json_equal(
            fresh_instructions, instructions):
        errors.append(
            f"known_instruction_recovery_get_state_mismatch:{instruction_id}")

    status = str(recovery.get("status", "")).lower()
    if status not in {"present", "absent"}:
        errors.append(
            f"known_instruction_recovery_status_invalid:{instruction_id}")
    present_ids = order_instruction_ids(instructions)
    fresh_ids = order_instruction_ids(fresh_instructions)
    if status == "present":
        if instruction_id not in present_ids or instruction_id not in fresh_ids:
            errors.append(
                f"known_instruction_recovery_present_id_missing:"
                f"{instruction_id}")
        instruction = recovery.get("instruction")
        if not isinstance(instruction, Mapping):
            errors.append(
                f"known_instruction_recovery_instruction_required:"
                f"{instruction_id}")
        else:
            for field in (
                "action",
                "quantity",
                "order_type",
                "time_in_force",
                "rationale_one_line",
                "review_condition",
                "rollback_condition",
            ):
                field_value = instruction.get(field)
                if field_value is None or (
                    isinstance(field_value, str) and not field_value.strip()
                ):
                    errors.append(
                        f"known_instruction_recovery_instruction_field:"
                        f"{field}")
            if not any(
                str(instruction.get(field, "")).strip()
                for field in (
                    "symbol", "contract_description", "contract_id_ex",
                )
            ):
                errors.append(
                    "known_instruction_recovery_instruction_identity")
            source_instruction = source_decision.get("instruction")
            source_instruction = (
                source_instruction
                if isinstance(source_instruction, Mapping)
                else {}
            )
            for field in ("action", "quantity", "order_type", "limit_price"):
                if field in source_instruction and not _strict_json_equal(
                        instruction.get(field), source_instruction[field]):
                    errors.append(
                        f"known_instruction_recovery_instruction_mismatch:"
                        f"{field}")
            source_request = (
                source_create.get("request")
                if isinstance(source_create, Mapping)
                and isinstance(source_create.get("request"), Mapping)
                else {}
            )
            if instruction.get("time_in_force") != source_request.get(
                    "time_in_force"):
                errors.append(
                    "known_instruction_recovery_instruction_mismatch:"
                    "time_in_force")
            if instruction.get("contract_id_ex") != source_request.get(
                    "contract_id_ex"):
                errors.append(
                    "known_instruction_recovery_instruction_mismatch:"
                    "contract_id_ex")
    elif status == "absent" and (
        instruction_id in present_ids or instruction_id in fresh_ids
    ):
        errors.append(
            f"known_instruction_recovery_absent_id_present:{instruction_id}")
    return errors


def validate_input(
    data: Mapping[str, Any],
    name: str,
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    input_dir: Path | None = None,
    require_full_schema: bool = False,
    enforce_runtime_time_bounds: bool = False,
    validation_now: datetime | None = None,
) -> list[str]:
    """Refuse an input that cannot support a receipt.

    These are the same requirements the handler factories enforce, checked
    before anything is appended so a bad input fails loudly instead of
    half-writing a cycle into an append-only journal.
    """
    errors: list[str] = []
    snapshot = effective_snapshot(data)
    if input_dir is not None:
        from .schedule_ledger import (
            load_schedule_contract,
            validate_schedule_context,
        )

        profile_root = input_dir.resolve().parent
        try:
            schedule_contract = load_schedule_contract(profile_root)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"schedule_contract_invalid:{exc}")
        else:
            errors.extend(
                validate_schedule_context(
                    data.get("schedule_context"),
                    contract=schedule_contract,
                    candidate_as_of=snapshot.get("as_of"),
                )
            )
    if not isinstance(data.get("snapshot"), Mapping):
        errors.append("missing_snapshot")
    # Disagreement between the two levels is itself a defect: one of them is
    # wrong and nothing here can tell which.
    for field in ("source", "as_of", "order_submission_used"):
        if field in data and field in (data.get("snapshot") or {}):
            if data[field] != data["snapshot"][field]:
                errors.append(f"contradictory_{field}")
    if str(snapshot.get("source", "")).lower() != "ibkr":
        errors.append("source_must_be_ibkr")
    as_of = str(snapshot.get("as_of", "") or "").strip()
    if not as_of:
        errors.append("missing_as_of")
    else:
        parsed = parse_iso_timestamp(as_of)
        if parsed is None:
            errors.append(f"unparseable_as_of:{as_of}")
        else:
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                errors.append(f"as_of_without_timezone:{as_of}")
            elif (
                require_full_schema or enforce_runtime_time_bounds
            ) and parsed > (
                validation_now or datetime.now(timezone.utc)
            ) + MAX_STAGED_FUTURE_SKEW:
                errors.append(f"as_of_in_future:{as_of}")
    declared = snapshot.get("order_submission_used")
    if declared is None:
        errors.append("order_submission_declaration_required")
    elif declared is not False:
        errors.append("live_order_submission_forbidden")
    research = data.get("research")
    if not isinstance(research, list) or not research:
        errors.append("missing_research")
    else:
        # A research pass with no tool_calls is a conclusion without the
        # evidence that produced it, which cannot be audited later. That is
        # the gap that made every earlier cycle unverifiable, so an empty
        # list is refused rather than accepted as "no tools needed": a pass
        # that genuinely used none should not be reported as research.
        for index, row in enumerate(research):
            if not isinstance(row, Mapping):
                errors.append(f"research_entry_not_an_object:{index}")
                continue
            label = row.get("question", index)
            # The comment above promises the conclusion carries the evidence
            # that produced it, but only the presence of a non-empty list was
            # ever checked. A row with no question, no finding and the integer
            # 1 as its sole tool call passed here and then crashed the
            # research handler mid-cycle, after the portfolio stage had
            # already been journaled. Checking the whole shape up front is
            # what makes "refused before anything is appended" true.
            if not str(row.get("question", "")).strip():
                errors.append(f"research_without_question:{index}")
            if not str(row.get("finding", "")).strip():
                errors.append(f"research_without_finding:{label}")
            calls = row.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                errors.append(f"research_without_tool_calls:{label}")
                continue
            for call_index, call in enumerate(calls):
                if not isinstance(call, Mapping):
                    errors.append(f"tool_call_not_an_object:{label}:{call_index}")
                    continue
                if not str(call.get("tool", "")).strip():
                    errors.append(f"tool_call_without_tool:{label}:{call_index}")
                # An empty result is legitimate -- {"orders": []} is a real
                # answer -- but an absent one means nothing came back, and a
                # call that returned nothing is not evidence.
                if "result" not in call:
                    errors.append(f"tool_call_without_result:{label}:{call_index}")
                if is_full_cycle(data) and (
                    schema_version(data) == 4
                    or provenance_required(as_of)
                ):
                    for problem in validate_tool_call_provenance(
                        call,
                        cycle_as_of=as_of,
                        schema_version=schema_version(data),
                        validation_now=validation_now,
                    ):
                        errors.append(
                            f"tool_provenance_invalid:{index}:"
                            f"{call_index}:{problem}"
                        )
    # IBKR is the authority on what instructions exist, not our journal. A
    # cycle that does not look cannot know whether something it staged days
    # ago is still sitting there, transmittable, on a thesis that has since
    # broken. The last three cycles sent no order data at all.
    #
    # An ABSENT field is refused rather than read as "none", the same rule as
    # order_submission_used: not looking and finding nothing are different
    # facts, and only one of them is evidence. An empty list is a real answer
    # and is accepted.
    instructions = effective_order_instructions(data, snapshot)
    if instructions is None:
        errors.append("order_instructions_required")
    elif not isinstance(instructions, list):
        errors.append("order_instructions_must_be_a_list")
    activity = data.get("order_instruction_activity")
    activity_rows: list[Mapping[str, Any]] = []
    if activity is not None:
        if not isinstance(activity, list):
            errors.append("order_instruction_activity_must_be_a_list")
        else:
            for index, row in enumerate(activity):
                if not isinstance(row, Mapping):
                    errors.append(
                        f"order_instruction_activity_not_object:{index}")
                    continue
                activity_rows.append(row)
                operation = str(row.get("operation", "")).lower()
                if operation not in ORDER_INSTRUCTION_OPERATIONS:
                    errors.append(
                        f"order_instruction_operation_invalid:{index}:"
                        f"{operation}")
                if not str(row.get("tool", "")).strip():
                    errors.append(
                        f"order_instruction_tool_required:{index}")
                if "result" not in row:
                    errors.append(
                        f"order_instruction_result_required:{index}")
                if operation in {"create", "delete"} and "request" not in row:
                    errors.append(
                        f"order_instruction_request_required:{index}")
    lifecycle_updates = data.get("instruction_lifecycle_updates")
    if lifecycle_updates is not None:
        if not isinstance(lifecycle_updates, list):
            errors.append("instruction_lifecycle_updates_must_be_a_list")
        else:
            from .lifecycle import validate_event_mapping

            for index, row in enumerate(lifecycle_updates):
                if not isinstance(row, Mapping):
                    errors.append(
                        f"instruction_lifecycle_update_not_object:{index}")
                    continue
                errors.extend(
                    f"instruction_lifecycle_update_invalid:{index}:{error}"
                    for error in validate_event_mapping(row)
                )
                evidence = row.get("evidence")
                if not isinstance(evidence, list) or not evidence:
                    errors.append(
                        f"instruction_lifecycle_evidence_required:{index}")
                    continue
                evidence_tools = []
                for evidence_index, item in enumerate(evidence):
                    if (
                        not isinstance(item, Mapping)
                        or not str(item.get("tool", "")).strip()
                        or "result" not in item
                    ):
                        errors.append(
                            "instruction_lifecycle_evidence_invalid:"
                            f"{index}:{evidence_index}")
                    elif isinstance(item, Mapping):
                        evidence_tools.append(
                            str(item.get("tool", "")).strip().lower())
                to_state = str(row.get("to_state", "")).lower()
                if (
                    to_state in {"approved", "submitted"}
                    and not any("get account orders" in tool
                                for tool in evidence_tools)
                ):
                    errors.append(
                        "instruction_submission_requires_account_orders:"
                        f"{index}")
                if (
                    to_state == "executed"
                    and not any("get account trades" in tool
                                for tool in evidence_tools)
                ):
                    errors.append(
                        f"instruction_execution_requires_account_trades:{index}")
                if (
                    to_state == "deleted"
                    and not any(
                        "delete order instruction" in tool
                        or "operator confirmation" in tool
                        for tool in evidence_tools
                    )
                ):
                    errors.append(
                        f"instruction_deletion_requires_explicit_evidence:"
                        f"{index}")
                if to_state == "deleted":
                    metadata = row.get("metadata")
                    instruction_id = ""
                    if isinstance(metadata, Mapping):
                        instruction_id = str(
                            metadata.get("ibkr_instruction_id", "")
                        ).strip()
                    if not instruction_id:
                        evidence_ids = row.get("evidence_ids")
                        if isinstance(evidence_ids, list) and evidence_ids:
                            instruction_id = str(evidence_ids[0]).strip()
                    if (
                        instruction_id
                        and instruction_id in order_instruction_ids(instructions)
                    ):
                        errors.append(
                            "instruction_deletion_conflicts_with_fresh_"
                            f"connector_state:{index}:{instruction_id}")
    errors.extend(validate_instruction_reconciliations(
        data.get("instruction_reconciliations"),
        data=data,
        records=records or (),
    ))
    errors.extend(validate_instruction_expiry_decisions(
        data.get("instruction_expiry_decisions"),
        data=data,
        records=records or (),
    ))
    errors.extend(validate_mutation_block(data))
    from .lessons import validate_lessons
    errors.extend(validate_lessons(data))
    from .memory import validate_distillation_envelope
    errors.extend(validate_distillation_envelope(
        data.get("memory_distillation")))
    from .tool_inventory import (
        lookalike_manifest_preview,
        validate_tool_manifest_report,
    )
    tool_manifest_report = data.get("tool_manifest_report")
    if not isinstance(tool_manifest_report, Mapping):
        for key in data:
            normalized_key = str(key).casefold()
            for separator in ("_", "-", " "):
                normalized_key = normalized_key.replace(separator, "")
            if (
                key != "tool_manifest_report"
                and normalized_key.startswith("tool")
                and (
                    "manifest" in normalized_key
                    or "inventory" in normalized_key
                )
            ):
                errors.append(
                    f"tool_manifest_lookalike_key_unsupported:{key}")
                errors.extend(lookalike_manifest_preview(data.get(key), key))
    for key in data:
        normalized_key = str(key).casefold().replace("-", "_").replace(
            " ", "_")
        if (
            normalized_key.startswith("forecast_outcome")
            and key != "forecast_outcomes"
        ):
            errors.append(
                f"forecast_outcome_lookalike_key_unsupported:{key}"
            )
    errors.extend(validate_tool_manifest_report(tool_manifest_report))
    errors.extend(validate_tool_probations(
        data.get("tool_probations"),
        data=data,
        records=records or (),
    ))
    for field, expected in (
        ("portfolio_mechanics", Mapping),
        ("historical_backfill", Mapping),
        ("expressions", list),
        ("covered_call_candidates", list),
        ("source_arbitrations", list),
        ("backtests", list),
        ("calibration_requests", list),
        ("experiment_evaluations", list),
        ("goal_observations", list),
    ):
        if field in data and not isinstance(data[field], expected):
            errors.append(f"mechanical_input_invalid:{field}")
    errors.extend(validate_goal_observations(data, records or ()))
    decision = data.get("decision")
    if not isinstance(decision, Mapping):
        errors.append("missing_decision")
    else:
        status = str(decision.get("status", "")).strip().lower()
        if not status:
            errors.append("missing_decision")
        elif status not in ALLOWED_DECISIONS:
            # Any non-empty string used to pass, and the executor silently
            # rewrites an unrecognised status to "blocked". A decision the
            # host meant as one thing being recorded as another is the kind
            # of quiet substitution this runtime exists to prevent.
            errors.append(f"decision_status_not_allowed:{status}")
        if not str(decision.get("rationale", "")).strip():
            errors.append("decision_without_rationale")
        if status == "experiment":
            experiment = decision.get("experiment")
            if not isinstance(experiment, Mapping):
                errors.append("experiment_contract_required")
            else:
                for field in (
                    "hypothesis",
                    "mechanism",
                    "measurement",
                    "counter_metric",
                    "evaluation_window",
                    "rollback_condition",
                ):
                    if not str(experiment.get(field, "")).strip():
                        errors.append(
                            f"experiment_field_required:{field}")
        operations = {
            str(row.get("operation", "")).lower()
            for row in activity_rows
        }
        if status == "recommended":
            instruction = decision.get("instruction")
            if not isinstance(instruction, Mapping):
                errors.append("recommended_instruction_required")
            else:
                for field in (
                    "action",
                    "quantity",
                    "order_type",
                    "time_in_force",
                    "rationale_one_line",
                    "review_condition",
                    "rollback_condition",
                ):
                    value = instruction.get(field)
                    if value is None or (
                        isinstance(value, str) and not value.strip()
                    ):
                        errors.append(
                            f"recommended_instruction_field_required:{field}")
                if not any(
                    str(instruction.get(field, "")).strip()
                    for field in (
                        "symbol", "contract_description", "contract_id_ex",
                    )
                ):
                    errors.append("recommended_instruction_identity_required")
                quantity = instruction.get("quantity")
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, (int, float))
                    or quantity <= 0
                ):
                    errors.append("recommended_instruction_quantity_invalid")
                if (
                    "limit" in str(
                        instruction.get("order_type", "")).lower()
                    and (
                        isinstance(instruction.get("limit_price"), bool)
                        or not isinstance(
                            instruction.get("limit_price"), (int, float))
                        or instruction["limit_price"] <= 0
                    )
                ):
                    errors.append("recommended_instruction_limit_price_invalid")
            if decision.get("instruction_staged") is not True:
                errors.append("recommended_instruction_not_staged")
            instruction_id = str(
                decision.get("ibkr_instruction_id", "")).strip()
            if not instruction_id:
                errors.append("recommended_instruction_id_required")
            if "create" not in operations:
                errors.append("recommended_instruction_create_evidence_required")
            if "get" not in operations:
                errors.append("recommended_instruction_post_get_required")
            if (
                instruction_id
                and isinstance(instructions, list)
                and instruction_id not in order_instruction_ids(instructions)
            ):
                errors.append(
                    "recommended_instruction_missing_from_post_state:"
                    f"{instruction_id}")
        elif "create" in operations:
            errors.append(
                f"instruction_created_without_recommendation:{status}")
    raw_version = data.get("host_input_schema_version")
    if require_full_schema and raw_version is None:
        errors.append("host_input_schema_version_required")
    version = schema_version(data)
    errors.extend(validate_opportunity_updates(
        data.get("opportunity_updates"),
        data=data,
        records=records or (),
        require_research_state=require_full_schema,
    ))
    errors.extend(validate_forecast_registrations(
        data.get("forecast_registrations"),
        data=data,
        records=records or (),
        block_on_overdue=(
            require_full_schema or enforce_runtime_time_bounds
        ),
        now=validation_now,
    ))
    errors.extend(validate_forecast_outcomes(
        data.get("forecast_outcomes"),
        data=data,
        records=records or (),
    ))
    errors.extend(validate_adversarial_disputes(
        data.get("adversarial_disputes"),
        data=data,
        records=records or (),
    ))
    if raw_version is not None:
        if version is None:
            errors.append("invalid_host_input_schema_version")
        else:
            if version not in SUPPORTED_FULL_CYCLE_VERSIONS:
                errors.append(
                    f"unsupported_host_input_schema_version:{version}")
            else:
                require_market_scout = (
                    require_full_schema
                    and version in CANONICAL_STAGED_INPUT_VERSIONS
                )
                errors.extend(validate_full_cycle_stages(
                    data,
                    require_market_scout=require_market_scout,
                ))
                errors.extend(validate_market_scout(
                    data,
                    required=require_market_scout,
                    validation_now=validation_now,
                ))
                errors.extend(validate_tool_call_id_consistency(data))
                if (
                    data.get("evidence_coverage_schema_version") is not None
                    or data.get("evidence_calls") is not None
                    or version == 4
                ):
                    errors.extend(validate_evidence_coverage(
                        data,
                        validation_now=validation_now,
                    ))
                errors.extend(validate_rediscovery_candidates(
                    data,
                    records=records or (),
                    enforce=require_market_scout,
                    cycle_id=str(
                        data.get("cycle_id") or f"cycle-{Path(name).stem}"
                    ),
                ))
                errors.extend(validate_research_allocation(
                    data,
                    required=require_market_scout,
                    records=records or (),
                ))
                from .market_sessions import validate_market_sessions

                errors.extend(validate_market_sessions(
                    data.get("market_sessions"),
                    expected_at=effective_snapshot(data).get("as_of"),
                ))
                if version in LEARNING_DISPOSITION_SCHEMA_VERSIONS:
                    errors.extend(validate_learning_dispositions(
                        data.get("learning_stage_dispositions"),
                        data=data,
                        records=records or (),
                    ))
                if (
                    require_full_schema
                    and version not in CANONICAL_STAGED_INPUT_VERSIONS
                ):
                    errors.append(
                        "staged_host_input_schema_version_required:"
                        f"{version}"
                    )
    if records is not None and input_dir is not None:
        errors.extend(validate_known_instruction_recovery(
            data,
            records=records,
            input_dir=input_dir,
        ))
    return errors


def validate_full_cycle_stages(
    data: Mapping[str, Any],
    *,
    require_market_scout: bool = False,
) -> list[str]:
    """Validate a v2 host-authored cognitive plan and its outputs."""
    rows = data.get("cognitive_stages")
    if not isinstance(rows, list) or not rows:
        return ["cognitive_stages_must_be_nonempty_list"]
    errors: list[str] = []
    ids = []
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            errors.append(f"cognitive_stage_not_an_object:{index}")
            continue
        stage_id = str(row.get("stage_id", "")).strip()
        if not stage_id:
            errors.append(f"cognitive_stage_missing:stage_id:{index}")
            continue
        ids.append(stage_id)
        by_id[stage_id] = row
        for field in ("phase", "status", "output", "tools_used", "depends_on"):
            if field not in row:
                errors.append(f"cognitive_stage_missing:{field}:{stage_id}")
        if not isinstance(row.get("output"), Mapping):
            errors.append(f"cognitive_stage_output_not_object:{stage_id}")
        else:
            output = row["output"]
            missing_output = sorted(COGNITIVE_OUTPUT_FIELDS - set(output))
            if missing_output:
                errors.append(
                    f"cognitive_stage_output_missing:{stage_id}:"
                    + "|".join(missing_output))
            for field in ("observations", "blockers", "next_actions"):
                if field in output and not isinstance(output[field], list):
                    errors.append(
                        f"cognitive_stage_output_not_list:{field}:{stage_id}")
            evidence_status = output.get("evidence_status")
            if (
                "evidence_status" in output
                and evidence_status not in COGNITIVE_EVIDENCE_STATUSES
            ):
                errors.append(
                    f"cognitive_stage_evidence_status_invalid:{stage_id}:"
                    f"{evidence_status}")
            confidence = output.get("confidence")
            if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0.0 <= float(confidence) <= 1.0
            ):
                errors.append(
                    f"cognitive_stage_confidence_invalid:{stage_id}")
            if (
                row.get("status") in {"blocked", "failed"}
                and isinstance(output.get("blockers"), list)
                and not output["blockers"]
            ):
                errors.append(
                    f"cognitive_stage_blockers_empty:{stage_id}")
        if not isinstance(row.get("tools_used"), list):
            errors.append(f"cognitive_stage_tools_not_list:{stage_id}")
        if not isinstance(row.get("depends_on"), list):
            errors.append(f"cognitive_stage_dependencies_not_list:{stage_id}")
        if row.get("status") not in {
            "completed", "blocked", "skipped", "failed",
        }:
            errors.append(
                f"cognitive_stage_status_invalid:{stage_id}:"
                f"{row.get('status')}")
    if len(ids) != len(set(ids)):
        errors.append("duplicate_cognitive_stage_id")
    required_core_stages = FULL_CYCLE_CORE_STAGES
    if not require_market_scout:
        required_core_stages -= {"market_scout"}
    missing = sorted(required_core_stages - set(ids))
    errors.extend(f"full_cycle_stage_missing:{stage_id}" for stage_id in missing)
    for stage_id in FULL_CYCLE_CORE_STAGES.intersection(by_id):
        if by_id[stage_id].get("required") is not True:
            errors.append(f"full_cycle_stage_not_required:{stage_id}")

    specialist_ids = set(ids) - FULL_CYCLE_CORE_STAGES
    for stage_id in sorted(specialist_ids):
        row = by_id[stage_id]
        if row.get("required") is not True:
            errors.append(f"full_cycle_specialist_not_required:{stage_id}")
        if row.get("depends_on") != ["memory_retrieval"]:
            errors.append(
                f"full_cycle_specialist_not_isolated:{stage_id}:"
                "depends_on_must_be_memory_retrieval")
    arbitration = by_id.get("evidence_arbitration")
    if isinstance(arbitration, Mapping):
        arbitration_dependencies = arbitration.get("depends_on")
        if isinstance(arbitration_dependencies, list):
            expected = specialist_ids or {"memory_retrieval"}
            if set(arbitration_dependencies) != expected:
                errors.append(
                    "evidence_arbitration_dependencies_mismatch:"
                    f"expected={','.join(sorted(expected))}:"
                    f"actual={','.join(sorted(map(str, arbitration_dependencies)))}"
                )

    director = by_id.get("research_director")
    agenda: Mapping[str, Any] | None = None
    if isinstance(director, Mapping):
        output = director.get("output")
        if isinstance(output, Mapping):
            candidate = output.get("research_agenda")
            if isinstance(candidate, Mapping):
                agenda = candidate
    if agenda is None:
        errors.append("research_agenda_invalid:missing")
    else:
        drivers = agenda.get("drivers")
        if not isinstance(drivers, list) or not drivers:
            errors.append("research_agenda_invalid:drivers")
        else:
            for index, driver in enumerate(drivers):
                if not isinstance(driver, Mapping):
                    errors.append(
                        f"research_agenda_invalid:driver:{index}:object")
                    continue
                for field in ("observation", "source", "portfolio_relevance"):
                    if not str(driver.get(field, "")).strip():
                        errors.append(
                            "research_agenda_invalid:"
                            f"driver:{index}:{field}")

        candidates = agenda.get("candidates")
        selected_ids: set[str] = set()
        rejected_count = 0
        if not isinstance(candidates, list) or not candidates:
            errors.append("research_agenda_invalid:candidates")
        else:
            candidate_ids: set[str] = set()
            for index, candidate in enumerate(candidates):
                if not isinstance(candidate, Mapping):
                    errors.append(
                        f"research_agenda_invalid:candidate:{index}:object")
                    continue
                for field in (
                    "candidate_id",
                    "instrument",
                    "strategy_family",
                    "trigger",
                    "selection_reason",
                ):
                    if not str(candidate.get(field, "")).strip():
                        errors.append(
                            "research_agenda_invalid:"
                            f"candidate:{index}:{field}")
                candidate_id = str(
                    candidate.get("candidate_id", "")
                ).strip()
                if candidate_id:
                    if candidate_id in candidate_ids:
                        errors.append(
                            "research_agenda_invalid:"
                            f"duplicate_candidate:{candidate_id}")
                    candidate_ids.add(candidate_id)
                selected = candidate.get("selected")
                if not isinstance(selected, bool):
                    errors.append(
                        "research_agenda_invalid:"
                        f"candidate:{index}:selected")
                elif selected:
                    if candidate_id:
                        selected_ids.add(candidate_id)
                    if candidate_id and candidate_id not in specialist_ids:
                        errors.append(
                            "research_agenda_invalid:"
                            f"selected_specialist:{candidate_id}")
                else:
                    rejected_count += 1
                    if not str(candidate.get("rejection_reason", "")).strip():
                        errors.append(
                            "research_agenda_invalid:"
                            f"candidate:{index}:rejection_reason")
            if not selected_ids:
                errors.append("research_agenda_invalid:selected")
            if rejected_count == 0:
                errors.append(
                    "research_agenda_invalid:rejected_alternative")

        if not str(agenda.get("selection_rationale", "")).strip():
            errors.append(
                "research_agenda_invalid:selection_rationale")

        for index, row in enumerate(data.get("research") or ()):
            if not isinstance(row, Mapping):
                continue
            stage_id = str(row.get("specialist_stage_id", "")).strip()
            if not stage_id:
                errors.append(
                    f"research_binding_invalid:{index}:missing")
            elif stage_id not in selected_ids:
                errors.append(
                    f"research_binding_invalid:{index}:not_selected:{stage_id}")

    for stage_id, row in by_id.items():
        if row.get("status") != "completed":
            continue
        depends = row.get("depends_on")
        if not isinstance(depends, list):
            continue
        noncompleted = sorted(
            dependency
            for dependency in depends
            if isinstance(by_id.get(str(dependency)), Mapping)
            and by_id[str(dependency)].get("status") != "completed"
        )
        if noncompleted:
            errors.append(
                f"cognitive_stage_completed_after_noncompleted_dependency:"
                f"{stage_id}:{'|'.join(noncompleted)}")

    jobs = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("stage_id"):
            continue
        depends = row.get("depends_on")
        if not isinstance(depends, list):
            continue
        jobs.append(AgentJob(
            str(row["stage_id"]),
            str(row.get("phase", "")),
            tuple(str(value) for value in depends),
            bool(row.get("required", True)),
            str(row.get("reason", "")),
        ))
    from .orchestrator import validate_plan

    errors.extend(f"cognitive_plan:{error}" for error in validate_plan(jobs))
    decision = data.get("decision")
    decision_stage = by_id.get("decision")
    if isinstance(decision, Mapping) and isinstance(decision_stage, Mapping):
        output = decision_stage.get("output")
        output = output if isinstance(output, Mapping) else {}
        if str(output.get("decision_status", "")).lower() != str(
                decision.get("status", "")).lower():
            errors.append("decision_stage_disagrees_with_decision")
        if not str(output.get("rationale", "")).strip():
            errors.append("decision_stage_missing_rationale")
    return errors


def _mechanical_analysis(data: Mapping[str, Any]) -> dict[str, Any]:
    """Run deterministic components the host explicitly supplied."""
    from .ibkr_backfill import evaluate_host_backfill
    from .effectiveness import evaluate_calibration_requests
    from .engine import evaluate_backtests, evaluate_source_arbitrations
    from .experiments import evaluate_experiment_requests
    from .governance import evaluate_goal_observations
    from .instruments import evaluate_expressions
    from .opportunity import evaluate_covered_calls
    from .research import evaluate_portfolio_mechanics

    result: dict[str, Any] = {}
    portfolio = data.get("portfolio_mechanics")
    if isinstance(portfolio, Mapping):
        result["portfolio"] = evaluate_portfolio_mechanics(portfolio)
    expressions = data.get("expressions")
    if isinstance(expressions, list):
        result["expressions"] = evaluate_expressions(expressions)
    covered_calls = data.get("covered_call_candidates")
    if isinstance(covered_calls, list):
        result["covered_calls"] = evaluate_covered_calls(covered_calls)
    backfill = data.get("historical_backfill")
    if isinstance(backfill, Mapping):
        result["historical_backfill"] = evaluate_host_backfill(backfill)
    arbitrations = data.get("source_arbitrations")
    if isinstance(arbitrations, list):
        result["source_arbitrations"] = evaluate_source_arbitrations(
            arbitrations)
    backtests = data.get("backtests")
    if isinstance(backtests, list):
        result["backtests"] = evaluate_backtests(backtests)
    calibrations = data.get("calibration_requests")
    if isinstance(calibrations, list):
        result["calibration"] = [
            {
                **row,
                "source": "host_supplied_unverified",
            }
            for row in evaluate_calibration_requests(calibrations)
        ]
    experiments = data.get("experiment_evaluations")
    if isinstance(experiments, list):
        result["experiments"] = evaluate_experiment_requests(experiments)
    goals = data.get("goal_observations")
    if isinstance(goals, list):
        result["goals"] = evaluate_goal_observations(goals)
    return result


def _handlers(data: Mapping[str, Any]) -> dict[str, Any]:
    """Handlers that return the host's own observations, unmodified."""
    snapshot = effective_snapshot(data)
    research_rows = [dict(row) for row in data.get("research") or ()
                     if isinstance(row, Mapping)]
    research_trace = [
        {
            "question": row.get("question"),
            "tools": [
                call.get("tool")
                for call in row.get("tool_calls") or ()
                if isinstance(call, Mapping)
            ],
        }
        for row in research_rows
    ]
    ex_ante = build_ex_ante_snapshot(
        portfolio=snapshot,
        evidence=research_rows,
        research_trace=research_trace,
        system_version=str(data.get("system_version", "unknown")),
    )
    snapshot_errors = verify_snapshot_integrity(ex_ante)
    if snapshot_errors:
        raise RuntimeError(
            "ex_ante_snapshot_invalid:" + ",".join(snapshot_errors))
    mechanical = _mechanical_analysis(data)

    def portfolio(job: AgentJob, _context: Any, _deps: Any) -> dict[str, Any]:
        return {"stage": job.agent_id, "status": "completed", "snapshot": snapshot,
                "ibkr_as_of": snapshot.get("as_of"),
                "ex_ante_snapshot_hash": ex_ante["snapshot_hash"],
                "mechanical_analysis": mechanical.get("portfolio"),
                "tools_used": ["Interactive Brokers (IBKR)"]}

    def research(job: AgentJob, _context: Any, _deps: Any) -> dict[str, Any]:
        rows = research_rows
        calls = [c for row in rows for c in (row.get("tool_calls") or [])]
        return {"stage": job.agent_id, "status": "completed", "research": rows,
                "tool_calls": calls,
                "mechanical_analysis": {
                    key: value for key, value in mechanical.items()
                    if key != "portfolio"
                },
                "tools_used": sorted({str(c.get("tool", "unknown")) for c in calls})}

    def decision(job: AgentJob, _context: Any, _deps: Any) -> dict[str, Any]:
        row = dict(data.get("decision") or {})
        return {"stage": job.agent_id, "status": "completed",
                "decision_status": str(row.get("status", "")).lower(),
                # Carried through so the open-recommendation board can tell a
                # staged instruction from a bare proposal. One sits in IBKR
                # and can still be transmitted; the other does not exist
                # anywhere the operator will see it.
                "instruction": row.get("instruction"),
                "instruction_staged": bool(row.get("instruction_staged")),
                "ibkr_instruction_id": row.get("ibkr_instruction_id"),
                "snapshot_hash": ex_ante["snapshot_hash"],
                "ex_ante_snapshot": ex_ante,
                "mechanical_analysis": mechanical,
                "rationale": row.get("rationale"), "rests_on": row.get("rests_on"),
                "findings": list(data.get("findings") or []), "tools_used": []}

    return {"portfolio": portfolio, "research": research, "decision": decision}


def _full_cycle(data: Mapping[str, Any]) -> tuple[list[AgentJob],
                                                   dict[str, Any]]:
    """Replay the exact cognitive plan and outputs the host committed."""
    mechanical = _mechanical_analysis(data)
    snapshot = effective_snapshot(data)
    research_rows = [dict(row) for row in data.get("research") or ()
                     if isinstance(row, Mapping)]
    ex_ante = build_ex_ante_snapshot(
        portfolio=snapshot,
        evidence=research_rows,
        research_trace=[
            {
                "question": row.get("question"),
                "tools": [
                    call.get("tool")
                    for call in row.get("tool_calls") or ()
                    if isinstance(call, Mapping)
                ],
            }
            for row in research_rows
        ],
        system_version=str(data.get("system_version", "unknown")),
    )
    snapshot_errors = verify_snapshot_integrity(ex_ante)
    if snapshot_errors:
        raise RuntimeError(
            "ex_ante_snapshot_invalid:" + ",".join(snapshot_errors))

    jobs = []
    handlers: dict[str, Any] = {}
    for row in data["cognitive_stages"]:
        stage_id = str(row["stage_id"])
        jobs.append(AgentJob(
            stage_id,
            str(row["phase"]),
            tuple(str(value) for value in row["depends_on"]),
            bool(row.get("required", True)),
            str(row.get("reason", "")),
        ))
        committed_output = dict(row["output"])
        committed_output["status"] = row["status"]
        committed_output["tools_used"] = list(row["tools_used"])
        if stage_id == "portfolio":
            committed_output["snapshot"] = snapshot
            committed_output["market_sessions"] = data.get("market_sessions")
            committed_output["ex_ante_snapshot_hash"] = ex_ante[
                "snapshot_hash"]
            committed_output["mechanical_analysis"] = mechanical.get(
                "portfolio")
        elif stage_id == "evidence_arbitration":
            committed_output["mechanical_analysis"] = {
                key: value for key, value in mechanical.items()
                if key != "portfolio"
            }
        elif stage_id == "decision":
            committed_output["decision_status"] = str(
                data["decision"]["status"]).lower()
            committed_output["findings"] = list(data.get("findings") or ())
            committed_output["instruction"] = data["decision"].get(
                "instruction")
            committed_output["instruction_staged"] = data["decision"].get(
                "instruction_staged") is True
            committed_output["ibkr_instruction_id"] = data["decision"].get(
                "ibkr_instruction_id")
            committed_output["order_instruction_activity"] = list(
                data.get("order_instruction_activity") or ())
            committed_output["snapshot_hash"] = ex_ante["snapshot_hash"]
            committed_output["ex_ante_snapshot"] = ex_ante
            committed_output["mechanical_analysis"] = mechanical

        def make_handler(output: Mapping[str, Any]):
            def handler(_job: AgentJob, _context: Any,
                        _dependencies: Any) -> dict[str, Any]:
                return dict(output)
            return handler

        handlers[stage_id] = make_handler(committed_output)
    return jobs, handlers


def persist_order_instruction_activity(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> None:
    """Persist host-observed instruction operations after a completed cycle."""
    activity = data.get("order_instruction_activity")
    if not isinstance(activity, list):
        return
    decision = data.get("decision")
    decision = decision if isinstance(decision, Mapping) else {}
    instructions = effective_order_instructions(data)
    post_ids = order_instruction_ids(instructions)
    cycle_id = str(receipt["cycle_id"])
    receipt_id = f"cycle-receipt:{cycle_id}"
    cycle_as_of = effective_as_of(data)

    for index, row in enumerate(activity):
        if not isinstance(row, Mapping):
            continue
        operation = str(row.get("operation", "")).lower()
        instruction_id = str(
            row.get("instruction_id")
            or decision.get("ibkr_instruction_id")
            or ""
        ).strip()
        record_id = f"order-instruction:{cycle_id}:{index}:{operation}"
        if any(r.get("record_id") == record_id for r in journal.read()):
            continue
        verified_present = (
            operation == "create"
            and bool(instruction_id)
            and instruction_id in post_ids
        )
        verified_absent = (
            operation == "delete"
            and bool(instruction_id)
            and instruction_id not in post_ids
        )
        journal.append(
            record_id=record_id,
            record_type="order_instruction_event",
            agent="sovereign-host",
            caused_by=(receipt_id,),
            payload={
                "cycle_id": cycle_id,
                "decision_status": decision.get("status"),
                "operation": operation,
                "tool": row.get("tool"),
                "request": row.get("request"),
                "result": row.get("result"),
                "instruction": decision.get("instruction"),
                "instruction_id": instruction_id or None,
                "verified_present": verified_present,
                "verified_absent": verified_absent,
                "post_state_count": (
                    len(instructions) if isinstance(instructions, list)
                    else None
                ),
                "order_submission_used": False,
                "at": cycle_as_of,
            },
        )
        if verified_present:
            lifecycle_id = (
                f"lifecycle:{cycle_id}:instruction_created:{instruction_id}")
            if not any(
                r.get("record_id") == lifecycle_id for r in journal.read()
            ):
                journal.append(
                    record_id=lifecycle_id,
                    record_type="lifecycle_event",
                    agent="sovereign-host",
                    caused_by=(receipt_id, record_id),
                    payload={
                        "recommendation_id": cycle_id,
                        "event_id": lifecycle_id,
                        "from_state": "recommended",
                        "to_state": "instruction_created",
                        "evidence_ids": [record_id],
                        "metadata": {
                            "ibkr_instruction_id": instruction_id,
                        },
                    },
                )


def persist_staged_order_instruction_recovery(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> None:
    """Persist recovered historical create lineage without replaying it."""
    recovery = data.get("staged_order_instruction_recovery")
    if not isinstance(recovery, Mapping):
        return
    status = str(recovery.get("status", "")).lower()
    if status not in {"present", "absent"}:
        return
    create_activity = recovery.get("create_activity")
    create_activity = (
        create_activity if isinstance(create_activity, Mapping) else {}
    )
    instruction_id = str(
        create_activity.get("instruction_id", "")
    ).strip()
    if not instruction_id:
        return
    cycle_id = str(receipt["cycle_id"])
    receipt_id = f"cycle-receipt:{cycle_id}"
    record_id = f"order-instruction-recovery:{cycle_id}:{instruction_id}"
    if any(r.get("record_id") == record_id for r in journal.read()):
        return
    fresh_get = recovery.get("fresh_get")
    fresh_get = fresh_get if isinstance(fresh_get, Mapping) else {}
    journal.append(
        record_id=record_id,
        record_type="order_instruction_event",
        agent="sovereign-host",
        caused_by=(receipt_id,),
        payload={
            "cycle_id": cycle_id,
            "decision_status": recovery.get("source_decision_status"),
            "current_decision_status": (
                data.get("decision", {}).get("status")
                if isinstance(data.get("decision"), Mapping)
                else None
            ),
            "operation": (
                "recovered_create" if status == "present"
                else "recovery_absent"
            ),
            "tool": create_activity.get("tool"),
            "request": create_activity.get("request"),
            "result": create_activity.get("result"),
            "instruction": recovery.get("instruction"),
            "instruction_id": instruction_id,
            "verified_present": status == "present",
            "verified_absent": status == "absent",
            "order_submission_used": False,
            "recovered_from_cycle": recovery.get("source_cycle_id"),
            "source_sha256": recovery.get("source_sha256"),
            "fresh_get": dict(fresh_get),
            "at": fresh_get.get("observed_at"),
        },
    )


def persist_instruction_lifecycle_updates(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> None:
    """Persist explicit operator engagement or execution evidence."""
    updates = data.get("instruction_lifecycle_updates")
    if not isinstance(updates, list):
        return
    receipt_id = f"cycle-receipt:{receipt['cycle_id']}"
    for row in updates:
        if not isinstance(row, Mapping):
            continue
        event_id = str(row.get("event_id", "")).strip()
        if not event_id:
            continue
        record_id = (
            event_id if event_id.startswith("lifecycle:")
            else f"lifecycle:{event_id}"
        )
        if any(r.get("record_id") == record_id for r in journal.read()):
            continue
        payload = dict(row)
        payload["event_id"] = record_id
        journal.append(
            record_id=record_id,
            record_type="lifecycle_event",
            agent="sovereign-host",
            caused_by=(receipt_id,),
            payload=payload,
        )


def persist_tool_inventory(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> None:
    """Persist the host-visible connector action inventory."""
    report = data.get("tool_manifest_report")
    if not isinstance(report, Mapping):
        return
    cycle_id = str(receipt["cycle_id"])
    record_id = f"tool-inventory:{cycle_id}"
    if any(r.get("record_id") == record_id for r in journal.read()):
        return
    from .tool_inventory import inventory_diff, latest_tool_inventory

    payload = dict(report)
    previous_inventory = latest_tool_inventory(journal.read())
    payload["changes"] = inventory_diff(previous_inventory, report)
    payload["changes"]["baseline_established"] = not bool(
        previous_inventory)
    journal.append(
        record_id=record_id,
        record_type="tool_inventory",
        agent="sovereign-host",
        caused_by=(f"cycle-receipt:{cycle_id}",),
        payload=payload,
    )


def persist_tool_provenance(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
    *,
    artifact_specs: Mapping[
        tuple[str, int, int], Mapping[str, Any]
    ] | None = None,
) -> bool:
    """Persist one idempotent result-hash and source-reference index."""
    as_of = effective_snapshot(data).get("as_of")
    if not is_full_cycle(data) or (
        data.get("host_input_schema_version") != 4
        and not provenance_required(as_of)
    ):
        return False
    cycle_id = str(receipt["cycle_id"])
    record_id = f"tool-provenance:{cycle_id}"
    existing = next(
        (
            record for record in journal.read()
            if record.get("record_id") == record_id
        ),
        None,
    )
    specs = artifact_specs
    if data.get("host_input_schema_version") == 4:
        specs = specs or build_artifact_specs(
            data,
            records=journal.read(),
        )
        completed_at = parse_iso_timestamp(receipt.get("completed_at"))
        for descriptor in iter_tool_calls(data):
            provenance = descriptor["call"].get("provenance")
            observed_at = parse_iso_timestamp(
                provenance.get("observed_at")
                if isinstance(provenance, Mapping)
                else None
            )
            if (
                observed_at is None
                or completed_at is None
                or observed_at > completed_at
            ):
                raise ValueError(
                    "tool_provenance_after_receipt:"
                    f"{descriptor['call'].get('tool_call_id')}"
                )
    if existing is not None:
        payload = existing.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError(
                f"tool_provenance_payload_mismatch:{record_id}:"
                "payload_not_object"
            )
        errors = persisted_tool_provenance_errors(
            data,
            payload,
            cycle_id=cycle_id,
            recorded_at=existing.get("created_at"),
            artifact_specs=specs,
        )
        if errors:
            raise ValueError(
                f"tool_provenance_payload_mismatch:{record_id}:"
                + "|".join(errors)
            )
        if data.get("host_input_schema_version") == 4:
            materialize_artifacts(
                specs,
                profile_root=profile_root_for_journal(journal.path),
            )
        return False
    if data.get("host_input_schema_version") == 4:
        materialize_artifacts(
            specs,
            profile_root=profile_root_for_journal(journal.path),
        )
    payload = build_tool_provenance_index(
        data,
        cycle_id=cycle_id,
        artifact_specs=specs,
    )
    journal.append(
        record_id=record_id,
        record_type="tool_provenance",
        agent="sovereign-host",
        caused_by=(f"cycle-receipt:{cycle_id}",),
        payload=payload,
    )
    return True


def persist_market_sessions(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> None:
    """Persist source-backed EU/US session state for the completed cycle."""
    if not is_full_cycle(data):
        return
    sessions = data.get("market_sessions")
    if not isinstance(sessions, Mapping):
        return
    cycle_id = str(receipt["cycle_id"])
    record_id = f"market-sessions:{cycle_id}"
    if any(r.get("record_id") == record_id for r in journal.read()):
        return
    journal.append(
        record_id=record_id,
        record_type="market_sessions",
        agent="sovereign-host",
        caused_by=(f"cycle-receipt:{cycle_id}",),
        payload=dict(sessions),
    )


def persist_goal_observations(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> None:
    """Persist validated creation, progress, or closure after the receipt."""
    from .governance import (
        GOAL_TERMINAL_STATUSES,
        created_goal_states,
        goal_terminal_status,
        latest_goal_states,
    )

    requests = data.get("goal_observations")
    if not isinstance(requests, list):
        return
    for request in requests:
        if (
            not isinstance(request, Mapping)
            or request.get("mode") != "create"
            or not isinstance(request.get("goal"), Mapping)
        ):
            continue
        goal = dict(request["goal"])
        goal_id = str(goal.get("goal_id", "")).strip()
        if not goal_id:
            continue
        record_id = f"goal:{goal_id}"
        if any(r.get("record_id") == record_id for r in journal.read()):
            continue
        journal.append(
            record_id=record_id,
            record_type="goal_event",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{receipt['cycle_id']}",),
            payload={
                "goal_id": goal_id,
                "event": "created",
                "status": "open",
                "opened_at": goal.get("created_at"),
                "source_cycle_id": receipt["cycle_id"],
                "goal": goal,
            },
        )
    for request in requests:
        if (
            not isinstance(request, Mapping)
            or request.get("mode") != "progress"
        ):
            continue
        goal_id = str(request.get("goal_id", "")).strip()
        state = latest_goal_states(journal.read()).get(goal_id)
        if not goal_id or not isinstance(state, Mapping):
            continue
        goal = state.get("goal")
        if not isinstance(goal, Mapping):
            continue
        previous_value = state.get(
            "observed_value", goal.get("baseline"))
        observed_value = request.get("observed_value")
        if not isinstance(previous_value, (int, float)) or not isinstance(
                observed_value, (int, float)):
            continue
        target = float(goal["success_target"])
        observed = float(observed_value)
        direction = str(goal.get("direction", ""))
        remaining = (
            max(target - observed, 0.0)
            if direction == "higher_is_better"
            else max(observed - target, 0.0)
        )
        cycle_id = str(receipt["cycle_id"])
        record_id = f"goal:{goal_id}:progress:{cycle_id}"
        if any(r.get("record_id") == record_id for r in journal.read()):
            continue
        journal.append(
            record_id=record_id,
            record_type="goal_event",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{cycle_id}",),
            payload={
                "goal_id": goal_id,
                "event": "progress",
                "status": "open",
                "goal": dict(goal),
                "opened_at": state.get("opened_at"),
                "observed_at": request.get("observed_at"),
                "previous_value": previous_value,
                "observed_value": observed_value,
                "delta": observed - float(previous_value),
                "remaining_to_target": remaining,
                "assessment": request.get("assessment"),
                "evidence": list(request.get("evidence") or ()),
                "cognitive_causes": list(
                    request.get("caused_by") or ()),
                "source_cycle_id": cycle_id,
            },
        )
    for request in requests:
        if (
            not isinstance(request, Mapping)
            or request.get("mode") != "close"
        ):
            continue
        goal_id = str(request.get("goal_id", "")).strip()
        records = journal.read()
        state = latest_goal_states(records).get(goal_id)
        creation = created_goal_states(records).get(goal_id)
        if (
            not goal_id
            or not isinstance(state, Mapping)
            or state.get("status") != "open"
            or not isinstance(creation, Mapping)
        ):
            raise RuntimeError(
                f"goal_close_persistence_state_invalid:{goal_id}")
        goal_value = creation.get("goal")
        if not isinstance(goal_value, Mapping):
            raise RuntimeError(
                f"goal_close_creation_snapshot_missing:{goal_id}")
        goal = dict(goal_value)
        basis = str(request.get("closure_basis", ""))
        terminal_status = (
            "invalidated"
            if basis == "invalidated"
            else goal_terminal_status(
                goal, request.get("observed_value"))
        )
        if terminal_status not in GOAL_TERMINAL_STATUSES:
            raise RuntimeError(
                f"goal_close_terminal_status_invalid:{goal_id}")
        cycle_id = str(receipt["cycle_id"])
        record_id = f"goal:{goal_id}:closed:{cycle_id}"
        if any(r.get("record_id") == record_id for r in records):
            continue
        progress_count = sum(
            1
            for record in records
            if record.get("record_type") == "goal_event"
            and isinstance(record.get("payload"), Mapping)
            and record["payload"].get("goal_id") == goal_id
            and record["payload"].get("event") == "progress"
        )
        partial = goal.get("partial_target")
        partial_available = (
            isinstance(partial, (int, float))
            and not isinstance(partial, bool)
            and math.isfinite(float(partial))
        )
        journal.append(
            record_id=record_id,
            record_type="goal_event",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{cycle_id}",),
            payload={
                "goal_id": goal_id,
                "event": "closed",
                "status": "closed",
                "terminal_status": terminal_status,
                "closure_basis": basis,
                "goal": goal,
                "opened_at": creation.get("opened_at"),
                "closed_at": request.get("observed_at"),
                "created_cycle_id": creation.get("source_cycle_id"),
                "source_cycle_id": cycle_id,
                "last_observed_value": state.get(
                    "observed_value", goal.get("baseline")),
                **({
                    "observed_value": request.get("observed_value"),
                } if basis == "measurement" else {}),
                "partial_grading_available": partial_available,
                "invalidation_reason": request.get(
                    "invalidation_reason"),
                "evidence": list(request.get("evidence") or ()),
                "cognitive_causes": list(
                    request.get("caused_by") or ()),
                "analysis": dict(request.get("analysis") or {}),
                "progress_count": progress_count,
            },
        )


def run_one(path: Path, journal: AuditJournal, *, cycle_id: str | None = None,
            allow_candidate_execution: bool = False) -> dict[str, Any]:
    document = load_input_document(path)
    data = document.hydrated
    persisted_data = document.normalized
    cycle_as_of = effective_as_of(data)
    errors = validate_input(
        data,
        path.name,
        records=journal.read(),
        input_dir=path.parent,
        enforce_runtime_time_bounds=True,
        validation_now=datetime.now(timezone.utc),
    )
    blocking_errors, evidence_advisories = partition_validation_errors(
        data,
        errors,
    )
    if blocking_errors:
        raise ValueError(
            f"invalid_host_input:{path.name}:"
            + ",".join(blocking_errors)
        )

    full_cycle = is_full_cycle(data)
    if full_cycle:
        jobs, handlers = _full_cycle(persisted_data)
        mode = "production-host-full-cycle"
        host_claim = (
            f"the host committed a complete cognitive plan and outputs in "
            f"{path.name}; the executor validated its dependency graph and "
            f"persisted every required stage exactly once.")
    else:
        jobs = [
            AgentJob("portfolio", "observe"),
            AgentJob("research", "research", ("portfolio",)),
            AgentJob("decision", "decide", ("portfolio", "research")),
        ]
        handlers = _handlers(persisted_data)
        mode = "host_input_replay"
        host_claim = (
            f"host reasoning committed in {path.name}; stages executed by "
            "run_host_cycle against that committed input. THREE stages ran: "
            "portfolio, research, decision. Governance, adversarial "
            "re-derivation, counterfactual and learning did NOT run here -- "
            "this replays a committed host input and is not a full cognitive "
            "cycle, which is why mode is host_input_replay rather than "
            "production.")
    executor = ProductionHostExecutor(journal, all_records=load_journal_records)
    _result, receipt, _resume = executor.run(
        jobs=jobs, handlers=handlers,
        mode=mode,
        context={"snapshot_id": f"{path.stem}:{input_fingerprint(data)}",
                 "host_input": path.name},
        cycle_id=str(cycle_id or data.get("cycle_id") or f"cycle-{path.stem}"),
        run_id=str(data.get("run_id") or f"run-{path.stem}-{input_fingerprint(data)}"),
        host_claim=host_claim,
        self_improvement=self_improvement_state(
            data, allow_execution=allow_candidate_execution),
        host_input_schema_version=schema_version(data),
        carry_forward=(
            data.get("carry_forward")
            if isinstance(data.get("carry_forward"), Mapping)
            else None
        ),
        evidence_completeness=(
            "partial" if evidence_advisories else "complete"
        ),
        evidence_advisories=evidence_advisories,
    )
    persist_instruction_reconciliations(
        data,
        journal,
        receipt,
        all_records=load_journal_records(),
    )
    persist_instruction_expiry_decisions(data, journal, receipt)
    persist_goal_observations(data, journal, receipt)
    # Persist any lessons the host drew with THIS input. Its conclusion about
    # how it decides would otherwise die with the cycle that produced it,
    # which is the same cold-start problem as memory and theses, one level up.
    for index, row in enumerate(data.get("lessons") or ()):
        if not isinstance(row, Mapping):
            continue
        lesson_id = str(row.get("lesson_id") or f"{receipt['cycle_id']}:lesson:{index}")
        record_id = f"lesson:{lesson_id}"
        if any(r.get("record_id") == record_id for r in journal.read()):
            continue
        journal.append(
            record_id=record_id, record_type="lesson", agent="sovereign-host",
            caused_by=(f"cycle-receipt:{receipt['cycle_id']}",),
            payload={"lesson_id": lesson_id, "lesson": row.get("lesson"),
                     "evidence": row.get("evidence"),
                     "falsified_if": row.get("falsified_if"),
                     "supersedes": list(row.get("supersedes") or ()),
                     "status": row.get("status", "held"),
                     "at": cycle_as_of})

    distillation = data.get("memory_distillation")
    if isinstance(distillation, Mapping):
        from .memory import evaluate_distillation, evaluate_retirements

        cycle_id = str(receipt["cycle_id"])
        record_id = f"memory-distillation:{cycle_id}"
        records_before = journal.read()
        evaluation_records = _without_record_descendants(
            records_before,
            record_id,
        )
        evaluation = evaluate_distillation(distillation)
        distillation_id = str(distillation["distillation_id"])
        reused_distillation_id = any(
            record.get("record_type") == "memory_distillation"
            and isinstance(record.get("payload"), Mapping)
            and record["payload"].get("distillation_id") == distillation_id
            for record in evaluation_records
        )
        if reused_distillation_id:
            evaluation = dict(evaluation)
            evaluation["admitted"] = False
            evaluation["research_admitted"] = False
            evaluation["errors"] = list(evaluation.get("errors") or ()) + [
                f"distillation_id_reused:{distillation_id}"]
            evaluation["research_errors"] = list(
                evaluation.get("research_errors") or ()) + [
                    f"distillation_id_reused:{distillation_id}"]

        retirement_evaluation = evaluate_retirements(
            distillation,
            evaluation_records,
            research_admitted=(
                evaluation.get("research_admitted") is True),
        )
        research_objects = [
            dict(item)
            for item in distillation.get("memory_objects") or ()
            if isinstance(item, Mapping)
            and item.get("layer") == "research_memory"
        ]
        ignored_objects = [
            {
                "memory_id": item.get("memory_id"),
                "layer": item.get("layer"),
                "disposition": "not_research_memory",
            }
            for item in distillation.get("memory_objects") or ()
            if isinstance(item, Mapping)
            and item.get("layer") != "research_memory"
        ]
        active_proposals = [
            dict(item)
            for item in distillation.get("active_brain_proposals") or ()
            if isinstance(item, Mapping)
        ]
        lifecycle = {
            "research_memory": [
                {
                    "memory_id": item.get("memory_id"),
                    "disposition": (
                        "persisted_available_candidate"
                        if (
                            evaluation.get("research_admitted") is True
                            and item.get("status") == "validated"
                            and item.get("reconstruction_status") == "passed"
                        )
                        else "persisted_inactive"
                    ),
                }
                for item in research_objects
            ],
            "ignored_memory_objects": ignored_objects,
            "active_brain": [
                {
                    "memory_id": item.get("memory_id"),
                    "disposition": (
                        "persisted"
                        if evaluation.get("admitted") is True
                        else "not_admitted"
                    ),
                }
                for item in active_proposals
            ],
            "retirements_applied": [
                row.get("memory_id")
                for row in retirement_evaluation["applied"]
            ],
            "retirements_rejected": list(
                retirement_evaluation["rejected"]),
        }
        distillation_record, _created = journal.append_idempotent(
            record_id=record_id,
            record_type="memory_distillation",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{cycle_id}",),
            payload={
                "distillation_id": distillation_id,
                "source_ids": list(distillation.get("source_ids") or ()),
                "source_time_bounds": dict(
                    distillation.get("source_time_bounds") or {}),
                "brain_version": distillation.get("brain_version"),
                "evaluation": evaluation,
                "lifecycle": lifecycle,
                "blockers": list(distillation.get("blockers") or ()),
                "at": cycle_as_of,
            },
        )
        persisted_payload = distillation_record.get("payload")
        if not isinstance(persisted_payload, Mapping):
            raise ValueError(
                f"memory_distillation_payload_invalid:{record_id}"
            )
        persisted_evaluation = persisted_payload.get("evaluation")
        if not isinstance(persisted_evaluation, Mapping):
            raise ValueError(
                f"memory_distillation_evaluation_invalid:{record_id}"
            )
        evaluation = persisted_evaluation
        for item in research_objects:
            memory_id = str(item.get("memory_id", "")).strip()
            if not memory_id:
                continue
            journal.append_idempotent(
                record_id=_memory_version_record_id(
                    "research-memory", cycle_id, memory_id),
                record_type="research_memory",
                agent="sovereign-host",
                caused_by=(record_id,),
                payload=_research_memory_payload(
                    item, distillation, evaluation),
            )
        if evaluation["admitted"]:
            for item in active_proposals:
                memory_id = str(item.get("memory_id", "")).strip()
                if not memory_id:
                    continue
                payload = dict(item)
                payload.update({
                    "distillation_id": distillation_id,
                    "brain_version": (
                        item.get("brain_version")
                        or distillation.get("brain_version")
                    ),
                    "distillation_admitted": True,
                })
                journal.append_idempotent(
                    record_id=_memory_version_record_id(
                        "memory", cycle_id, memory_id),
                    record_type="memory",
                    agent="sovereign-host",
                    caused_by=(record_id,),
                    payload=payload,
                )
        for retirement in retirement_evaluation["applied"]:
            memory_id = str(retirement.get("memory_id", "")).strip()
            if not memory_id:
                continue
            journal.append_idempotent(
                record_id=_memory_version_record_id(
                    "memory-retirement", cycle_id, memory_id),
                record_type="memory_retirement",
                agent="sovereign-host",
                caused_by=(record_id,),
                payload={
                    **dict(retirement),
                    "distillation_id": distillation_id,
                    "brain_version": distillation.get("brain_version"),
                    "at": cycle_as_of,
                },
                )

    persist_order_instruction_activity(data, journal, receipt)
    persist_staged_order_instruction_recovery(data, journal, receipt)
    persist_instruction_lifecycle_updates(data, journal, receipt)
    persist_tool_inventory(data, journal, receipt)
    persist_tool_probations(data, journal, receipt)
    persist_market_sessions(data, journal, receipt)
    persist_opportunity_updates(data, journal, receipt)
    persist_forecast_registrations(
        data,
        journal,
        receipt,
        all_records=load_journal_records(),
    )
    persist_forecast_outcomes(
        data,
        journal,
        receipt,
        all_records=load_journal_records(),
    )
    persist_adversarial_disputes(data, journal, receipt)
    persist_learning_dispositions(data, journal, receipt)
    if receipt.get("evidence_completeness") != "partial":
        persist_tool_provenance(data, journal, receipt)
    persist_cycle_finalization(
        data,
        journal,
        receipt,
        input_name=path.name,
        required_record_types=required_finalization_record_types(
            data,
            receipt,
        ),
    )
    return receipt


def backfill_research_memory(
    inputs: Sequence[Path],
    journal: AuditJournal,
) -> int:
    """Append missing Research Memory records from already accepted cycles."""
    from .memory import validate_memory_object

    added = 0
    records = journal.read()
    existing_ids = {
        str(record.get("record_id", ""))
        for record in records
    }
    for path in inputs:
        try:
            data = load_input(path)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        cycle_id = str(data.get("cycle_id") or f"cycle-{path.stem}")
        receipt_id = f"cycle-receipt:{cycle_id}"
        if receipt_id not in existing_ids:
            continue
        distillation = data.get("memory_distillation")
        if not isinstance(distillation, Mapping):
            continue
        distillation_record = next(
            (
                record
                for record in records
                if record.get("record_type") == "memory_distillation"
                and receipt_id in record.get("caused_by", ())
            ),
            None,
        )
        if not isinstance(distillation_record, Mapping):
            continue
        distillation_payload = distillation_record.get("payload")
        if not isinstance(distillation_payload, Mapping):
            continue
        evaluation = distillation_payload.get("evaluation")
        if not isinstance(evaluation, Mapping):
            continue
        for item in distillation.get("memory_objects") or ():
            if (
                not isinstance(item, Mapping)
                or item.get("layer") != "research_memory"
                or not validate_memory_object(item).valid
            ):
                continue
            memory_id = str(item.get("memory_id", "")).strip()
            if not memory_id:
                continue
            record_id = _memory_version_record_id(
                "research-memory", cycle_id, memory_id)
            if record_id in existing_ids:
                continue
            journal.append(
                record_id=record_id,
                record_type="research_memory",
                agent="sovereign-runtime",
                caused_by=(str(distillation_record["record_id"]),),
                payload={
                    **_research_memory_payload(
                        item, distillation, evaluation),
                    "backfilled": True,
                },
            )
            existing_ids.add(record_id)
            added += 1
    return added


def already_persisted(journal: AuditJournal, cycle_id: str) -> bool:
    return any(r.get("record_id") == f"cycle-receipt:{cycle_id}" for r in journal.read())


def persisted_snapshot_id(records: Sequence[Mapping[str, Any]], cycle_id: str) -> str | None:
    for record in records:
        if record.get("record_id") == f"cycle-receipt:{cycle_id}":
            payload = record.get("payload") or {}
            return str(payload.get("snapshot_id", "")) or None
    return None


def latest_mechanical_analysis(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Most recent persisted decision-stage mechanical output."""
    for record in reversed(list(records)):
        if record.get("record_type") != "cycle_stage":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping) or payload.get(
                "stage_id") != "decision":
            continue
        output = payload.get("output")
        if not isinstance(output, Mapping):
            continue
        analysis = output.get("mechanical_analysis")
        if isinstance(analysis, Mapping):
            return dict(analysis)
    return {}


HOST_INPUT_COMMITTED = "host_input_committed"
PUBLICATION_UNVALIDATED = "publication_unvalidated"
EXECUTION_VERIFIED = "execution_verified"
EXECUTION_FAILED = "execution_failed"


def all_host_input_paths(input_dir: Path | str) -> list[Path]:
    """Cycle inputs only, excluding runtime-owned feedback and state files."""
    return [
        path for path in sorted(Path(input_dir).glob("*.json"))
        if path.name != FEEDBACK_FILENAME and not path.name.startswith(".")
    ]


def host_input_paths(input_dir: Path | str) -> list[Path]:
    """Canonical inputs only; staged or direct unvalidated files are inert."""
    from .host_publication import is_promoted_input, load_policy

    policy = load_policy(input_dir)
    return [
        path for path in all_host_input_paths(input_dir)
        if is_promoted_input(path, policy)
    ]


def execution_status(input_dir: Path | str,
                     records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-input state, DERIVED from the journal rather than declared.

    "The host succeeded" and "the cycle executed" are different facts, and
    collapsing them is how a system reports E2E PASS when only the front half
    ran. A committed input with no receipt is not a failure of the host; it
    is a cycle that has not been executed yet, and it must not read as either
    success or as the host's fault.

    Derived because a status field somebody writes by hand drifts from what
    it describes and then outranks it -- which is exactly what STATE.json did
    while claiming the first receipt was still pending.
    """
    from .cycle_receipt import ALLOWED_DECISIONS, validate_audit_receipt_record

    receipts = {
        str(r.get("record_id", "")): r for r in records
        if r.get("record_type") == "cycle_receipt"
    }
    rows: list[dict[str, Any]] = []
    from .host_publication import is_promoted_input, load_policy

    policy = load_policy(input_dir)
    for path in all_host_input_paths(input_dir):
        if not is_promoted_input(path, policy):
            rows.append({
                "input": path.name,
                "state": PUBLICATION_UNVALIDATED,
                "receipt": None,
                "detail": (
                    "no matching promotion marker; the executor will ignore "
                    "this direct publication"
                ),
            })
            continue
        try:
            data = load_input(path)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            rows.append({
                "input": path.name,
                "state": EXECUTION_FAILED,
                "receipt": None,
                "detail": f"input unreadable: {error}",
            })
            continue
        fingerprint = f"{path.stem}:{input_fingerprint(data)}"
        matched = [
            r for r in receipts.values()
            if str((r.get("payload") or {}).get("snapshot_id", "")) == fingerprint
        ]
        if not matched:
            rows.append({"input": path.name, "state": HOST_INPUT_COMMITTED,
                         "receipt": None,
                         "detail": "no receipt carries this input's fingerprint; "
                                   "the executor has not run it"})
            continue
        record = matched[0]
        errors = validate_audit_receipt_record(record)
        rows.append({
            "input": path.name,
            "state": EXECUTION_VERIFIED if not errors else EXECUTION_FAILED,
            "receipt": record.get("record_id"),
            "detail": "receipt validates" if not errors else ";".join(errors),
        })
    return rows


_MUTATION_FIELDS = (
    "mutation_id", "parent_version", "mutation_type", "targets", "rationale",
    "failure_ids", "patch", "expected_effect", "counter_metrics",
    "sample_requirement", "evaluation_window", "rollback_condition",
    "created_at",
)


def validate_mutation_block(data: Mapping[str, Any]) -> list[str]:
    """Check a host-proposed mutation before anything acts on it."""
    mutation = data.get("mutation")
    if mutation is None:
        return []
    if not isinstance(mutation, Mapping):
        return ["mutation_not_an_object"]
    return [f"mutation_missing_field:{field}" for field in _MUTATION_FIELDS
            if not str(mutation.get(field, "")).strip()
            and not isinstance(mutation.get(field), (list, tuple, int))]


def self_improvement_state(data: Mapping[str, Any], *,
                           allow_execution: bool = False) -> dict[str, Any]:
    """What the self-improvement stage actually did this cycle.

    This used to be hardcoded to {"status": "none"} regardless of what the
    host sent, which made the whole stack unreachable: evaluate_mutation had
    no production caller at all, so the sandbox, measurement and evidence
    gates were only ever exercised by their own tests. A receipt that reports
    "none" for a cycle where the host DID propose a mutation is also simply
    false.

    Executing a candidate is held behind an explicit opt-in. The sandbox is
    isolated, but measurement runs the candidate's code to compare it against
    baseline, and an unattended hourly loop that executes model-proposed
    patches on the operator's machine is a decision the operator makes, not
    one this module makes quietly by being wired up. Without the opt-in the
    proposal is recorded and the verdict is withheld -- which is reported as
    withheld, not as a pass.
    """
    mutation = data.get("mutation")
    if not isinstance(mutation, Mapping):
        return {"status": "none", "mutation_ids": [], "gates": {}}
    mutation_id = str(mutation.get("mutation_id", "")) or "unnamed"
    if not allow_execution:
        return {
            "status": "proposed_not_evaluated",
            "mutation_ids": [mutation_id],
            "gates": {"candidate_execution": "not_enabled"},
            "note": ("The host proposed this mutation. Evaluating it means "
                     "running its code, which requires explicit operator "
                     "opt-in (--allow-candidate-execution). No verdict was "
                     "produced, and none is implied."),
        }
    try:
        verdict = evaluate_proposed_mutation(mutation)
    except Exception as error:
        # A proposal is optional extra work attached to a cycle. The sandbox
        # refusing it, or the measurement harness failing, says something
        # about the PROPOSAL; it says nothing about the portfolio observation
        # or the decision, and letting it propagate would refuse the whole
        # input over a bad patch. Recorded as a failed evaluation, which is
        # not the same as a rejection on evidence.
        return {
            "status": "evaluation_failed",
            "mutation_ids": [mutation_id],
            "gates": {"candidate_execution": "errored"},
            "note": f"{type(error).__name__}: {error}",
        }
    return {
        "status": verdict["status"],
        "mutation_ids": [mutation_id],
        "gates": verdict["gates"],
        "note": verdict["reason"],
    }


def evaluate_proposed_mutation(mutation: Mapping[str, Any]) -> dict[str, Any]:
    """Sandbox, measure and rule on one host-proposed mutation.

    Imported here rather than at module scope on purpose: these are the only
    parts of the runtime that need a shell, and a host with repository access
    but no shell must still be able to run a full cognitive cycle.
    """
    from .measurement import default_tasks, measure_candidate
    from .sandbox import run_candidate
    from .self_improvement import MutationProposal, evaluate_mutation

    repo_root = code_root()
    proposal = MutationProposal(**{
        field: tuple(mutation[field]) if field in ("targets", "failure_ids",
                                                   "counter_metrics")
        else mutation[field]
        for field in _MUTATION_FIELDS
    })
    report = run_candidate(proposal, repo_root=repo_root)
    # default_tasks() reads the committed corpus. A caller that assembles its
    # own tasks per run can pick ones that flatter the candidate.
    suite = measure_candidate(proposal, default_tasks(), repo_root=repo_root)
    verdict = evaluate_mutation(
        proposal, sandbox_report=report, measurement_suite=suite,
        sample_size=int(mutation.get("sample_size", 0)),
        primary_delta=None,
        counter_metric_deltas=dict(mutation.get("counter_metric_deltas") or {}),
        counter_metric_directions=dict(
            mutation.get("counter_metric_directions") or {}),
        out_of_sample=False,
        sandbox_passed=report.passed,
        rollback_triggered=False,
    )
    return {"status": verdict.status, "reason": verdict.reason,
            "gates": {"sandbox": "passed" if report.passed else "failed",
                      "measurement": "produced",
                      "evidence": verdict.reason}}


def input_age_minutes(path: Path) -> float | None:
    """How long ago this input landed, by commit time, or None if unknown.

    Commit time rather than mtime: a fresh checkout stamps every file with
    the clone time, so on a CI runner mtime says "seconds old" for an input
    committed hours ago, which would make a staleness gate refuse everything
    forever.

    Returns None when git cannot answer. The caller treats unknown as "do not
    skip", because refusing to execute an input on the strength of an age
    nobody could measure would strand it permanently.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", path.name],
            capture_output=True, text=True, timeout=30, cwd=path.parent)
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = result.stdout.strip()
    if result.returncode != 0 or not stamp:
        return None
    try:
        committed = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return (datetime.now(timezone.utc) - committed).total_seconds() / 60.0


def record_refusal(
    journal: AuditJournal,
    path: Path,
    reason: str,
    *,
    pass_id: str | None = None,
) -> bool:
    """Persist why one input was refused, so the host can read it next run.

    One malformed file used to abort the whole pass, so every LATER valid
    cycle was lost to it: the host drifted once at 13:03, corrected itself by
    14:01, and the corrected cycle never ran because the runner never got
    past the bad one. A refusal must not become a queue-wide poison pill.

    It must also not be silent. An input that was refused leaves a durable,
    machine-readable record keyed by its content, which is the feedback the
    host needs to fix itself without a human reading a terminal. Keyed by
    content so re-running does not append the same refusal twice, and so a
    CORRECTED file of the same name records a new one.
    """
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    record_id = f"host-input-refusal:{path.stem}:{fingerprint}"
    if any(r.get("record_id") == record_id for r in journal.read()):
        return False
    payload = {
        "input": path.name,
        "input_sha256_12": fingerprint,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if pass_id:
        payload["pass_id"] = pass_id
    journal.append(
        record_id=record_id, record_type="host_input_refusal",
        agent="sovereign-runtime", caused_by=(),
        payload=payload,
    )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir",
                        default=str(profile_root() / "host_input"))
    parser.add_argument("--journal", default=None,
                        help="journal file; defaults to the newest in audit/")
    parser.add_argument("--status", action="store_true",
                        help="report per-input execution state and exit without running")
    parser.add_argument(
        "--allow-candidate-execution", action="store_true",
        help="Evaluate a host-proposed mutation by actually running it. The "
             "sandbox is isolated, but measurement executes the candidate's "
             "code to compare it against baseline. Off by default: an "
             "unattended hourly loop that runs model-proposed patches is the "
             "operator's decision, not a side effect of the stage existing.")
    parser.add_argument(
        "--min-input-age-minutes", type=float, default=0.0,
        help="Skip inputs that landed more recently than this. Used by the "
             "fallback executor so the primary one, which polls far more "
             "often, always gets first refusal and the two cannot both "
             "append to the journal for the same cycle.")
    parser.add_argument("--cycle-id", default=None,
                        help=("identity for this run, when the input file was refreshed in "
                              "place. Preferred over editing a host-committed file, which is "
                              "evidence: the name is addressing, the content is the record."))
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.status:
        for row in execution_status(args.input_dir, load_journal_records()):
            print(f"{row['state']:22s} {row['input']:34s} {row['detail']}")
        return 0

    journal_path = (
        Path(args.journal)
        if args.journal
        else sorted(JOURNAL_DIR.glob("*.jsonl"))[-1]
    )
    journal = AuditJournal(journal_path)
    profile_root = Path(args.input_dir).resolve().parent
    refusal_count = sync_rejection_ledger(
        profile_root / "host_staging" / "rejected" / "REJECTIONS.jsonl",
        journal,
    )
    if refusal_count:
        print(
            f"backfilled {refusal_count} staged refusal record(s) "
            "into the audit journal"
        )

    # The feedback file lives in the input directory so the host reads it in
    # the same place it writes, but it is the runtime's reply, not an input.
    # Globbing it made the runner refuse its own message every pass.
    # pathlib's glob matches dotfiles, unlike a shell's. A host input is
    # never hidden, while the runtime's own bookkeeping (.high_water.json)
    # is, so an unfiltered glob hands the executor its own state file and
    # refuses it as a malformed cycle.
    inputs = host_input_paths(args.input_dir)
    if not inputs:
        print(f"no host input in {args.input_dir}/; nothing to run")
        return 0

    ran = 0
    refused = 0
    incomplete = 0
    pass_id = (
        "executor-pass:"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    accepted: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    incomplete_executions: list[dict[str, Any]] = []
    skipped: list[str] = []
    for path in inputs:
        data: dict[str, Any] | None = None
        cycle_id: str | None = None
        # The fallback executor must not race the primary one. Both append to
        # one hash chain, and two writers picking the same parent is how an
        # append-only journal forks irreversibly. A minimum age means the
        # local agent, which polls far more often, always gets first refusal;
        # the fallback only ever sees what the laptop was asleep for.
        if args.min_input_age_minutes > 0:
            age = input_age_minutes(path)
            if age is not None and age < args.min_input_age_minutes:
                print(f"{path.name}: {age:.1f} min old, leaving it to the "
                      f"primary executor")
                continue
        # Each input stands or falls on its own. Raising out of the loop
        # meant one bad file outranked every good one behind it.
        try:
            data = load_input(path)
            cycle_id = str(args.cycle_id or data.get("cycle_id") or f"cycle-{path.stem}")
            if already_persisted(journal, cycle_id):
                # Re-running must not append the same cycle twice. But a file
                # REWRITTEN in place -- which is what a daily cron does when it
                # runs twice in one day -- keeps the same derived cycle_id while
                # carrying different analysis. Skipping that silently discards a
                # real cycle and prints a line that reads like success.
                previous = persisted_snapshot_id(journal.read(), cycle_id)
                current = f"{path.stem}:{input_fingerprint(data)}"
                # A receipt persisted before snapshot_id carried a fingerprint
                # has no hash to compare, so it cannot be judged either way.
                # A receipt persisted before inputs were fingerprinted has
                # nothing to compare against. That is NOT evidence the input is
                # unchanged, and treating it as such let the first real rewritten
                # input through silently -- the exact bug, surviving inside the
                # carve-out written to avoid a cosmetic failure on one legacy
                # record.
                #
                # Cannot-verify-it-is-the-same is not it-is-the-same, the same
                # distinction as unknown-is-not-zero and silence-is-not-a-denial
                # elsewhere here.
                comparable = previous is not None and ":" in previous
                if previous is None or not comparable:
                    raise ValueError(
                        f"cannot_verify_input_unchanged:{path.name}: cycle {cycle_id} was "
                        f"persisted before inputs were fingerprinted, so this file cannot be "
                        f"shown to be the same one. Give this cycle its own cycle_id."
                    )
                if previous != current:
                    raise ValueError(
                        f"input_changed_after_persist:{path.name}: cycle {cycle_id} was "
                        f"persisted from different content. Give this cycle its own "
                        f"cycle_id (or filename) rather than overwriting the previous one."
                    )
                receipt = next(
                    (
                        record.get("payload")
                        for record in journal.read()
                        if record.get("record_type") == "cycle_receipt"
                        and isinstance(record.get("payload"), Mapping)
                        and record["payload"].get("cycle_id") == cycle_id
                    ),
                    None,
                )
                if not isinstance(receipt, Mapping):
                    raise ValueError(
                        f"persisted_receipt_payload_missing:{cycle_id}"
                    )
                finalization_id = finalization_record_id(cycle_id)
                has_finalization = any(
                    record.get("record_id") == finalization_id
                    for record in journal.read()
                )
                requires_finalization = (
                    receipt.get("finalization_schema_version") == 1
                )
                if not requires_finalization:
                    pass
                elif has_finalization:
                    if (
                        receipt.get("evidence_completeness")
                        != "partial"
                    ):
                        persist_tool_provenance(
                            data,
                            journal,
                            receipt,
                        )
                    persist_cycle_finalization(
                        data,
                        journal,
                        receipt,
                        input_name=path.name,
                        required_record_types=(
                            required_finalization_record_types(
                                data,
                                receipt,
                            )
                        ),
                    )
                else:
                    run_one(
                        path,
                        journal,
                        cycle_id=cycle_id,
                        allow_candidate_execution=(
                            args.allow_candidate_execution
                        ),
                    )
                    print(
                        f"{path.name}: recovered incomplete finalization "
                        f"for {cycle_id}"
                    )
                print(f"{path.name}: already persisted as {cycle_id}, skipping")
                skipped.append(path.name)
                continue
            receipt = run_one(path, journal, cycle_id=cycle_id,
                              allow_candidate_execution=args.allow_candidate_execution)
            ran += 1
            accepted.append({"input": path.name, "cycle_id": receipt["cycle_id"],
                             "status": receipt["status"],
                             "decision_status": receipt["decision_status"]})
            print(f"{path.name}: persisted {receipt['cycle_id']} "
                  f"status={receipt['status']} decision={receipt['decision_status']}")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            previous = (
                persisted_snapshot_id(journal.read(), cycle_id)
                if cycle_id is not None
                else None
            )
            current = (
                f"{path.stem}:{input_fingerprint(data)}"
                if isinstance(data, Mapping)
                else None
            )
            if previous is not None and previous == current:
                reason = f"{type(error).__name__}: {error}"
                incomplete += 1
                incomplete_executions.append({
                    "input": path.name,
                    "cycle_id": cycle_id,
                    "reason": reason,
                })
                print(f"{path.name}: INCOMPLETE {reason}")
                continue
            # Refused, recorded, and the pass continues. The record is the
            # feedback channel back to the host, which cannot read a
            # terminal; the non-zero exit is the alert to the operator, who
            # can.
            reason = f"{type(error).__name__}: {error}"
            # record_refusal reports whether this content was already refused.
            # A file the host cannot or will not fix sits here forever, and
            # counting it every hour made the exit code a constant alarm,
            # which is the same as no alarm. Only a refusal nobody has seen
            # before is news; the rest is still printed and still in the
            # journal, it simply does not re-raise the alert.
            is_new = record_refusal(
                journal, path, reason, pass_id=pass_id)
            refusals.append({"input": path.name, "reason": reason,
                             "first_seen_this_pass": is_new})
            if is_new:
                refused += 1
            print(f"{path.name}: REFUSED{'' if is_new else ' (already recorded)'} "
                  f"{reason}")
            continue
    backfill_inputs = [] if incomplete else inputs
    backfilled_memory = backfill_research_memory(backfill_inputs, journal)
    if backfilled_memory:
        print(
            f"backfilled {backfilled_memory} research memory record(s) "
            "from accepted inputs")
    backfilled_opportunities = backfill_opportunity_events(
        backfill_inputs,
        journal,
    )
    if backfilled_opportunities:
        print(
            f"backfilled {backfilled_opportunities} opportunity event(s) "
            "from accepted inputs")
    backfilled_forecasts = backfill_forecast_registrations(
        backfill_inputs,
        journal,
        all_records=load_journal_records(),
    )
    if backfilled_forecasts:
        print(
            f"backfilled {backfilled_forecasts} forecast record(s) "
            "from accepted inputs")
    backfilled_forecast_outcomes = backfill_forecast_outcomes(
        backfill_inputs,
        journal,
        all_records=load_journal_records(),
    )
    if backfilled_forecast_outcomes:
        print(
            "backfilled "
            f"{backfilled_forecast_outcomes} forecast outcome record(s) "
            "from accepted inputs")
    backfilled_instruction_reconciliations = (
        backfill_instruction_reconciliations(
            backfill_inputs,
            journal,
            all_records=load_journal_records(),
        )
    )
    if backfilled_instruction_reconciliations:
        print(
            "backfilled "
            f"{backfilled_instruction_reconciliations} instruction "
            "reconciliation record(s) from accepted inputs")
    backfilled_disputes = backfill_adversarial_disputes(
        backfill_inputs, journal)
    if backfilled_disputes:
        print(
            f"backfilled {backfilled_disputes} adversarial dispute "
            "record(s) from accepted inputs")
    # Advance the truncation mark after any work. It is monotonic, so this
    # cannot launder a truncation by running afterwards; it only ever records
    # that the journal has been at least this long.
    from .high_water import update_mark
    update_mark(journal.read(), journal_path.parent)

    # Written every pass, including a clean one, so the host can always tell
    # the difference between "my last input was accepted" and "the runner has
    # not run since I committed". A file that only appears on failure is
    # indistinguishable from a stale one.
    from .recommendations import summarise
    from .reliability import operational_reliability
    from .decision_outcomes import summarise as outcome_summary
    from .calibration_dataset import empirical_calibration_summary
    from .delivery_acceptance import acceptance_status
    from .governance import summarise_goal_attribution, summarise_goals
    from .lessons import summarise as lesson_summary
    from .memory import (
        active_memory,
        latest_distillation_evaluation,
        research_memory,
    )
    from .market_sessions import latest_market_sessions
    from .theses import summarise as thesis_summary
    from .strategy_coverage import (
        candidates_for, coverage, load_accepted_inputs, open_experiments,
        recent_reasoning, research_agenda_summary, source_coverage)
    from .tool_inventory import tool_inventory_feedback
    records = journal.read()
    recent_inputs, recent_input_selection = load_accepted_inputs(
        args.input_dir, records)
    feedback = write_feedback(
        Path(args.input_dir), accepted=accepted, refusals=refusals,
        skipped=skipped, incomplete_executions=incomplete_executions,
        open_recommendations=summarise(records),
        strategy_coverage=coverage(recent_inputs),
        source_coverage=source_coverage(recent_inputs),
        recent_reasoning=recent_reasoning(recent_inputs),
        research_agenda=research_agenda_summary(recent_inputs),
        market_scout=market_scout_summary(records),
        candidate_registry=candidate_registry_summary(records),
        opportunity_ledger=opportunity_ledger_summary(records),
        forecast_ledger=forecast_ledger_summary(records),
        forecast_outcomes=forecast_outcome_summary(records),
        instruction_reconciliation=instruction_reconciliation_summary(
            records),
        instruction_expiry=instruction_expiry_summary(
            records,
            latest_input=(
                recent_inputs[-1] if recent_inputs else None
            ),
        ),
        empirical_calibration=empirical_calibration_summary(records),
        research_value_census=research_value_census(records),
        research_inbox=research_inbox_summary(
            Path(args.input_dir).resolve().parent
        ),
        learning_dispositions=learning_disposition_summary(records),
        goals=summarise_goals(records),
        goal_attribution=summarise_goal_attribution(records),
        recent_input_selection=recent_input_selection,
        reliability=operational_reliability(
            records, journal_path=journal_path),
        open_experiments=open_experiments(recent_inputs),
        theses=thesis_summary(),
        decision_outcomes=outcome_summary(recent_inputs),
        lessons=lesson_summary(records),
        research_candidates=candidates_for(recent_inputs),
        active_memory={"count": len(active_memory(records)),
                       "items": active_memory(records)},
        research_memory=research_memory(records),
        memory_distillation=latest_distillation_evaluation(records),
        tool_inventory=tool_inventory_feedback(
            records, journal_path=journal_path),
        tool_probation=tool_probation_summary(records),
        tool_provenance=latest_tool_provenance(records),
        market_sessions=latest_market_sessions(records),
        mechanical_analysis=latest_mechanical_analysis(records),
        delivery_probes=acceptance_status(records))
    print(f"feedback for the host written to {feedback}")
    if ran == 0 and not refused and not incomplete:
        print("every input was already persisted; journal unchanged")
    if refused or incomplete:
        # Valid cycles still ran. The non-zero exit says something was
        # refused, which is not the same as saying nothing worked.
        print(
            f"{refused} input(s) refused, "
            f"{incomplete} cycle(s) incomplete, "
            f"{ran} cycle(s) persisted"
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
