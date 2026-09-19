"""Reconcile operator instruction decisions with account orders and trades."""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import input_fingerprint
from .input_artifacts import load_input_data
from .audit_store import AuditJournal
from .integrity import order_chain
from .lifecycle import apply_from_state, event_from_mapping
from .schema_versions import STRUCTURED_FULL_CYCLE_VERSIONS
from .timestamps import effective_as_of, parse_iso_timestamp
from .tool_provenance import machine_witnessed, resolve_tool_call

MAX_RECONCILIATIONS_PER_CYCLE = 8
MAX_RECONCILIATION_TEXT_CHARS = 600
OPERATOR_DISPOSITIONS = frozenset({
    "accepted",
    "rejected",
    "deleted",
    "unknown",
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _aware_timestamp(value: Any) -> datetime | None:
    parsed = parse_iso_timestamp(value)
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed.utcoffset() is None
    ):
        return None
    return parsed


def _ordered_records(
    records: Sequence[Mapping[str, Any]],
    record_type: str,
) -> list[Mapping[str, Any]]:
    ordered, failures = order_chain(records)
    rows = ordered if not failures else list(records)
    return [
        row for row in rows
        if row.get("record_type") == record_type
        and isinstance(row.get("payload"), Mapping)
    ]


def _current_evidence_anchors(
    data: Mapping[str, Any],
) -> dict[str, str]:
    cycle_id = _text(data.get("cycle_id"))
    anchors = {
        f"stage:{stage_id}": f"cycle-stage:{cycle_id}:{stage_id}"
        for row in data.get("cognitive_stages") or ()
        if isinstance(row, Mapping)
        and (stage_id := _text(row.get("stage_id")))
    }
    decision_anchor = f"cycle-stage:{cycle_id}:decision"
    anchors.update({
        f"finding:{finding_id}": decision_anchor
        for row in data.get("findings") or ()
        if isinstance(row, Mapping)
        and (finding_id := _text(row.get("id")))
    })
    return anchors


def _instruction_id(payload: Mapping[str, Any]) -> str:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return _text(
        payload.get("instruction_id")
        or payload.get("ibkr_instruction_id")
        or metadata.get("ibkr_instruction_id")
    )


def _proposal_record(
    records: Sequence[Mapping[str, Any]],
    *,
    recommendation_id: str,
    instruction_id: str,
) -> Mapping[str, Any] | None:
    rows = []
    for record in _ordered_records(records, "order_instruction_event"):
        payload = record["payload"]
        if (
            _instruction_id(payload) != instruction_id
            or not isinstance(payload.get("instruction"), Mapping)
        ):
            continue
        recovered = _text(payload.get("recovered_from_cycle"))
        cycle_id = _text(payload.get("cycle_id"))
        if recovered == recommendation_id:
            priority = 0
        elif cycle_id == recommendation_id:
            priority = 1
        else:
            continue
        rows.append((priority, record))
    if rows:
        return sorted(rows, key=lambda item: item[0])[0][1]
    return next((
        record for record in _ordered_records(records, "cycle_receipt")
        if _text(record["payload"].get("cycle_id")) == recommendation_id
        and _text(record["payload"].get("ibkr_instruction_id"))
        == instruction_id
        and isinstance(record["payload"].get("instruction"), Mapping)
    ), None)


def _proposal_creation_time(
    proposal_record: Mapping[str, Any],
    instruction_id: str,
) -> str | None:
    payload = proposal_record["payload"]
    instruction = payload.get("instruction")
    instruction = instruction if isinstance(instruction, Mapping) else {}
    for field in ("creation_time", "created_at", "observed_at"):
        if _text(instruction.get(field)):
            return _text(instruction.get(field))
    fresh_get = payload.get("fresh_get")
    fresh_get = fresh_get if isinstance(fresh_get, Mapping) else {}
    result = fresh_get.get("result")
    result = result if isinstance(result, Mapping) else {}
    rows = (
        result.get("order_instructions")
        or result.get("instructions")
        or []
    )
    if isinstance(rows, list):
        for row in rows:
            if (
                isinstance(row, Mapping)
                and _text(row.get("id")) == instruction_id
            ):
                for field in ("creation_time", "created_at", "observed_at"):
                    if _text(row.get(field)):
                        return _text(row.get(field))
    return _text(payload.get("at")) or None


def _side(value: Any) -> str | None:
    text = re.sub(r"[^A-Z]", "_", _text(value).upper()).strip("_")
    if text.startswith("BUY"):
        return "BUY"
    if text.startswith("SELL"):
        return "SELL"
    return text or None


def _first(value: Mapping[str, Any], *fields: str) -> Any:
    for field in fields:
        if value.get(field) is not None:
            return value.get(field)
    return None


def _instrument_refs(value: Mapping[str, Any]) -> dict[str, list[str]]:
    conids: set[str] = set()
    symbols: set[str] = set()
    descriptions: set[str] = set()
    for field in (
        "contract_id_ex",
        "contractIdEx",
        "contract_id",
        "contractId",
        "conid",
    ):
        raw = _text(value.get(field))
        if raw:
            conids.add(raw.casefold())
            conids.add(raw.split("@", 1)[0].casefold())
    for field in ("symbol", "ticker"):
        raw = _text(value.get(field))
        if raw:
            symbols.add(raw.casefold())
    for field in ("instrument", "contract_description", "description"):
        raw = _text(value.get(field))
        if raw:
            descriptions.add(raw.casefold())
    return {
        "conids": sorted(conids),
        "symbols": sorted(symbols),
        "descriptions": sorted(descriptions),
    }


def _normalize_terms(value: Mapping[str, Any]) -> dict[str, Any]:
    limit = _first(
        value,
        "limit_price",
        "lmt_price",
        "lmtPrice",
        "price",
    )
    quantity = _first(
        value,
        "quantity",
        "total_quantity",
        "totalQuantity",
        "size",
    )
    return {
        "instrument_refs": _instrument_refs(value),
        "side": _side(_first(value, "side", "action")),
        "quantity": (
            float(quantity) if _finite_number(quantity) else None
        ),
        "order_type": _text(
            _first(value, "order_type", "orderType")
        ).upper() or None,
        "limit_price": (
            float(limit) if _finite_number(limit) else None
        ),
        "time_in_force": _text(
            _first(value, "time_in_force", "tif")
        ).upper() or None,
        "order_id": _text(
            _first(value, "order_id", "orderId", "id")
        ) or None,
        "observed_at": _text(_first(
            value,
            "creation_time",
            "created_at",
            "order_time",
            "submit_time",
            "time",
            "trade_time",
        )) or None,
    }


def _is_derivative(terms: Mapping[str, Any]) -> bool:
    descriptions = terms["instrument_refs"]["descriptions"]
    return any(
        token in description.upper().split()
        for description in descriptions
        for token in ("PUT", "CALL", "OPTION", "FUTURE")
    )


def _candidate_rows(value: Any, *, kind: str) -> list[Mapping[str, Any]]:
    keys = (
        ("orders", "order", "live_orders")
        if kind == "order"
        else ("trades", "executions", "fills", "trade")
    )
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    if not isinstance(value, Mapping):
        return []
    for key in keys:
        rows = value.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, Mapping)]
        if isinstance(rows, Mapping):
            return [rows]
    marker_fields = {
        "order_id", "orderId", "trade_id", "tradeId", "contract_id",
        "contract_id_ex", "conid", "symbol", "side", "action",
    }
    return [value] if marker_fields.intersection(value) else []


def _after_creation(
    terms: Mapping[str, Any],
    creation_time: str | None,
) -> bool:
    if creation_time is None:
        return True
    created = _aware_timestamp(creation_time)
    observed = _aware_timestamp(terms.get("observed_at"))
    return (
        created is not None
        and observed is not None
        and observed >= created
    )


def _matches_proposal(
    proposal: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    creation_time: str | None,
) -> bool:
    proposal_refs = proposal["instrument_refs"]
    actual_refs = actual["instrument_refs"]
    conid_match = bool(
        set(proposal_refs["conids"]).intersection(actual_refs["conids"])
    )
    symbol_match = bool(
        set(proposal_refs["symbols"]).intersection(actual_refs["symbols"])
    )
    if proposal_refs["conids"]:
        identity_match = conid_match
    else:
        identity_match = conid_match or symbol_match
    return (
        identity_match
        and proposal.get("side") == actual.get("side")
        and proposal.get("quantity") == actual.get("quantity")
        and _after_creation(actual, creation_time)
    )


def _matches_trade_identity(
    proposal: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    creation_time: str | None,
) -> bool:
    proposal_refs = proposal["instrument_refs"]
    actual_refs = actual["instrument_refs"]
    conid_match = bool(
        set(proposal_refs["conids"]).intersection(actual_refs["conids"])
    )
    symbol_match = bool(
        set(proposal_refs["symbols"]).intersection(actual_refs["symbols"])
    )
    identity_match = (
        conid_match
        if proposal_refs["conids"]
        else conid_match or symbol_match
    )
    return (
        identity_match
        and proposal.get("side") == actual.get("side")
        and _after_creation(actual, creation_time)
    )


def _matching_order(
    result: Any,
    proposal: Mapping[str, Any],
    *,
    creation_time: str | None,
) -> tuple[Mapping[str, Any] | None, list[str]]:
    matches = []
    for row in _candidate_rows(result, kind="order"):
        terms = _normalize_terms(row)
        if _matches_proposal(
            proposal,
            terms,
            creation_time=creation_time,
        ):
            matches.append((row, terms))
    if len(matches) > 1:
        return None, ["multiple_matching_orders"]
    return (matches[0][0], []) if matches else (None, [])


def _matching_trades(
    result: Any,
    proposal: Mapping[str, Any],
    *,
    order: Mapping[str, Any] | None,
    creation_time: str | None,
) -> list[Mapping[str, Any]]:
    order_terms = _normalize_terms(order or {})
    order_id = order_terms.get("order_id")
    matches = []
    quantity = 0.0
    for row in _candidate_rows(result, kind="trade"):
        terms = _normalize_terms(row)
        row_order_id = terms.get("order_id")
        if order_id and row_order_id:
            matched = order_id == row_order_id
        else:
            matched = _matches_trade_identity(
                proposal,
                terms,
                creation_time=creation_time,
            )
        if not matched:
            continue
        row_quantity = terms.get("quantity")
        if row_quantity is not None:
            quantity += abs(float(row_quantity))
        matches.append(row)
    proposed_quantity = proposal.get("quantity")
    if (
        proposed_quantity is not None
        and quantity > abs(float(proposed_quantity))
    ):
        return []
    return matches


def _field_changes(
    proposal: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[dict[str, Any]]:
    changes = []
    for field in (
        "order_type",
        "limit_price",
        "time_in_force",
    ):
        if proposal.get(field) != actual.get(field):
            changes.append({
                "field": field,
                "proposed": proposal.get(field),
                "submitted": actual.get(field),
            })
    return changes


def _tool_action(call: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    combined = " ".join((
        _text(call.get("tool")),
        _text(call.get("call")),
        _text(metadata.get("tool")),
    ))
    return re.sub(r"[^a-z0-9]", "", combined.casefold())


def _saved_instruction_present(
    data: Mapping[str, Any],
    instruction_id: str,
) -> bool:
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    instructions = (
        data.get("order_instructions")
        if data.get("order_instructions") is not None
        else snapshot.get("order_instructions")
    )
    instructions = instructions if isinstance(instructions, list) else []
    return any(
        isinstance(row, Mapping)
        and _text(row.get("id")) == instruction_id
        for row in instructions
    )


def _lifecycle_context(
    records: Sequence[Mapping[str, Any]],
    recommendation_id: str,
) -> tuple[str, list[str]]:
    events = []
    errors = []
    for record in _ordered_records(records, "lifecycle_event"):
        payload = record["payload"]
        if _text(payload.get("recommendation_id")) != recommendation_id:
            continue
        try:
            events.append(event_from_mapping(
                payload,
                caused_by=tuple(record.get("caused_by") or ()),
            ))
        except ValueError as error:
            errors.append(str(error))
    if errors:
        return "unknown", errors
    try:
        return apply_from_state("recommended", events), []
    except ValueError as error:
        return "unknown", [str(error)]


def _operator_lifecycle_record(
    records: Sequence[Mapping[str, Any]],
    instruction_id: str,
    disposition: str,
) -> str | None:
    expected_state = {
        "rejected": "rejected",
        "deleted": "deleted",
    }.get(disposition)
    if expected_state is None:
        return None
    for record in reversed(_ordered_records(records, "lifecycle_event")):
        payload = record["payload"]
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if (
            _text(metadata.get("ibkr_instruction_id")) == instruction_id
            and _text(payload.get("to_state")) == expected_state
        ):
            return _text(record.get("record_id"))
    return None


def _known_reconciliations(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str], set[str]]:
    by_id = {}
    superseded = set()
    for record in _ordered_records(records, "instruction_reconciliation"):
        payload = record["payload"]
        reconciliation_id = _text(payload.get("reconciliation_id"))
        if reconciliation_id:
            by_id[reconciliation_id] = payload
        prior = _text(payload.get("supersedes_reconciliation_id"))
        if prior:
            superseded.add(prior)
    active_by_proposal = {
        _text(payload.get("proposal_record_id")): reconciliation_id
        for reconciliation_id, payload in by_id.items()
        if reconciliation_id not in superseded
    }
    return by_id, active_by_proposal, superseded


def validate_instruction_reconciliations(
    rows: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    if rows is None:
        return []
    if data.get("host_input_schema_version") not in (
            STRUCTURED_FULL_CYCLE_VERSIONS):
        return ["instruction_reconciliations_require_schema_v3"]
    if not isinstance(rows, list):
        return ["instruction_reconciliations_must_be_a_list"]
    if len(rows) > MAX_RECONCILIATIONS_PER_CYCLE:
        return ["instruction_reconciliations_too_many"]
    anchors = _current_evidence_anchors(data)
    cycle_as_of = _aware_timestamp(effective_as_of(data))
    known, active_by_proposal, superseded = _known_reconciliations(records)
    seen_ids = set()
    seen_proposals = set()
    errors = []
    expected = {
        "reconciliation_id",
        "recommendation_id",
        "instruction_id",
        "supersedes_reconciliation_id",
        "operator_observation",
        "account_orders_tool_call_id",
        "account_trades_tool_call_id",
        "evidence",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            errors.append(
                f"instruction_reconciliation_invalid:{index}:object"
            )
            continue
        if set(row) != expected:
            errors.append(
                f"instruction_reconciliation_invalid:{index}:fields"
            )
        reconciliation_id = _text(row.get("reconciliation_id"))
        if (
            not reconciliation_id
            or _SAFE_ID.fullmatch(reconciliation_id) is None
        ):
            errors.append(
                f"instruction_reconciliation_invalid:{index}:id"
            )
        elif reconciliation_id in seen_ids or reconciliation_id in known:
            errors.append(
                "instruction_reconciliation_duplicate_id:"
                f"{index}:{reconciliation_id}"
            )
        seen_ids.add(reconciliation_id)
        recommendation_id = _text(row.get("recommendation_id"))
        instruction_id = _text(row.get("instruction_id"))
        proposal = _proposal_record(
            records,
            recommendation_id=recommendation_id,
            instruction_id=instruction_id,
        )
        if proposal is None:
            errors.append(
                f"instruction_reconciliation_proposal_missing:{index}"
            )
            continue
        proposal_record_id = _text(proposal.get("record_id"))
        if proposal_record_id in seen_proposals:
            errors.append(
                f"instruction_reconciliation_duplicate_proposal:{index}"
            )
        seen_proposals.add(proposal_record_id)
        active_id = active_by_proposal.get(proposal_record_id)
        supersedes = row.get("supersedes_reconciliation_id")
        supersedes_id = _text(supersedes) if supersedes is not None else ""
        if active_id and supersedes_id != active_id:
            errors.append(
                "instruction_reconciliation_supersession_required:"
                f"{index}:{active_id}"
            )
        if supersedes_id:
            prior = known.get(supersedes_id)
            if (
                prior is None
                or supersedes_id in superseded
                or _text(prior.get("proposal_record_id"))
                != proposal_record_id
            ):
                errors.append(
                    "instruction_reconciliation_supersession_invalid:"
                    f"{index}:{supersedes_id}"
                )
        elif supersedes is not None:
            errors.append(
                f"instruction_reconciliation_supersession_invalid:{index}:value"
            )

        observation = row.get("operator_observation")
        if not isinstance(observation, Mapping):
            errors.append(
                "instruction_reconciliation_operator_invalid:"
                f"{index}:not_object"
            )
        else:
            if set(observation) != {
                "observed_at",
                "disposition",
                "app_saved_instruction_visible",
                "quote",
            }:
                errors.append(
                    "instruction_reconciliation_operator_invalid:"
                    f"{index}:fields"
                )
            disposition = observation.get("disposition")
            if disposition not in OPERATOR_DISPOSITIONS:
                errors.append(
                    "instruction_reconciliation_operator_invalid:"
                    f"{index}:disposition"
                )
            visible = observation.get("app_saved_instruction_visible")
            if visible is not None and not isinstance(visible, bool):
                errors.append(
                    "instruction_reconciliation_operator_invalid:"
                    f"{index}:visibility"
                )
            quote = _text(observation.get("quote"))
            if (
                not quote
                or len(quote) > MAX_RECONCILIATION_TEXT_CHARS
            ):
                errors.append(
                    "instruction_reconciliation_operator_invalid:"
                    f"{index}:quote"
                )
            observed_at = _aware_timestamp(observation.get("observed_at"))
            if observed_at is None:
                errors.append(
                    "instruction_reconciliation_operator_invalid:"
                    f"{index}:observed_at"
                )
            elif cycle_as_of is not None and observed_at > cycle_as_of:
                errors.append(
                    "instruction_reconciliation_operator_invalid:"
                    f"{index}:after_cycle"
                )
            created_at = _aware_timestamp(
                _proposal_creation_time(proposal, instruction_id)
            )
            if (
                observed_at is not None
                and created_at is not None
                and observed_at < created_at
            ):
                errors.append(
                    "instruction_reconciliation_operator_invalid:"
                    f"{index}:before_instruction"
                )

        for field, action in (
            ("account_orders_tool_call_id", "getaccountorders"),
            ("account_trades_tool_call_id", "getaccounttrades"),
        ):
            tool_call_id = _text(row.get(field))
            resolved = resolve_tool_call(data, tool_call_id)
            if resolved is None:
                errors.append(
                    f"instruction_reconciliation_tool_invalid:{index}:{field}"
                )
                continue
            metadata, call = resolved
            if not machine_witnessed(metadata):
                errors.append(
                    "instruction_reconciliation_tool_invalid:"
                    f"{index}:{field}:capture_origin"
                )
            if action not in _tool_action(call, metadata):
                errors.append(
                    "instruction_reconciliation_tool_invalid:"
                    f"{index}:{field}:action"
                )
        evidence = row.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or len(evidence) > 8
        ):
            errors.append(
                f"instruction_reconciliation_evidence_invalid:{index}:count"
            )
        else:
            seen = set()
            for evidence_index, raw in enumerate(evidence):
                ref = _text(raw)
                if not (
                    ref.startswith("stage:")
                    or ref.startswith("finding:")
                ):
                    errors.append(
                        "instruction_reconciliation_evidence_invalid:"
                        f"{index}:invalid:{evidence_index}"
                    )
                elif ref in seen:
                    errors.append(
                        "instruction_reconciliation_evidence_invalid:"
                        f"{index}:duplicate:{ref}"
                    )
                elif ref not in anchors:
                    errors.append(
                        "instruction_reconciliation_evidence_invalid:"
                        f"{index}:dangling:{ref}"
                    )
                seen.add(ref)
    return sorted(set(errors))


def _payload(
    row: Mapping[str, Any],
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    recommendation_id = _text(row.get("recommendation_id"))
    instruction_id = _text(row.get("instruction_id"))
    proposal_record = _proposal_record(
        records,
        recommendation_id=recommendation_id,
        instruction_id=instruction_id,
    )
    if proposal_record is None:
        raise ValueError(
            f"instruction_reconciliation_proposal_missing:{instruction_id}"
        )
    proposal_payload = proposal_record["payload"]
    proposal_raw = proposal_payload["instruction"]
    proposal = _normalize_terms(proposal_raw)
    creation_time = _proposal_creation_time(
        proposal_record,
        instruction_id,
    )
    order_metadata, order_call = resolve_tool_call(
        data,
        _text(row.get("account_orders_tool_call_id")),
    ) or ({}, {})
    trade_metadata, trade_call = resolve_tool_call(
        data,
        _text(row.get("account_trades_tool_call_id")),
    ) or ({}, {})
    order, match_errors = _matching_order(
        order_call.get("result"),
        proposal,
        creation_time=creation_time,
    )
    trades = _matching_trades(
        trade_call.get("result"),
        proposal,
        order=order,
        creation_time=creation_time,
    )
    actual = _normalize_terms(order or {}) if order is not None else None
    observation = dict(row["operator_observation"])
    disposition = _text(observation.get("disposition"))
    saved_present = _saved_instruction_present(data, instruction_id)
    app_visible = observation.get("app_saved_instruction_visible")
    disagreements = list(match_errors)
    if app_visible is not None and app_visible != saved_present:
        disagreements.append("app_connector_saved_state_disagreement")
    if disposition == "accepted" and order is None:
        disagreements.append("accepted_without_matching_account_order")
    if disposition == "rejected" and (order is not None or trades):
        disagreements.append("rejected_with_account_activity")
    if disposition in {"rejected", "deleted"} and saved_present:
        disagreements.append("saved_instruction_still_present")

    changes = []
    if disagreements:
        status = "unknown"
    elif disposition == "accepted":
        changes = _field_changes(proposal, actual or {})
        status = (
            "accepted_modified" if changes else "accepted_unchanged"
        )
    elif disposition == "rejected":
        status = "rejected"
    elif disposition == "deleted":
        status = "deleted_saved_only"
    else:
        status = "unknown"
    prior_state, lifecycle_errors = _lifecycle_context(
        records,
        recommendation_id,
    )
    anchors = _current_evidence_anchors(data)
    evidence = [_text(ref) for ref in row.get("evidence") or ()]
    proposal_source = (
        "recovery_restatement"
        if _text(proposal_payload.get("operation")) == "recovered_create"
        else "original_create"
    )
    return {
        "schema_version": 1,
        "reconciliation_id": _text(row.get("reconciliation_id")),
        "supersedes_reconciliation_id":
            _text(row.get("supersedes_reconciliation_id")) or None,
        "cycle_id": _text(data.get("cycle_id")),
        "recommendation_id": recommendation_id,
        "instruction_id": instruction_id,
        "proposal_record_id": _text(proposal_record.get("record_id")),
        "proposal_provenance": proposal_source,
        "instruction_created_at": creation_time,
        "frozen_proposal": {
            "raw": dict(proposal_raw),
            "normalized": proposal,
        },
        "operator_observation": observation,
        "status": status,
        "saved_instruction_deleted": not saved_present,
        "live_order_observed": order is not None,
        "live_order_deleted": None,
        "live_trade_observed": bool(trades),
        "submission_state": (
            "submitted" if order is not None else "not_observed"
        ),
        "execution_state": (
            "executed" if trades else "not_observed"
        ),
        "actual_submitted_order": (
            {
                "raw": dict(order),
                "normalized": actual,
            }
            if order is not None
            else None
        ),
        "matching_trades": [dict(trade) for trade in trades],
        "field_changes": changes,
        "disagreements": sorted(set(disagreements)),
        "prior_lifecycle_state": prior_state,
        "prior_lifecycle_errors": lifecycle_errors,
        "account_observations": {
            "orders": {
                "tool_call_id": row.get("account_orders_tool_call_id"),
                "observed_at": order_metadata.get("observed_at"),
                "result_sha256": order_metadata.get("result_sha256"),
            },
            "trades": {
                "tool_call_id": row.get("account_trades_tool_call_id"),
                "observed_at": trade_metadata.get("observed_at"),
                "result_sha256": trade_metadata.get("result_sha256"),
            },
        },
        "operator_evidence_record_id": _operator_lifecycle_record(
            records,
            instruction_id,
            disposition,
        ),
        "evidence": evidence,
        "evidence_record_ids": list(dict.fromkeys(
            anchors[ref] for ref in evidence
        )),
        "source_disclaimer": (
            "Connector-response provenance binds the committed result but "
            "does not independently prove connector capture. The operator "
            "quote is an explicit host-carried observation and remains "
            "correctable through a superseding reconciliation."
        ),
    }


def persist_instruction_reconciliations(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
    *,
    all_records: Sequence[Mapping[str, Any]] = (),
) -> int:
    rows = data.get("instruction_reconciliations")
    if not isinstance(rows, list) or not rows:
        return 0
    combined = {
        _text(record.get("record_id")): record
        for record in [*all_records, *journal.read()]
        if _text(record.get("record_id"))
    }
    records = list(combined.values())
    cycle_id = _text(receipt.get("cycle_id"))
    existing = {
        _text(record.get("record_id")): record
        for record in records
        if record.get("record_type") == "instruction_reconciliation"
    }
    new_rows = []
    for row in rows:
        reconciliation_id = _text(row.get("reconciliation_id"))
        record_id = f"instruction-reconciliation:{reconciliation_id}"
        prior = existing.get(record_id)
        if prior is None:
            new_rows.append(row)
            continue
        payload = _payload(row, data=data, records=records)
        caused_by = [
            f"cycle-receipt:{cycle_id}",
            payload["proposal_record_id"],
            f"tool-provenance:{cycle_id}",
            *payload["evidence_record_ids"],
        ]
        if payload.get("operator_evidence_record_id"):
            caused_by.append(payload["operator_evidence_record_id"])
        supersedes = payload.get("supersedes_reconciliation_id")
        if supersedes:
            caused_by.append(f"instruction-reconciliation:{supersedes}")
        caused_by = list(dict.fromkeys(caused_by))
        if (
            prior.get("payload") != payload
            or prior.get("caused_by") != caused_by
        ):
            raise ValueError(
                f"instruction_reconciliation_payload_mismatch:{record_id}"
            )
    if not new_rows:
        return 0
    validation_records = [
        record for record in records
        if not (
            record.get("record_type") == "instruction_reconciliation"
            and isinstance(record.get("payload"), Mapping)
            and _text(record["payload"].get("cycle_id")) == cycle_id
        )
    ]
    errors = validate_instruction_reconciliations(
        new_rows,
        data=data,
        records=validation_records,
    )
    if errors:
        raise ValueError(
            "instruction_reconciliation_failed:" + ",".join(errors)
        )
    added = 0
    for row in new_rows:
        payload = _payload(row, data=data, records=records)
        reconciliation_id = payload["reconciliation_id"]
        caused_by = [
            f"cycle-receipt:{cycle_id}",
            payload["proposal_record_id"],
            f"tool-provenance:{cycle_id}",
            *payload["evidence_record_ids"],
        ]
        if payload.get("operator_evidence_record_id"):
            caused_by.append(payload["operator_evidence_record_id"])
        supersedes = payload.get("supersedes_reconciliation_id")
        if supersedes:
            caused_by.append(f"instruction-reconciliation:{supersedes}")
        journal.append(
            record_id=f"instruction-reconciliation:{reconciliation_id}",
            record_type="instruction_reconciliation",
            agent="sovereign-host",
            caused_by=list(dict.fromkeys(caused_by)),
            payload=payload,
        )
        added += 1
    return added


def backfill_instruction_reconciliations(
    inputs: Sequence[Path],
    journal: AuditJournal,
    *,
    all_records: Sequence[Mapping[str, Any]] = (),
) -> int:
    candidates = {}
    for path in inputs:
        try:
            import json
            data = load_input_data(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("instruction_reconciliations"), list)
            or not data.get("instruction_reconciliations")
        ):
            continue
        cycle_id = _text(data.get("cycle_id"))
        if cycle_id:
            candidates[(cycle_id, input_fingerprint(data))] = data
    combined = {
        _text(record.get("record_id")): record
        for record in [*all_records, *journal.read()]
        if _text(record.get("record_id"))
    }
    records = list(combined.values())
    added = 0
    for record in _ordered_records(records, "cycle_receipt"):
        payload = record["payload"]
        cycle_id = _text(payload.get("cycle_id"))
        snapshot_id = _text(payload.get("snapshot_id"))
        fingerprint = (
            snapshot_id.rsplit(":", 1)[-1] if ":" in snapshot_id else ""
        )
        data = candidates.get((cycle_id, fingerprint))
        if data is None:
            continue
        added += persist_instruction_reconciliations(
            data,
            journal,
            payload,
            all_records=records,
        )
        combined.update({
            _text(row.get("record_id")): row
            for row in journal.read()
            if _text(row.get("record_id"))
        })
        records = list(combined.values())
    return added


def instruction_reconciliation_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = 12,
) -> dict[str, Any]:
    rows = _ordered_records(records, "instruction_reconciliation")
    superseded = {
        _text(row["payload"].get("supersedes_reconciliation_id"))
        for row in rows
        if _text(row["payload"].get("supersedes_reconciliation_id"))
    }
    active = [
        row["payload"] for row in rows
        if _text(row["payload"].get("reconciliation_id")) not in superseded
    ]
    items = [{
        "reconciliation_id": row.get("reconciliation_id"),
        "recommendation_id": row.get("recommendation_id"),
        "instruction_id": row.get("instruction_id"),
        "proposal_record_id": row.get("proposal_record_id"),
        "proposal_provenance": row.get("proposal_provenance"),
        "status": row.get("status"),
        "operator_disposition": (
            row.get("operator_observation") or {}
        ).get("disposition"),
        "saved_instruction_deleted":
            row.get("saved_instruction_deleted"),
        "live_order_observed": row.get("live_order_observed"),
        "live_order_deleted": row.get("live_order_deleted"),
        "live_trade_observed": row.get("live_trade_observed"),
        "submission_state": row.get("submission_state"),
        "execution_state": row.get("execution_state"),
        "field_changes": row.get("field_changes"),
        "disagreements": row.get("disagreements"),
        "frozen_proposal": row.get("frozen_proposal"),
        "actual_submitted_order": row.get("actual_submitted_order"),
    } for row in reversed(active)]
    counts = {}
    for row in active:
        status = _text(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    shown = items[:limit]
    return {
        "total_records": len(rows),
        "active_count": len(active),
        "counts_by_status": dict(sorted(counts.items())),
        "items": shown,
        "not_shown": max(0, len(items) - len(shown)),
        "what_this_means": (
            "Explicit operator observations reconciled against current saved "
            "instructions, account orders and trades. accepted_modified is "
            "derived from frozen proposal versus submitted terms. "
            "live_order_deleted stays unknown without explicit live-order "
            "deletion evidence; connector and operator claims remain "
            "correctable through visible supersession."
        ),
    }
