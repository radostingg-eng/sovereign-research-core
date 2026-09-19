"""Private content-addressed artifacts for canonical tool responses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .profile_paths import profile_root

ARTIFACT_SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
REDACTION_SENTINEL = "__SOVEREIGN_REDACTED__"
CAPTURE_FIELDS = frozenset({
    "schema_version",
    "representation",
    "redactions",
})
REDACTION_FIELDS = frozenset({"path", "category", "reason"})
CAPTURE_REPRESENTATIONS = frozenset({
    "canonical_response",
    "redacted_canonical_response",
    "host_summary_no_response",
})
REDACTION_CATEGORIES = frozenset({
    "credential",
    "account_identifier",
    "contact_pii",
})
FORBIDDEN_REDACTION_COMPONENTS = frozenset({
    "instrument",
    "symbol",
    "ticker",
    "conid",
    "price",
    "last",
    "bid",
    "ask",
    "quantity",
    "position",
    "positions",
    "currency",
    "order",
    "orders",
    "execution",
    "executions",
    "fill",
    "fills",
    "timestamp",
    "observed_at",
    "valuation",
    "forecast",
    "exposure",
    "cash",
    "pnl",
    "buying",
    "power",
    "market",
    "cost",
    "qty",
    "size",
    "liquidation",
})
FORBIDDEN_REDACTION_ALIASES = frozenset({
    "observed_at",
    "net_liquidation_value",
    "market_value",
    "market_price",
    "last_price",
    "average_price",
    "avg_price",
    "cost_basis",
    "unrealized_pnl",
    "realized_pnl",
    "buying_power",
})
ALLOWED_REDACTION_LEAVES = {
    "credential": frozenset({
        "authorization",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "session_id",
        "sessionid",
        "cookie",
    }),
    "account_identifier": frozenset({
        "account_id",
        "accountid",
        "account_number",
        "accountnumber",
        "account_numbers",
        "client_account_id",
        "broker_account_id",
    }),
    "contact_pii": frozenset({
        "email",
        "email_address",
        "contact_email",
        "phone",
        "phone_number",
        "contact_phone",
        "mailing_address",
    }),
}
_ARTIFACT_REF = re.compile(
    r"^profile://([0-9a-f]{16})/tool-artifacts/sha256/"
    r"([0-9a-f]{2})/([0-9a-f]{64})\.json$"
)


def canonical_json_bytes(value: Any) -> bytes:
    """The exact bytes stored for one JSON-compatible response body."""
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def default_profile_root() -> Path:
    return profile_root()


def profile_root_for_journal(journal_path: Path) -> Path:
    parent = journal_path.resolve().parent
    return parent.parent if parent.name == "audit" else parent


def profile_namespace(records: Sequence[Mapping[str, Any]]) -> str:
    roots = [
        str(record.get("record_hash", ""))
        for record in records
        if record.get("prev_hash") is None
        and re.fullmatch(r"[0-9a-f]{64}", str(record.get("record_hash", "")))
    ]
    if len(roots) != 1:
        raise ValueError(
            f"tool_artifact_profile_root_invalid:{len(roots)}"
        )
    return roots[0][:16]


def iter_tool_calls(
    data: Mapping[str, Any],
) -> Iterator[dict[str, Any]]:
    stages = data.get("cognitive_stages")
    stages = stages if isinstance(stages, list) else []
    scout = next((
        row for row in stages
        if isinstance(row, Mapping)
        and row.get("stage_id") == "market_scout"
    ), None)
    output = scout.get("output") if isinstance(scout, Mapping) else {}
    output = output if isinstance(output, Mapping) else {}
    report = output.get("market_scout_report")
    report = report if isinstance(report, Mapping) else {}
    for call_index, call in enumerate(report.get("tool_calls") or ()):
        if isinstance(call, Mapping):
            yield {
                "scope": "market_scout",
                "research_index": 0,
                "call_index": call_index,
                "specialist_stage_id": "market_scout",
                "interpretation_ref": "stage:market_scout",
                "call": call,
            }
    for research_index, research in enumerate(data.get("research") or ()):
        if not isinstance(research, Mapping):
            continue
        for call_index, call in enumerate(research.get("tool_calls") or ()):
            if isinstance(call, Mapping):
                yield {
                    "scope": "research",
                    "research_index": research_index,
                    "call_index": call_index,
                    "specialist_stage_id": research.get(
                        "specialist_stage_id"),
                    "interpretation_ref": (
                        f"research:{research_index}:finding"
                    ),
                    "call": call,
                }


def _pointer_tokens(path: str) -> list[str] | None:
    if not path.startswith("/"):
        return None
    tokens = []
    for token in path[1:].split("/"):
        if re.search(r"~(?![01])", token):
            return None
        tokens.append(token.replace("~1", "/").replace("~0", "~"))
    return tokens


def _normalized_token(token: str) -> str:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", token)
    return re.sub(r"[^a-zA-Z0-9]+", "_", camel_split).strip("_").casefold()


def _token_components(token: str) -> set[str]:
    normalized = _normalized_token(token)
    return {
        component
        for component in normalized.split("_")
        if component
    }


def _redaction_hides_investment_evidence(tokens: Sequence[str]) -> bool:
    for token in tokens:
        normalized = _normalized_token(token)
        if normalized in FORBIDDEN_REDACTION_ALIASES:
            return True
        if _token_components(token) & FORBIDDEN_REDACTION_COMPONENTS:
            return True
    return False


def _redaction_leaf_allowed(tokens: Sequence[str], category: Any) -> bool:
    allowed = ALLOWED_REDACTION_LEAVES.get(category)
    if allowed is None:
        return False
    for token in reversed(tokens):
        normalized = _normalized_token(token)
        if not normalized or normalized.isdigit():
            continue
        return normalized in allowed
    return False


def _pointer_value(value: Any, tokens: Sequence[str]) -> Any:
    current = value
    for token in tokens:
        if isinstance(current, Mapping):
            if token not in current:
                raise KeyError(token)
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                raise KeyError(token)
            current = current[index]
        else:
            raise KeyError(token)
    return current


def _sentinel_paths(value: Any, prefix: str = "") -> set[str]:
    if value == REDACTION_SENTINEL:
        return {prefix or "/"}
    paths: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            paths.update(_sentinel_paths(child, f"{prefix}/{token}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.update(_sentinel_paths(child, f"{prefix}/{index}"))
    return paths


def validate_capture(
    result: Any,
    capture: Any,
    *,
    result_origin: Any,
) -> list[str]:
    if not isinstance(capture, Mapping):
        return ["capture_missing"]
    errors = []
    if set(capture) != CAPTURE_FIELDS:
        errors.append("capture_fields")
    if capture.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        errors.append("capture_schema_version")
    representation = capture.get("representation")
    if representation not in CAPTURE_REPRESENTATIONS:
        errors.append("capture_representation")
    redactions = capture.get("redactions")
    if not isinstance(redactions, list):
        errors.append("capture_redactions_not_list")
        redactions = []
    elif len(redactions) > 20:
        errors.append("capture_redactions_too_many")

    if result_origin == "host_summary":
        if representation != "host_summary_no_response":
            errors.append("capture_host_summary_representation")
        if redactions:
            errors.append("capture_host_summary_redactions")
        return sorted(set(errors))

    if representation == "host_summary_no_response":
        errors.append("capture_connector_representation")
    if representation == "canonical_response" and redactions:
        errors.append("capture_raw_has_redactions")
    if (
        representation == "redacted_canonical_response"
        and not redactions
    ):
        errors.append("capture_redactions_required")

    declared: set[str] = set()
    for index, row in enumerate(redactions):
        prefix = f"capture_redaction_{index}"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}_not_object")
            continue
        if set(row) != REDACTION_FIELDS:
            errors.append(f"{prefix}_fields")
        path = row.get("path")
        tokens = _pointer_tokens(path) if isinstance(path, str) else None
        if tokens is None:
            errors.append(f"{prefix}_path")
            continue
        if path in declared:
            errors.append(f"{prefix}_duplicate")
        declared.add(path)
        if _redaction_hides_investment_evidence(tokens):
            errors.append(f"{prefix}_investment_evidence_forbidden")
        category = row.get("category")
        if category not in REDACTION_CATEGORIES:
            errors.append(f"{prefix}_category")
        elif not _redaction_leaf_allowed(tokens, category):
            errors.append(f"{prefix}_path_not_allowed_for_category")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{prefix}_reason")
        try:
            marker = _pointer_value(result, tokens)
        except KeyError:
            errors.append(f"{prefix}_unresolved")
        else:
            if marker != REDACTION_SENTINEL:
                errors.append(f"{prefix}_marker")
    markers = _sentinel_paths(result)
    if markers != declared:
        errors.append("capture_redaction_markers_mismatch")
    return sorted(set(errors))


def build_artifact_specs(
    data: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int, int], dict[str, Any]]:
    if data.get("host_input_schema_version") != 4:
        return {}
    namespace = profile_namespace(records)
    specs: dict[tuple[str, int, int], dict[str, Any]] = {}
    for descriptor in iter_tool_calls(data):
        call = descriptor["call"]
        provenance = call.get("provenance")
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("result_origin") != "connector_response"
        ):
            continue
        content = canonical_json_bytes(call.get("result"))
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"tool_artifact_too_large:{call.get('tool_call_id')}:"
                f"{len(content)}"
            )
        digest = content_sha256(content)
        relative = (
            Path("tool_artifacts")
            / "sha256"
            / digest[:2]
            / f"{digest}.json"
        )
        ref = (
            f"profile://{namespace}/tool-artifacts/sha256/"
            f"{digest[:2]}/{digest}.json"
        )
        key = (
            descriptor["scope"],
            descriptor["research_index"],
            descriptor["call_index"],
        )
        specs[key] = {
            "content": content,
            "result_sha256": digest,
            "artifact_ref": ref,
            "artifact_path": relative,
            "artifact_byte_length": len(content),
        }
    return specs


def materialize_artifacts(
    specs: Mapping[tuple[str, int, int], Mapping[str, Any]],
    *,
    profile_root: Path | None = None,
) -> None:
    root = (profile_root or default_profile_root()).resolve()
    for spec in specs.values():
        relative = spec.get("artifact_path")
        content = spec.get("content")
        if not isinstance(relative, Path) or not isinstance(content, bytes):
            raise ValueError("tool_artifact_spec_invalid")
        target = (root / relative).resolve()
        if root != target and root not in target.parents:
            raise ValueError("tool_artifact_path_escape")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError(
                    f"tool_artifact_content_mismatch:{target.name}"
                )
            continue
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        if target.read_bytes() != content:
            raise ValueError(
                f"tool_artifact_write_mismatch:{target.name}"
            )


def _path_from_ref(
    ref: str,
    *,
    namespace: str,
    profile_root: Path,
) -> tuple[Path, str] | None:
    match = _ARTIFACT_REF.fullmatch(ref)
    if match is None or match.group(1) != namespace:
        return None
    digest = match.group(3)
    if match.group(2) != digest[:2]:
        return None
    path = (
        profile_root
        / "tool_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    ).resolve()
    if profile_root != path and profile_root not in path.parents:
        return None
    return path, digest


def verify_artifact_records(
    records: Sequence[Mapping[str, Any]],
    *,
    profile_root: Path | None = None,
) -> list[str]:
    root = (profile_root or default_profile_root()).resolve()
    try:
        namespace = profile_namespace(records)
    except ValueError as error:
        return [str(error)]
    errors = []
    v4_cycles = {
        str(payload.get("cycle_id", "")).strip()
        for record in records
        if record.get("record_type") == "cycle_receipt"
        and isinstance((payload := record.get("payload")), Mapping)
        and isinstance(payload.get("host_input_schema_version"), int)
        and not isinstance(payload.get("host_input_schema_version"), bool)
        and payload.get("host_input_schema_version") == 4
        and str(payload.get("cycle_id", "")).strip()
    }
    provenance_by_id = {
        str(record.get("record_id", "")): record
        for record in records
        if record.get("record_type") == "tool_provenance"
    }
    for cycle_id in sorted(v4_cycles):
        record_id = f"tool-provenance:{cycle_id}"
        if record_id not in provenance_by_id:
            errors.append(f"{record_id}:index_missing")
    for record in records:
        if record.get("record_type") != "tool_provenance":
            continue
        record_id = str(record.get("record_id", ""))
        cycle_id = record_id.removeprefix("tool-provenance:")
        v4_index = cycle_id in v4_cycles
        payload = record.get("payload")
        calls = (
            payload.get("calls")
            if isinstance(payload, Mapping)
            else None
        )
        if not isinstance(calls, list):
            if v4_index:
                errors.append(f"{record_id}:calls_invalid")
            continue
        for index, row in enumerate(calls):
            prefix = f"{record_id}:call_{index}"
            if not isinstance(row, Mapping):
                if v4_index:
                    errors.append(f"{prefix}:call_not_object")
                continue
            if v4_index:
                required_capture_fields = {
                    "scope",
                    "tool_call_id",
                    "action",
                    "request_sha256",
                    "interpretation_ref",
                    "capture_representation",
                    "redaction_count",
                    "web_source_count",
                    "artifact_ref",
                    "artifact_byte_length",
                }
                if not required_capture_fields.issubset(row):
                    errors.append(f"{prefix}:capture_fields_missing")
                    continue
            elif "artifact_ref" not in row:
                continue
            ref = row.get("artifact_ref")
            origin = row.get("result_origin")
            if origin == "host_summary":
                if ref is not None or row.get("artifact_byte_length") != 0:
                    errors.append(f"{prefix}:host_summary_has_artifact")
                continue
            if not isinstance(ref, str):
                errors.append(f"{prefix}:artifact_ref_missing")
                continue
            resolved = _path_from_ref(
                ref,
                namespace=namespace,
                profile_root=root,
            )
            if resolved is None:
                errors.append(f"{prefix}:artifact_ref_invalid")
                continue
            path, digest = resolved
            if not path.is_file():
                errors.append(f"{prefix}:artifact_missing")
                continue
            content = path.read_bytes()
            if content_sha256(content) != digest:
                errors.append(f"{prefix}:artifact_hash_mismatch")
            if row.get("result_sha256") != digest:
                errors.append(f"{prefix}:result_hash_mismatch")
            if row.get("artifact_byte_length") != len(content):
                errors.append(f"{prefix}:artifact_size_mismatch")
            try:
                parsed = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                errors.append(f"{prefix}:artifact_not_canonical_json")
            else:
                if canonical_json_bytes(parsed) != content:
                    errors.append(f"{prefix}:artifact_not_canonical_json")
    return sorted(set(errors))


def main() -> int:
    from .integrity import load_journal_records

    problems = verify_artifact_records(load_journal_records())
    for problem in problems:
        print(problem)
    print("tool artifacts: ok" if not problems else "tool artifacts: FAILED")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
