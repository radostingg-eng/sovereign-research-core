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
from urllib.parse import parse_qsl, urlparse

from .tool_artifacts import (
    MAX_ARTIFACT_BYTES,
    canonical_json_bytes,
    iter_tool_calls,
    validate_capture,
)

PROVENANCE_REQUIRED_FROM = datetime(
    2026, 9, 17, 15, 33, 44, tzinfo=timezone.utc)
OBSERVATION_WINDOW = timedelta(hours=48)
PROVENANCE_FIELDS = frozenset({
    "result_origin",
    "observed_at",
    "source_refs",
})
V4_PROVENANCE_FIELDS = PROVENANCE_FIELDS | {
    "capture",
    "web_sources",
}
SOURCE_REF_FIELDS = frozenset({"kind", "value"})
WEB_SOURCE_FIELDS = frozenset({
    "url",
    "title",
    "published_at",
    "retrieved_at",
})
RESULT_ORIGINS = frozenset({"connector_response", "host_summary"})
STABLE_LOCATOR_KINDS = frozenset({
    "url",
    "uri",
    "link",
    "response_id",
    "document_id",
    "accession_id",
})
_UNSAFE_URL_QUERY_KEYS = frozenset({
    "access_token",
    "token",
    "api_key",
    "apikey",
    "sig",
    "signature",
    "auth",
    "password",
    "secret",
    "account",
    "account_id",
})
MAX_PROVENANCE_FUTURE_SKEW = timedelta(minutes=5)


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
    schema_version: int | None = None,
    validation_now: datetime | None = None,
) -> list[str]:
    """Validate a flexible provenance envelope before cycle execution."""
    problems = []
    provenance = call.get("provenance")
    if not isinstance(provenance, Mapping):
        return ["provenance_missing"]
    expected_provenance = (
        V4_PROVENANCE_FIELDS
        if schema_version == 4
        else PROVENANCE_FIELDS
    )
    if set(provenance) - expected_provenance:
        problems.append("provenance_unexpected_fields")
    if schema_version == 4 and set(provenance) != expected_provenance:
        problems.append("provenance_v4_fields")
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
    if (
        schema_version == 4
        and observed is not None
        and observed > (validation_now or datetime.now(timezone.utc))
    ):
        problems.append("observed_at_in_future")

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

    if schema_version == 4:
        call_id = call.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            problems.append("tool_call_id_required")
        kind = call.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            problems.append("tool_call_kind_required")
        request = call.get("call")
        if not isinstance(request, Mapping):
            problems.append("call_not_object")
        else:
            if set(request) != {"action", "arguments"}:
                problems.append("call_fields")
            action = request.get("action")
            if not isinstance(action, str) or not action.strip():
                problems.append("call_action_required")
            arguments = request.get("arguments")
            if arguments is not None and not isinstance(arguments, Mapping):
                problems.append("call_arguments_invalid")
            try:
                canonical_result_hash(request)
            except (TypeError, ValueError):
                problems.append("call_not_canonical_json")
        if origin == "connector_response" and isinstance(result, str):
            problems.append("connector_result_string_invalid")
        if origin == "connector_response":
            try:
                artifact_size = len(canonical_json_bytes(result))
            except (TypeError, ValueError):
                artifact_size = 0
            if artifact_size > MAX_ARTIFACT_BYTES:
                problems.append("capture_artifact_too_large")
        problems.extend(validate_capture(
            result,
            provenance.get("capture"),
            result_origin=origin,
        ))
        problems.extend(_validate_web_sources(
            provenance.get("web_sources"),
            refs=refs,
            validation_now=validation_now,
        ))
    return sorted(set(problems))


def _validate_web_sources(
    value: Any,
    *,
    refs: Sequence[Mapping[str, Any]],
    validation_now: datetime | None,
) -> list[str]:
    if not isinstance(value, list):
        return ["web_sources_not_list"]
    if len(value) > 10:
        return ["web_sources_too_many"]
    errors = []
    url_refs = {
        str(ref.get("value", "")).strip()
        for ref in refs
        if isinstance(ref, Mapping)
        and str(ref.get("kind", "")).strip().casefold() in {"url", "link"}
    }
    urls = set()
    now = validation_now or datetime.now(timezone.utc)
    for index, row in enumerate(value):
        prefix = f"web_source_{index}"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}_not_object")
            continue
        if set(row) != WEB_SOURCE_FIELDS:
            errors.append(f"{prefix}_fields")
        url = row.get("url")
        if not isinstance(url, str) or not url.strip():
            errors.append(f"{prefix}_url")
            continue
        url = url.strip()
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
        ):
            errors.append(f"{prefix}_url")
        if parsed.username is not None or parsed.password is not None:
            errors.append(f"{prefix}_url_credentials")
        query_keys = {
            key.casefold()
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        if query_keys & _UNSAFE_URL_QUERY_KEYS:
            errors.append(f"{prefix}_url_sensitive_query")
        if url in urls:
            errors.append(f"{prefix}_duplicate")
        urls.add(url)
        title = row.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{prefix}_title")
        retrieved = _timestamp(row.get("retrieved_at"))
        if retrieved is None:
            errors.append(f"{prefix}_retrieved_at")
        elif retrieved > now + MAX_PROVENANCE_FUTURE_SKEW:
            errors.append(f"{prefix}_retrieved_at_future")
        published_value = row.get("published_at")
        published = (
            None
            if published_value is None
            else _timestamp(published_value)
        )
        if published_value is not None and published is None:
            errors.append(f"{prefix}_published_at")
        if (
            published is not None
            and retrieved is not None
            and published > retrieved
        ):
            errors.append(f"{prefix}_publication_after_retrieval")
    if urls != url_refs:
        errors.append("web_sources_url_refs_mismatch")
    return sorted(set(errors))


def build_tool_provenance_index(
    data: Mapping[str, Any],
    *,
    cycle_id: str,
    artifact_specs: Mapping[
        tuple[str, int, int], Mapping[str, Any]
    ] | None = None,
) -> dict[str, Any]:
    """Build the fully validated post-receipt index."""
    rows = []
    version = data.get("host_input_schema_version")
    specs = artifact_specs or {}
    for descriptor in iter_tool_calls(data):
        call = descriptor["call"]
        provenance = call.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError("tool_provenance_missing_after_validation")
        refs = provenance.get("source_refs")
        if not isinstance(refs, list):
            raise ValueError("tool_source_refs_invalid_after_validation")
        call_id = call.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = (
                f"research:{descriptor['research_index']}:"
                f"{descriptor['call_index']}"
            )
        row = {
            "scope": descriptor["scope"],
            "tool_call_id": call_id,
            "research_index": descriptor["research_index"],
            "call_index": descriptor["call_index"],
            "specialist_stage_id": descriptor["specialist_stage_id"],
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
        }
        if version == 4:
            request = call.get("call")
            capture = provenance.get("capture")
            capture = capture if isinstance(capture, Mapping) else {}
            key = (
                descriptor["scope"],
                descriptor["research_index"],
                descriptor["call_index"],
            )
            spec = specs.get(key)
            if provenance.get("result_origin") == "connector_response":
                if not isinstance(spec, Mapping):
                    raise ValueError(
                        f"tool_artifact_spec_missing:{call_id}"
                    )
                row["result_sha256"] = spec["result_sha256"]
                artifact_ref = spec["artifact_ref"]
                artifact_bytes = spec["artifact_byte_length"]
            else:
                artifact_ref = None
                artifact_bytes = 0
            row.update({
                "action": request.get("action"),
                "request_sha256": canonical_result_hash(request),
                "interpretation_ref": descriptor["interpretation_ref"],
                "capture_representation": capture.get("representation"),
                "redaction_count": len(capture.get("redactions") or ()),
                "web_source_count": len(
                    provenance.get("web_sources") or ()),
                "artifact_ref": artifact_ref,
                "artifact_byte_length": artifact_bytes,
            })
        rows.append(row)
    return {"cycle_id": cycle_id, "calls": rows}


def resolve_tool_call(
    data: Mapping[str, Any],
    tool_call_id: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Return validated index metadata and the corresponding raw call."""
    for descriptor in iter_tool_calls(data):
        call = descriptor["call"]
        candidate = call.get("tool_call_id")
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = (
                f"research:{descriptor['research_index']}:"
                f"{descriptor['call_index']}"
            )
        if candidate != tool_call_id:
            continue
        provenance = call.get("provenance")
        if not isinstance(provenance, Mapping):
            return None
        row = {
            "scope": descriptor["scope"],
            "tool_call_id": candidate,
            "research_index": descriptor["research_index"],
            "call_index": descriptor["call_index"],
            "specialist_stage_id": descriptor["specialist_stage_id"],
            "tool": call.get("tool"),
            "result_origin": provenance.get("result_origin"),
            "observed_at": provenance.get("observed_at"),
            "source_refs": provenance.get("source_refs"),
            "result_sha256": canonical_result_hash(call.get("result")),
        }
        return row, call
    return None


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
    capture_required = current_required | {
        "action",
        "request_sha256",
        "interpretation_ref",
        "capture_representation",
        "redaction_count",
        "web_source_count",
        "artifact_ref",
        "artifact_byte_length",
    }
    for index, row in enumerate(calls):
        prefix = f"tool_provenance_record_invalid:call_{index}"
        if (
            not isinstance(row, Mapping)
            or set(row) not in (
                legacy_required,
                current_required,
                capture_required,
            )
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
        if set(row) == capture_required:
            if (
                not isinstance(row["action"], str)
                or not row["action"].strip()
            ):
                raise ValueError(f"{prefix}_action")
            if (
                not isinstance(row["request_sha256"], str)
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    row["request_sha256"],
                )
            ):
                raise ValueError(f"{prefix}_request_sha256")
            if (
                not isinstance(row["interpretation_ref"], str)
                or not row["interpretation_ref"].strip()
            ):
                raise ValueError(f"{prefix}_interpretation_ref")
            if row["capture_representation"] not in {
                "canonical_response",
                "redacted_canonical_response",
                "host_summary_no_response",
            }:
                raise ValueError(f"{prefix}_capture_representation")
            for field in (
                "redaction_count",
                "web_source_count",
                "artifact_byte_length",
            ):
                if (
                    not isinstance(row[field], int)
                    or isinstance(row[field], bool)
                    or row[field] < 0
                ):
                    raise ValueError(f"{prefix}_{field}")
            if row["result_origin"] == "connector_response":
                if (
                    not isinstance(row["artifact_ref"], str)
                    or not row["artifact_ref"].strip()
                    or row["artifact_byte_length"] <= 0
                ):
                    raise ValueError(f"{prefix}_artifact")
            elif (
                row["artifact_ref"] is not None
                or row["artifact_byte_length"] != 0
            ):
                raise ValueError(f"{prefix}_host_summary_artifact")
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
        if str(ref.get("kind", "")).casefold() in {"url", "link"}:
            parsed = urlparse(value)
            value = parsed._replace(query="", fragment="").geturl()
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
        projected = {
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
        }
        if "artifact_ref" in row:
            projected.update({
                "action": row.get("action"),
                "request_sha256": row.get("request_sha256"),
                "interpretation_ref": row.get("interpretation_ref"),
                "capture_representation": row.get(
                    "capture_representation"),
                "redaction_count": row.get("redaction_count"),
                "web_source_count": row.get("web_source_count"),
                "artifact_ref": row.get("artifact_ref"),
                "artifact_byte_length": row.get(
                    "artifact_byte_length"),
            })
        rows.append(projected)
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
            "the canonical result committed by the host and detect later "
            "edits; they do "
            "not prove the connector returned it or detect fabrication at "
            "capture time. Schema-v4 connector responses also reference "
            "private content-addressed artifacts; host summaries explicitly "
            "have no response artifact. Declared redactions cannot hide "
            "investment-evidence paths, but connector-specific silent "
            "omission cannot be detected without a connector schema. Source "
            "references are host-asserted locators for external evidence, "
            "not independent verification."
        ),
    }
