"""Build canonical schema-v4 candidates from smaller host-authored semantics."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .tool_artifacts import canonical_json_bytes, json_pointer_value
from .profile_paths import code_root

SEMANTIC_INPUT_SCHEMA_VERSION = 1
SEMANTIC_BUILDER_VERSION = 1
SEMANTIC_EXAMPLE_PATH = (
    code_root()
    / "schemas"
    / "host_semantic_v1.example.json"
)

CORE_STAGE_GRAPH = (
    ("portfolio", "observe", ()),
    ("market_scout", "discovery", ("portfolio",)),
    ("research_director", "direct", ("market_scout",)),
    ("memory_retrieval", "memory", ("research_director",)),
    ("evidence_arbitration", "gate", ()),
    ("portfolio_fit", "gate", ("evidence_arbitration",)),
    ("counterfactual", "gate", ("portfolio_fit",)),
    ("adversarial", "gate", ("counterfactual",)),
    ("governance_review", "governance", ("adversarial",)),
    ("decision", "decision", ("governance_review",)),
    ("learning_audit", "learning", ("decision",)),
    ("meta_research", "meta_research", ("learning_audit",)),
    ("self_improvement", "self_improvement", ("meta_research",)),
)
CORE_STAGE_IDS = frozenset(row[0] for row in CORE_STAGE_GRAPH)
REQUIRED_TOP_LEVEL = frozenset({
    "cycle_id",
    "as_of",
    "source",
    "order_submission_used",
    "order_instructions",
    "snapshot",
    "market_sessions",
    "evidence_calls",
    "market_scout_report",
    "research_agenda",
    "research",
    "findings",
    "decision",
    "stage_outputs",
    "learning_stage_dispositions",
})
RESERVED_TOP_LEVEL = frozenset({
    "semantic_input_schema_version",
    "corrects_candidate_id",
    "market_scout_report",
    "research_agenda",
    "stage_outputs",
})
STAGE_MECHANIC_FIELDS = frozenset({"status", "tools_used"})
EVIDENCE_STATUS_ALIASES = {
    "partially_verified": "partial",
}
MARKET_STATUS_ALIASES = {
    "closed_weekend": "closed",
}


@dataclass(frozen=True)
class SemanticIssue:
    code: str
    pointer: str
    detail: str = ""


@dataclass(frozen=True)
class BuiltSemanticCandidate:
    canonical: dict[str, Any]
    canonical_bytes: bytes
    target_name: str
    pointer_map: dict[str, str]
    builder_version: int


class SemanticCandidateError(ValueError):
    def __init__(self, issues: Sequence[SemanticIssue]):
        self.issues = tuple(issues)
        super().__init__(
            ",".join(
                f"{issue.code}|{issue.pointer}|{issue.detail}"
                for issue in self.issues
            )
        )


def is_semantic_candidate(value: Any, *, filename: str = "") -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("semantic_input_schema_version") is not None
    ) or filename.endswith(".semantic.json")


def canonical_target_name(filename: str) -> str:
    if not filename.endswith(".semantic.json"):
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_filename_required",
                "/",
                "filename must end with .semantic.json",
            ),
        ))
    return filename.removesuffix(".semantic.json") + ".json"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_json_bytes(left) == canonical_json_bytes(right)
    except (TypeError, ValueError):
        return False


def _matching_source_paths(value: Any, target: Any, prefix: str = "") -> list[str]:
    matches = []
    if _canonical_equal(value, target):
        matches.append(prefix or "/")
    if isinstance(value, Mapping):
        for key, child in value.items():
            matches.extend(_matching_source_paths(
                child,
                target,
                f"{prefix}/{_pointer_token(str(key))}",
            ))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(_matching_source_paths(
                child,
                target,
                f"{prefix}/{index}",
            ))
    return matches


def _source_path_for_target(result: Any, target: Any, target_path: str) -> str:
    matches = _matching_source_paths(result, target)
    leaf = target_path.rsplit("/", 1)[-1]
    leaf_matches = [
        path for path in matches
        if path.rsplit("/", 1)[-1] == leaf
    ]
    selected = leaf_matches or matches
    if len(selected) != 1:
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_projection_source_ambiguous",
                target_path,
                f"matches={len(selected)}",
            ),
        ))
    return selected[0]


def _web_sources(
    value: Any,
    *,
    pointer: str,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SemanticCandidateError((
            SemanticIssue("semantic_web_sources_list", pointer),
        ))
    rows = []
    for index, item in enumerate(value):
        item_pointer = f"{pointer}/{index}"
        if not isinstance(item, Mapping):
            raise SemanticCandidateError((
                SemanticIssue("semantic_web_source_object", item_pointer),
            ))
        excerpt = item.get("excerpt")
        if excerpt is not None and not isinstance(excerpt, str):
            raise SemanticCandidateError((
                SemanticIssue(
                    "semantic_web_source_excerpt",
                    f"{item_pointer}/excerpt",
                ),
            ))
        rows.append({
            "url": item.get("url"),
            "title": item.get("title"),
            "published_at": item.get("published_at"),
            "retrieved_at": item.get("retrieved_at"),
            "excerpt": excerpt,
            "excerpt_sha256": (
                hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
                if excerpt
                else None
            ),
            "reconstruction_status": (
                "bounded_source_excerpt" if excerpt else "locator_only"
            ),
        })
    return rows


def _canonical_call(
    value: Any,
    *,
    pointer: str,
    pointer_map: dict[str, str],
    canonical_pointer: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SemanticCandidateError((
            SemanticIssue("semantic_tool_call_object", pointer),
        ))
    required = (
        "tool_call_id",
        "kind",
        "tool",
        "action",
        "arguments",
        "result",
        "capture_origin",
        "observed_at",
    )
    issues = [
        SemanticIssue(
            "semantic_tool_call_missing",
            f"{pointer}/{field}",
            field,
        )
        for field in required
        if field not in value
    ]
    if issues:
        raise SemanticCandidateError(issues)
    origin = _text(value.get("capture_origin"))
    if origin not in {
        "direct_connector_response",
        "host_transcribed_response",
        "host_summary",
    }:
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_capture_origin",
                f"{pointer}/capture_origin",
                origin,
            ),
        ))
    web_sources = _web_sources(
        value.get("web_sources"),
        pointer=f"{pointer}/web_sources",
    )
    result_origin = (
        "host_summary" if origin == "host_summary"
        else "connector_response"
    )
    redactions = list(value.get("redactions") or ())
    request_redactions = list(value.get("request_redactions") or ())
    capture = {
        "schema_version": 2,
        "representation": (
            "host_summary_no_response"
            if origin == "host_summary"
            else "redacted_canonical_response"
            if redactions
            else "canonical_response"
        ),
        "redactions": redactions,
        "capture_origin": origin,
        "request_redactions": request_redactions,
        "reconstruction_status": (
            "bounded_source_excerpt"
            if origin == "host_summary"
            and any(source.get("excerpt") for source in web_sources)
            else "locator_only"
            if origin == "host_summary"
            else "exact_response"
        ),
    }
    call = {
        "tool_call_id": value.get("tool_call_id"),
        "kind": value.get("kind"),
        "tool": value.get("tool"),
        "call": {
            "action": value.get("action"),
            "arguments": deepcopy(value.get("arguments")),
        },
        "result": deepcopy(value.get("result")),
        "provenance": {
            "result_origin": result_origin,
            "observed_at": value.get("observed_at"),
            "source_refs": deepcopy(list(value.get("source_refs") or ())),
            "capture": capture,
            "web_sources": web_sources,
        },
    }
    pointer_map[canonical_pointer] = pointer
    pointer_map[f"{canonical_pointer}/call"] = f"{pointer}/action"
    pointer_map[f"{canonical_pointer}/provenance"] = (
        f"{pointer}/capture_origin"
    )
    pointer_map[f"{canonical_pointer}/provenance/capture"] = (
        f"{pointer}/capture_origin"
    )
    pointer_map[f"{canonical_pointer}/provenance/web_sources"] = (
        f"{pointer}/web_sources"
    )
    return call


def _selected_specialists(agenda: Any) -> list[str]:
    if not isinstance(agenda, Mapping):
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_research_agenda_object",
                "/research_agenda",
            ),
        ))
    selected = []
    for index, candidate in enumerate(agenda.get("candidates") or ()):
        if not isinstance(candidate, Mapping):
            continue
        if candidate.get("selected") is True:
            candidate_id = _text(candidate.get("candidate_id"))
            if candidate_id:
                selected.append(candidate_id)
    if not selected:
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_selected_specialist_required",
                "/research_agenda/candidates",
            ),
        ))
    return selected


def _stage_row(
    stage_id: str,
    phase: str,
    depends_on: Sequence[str],
    value: Any,
    *,
    extra_output: Mapping[str, Any] | None,
    pointer_map: dict[str, str],
    index: int,
) -> dict[str, Any]:
    pointer = f"/stage_outputs/{_pointer_token(stage_id)}"
    if not isinstance(value, Mapping):
        raise SemanticCandidateError((
            SemanticIssue("semantic_stage_output_object", pointer),
        ))
    issues = [
        SemanticIssue(
            "semantic_stage_field_missing",
            f"{pointer}/{field}",
            field,
        )
        for field in (
            "status",
            "tools_used",
            "observations",
            "evidence_status",
            "blockers",
            "confidence",
            "next_actions",
        )
        if field not in value
    ]
    if issues:
        raise SemanticCandidateError(issues)
    output = {
        key: deepcopy(item)
        for key, item in value.items()
        if key not in STAGE_MECHANIC_FIELDS
    }
    evidence_status = output.get("evidence_status")
    if evidence_status in EVIDENCE_STATUS_ALIASES:
        output["evidence_status"] = EVIDENCE_STATUS_ALIASES[evidence_status]
    if extra_output:
        output.update(deepcopy(dict(extra_output)))
    canonical_pointer = f"/cognitive_stages/{index}"
    pointer_map[canonical_pointer] = pointer
    pointer_map[f"{canonical_pointer}/output"] = pointer
    return {
        "stage_id": stage_id,
        "phase": phase,
        "depends_on": list(depends_on),
        "required": True,
        "status": value.get("status"),
        "tools_used": deepcopy(value.get("tools_used")),
        "output": output,
    }


def _derive_projection(
    producer: str,
    result: Any,
    canonical: Mapping[str, Any],
    *,
    market_region: str,
    pointer: str,
) -> dict[str, Any] | None:
    if producer == "market_sessions" and market_region:
        markets = canonical.get("market_sessions", {}).get("markets", [])
        index = next((
            index for index, market in enumerate(markets)
            if isinstance(market, Mapping)
            and _text(market.get("region")).upper() == market_region.upper()
        ), None)
        targets = (
            [f"/market_sessions/markets/{index}/is_open"]
            if index is not None
            else []
        )
    elif producer == "portfolio":
        targets = [
            path for path in (
                "/snapshot/net_liquidation_value",
                "/snapshot/cash",
                "/snapshot/positions",
            )
            if _pointer_exists(canonical, path)
        ]
    elif producer == "saved_instructions":
        targets = [
            path for path in (
                "/order_instructions",
                "/snapshot/order_instructions",
                "/snapshot/saved_order_instructions",
            )
            if _pointer_exists(canonical, path)
        ]
    elif producer == "account_orders":
        targets = [
            "/snapshot/open_orders"
        ] if _pointer_exists(canonical, "/snapshot/open_orders") else []
    elif producer == "account_trades":
        targets = [
            "/snapshot/trades"
        ] if _pointer_exists(canonical, "/snapshot/trades") else []
    else:
        targets = []
    if not targets:
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_evidence_target_missing",
                pointer,
                producer,
            ),
        ))
    bindings = []
    for target_path in targets:
        target = json_pointer_value(canonical, target_path)
        bindings.append({
            "source_path": _source_path_for_target(
                result,
                target,
                target_path,
            ),
            "target_path": target_path,
        })
    return {"extractor": "json_pointer_v1", "bindings": bindings}


def _pointer_exists(value: Mapping[str, Any], path: str) -> bool:
    try:
        json_pointer_value(value, path)
    except (KeyError, ValueError):
        return False
    return True


def _tool_manifest(
    value: Any,
    records: Sequence[Mapping[str, Any]],
) -> Any:
    if value is None or not isinstance(value, Mapping):
        return deepcopy(value)
    connectors = value.get("connectors")
    if not isinstance(connectors, list):
        return deepcopy(value)
    latest = next((
        record.get("payload")
        for record in reversed(list(records))
        if record.get("record_type") == "tool_inventory"
        and isinstance(record.get("payload"), Mapping)
    ), None)
    known_connectors = {
        _text(connector.get("name")).casefold(): connector
        for connector in (
            latest.get("connectors") or ()
            if isinstance(latest, Mapping)
            else ()
        )
        if isinstance(connector, Mapping)
    }
    result = deepcopy(dict(value))
    expanded = []
    for connector in connectors:
        if not isinstance(connector, Mapping):
            expanded.append(deepcopy(connector))
            continue
        row = deepcopy(dict(connector))
        actions = row.get("actions")
        if (
            isinstance(actions, list)
            and actions
            and all(isinstance(action, str) for action in actions)
        ):
            known = known_connectors.get(
                _text(row.get("name")).casefold()
            )
            known_actions = {
                _text(action.get("name")).casefold(): action
                for action in (
                    known.get("actions") or ()
                    if isinstance(known, Mapping)
                    else ()
                )
                if isinstance(action, Mapping)
            }
            missing = [
                action for action in actions
                if action.casefold() not in known_actions
            ]
            if missing:
                raise SemanticCandidateError(tuple(
                    SemanticIssue(
                        "semantic_tool_manifest_action_unknown",
                        "/tool_manifest_report/connectors",
                        action,
                    )
                    for action in missing
                ))
            row["actions"] = [
                deepcopy(dict(known_actions[action.casefold()]))
                for action in actions
            ]
        expanded.append(row)
    result["connectors"] = expanded
    return result


def translate_pointer(
    pointer: str,
    pointer_map: Mapping[str, str],
) -> str:
    match = max(
        (
            prefix for prefix in pointer_map
            if pointer == prefix or pointer.startswith(prefix + "/")
        ),
        key=len,
        default=None,
    )
    if match is None:
        return pointer
    return pointer_map[match] + pointer[len(match):]


def build_semantic_candidate(
    value: Mapping[str, Any],
    *,
    filename: str,
    records: Sequence[Mapping[str, Any]] = (),
) -> BuiltSemanticCandidate:
    issues = []
    if value.get("semantic_input_schema_version") != (
        SEMANTIC_INPUT_SCHEMA_VERSION
    ):
        issues.append(SemanticIssue(
            "semantic_schema_version",
            "/semantic_input_schema_version",
        ))
    if "host_input_schema_version" in value:
        issues.append(SemanticIssue(
            "semantic_canonical_version_forbidden",
            "/host_input_schema_version",
        ))
    issues.extend(
        SemanticIssue(
            "semantic_top_level_missing",
            f"/{field}",
            field,
        )
        for field in sorted(REQUIRED_TOP_LEVEL - set(value))
    )
    if issues:
        raise SemanticCandidateError(issues)

    pointer_map: dict[str, str] = {}
    canonical = {
        key: deepcopy(item)
        for key, item in value.items()
        if key not in RESERVED_TOP_LEVEL
    }
    canonical.pop("semantic_input_schema_version", None)
    canonical["host_input_schema_version"] = 4
    canonical["evidence_coverage_schema_version"] = 1
    canonical["tool_manifest_report"] = _tool_manifest(
        canonical.get("tool_manifest_report"),
        records,
    )

    sessions = canonical.get("market_sessions")
    if isinstance(sessions, Mapping):
        for market in sessions.get("markets") or ():
            if not isinstance(market, dict):
                continue
            status = market.get("status")
            if status in MARKET_STATUS_ALIASES:
                market["status"] = MARKET_STATUS_ALIASES[status]

    for research_index, research in enumerate(
        canonical.get("research") or ()
    ):
        if not isinstance(research, dict):
            continue
        calls = []
        for call_index, call in enumerate(research.get("tool_calls") or ()):
            calls.append(_canonical_call(
                call,
                pointer=f"/research/{research_index}/tool_calls/{call_index}",
                pointer_map=pointer_map,
                canonical_pointer=(
                    f"/research/{research_index}/tool_calls/{call_index}"
                ),
            ))
        research["tool_calls"] = calls

    scout_report = deepcopy(value["market_scout_report"])
    if isinstance(scout_report, dict):
        calls = []
        for call_index, call in enumerate(
            scout_report.get("tool_calls") or ()
        ):
            calls.append(_canonical_call(
                call,
                pointer=f"/market_scout_report/tool_calls/{call_index}",
                pointer_map=pointer_map,
                canonical_pointer=(
                    "/market_scout_report/tool_calls/"
                    f"{call_index}"
                ),
            ))
        scout_report["tool_calls"] = calls

    evidence_calls = []
    for index, wrapper in enumerate(value.get("evidence_calls") or ()):
        pointer = f"/evidence_calls/{index}"
        if not isinstance(wrapper, Mapping):
            raise SemanticCandidateError((
                SemanticIssue("semantic_evidence_call_object", pointer),
            ))
        producer = _text(wrapper.get("producer"))
        call = _canonical_call(
            wrapper.get("call"),
            pointer=f"{pointer}/call",
            pointer_map=pointer_map,
            canonical_pointer=f"/evidence_calls/{index}/call",
        )
        origin = call["provenance"]["capture"]["capture_origin"]
        projection = (
            None
            if origin == "host_summary"
            else _derive_projection(
                producer,
                call.get("result"),
                canonical,
                market_region=_text(wrapper.get("market_region")),
                pointer=pointer,
            )
        )
        evidence_calls.append({
            "producer": producer,
            "projection": projection,
            "call": call,
        })
        if producer == "market_sessions":
            region = _text(wrapper.get("market_region"))
            for market in (
                canonical.get("market_sessions", {}).get("markets", [])
            ):
                if (
                    isinstance(market, dict)
                    and _text(market.get("region")).upper() == region.upper()
                ):
                    market.setdefault(
                        "evidence_tool_call_ids",
                        [],
                    ).append(call["tool_call_id"])
    canonical["evidence_calls"] = evidence_calls

    agenda = deepcopy(value["research_agenda"])
    specialists = _selected_specialists(agenda)
    stage_outputs = value.get("stage_outputs")
    if not isinstance(stage_outputs, Mapping):
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_stage_outputs_object",
                "/stage_outputs",
            ),
        ))
    stage_specs = []
    for stage_id, phase, dependencies in CORE_STAGE_GRAPH:
        if stage_id == "evidence_arbitration":
            dependencies = tuple(specialists) or ("memory_retrieval",)
        extra = (
            {"market_scout_report": scout_report}
            if stage_id == "market_scout"
            else {"research_agenda": agenda}
            if stage_id == "research_director"
            else {
                "decision_status": canonical["decision"].get("status"),
                "rationale": canonical["decision"].get("rationale"),
            }
            if stage_id == "decision"
            else None
        )
        stage_specs.append((stage_id, phase, dependencies, extra))
        if stage_id == "memory_retrieval":
            stage_specs.extend(
                (
                    specialist,
                    "specialist",
                    ("memory_retrieval",),
                    None,
                )
                for specialist in specialists
            )
    canonical["cognitive_stages"] = [
        _stage_row(
            stage_id,
            phase,
            dependencies,
            stage_outputs.get(stage_id),
            extra_output=extra,
            pointer_map=pointer_map,
            index=index,
        )
        for index, (stage_id, phase, dependencies, extra)
        in enumerate(stage_specs)
    ]
    for index, (stage_id, _phase, _dependencies, _extra) in enumerate(
        stage_specs
    ):
        output_pointer = f"/cognitive_stages/{index}/output"
        if stage_id == "market_scout":
            pointer_map[
                f"{output_pointer}/market_scout_report"
            ] = "/market_scout_report"
        elif stage_id == "research_director":
            pointer_map[
                f"{output_pointer}/research_agenda"
            ] = "/research_agenda"
        elif stage_id == "decision":
            pointer_map[
                f"{output_pointer}/decision_status"
            ] = "/decision/status"
            pointer_map[
                f"{output_pointer}/rationale"
            ] = "/decision/rationale"
    canonical_bytes = (
        json.dumps(
            canonical,
            indent=2,
            ensure_ascii=True,
            sort_keys=False,
        )
        + "\n"
    ).encode("utf-8")
    return BuiltSemanticCandidate(
        canonical=canonical,
        canonical_bytes=canonical_bytes,
        target_name=canonical_target_name(filename),
        pointer_map=pointer_map,
        builder_version=SEMANTIC_BUILDER_VERSION,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build one canonical v4 candidate from semantic JSON.",
    )
    parser.add_argument("input")
    parser.add_argument("--output")
    args = parser.parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    path = Path(args.input)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise SemanticCandidateError((
                SemanticIssue(
                    "semantic_root_object_required",
                    "/",
                ),
            ))
        built = build_semantic_candidate(
            value,
            filename=path.name,
        )
    except (
        OSError,
        json.JSONDecodeError,
        SemanticCandidateError,
    ) as error:
        print(f"semantic candidate refused: {error}", file=sys.stderr)
        return 1
    if args.output:
        Path(args.output).write_bytes(built.canonical_bytes)
    else:
        sys.stdout.buffer.write(built.canonical_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
