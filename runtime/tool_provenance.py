"""Bounded provenance for host-authored research and discovery results.

Hashes bind the result committed by the host to the journal. They do not prove
that a connector returned that value, and source references remain host
assertions.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

PROVENANCE_REQUIRED_FROM = datetime(
    2026, 9, 17, 15, 33, 44, tzinfo=timezone.utc)
OBSERVATION_WINDOW = timedelta(hours=48)
PROVENANCE_FIELDS = frozenset({
    "result_origin",
    "observed_at",
    "source_refs",
})
SOURCE_REF_FIELDS = frozenset({"kind", "value"})
RESULT_ORIGINS = frozenset({"connector_response", "host_summary"})
STABLE_LOCATOR_KINDS = frozenset({
    "url",
    "uri",
    "link",
    "response_id",
    "document_id",
    "accession_id",
})


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def provenance_required(cycle_as_of: Any) -> bool:
    observed = _timestamp(cycle_as_of)
    return observed is not None and observed >= PROVENANCE_REQUIRED_FROM


def canonical_result_hash(result: Any) -> str:
    encoded = json.dumps(
        result,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_tool_call_provenance(
    call: Mapping[str, Any],
    *,
    cycle_as_of: Any,
) -> list[str]:
    """Validate a flexible provenance envelope before cycle execution."""
    problems = []
    provenance = call.get("provenance")
    if not isinstance(provenance, Mapping):
        return ["provenance_missing"]
    if set(provenance) - PROVENANCE_FIELDS:
        problems.append("provenance_unexpected_fields")
    origin = provenance.get("result_origin")
    if origin not in RESULT_ORIGINS:
        problems.append("result_origin_invalid")

    observed_at = provenance.get("observed_at")
    if observed_at is None or observed_at == "":
        problems.append("observed_at_missing")
        observed = None
    elif not isinstance(observed_at, str):
        problems.append("observed_at_not_string")
        observed = None
    else:
        observed = _timestamp(observed_at)
        if observed is None:
            problems.append("observed_at_timezone_required")
    cycle_time = _timestamp(cycle_as_of)
    if observed is not None and cycle_time is not None:
        if abs(observed - cycle_time) > OBSERVATION_WINDOW:
            problems.append("observed_at_outside_cycle_window")

    refs = provenance.get("source_refs")
    stable_locator_found = False
    if not isinstance(refs, list):
        problems.append("source_refs_not_list")
        refs = []
    elif len(refs) > 10:
        problems.append("source_refs_too_many")
    seen = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, Mapping):
            problems.append(f"source_ref_{index}_not_object")
            continue
        if set(ref) - SOURCE_REF_FIELDS:
            problems.append(f"source_ref_{index}_unexpected_fields")
        raw_kind = ref.get("kind")
        raw_value = ref.get("value")
        kind = raw_kind.strip() if isinstance(raw_kind, str) else ""
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if raw_kind is not None and not isinstance(raw_kind, str):
            problems.append(f"source_ref_{index}_kind_not_string")
        elif not kind:
            problems.append(f"source_ref_{index}_kind_missing")
        if raw_value is not None and not isinstance(raw_value, str):
            problems.append(f"source_ref_{index}_value_not_string")
        elif not value:
            problems.append(f"source_ref_{index}_value_missing")
        elif len(value) > 1000:
            problems.append(f"source_ref_{index}_value_too_long")
        normalized = (kind.casefold(), value)
        if kind and value and normalized in seen:
            problems.append(f"source_ref_{index}_duplicate")
        seen.add(normalized)
        normalized_kind = kind.casefold()
        if normalized_kind in STABLE_LOCATOR_KINDS and value:
            stable_locator_found = True
        if normalized_kind in {"url", "link"} and value:
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                problems.append(f"source_ref_{index}_url_invalid")
        if normalized_kind == "uri" and value:
            if not urlparse(value).scheme:
                problems.append(f"source_ref_{index}_uri_invalid")

    result = call.get("result")
    if origin == "host_summary":
        if not isinstance(result, str) or not result.strip():
            problems.append("host_summary_result_not_nonempty_string")
        if not stable_locator_found:
            problems.append("host_summary_stable_ref_required")
    elif origin == "connector_response":
        if not isinstance(result, (Mapping, list)) and not stable_locator_found:
            problems.append("scalar_connector_stable_ref_required")
    try:
        canonical_result_hash(result)
    except (TypeError, ValueError):
        problems.append("result_not_canonical_json")
    return sorted(set(problems))


def build_tool_provenance_index(
    data: Mapping[str, Any],
    *,
    cycle_id: str,
) -> dict[str, Any]:
    """Build the fully validated post-receipt index."""
    rows = []
    stages = data.get("cognitive_stages")
    stages = stages if isinstance(stages, list) else []
    scout = next((
        row for row in stages
        if isinstance(row, Mapping)
        and row.get("stage_id") == "market_scout"
    ), None)
    scout_output = scout.get("output") if isinstance(scout, Mapping) else {}
    scout_output = scout_output if isinstance(scout_output, Mapping) else {}
    scout_report = scout_output.get("market_scout_report")
    scout_report = (
        scout_report if isinstance(scout_report, Mapping) else {}
    )
    for call_index, call in enumerate(scout_report.get("tool_calls") or ()):
        if not isinstance(call, Mapping):
            continue
        provenance = call.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError("tool_provenance_missing_after_validation")
        refs = provenance.get("source_refs")
        if not isinstance(refs, list):
            raise ValueError("tool_source_refs_invalid_after_validation")
        rows.append({
            "scope": "market_scout",
            "tool_call_id": call.get("tool_call_id"),
            "research_index": 0,
            "call_index": call_index,
            "specialist_stage_id": "market_scout",
            "tool": call.get("tool"),
            "result_origin": provenance.get("result_origin"),
            "observed_at": provenance.get("observed_at"),
            "source_refs": [
                {
                    "kind": str(ref.get("kind", "")).strip(),
                    "value": str(ref.get("value", "")).strip(),
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ],
            "result_sha256": canonical_result_hash(call.get("result")),
        })
    for research_index, research in enumerate(data.get("research") or ()):
        if not isinstance(research, Mapping):
            continue
        for call_index, call in enumerate(research.get("tool_calls") or ()):
            if not isinstance(call, Mapping):
                continue
            provenance = call.get("provenance")
            if not isinstance(provenance, Mapping):
                raise ValueError("tool_provenance_missing_after_validation")
            refs = provenance.get("source_refs")
            if not isinstance(refs, list):
                raise ValueError("tool_source_refs_invalid_after_validation")
            rows.append({
                "scope": "research",
                "tool_call_id": f"research:{research_index}:{call_index}",
                "research_index": research_index,
                "call_index": call_index,
                "specialist_stage_id": research.get(
                    "specialist_stage_id"),
                "tool": call.get("tool"),
                "result_origin": provenance.get("result_origin"),
                "observed_at": provenance.get("observed_at"),
                "source_refs": [
                    {
                        "kind": str(ref.get("kind", "")).strip(),
                        "value": str(ref.get("value", "")).strip(),
                    }
                    for ref in refs
                    if isinstance(ref, Mapping)
                ],
                "result_sha256": canonical_result_hash(
                    call.get("result")),
            })
    return {"cycle_id": cycle_id, "calls": rows}


def resolve_tool_call(
    data: Mapping[str, Any],
    tool_call_id: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Return validated index metadata and the corresponding raw call."""
    cycle_id = str(data.get("cycle_id", "")).strip()
    try:
        index = build_tool_provenance_index(data, cycle_id=cycle_id)
    except (KeyError, TypeError, ValueError):
        return None
    row = next((
        item for item in index["calls"]
        if str(item.get("tool_call_id", "")).strip() == tool_call_id
    ), None)
    if not isinstance(row, Mapping):
        return None
    if row.get("scope") == "market_scout":
        stages = data.get("cognitive_stages")
        stages = stages if isinstance(stages, list) else []
        scout = next((
            stage for stage in stages
            if isinstance(stage, Mapping)
            and stage.get("stage_id") == "market_scout"
        ), None)
        output = scout.get("output") if isinstance(scout, Mapping) else {}
        output = output if isinstance(output, Mapping) else {}
        report = output.get("market_scout_report")
        report = report if isinstance(report, Mapping) else {}
        calls = report.get("tool_calls")
        calls = calls if isinstance(calls, list) else []
    else:
        research = data.get("research")
        research = research if isinstance(research, list) else []
        research_index = row.get("research_index")
        if (
            not isinstance(research_index, int)
            or research_index >= len(research)
            or not isinstance(research[research_index], Mapping)
        ):
            return None
        calls = research[research_index].get("tool_calls")
        calls = calls if isinstance(calls, list) else []
    call_index = row.get("call_index")
    if (
        not isinstance(call_index, int)
        or call_index >= len(calls)
        or not isinstance(calls[call_index], Mapping)
    ):
        return None
    return row, calls[call_index]


def _validate_index_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if set(payload) != {"cycle_id", "calls"}:
        raise ValueError("tool_provenance_record_invalid:payload_fields")
    if not isinstance(payload.get("cycle_id"), str) or not payload["cycle_id"]:
        raise ValueError("tool_provenance_record_invalid:cycle_id")
    calls = payload.get("calls")
    if not isinstance(calls, list):
        raise ValueError("tool_provenance_record_invalid:calls")
    legacy_required = {
        "research_index",
        "call_index",
        "specialist_stage_id",
        "tool",
        "result_origin",
        "observed_at",
        "source_refs",
        "result_sha256",
    }
    current_required = legacy_required | {"scope", "tool_call_id"}
    for index, row in enumerate(calls):
        prefix = f"tool_provenance_record_invalid:call_{index}"
        if (
            not isinstance(row, Mapping)
            or set(row) not in (legacy_required, current_required)
        ):
            raise ValueError(f"{prefix}_fields")
        if "scope" in row and row["scope"] not in {
            "research", "market_scout",
        }:
            raise ValueError(f"{prefix}_scope")
        if "tool_call_id" in row and (
            not isinstance(row["tool_call_id"], str)
            or not row["tool_call_id"].strip()
        ):
            raise ValueError(f"{prefix}_tool_call_id")
        if (
            not isinstance(row["research_index"], int)
            or isinstance(row["research_index"], bool)
            or row["research_index"] < 0
            or not isinstance(row["call_index"], int)
            or isinstance(row["call_index"], bool)
            or row["call_index"] < 0
        ):
            raise ValueError(f"{prefix}_coordinates")
        if not isinstance(row["tool"], str) or not row["tool"].strip():
            raise ValueError(f"{prefix}_tool")
        if (
            not isinstance(row["specialist_stage_id"], str)
            or not row["specialist_stage_id"].strip()
        ):
            raise ValueError(f"{prefix}_specialist_stage_id")
        if row["result_origin"] not in RESULT_ORIGINS:
            raise ValueError(f"{prefix}_result_origin")
        if _timestamp(row["observed_at"]) is None:
            raise ValueError(f"{prefix}_observed_at")
        if (
            not isinstance(row["result_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["result_sha256"])
        ):
            raise ValueError(f"{prefix}_result_sha256")
        refs = row["source_refs"]
        if not isinstance(refs, list) or len(refs) > 10:
            raise ValueError(f"{prefix}_source_refs")
        for ref_index, ref in enumerate(refs):
            if (
                not isinstance(ref, Mapping)
                or set(ref) != SOURCE_REF_FIELDS
                or not isinstance(ref["kind"], str)
                or not ref["kind"].strip()
                or not isinstance(ref["value"], str)
                or not ref["value"].strip()
            ):
                raise ValueError(
                    f"{prefix}_source_ref_{ref_index}")
    return calls


def latest_tool_provenance(
    records: Sequence[Mapping[str, Any]],
    *,
    row_limit: int = 10,
    ref_limit: int = 3,
) -> dict[str, Any]:
    """Bounded feedback view of the latest provenance index."""
    payload = None
    for record in records:
        if record.get("record_type") != "tool_provenance":
            continue
        candidate = record.get("payload")
        if not isinstance(candidate, Mapping):
            raise ValueError(
                "tool_provenance_record_invalid:payload_not_object")
        _validate_index_payload(candidate)
        cycle_id = candidate["cycle_id"]
        if record.get("record_id") != f"tool-provenance:{cycle_id}":
            raise ValueError(
                "tool_provenance_record_invalid:record_id")
        if list(record.get("caused_by") or ()) != [
            f"cycle-receipt:{cycle_id}",
        ]:
            raise ValueError(
                "tool_provenance_record_invalid:caused_by")
        payload = candidate
    if payload is None:
        return {
            "cycle_id": None,
            "call_count": 0,
            "connector_response_count": 0,
            "host_summary_count": 0,
            "rows": [],
            "not_shown": 0,
            "what_this_means": (
                "No research tool provenance index exists in the supplied "
                "records."
            ),
        }
    calls = _validate_index_payload(payload)

    def bounded_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
        value = str(ref.get("value", ""))
        return {
            "kind": ref.get("kind"),
            "value": value[:200],
            "value_truncated": len(value) > 200,
        }

    rows = []
    for row in calls[:row_limit]:
        refs = [
            bounded_ref(ref)
            for ref in (row.get("source_refs") or ())[:ref_limit]
            if isinstance(ref, Mapping)
        ]
        rows.append({
            "scope": row.get("scope", "research"),
            "tool_call_id": row.get(
                "tool_call_id",
                f"research:{row.get('research_index')}:{row.get('call_index')}",
            ),
            "research_index": row.get("research_index"),
            "call_index": row.get("call_index"),
            "specialist_stage_id": row.get("specialist_stage_id"),
            "tool": row.get("tool"),
            "result_origin": row.get("result_origin"),
            "observed_at": row.get("observed_at"),
            "source_refs": refs,
            "source_refs_not_shown": max(
                0, len(row.get("source_refs") or ()) - ref_limit),
            "result_sha256": row.get("result_sha256"),
        })
    return {
        "cycle_id": payload.get("cycle_id"),
        "call_count": len(calls),
        "connector_response_count": sum(
            1 for row in calls
            if row.get("result_origin") == "connector_response"
        ),
        "host_summary_count": sum(
            1 for row in calls
            if row.get("result_origin") == "host_summary"
        ),
        "rows": rows,
        "not_shown": max(0, len(calls) - row_limit),
        "what_this_means": (
            "This covers Market Scout and research tool calls. Hashes identify "
            "the result committed by the host and detect later edits; they do "
            "not prove the connector returned it or detect fabrication at "
            "capture time. Source references are host-asserted locators for "
            "external evidence, not independent verification."
        ),
    }
