"""Validate consequential evidence producers and exact cycle projections."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .tool_artifacts import (
    REDACTION_SENTINEL,
    canonical_json_bytes,
    json_pointer_value,
)
from .tool_provenance import validate_tool_call_provenance
from .timestamps import effective_as_of, parse_iso_timestamp

EVIDENCE_COVERAGE_SCHEMA_VERSION = 1
EVIDENCE_COVERAGE_REQUIRED_FROM = datetime(
    2026, 9, 19, 4, 0, tzinfo=timezone.utc,
)
EVIDENCE_PRODUCERS = frozenset({
    "portfolio",
    "saved_instructions",
    "account_orders",
    "account_trades",
    "market_sessions",
})
PROJECTION_FIELDS = frozenset({"extractor", "bindings"})
BINDING_FIELDS = frozenset({"source_path", "target_path"})
MAX_EVIDENCE_CALLS = 20
MAX_BINDINGS = 20


def evidence_coverage_required(data: Mapping[str, Any]) -> bool:
    if data.get("evidence_coverage_schema_version") is not None:
        return True
    observed = parse_iso_timestamp(effective_as_of(data))
    return (
        observed is not None
        and observed >= EVIDENCE_COVERAGE_REQUIRED_FROM
    )


def _contains_redaction(value: Any) -> bool:
    if value == REDACTION_SENTINEL:
        return True
    if isinstance(value, Mapping):
        return any(_contains_redaction(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_redaction(child) for child in value)
    return False


def _same_value(left: Any, right: Any) -> bool:
    try:
        return canonical_json_bytes(left) == canonical_json_bytes(right)
    except (TypeError, ValueError):
        return False


def _present_order_instruction_paths(
    data: Mapping[str, Any],
) -> list[str]:
    paths = []
    if "order_instructions" in data:
        paths.append("/order_instructions")
    snapshot = data.get("snapshot")
    if isinstance(snapshot, Mapping):
        if "order_instructions" in snapshot:
            paths.append("/snapshot/order_instructions")
        if "saved_order_instructions" in snapshot:
            paths.append("/snapshot/saved_order_instructions")
    return paths


def _required_producers(data: Mapping[str, Any]) -> set[str]:
    required = {"portfolio", "saved_instructions", "market_sessions"}
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if "open_orders" in snapshot:
        required.add("account_orders")
    if "trades" in snapshot:
        required.add("account_trades")
    for row in data.get("instruction_reconciliations") or ():
        if not isinstance(row, Mapping):
            continue
        if str(row.get("account_orders_tool_call_id", "")).strip():
            required.add("account_orders")
        if str(row.get("account_trades_tool_call_id", "")).strip():
            required.add("account_trades")
    for row in data.get("instruction_lifecycle_updates") or ():
        if not isinstance(row, Mapping):
            continue
        to_state = str(row.get("to_state", "")).strip().lower()
        if to_state in {"approved", "submitted", "executed"}:
            required.add("account_orders")
        if to_state == "executed":
            required.add("account_trades")
    return required


def _required_targets(data: Mapping[str, Any]) -> dict[str, set[str]]:
    targets = {
        "portfolio": {
            "/snapshot/net_liquidation_value",
            "/snapshot/cash",
            "/snapshot/positions",
        },
        "saved_instructions": set(
            _present_order_instruction_paths(data)
        ),
        "account_orders": {"/snapshot/open_orders"},
        "account_trades": {"/snapshot/trades"},
        "market_sessions": set(),
    }
    return targets


def _market_session_call_ids(data: Mapping[str, Any]) -> set[str]:
    ids = set()
    sessions = data.get("market_sessions")
    markets = (
        sessions.get("markets")
        if isinstance(sessions, Mapping)
        else None
    )
    for market in markets or ():
        if not isinstance(market, Mapping):
            continue
        for value in market.get("evidence_tool_call_ids") or ():
            if isinstance(value, str) and value.strip():
                ids.add(value.strip())
    return ids


def validate_evidence_coverage(
    data: Mapping[str, Any],
    *,
    validation_now: datetime | None = None,
) -> list[str]:
    required = evidence_coverage_required(data)
    version = data.get("evidence_coverage_schema_version")
    calls = data.get("evidence_calls")
    if not required and calls is None:
        return []
    errors: list[str] = []
    if version != EVIDENCE_COVERAGE_SCHEMA_VERSION:
        errors.append("evidence_coverage_schema_version_invalid")
    if not isinstance(calls, list):
        return sorted(set(
            errors + ["evidence_calls_must_be_a_list"]
        ))
    if len(calls) > MAX_EVIDENCE_CALLS:
        errors.append("evidence_calls_too_many")

    captured: dict[str, set[str]] = {}
    targets: dict[str, set[str]] = {}
    market_ids = _market_session_call_ids(data)
    for index, wrapper in enumerate(calls):
        prefix = f"evidence_call_invalid:{index}"
        if not isinstance(wrapper, Mapping):
            errors.append(f"{prefix}:not_object")
            continue
        if set(wrapper) != {"producer", "projection", "call"}:
            errors.append(f"{prefix}:fields")
        producer = str(wrapper.get("producer", "")).strip()
        if producer not in EVIDENCE_PRODUCERS:
            errors.append(f"{prefix}:producer")
        call = wrapper.get("call")
        if not isinstance(call, Mapping):
            errors.append(f"{prefix}:call")
            continue
        tool = call.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            errors.append(f"{prefix}:tool")
        call_id = call.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            errors.append(f"{prefix}:tool_call_id")
            call_id = ""
        else:
            call_id = call_id.strip()
            captured.setdefault(producer, set()).add(call_id)
        errors.extend(
            f"{prefix}:provenance:{problem}"
            for problem in validate_tool_call_provenance(
                call,
                cycle_as_of=effective_as_of(data),
                schema_version=4,
                validation_now=validation_now,
            )
        )
        provenance = call.get("provenance")
        origin = (
            provenance.get("result_origin")
            if isinstance(provenance, Mapping)
            else None
        )
        projection = wrapper.get("projection")
        if origin == "host_summary":
            if projection is not None:
                errors.append(f"{prefix}:host_summary_projection")
            continue
        if not isinstance(projection, Mapping):
            errors.append(f"{prefix}:projection")
            continue
        if set(projection) != PROJECTION_FIELDS:
            errors.append(f"{prefix}:projection_fields")
        if projection.get("extractor") != "json_pointer_v1":
            errors.append(f"{prefix}:extractor")
        bindings = projection.get("bindings")
        if not isinstance(bindings, list) or not bindings:
            errors.append(f"{prefix}:bindings")
            continue
        if len(bindings) > MAX_BINDINGS:
            errors.append(f"{prefix}:bindings_too_many")
        for binding_index, binding in enumerate(bindings):
            binding_prefix = f"{prefix}:binding:{binding_index}"
            if not isinstance(binding, Mapping):
                errors.append(f"{binding_prefix}:not_object")
                continue
            if set(binding) != BINDING_FIELDS:
                errors.append(f"{binding_prefix}:fields")
            source_path = binding.get("source_path")
            target_path = binding.get("target_path")
            if not isinstance(source_path, str):
                errors.append(f"{binding_prefix}:source_path")
                continue
            if not isinstance(target_path, str):
                errors.append(f"{binding_prefix}:target_path")
                continue
            try:
                source = json_pointer_value(
                    call.get("result"),
                    source_path,
                )
                target = json_pointer_value(data, target_path)
            except (KeyError, ValueError):
                errors.append(f"{binding_prefix}:unresolved")
                continue
            if _contains_redaction(source) or _contains_redaction(target):
                errors.append(f"{binding_prefix}:redacted")
            elif not _same_value(source, target):
                errors.append(f"{binding_prefix}:mismatch")
            targets.setdefault(producer, set()).add(target_path)

    if required:
        required_producers = _required_producers(data)
        for producer in sorted(required_producers - set(captured)):
            errors.append(f"evidence_producer_missing:{producer}")
        required_targets = _required_targets(data)
        for producer in sorted(required_producers):
            if producer == "market_sessions":
                if not market_ids:
                    errors.append(
                        "market_session_evidence_tool_call_ids_required"
                    )
                elif not market_ids.issubset(captured.get(producer, set())):
                    errors.append(
                        "market_session_evidence_tool_call_id_unresolved"
                    )
                continue
            missing = (
                required_targets.get(producer, set())
                - targets.get(producer, set())
            )
            errors.extend(
                f"evidence_projection_missing:{producer}:{path}"
                for path in sorted(missing)
            )
        order_paths = _present_order_instruction_paths(data)
        if order_paths:
            values = []
            for path in order_paths:
                try:
                    values.append(json_pointer_value(data, path))
                except (KeyError, ValueError):
                    pass
            if values and any(
                not _same_value(values[0], value)
                for value in values[1:]
            ):
                errors.append("order_instruction_projection_conflict")
    return sorted(set(errors))


def evidence_coverage_summary(
    data: Mapping[str, Any],
) -> dict[str, Any]:
    required = sorted(_required_producers(data))
    captured: dict[str, list[str]] = {}
    for wrapper in data.get("evidence_calls") or ():
        if not isinstance(wrapper, Mapping):
            continue
        producer = str(wrapper.get("producer", "")).strip()
        call = wrapper.get("call")
        call_id = (
            str(call.get("tool_call_id", "")).strip()
            if isinstance(call, Mapping)
            else ""
        )
        if producer in EVIDENCE_PRODUCERS and call_id:
            captured.setdefault(producer, []).append(call_id)
    captured_producers = sorted(captured)
    return {
        "schema_version": EVIDENCE_COVERAGE_SCHEMA_VERSION,
        "required_producers": required,
        "captured_producers": captured_producers,
        "missing_producers": sorted(
            set(required) - set(captured_producers)
        ),
        "producer_call_ids": {
            producer: sorted(set(call_ids))
            for producer, call_ids in sorted(captured.items())
        },
    }
