"""Live proof for delivery claims that tests alone cannot satisfy."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .delivery_state import parse_delivery_plan


FULL_CYCLE_STAGES = frozenset({
    "portfolio",
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

LIVE_PROOF_ITEMS = {
    "full_cycle_receipt": ("XL-02", "EFF-05"),
    "active_memory": ("HH-08", "XL-11"),
    "staged_order_instruction": ("HH-13",),
    "complete_tool_inventory": ("HH-14",),
    "market_session_awareness": ("HH-15",),
}
STALE_LEDGER_PREFIX = "live_proof_landed_but_status_stale:"


def _is_full_cycle_receipt(record: Mapping[str, Any]) -> bool:
    payload = record.get("payload")
    if record.get("record_type") != "cycle_receipt" or not isinstance(
            payload, Mapping):
        return False
    stages = {
        str(stage.get("stage_id"))
        for stage in payload.get("stages", ())
        if isinstance(stage, Mapping)
        and stage.get("status") == "completed"
    }
    return (
        payload.get("mode") == "production-host-full-cycle"
        and FULL_CYCLE_STAGES <= stages
        and payload.get("status") == "completed"
    )


def _has_admitted_memory_cause(
    record: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    payload = record.get("payload")
    if (
        record.get("record_type") != "memory"
        or record.get("agent") != "sovereign-host"
        or not isinstance(payload, Mapping)
        or payload.get("status") != "active"
        or payload.get("reconstruction_status") != "passed"
    ):
        return False
    for distillation_id in record.get("caused_by", ()):
        distillation = by_id.get(str(distillation_id))
        if (
            not isinstance(distillation, Mapping)
            or distillation.get("record_type") != "memory_distillation"
            or distillation.get("agent") != "sovereign-host"
        ):
            continue
        distillation_payload = distillation.get("payload")
        if (
            not isinstance(distillation_payload, Mapping)
            or not isinstance(distillation_payload.get("evaluation"), Mapping)
            or distillation_payload["evaluation"].get("admitted") is not True
        ):
            continue
        for receipt_id in distillation.get("caused_by", ()):
            receipt = by_id.get(str(receipt_id))
            if isinstance(receipt, Mapping) and _is_full_cycle_receipt(receipt):
                return True
    return False


def _has_staged_instruction_cause(
    record: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    payload = record.get("payload")
    if (
        record.get("record_type") != "order_instruction_event"
        or record.get("agent") != "sovereign-host"
        or not isinstance(payload, Mapping)
        or payload.get("operation") not in {"create", "recovered_create"}
        or payload.get("decision_status") != "recommended"
        or payload.get("verified_present") is not True
        or payload.get("order_submission_used") is not False
        or not str(payload.get("instruction_id", "")).strip()
        or not isinstance(payload.get("instruction"), Mapping)
        or not str(
            payload["instruction"].get("rationale_one_line", "")).strip()
        or not str(
            payload["instruction"].get("review_condition", "")).strip()
        or not str(
            payload["instruction"].get("rollback_condition", "")).strip()
    ):
        return False
    for receipt_id in record.get("caused_by", ()):
        receipt = by_id.get(str(receipt_id))
        if isinstance(receipt, Mapping) and _is_full_cycle_receipt(receipt):
            return True
    return False


def _has_complete_tool_inventory(
    record: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    payload = record.get("payload")
    if (
        record.get("record_type") != "tool_inventory"
        or record.get("agent") != "sovereign-host"
        or not isinstance(payload, Mapping)
    ):
        return False
    from .tool_inventory import validate_tool_manifest_report

    if validate_tool_manifest_report(payload):
        return False
    for receipt_id in record.get("caused_by", ()):
        receipt = by_id.get(str(receipt_id))
        if isinstance(receipt, Mapping) and _is_full_cycle_receipt(receipt):
            return True
    return False


def _has_market_session_cause(
    record: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    payload = record.get("payload")
    if (
        record.get("record_type") != "market_sessions"
        or record.get("agent") != "sovereign-host"
        or not isinstance(payload, Mapping)
    ):
        return False
    from .market_sessions import validate_market_sessions

    if validate_market_sessions(payload):
        return False
    for receipt_id in record.get("caused_by", ()):
        receipt = by_id.get(str(receipt_id))
        if isinstance(receipt, Mapping) and _is_full_cycle_receipt(receipt):
            return True
    return False


def live_proofs(records: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    """Proofs that must come from the committed journal."""
    by_id = {
        str(record.get("record_id")): record
        for record in records
        if record.get("record_id")
    }
    full_cycle = any(_is_full_cycle_receipt(record) for record in records)
    active_memory = any(
        _has_admitted_memory_cause(record, by_id)
        for record in records
    )
    staged_order_instruction = any(
        _has_staged_instruction_cause(record, by_id)
        for record in records
    )
    complete_tool_inventory = any(
        _has_complete_tool_inventory(record, by_id)
        for record in records
    )
    market_session_awareness = any(
        _has_market_session_cause(record, by_id)
        for record in records
    )
    return {
        "full_cycle_receipt": full_cycle,
        "active_memory": active_memory,
        "staged_order_instruction": staged_order_instruction,
        "complete_tool_inventory": complete_tool_inventory,
        "market_session_awareness": market_session_awareness,
    }


def acceptance_status(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from .tool_inventory import REQUIRED_IBKR_ACTIONS

    proofs = live_proofs(records)
    missing = [name for name, passed in proofs.items() if not passed]
    result = {
        "proofs": proofs,
        "missing": missing,
        "instructions": {
            "full_cycle_receipt": (
                "Commit one host_input schema v3 cycle with every required "
                "cognitive stage completed. The executor will persist a "
                "production-host-full-cycle receipt."
            ),
            "active_memory": (
                "While this proof is missing, perform one real "
                "memory_distillation in the next cycle rather than waiting "
                "for the normal weekly cadence. Admission must pass "
                "reconstruction, contradiction, provenance and budget gates."
            ),
            "staged_order_instruction": (
                "Recover the genuine instruction-102 evidence now through "
                "staged_order_instruction_recovery. Copy the immutable r27 "
                "create row, perform a fresh get, and report whether 102 is "
                "present or absent. Never call create again. This recovery "
                "does not force the current decision to recommend the order."
            ),
            "complete_tool_inventory": (
                "Enumerate every action currently exposed by every connector, "
                "not only connector names or actions used in this cycle. "
                "Record exact action names, inputs, return purpose, and "
                "read/write mode in tool_manifest_report."
            ),
            "market_session_awareness": (
                "Each schema-v3 cycle must include source-backed EU and US "
                "session state with IANA timezones, local time, next open and "
                "close, and the derived both/eu/us/none overlap."
            ),
        },
        "contracts": {
            "active_memory": {
                "top_level_key": "memory_distillation",
                "additive_to_schema_v3_cycle": True,
                "envelope_fields": [
                    "distillation_id",
                    "source_ids",
                    "source_time_bounds",
                    "memory_objects",
                    "claim_ids",
                    "contradiction_groups",
                    "active_brain_proposals",
                    "retirements",
                    "reconstruction_spec",
                    "compression_metrics",
                    "blockers",
                    "ex_post_material",
                    "brain_version",
                ],
                "active_brain_proposal_fields": [
                    "memory_id",
                    "layer",
                    "status",
                    "as_of",
                    "claim",
                    "source_ids",
                    "evidence_status",
                    "confidence",
                    "reconstruction_status",
                    "claim_ids",
                ],
                "required_proposal_values": {
                    "layer": "active_brain",
                    "status": "active",
                    "reconstruction_status": "passed",
                },
                "retirement_fields": [
                    "memory_id",
                    "status",
                    "reason",
                    "source_ids",
                ],
                "retirement_statuses": ["stale", "archived"],
                "memory_object_enums": {
                    "status": [
                        "raw",
                        "distilled",
                        "validated",
                        "active",
                        "stale",
                        "archived",
                        "experimental",
                    ],
                    "reconstruction_status": [
                        "not_run",
                        "passed",
                        "failed",
                        "blocked",
                    ],
                },
                "reconstruction_spec_fields": [
                    "required_claim_ids",
                    "distilled_claim_ids",
                    "source_claim_ids",
                    "distilled_contradiction_groups",
                ],
                "field_types": {
                    "source_ids": "list",
                    "source_time_bounds": "object",
                    "memory_objects": "list",
                    "claim_ids": "list",
                    "contradiction_groups": (
                        "object mapping group_id to a list of claim_ids"
                    ),
                    "active_brain_proposals": "list",
                    "retirements": "list",
                    "reconstruction_spec": "object",
                    "compression_metrics": (
                        "object with positive raw_units and distilled_units"
                    ),
                    "blockers": "list",
                    "ex_post_material": "list",
                },
                "admission_requirement": (
                    "At least one source-grounded active_brain_proposal must "
                    "survive every runtime admission gate and be causally "
                    "linked to the completed schema-v3 cycle receipt through "
                    "its admitted memory_distillation record."
                ),
            },
            "staged_order_instruction": {
                "top_level_key": "order_instruction_activity",
                "required_operations": ["create", "get"],
                "activity_fields": [
                    "operation", "tool", "request", "result",
                    "instruction_id",
                ],
                "decision_fields": [
                    "instruction", "instruction_staged",
                    "ibkr_instruction_id",
                ],
                "required_instruction_fields": [
                    "action",
                    "quantity",
                    "order_type",
                    "time_in_force",
                    "rationale_one_line",
                    "review_condition",
                    "rollback_condition",
                ],
                "identity_fields_any_of": [
                    "symbol", "contract_description", "contract_id_ex",
                ],
                "plugin_aliases_are_not_committed_fields": {
                    "tif": "time_in_force",
                    "instrument": (
                        "symbol, contract_description, or contract_id_ex"
                    ),
                },
                "postcondition": (
                    "order_instructions is the post-create get result and "
                    "contains ibkr_instruction_id"
                ),
                "safety_boundary": "order_submission_used remains false",
                "known_recovery_source": {
                    "instruction_id": "102",
                    "source_cycle_id": (
                        "cycle-20260917T000356Z-r27s3"
                    ),
                    "source_file": (
                        "host_input/"
                        "cycle-20260917T000356Z-r27s3.json"
                    ),
                    "source_sha256": (
                        "8b61783cea4942a865df80face57f4270"
                        "e4df4e78d7de11e0daf6b03d62b5bff"
                    ),
                    "create_activity_path": (
                        "$.order_instruction_activity[0]"
                    ),
                    "post_create_get_path": (
                        "$.order_instruction_activity[1]"
                    ),
                    "reuse_rule": (
                        "Use staged_order_instruction_recovery. Copy the exact "
                        "original create activity and pair it with a fresh "
                        "current-session get whose observed_at equals this "
                        "cycle's as_of. Do not call create again and do not "
                        "treat the refused source cycle as completed proof."
                    ),
                },
            },
            "complete_tool_inventory": {
                "top_level_key": "tool_manifest_report",
                "required_fields": [
                    "observed_at",
                    "complete_for_current_session",
                    "connectors",
                    "manifest_discrepancies",
                    "unreachable_manifest_connectors",
                ],
                "action_fields": ["name", "inputs", "returns", "mode"],
                "action_modes": [
                    "read", "write_nontransmitting", "write", "unknown",
                ],
                "known_ibkr_minimum": sorted(REQUIRED_IBKR_ACTIONS),
                "completeness_rule": (
                    "Include every action visible in the current host session; "
                    "the known minimum is not a ceiling."
                ),
            },
            "market_session_awareness": {
                "top_level_key": "market_sessions",
                "required_regions": ["EU", "US"],
                "market_fields": [
                    "region",
                    "venue",
                    "timezone",
                    "local_time",
                    "status",
                    "is_open",
                    "next_open",
                    "next_close",
                    "evidence",
                ],
                "overlap_values": [
                    "both_open", "eu_only", "us_only", "none_open",
                ],
                "source_guidance": {
                    "US": "Use current clock/calendar evidence.",
                    "EU": (
                        "Use the actual relevant venue calendar and session "
                        "evidence, not a copied weekday/hour assumption."
                    ),
                },
            },
        },
    }
    result["instructions"] = {
        name: instruction
        for name, instruction in result["instructions"].items()
        if name in missing
    }
    result["contracts"] = {
        name: contract
        for name, contract in result["contracts"].items()
        if name in missing
    }
    return result


def reconcile_live_proofs(
    plan_text: str,
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """A complete claim needs proof; a landed proof needs a ledger update."""
    statuses = parse_delivery_plan(plan_text)
    proofs = live_proofs(records)
    errors = []
    for proof, items in LIVE_PROOF_ITEMS.items():
        for item in items:
            status = statuses.get(item)
            if status is None:
                errors.append(f"live_proof_item_missing_from_plan:{item}")
            elif not proofs[proof] and status.startswith("complete"):
                errors.append(f"complete_without_live_proof:{item}:{proof}")
            elif proofs[proof] and status == "in-progress-live-proof":
                errors.append(f"live_proof_landed_but_status_stale:{item}:{proof}")
    return errors


def blocking_reconciliation_errors(
    errors: Sequence[str],
    *,
    strict_stale: bool = False,
) -> list[str]:
    """Only false completion blocks routine CI; strict closeout blocks drift."""
    if strict_stale:
        return list(errors)
    return [
        error for error in errors
        if not error.startswith(STALE_LEDGER_PREFIX)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?")
    parser.add_argument(
        "--strict-stale",
        action="store_true",
        help="Fail when a landed proof has not yet been copied to the ledger.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = (
        Path(args.root).resolve()
        if args.root
        else Path(__file__).resolve().parent.parent
    )
    from .integrity import load_journal_records

    records = load_journal_records()
    errors = reconcile_live_proofs(
        (root / "DELIVERY_PLAN.md").read_text(encoding="utf-8"),
        records,
    )
    status = acceptance_status(records)
    for proof, passed in status["proofs"].items():
        print(f"{proof}: {'PASS' if passed else 'MISSING'}")
    blocking = blocking_reconciliation_errors(
        errors,
        strict_stale=args.strict_stale,
    )
    for error in errors:
        prefix = (
            "DELIVERY ACCEPTANCE WARNING"
            if error.startswith(STALE_LEDGER_PREFIX)
            and not args.strict_stale
            else "DELIVERY ACCEPTANCE"
        )
        print(f"{prefix}: {error}")
    if blocking:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
