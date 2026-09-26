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
SEMANTIC_BUILDER_VERSION = 2
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
    "unchanged_from_prior",
    "carry_forward",
    "market_scout_report",
    "research_agenda",
    "stage_outputs",
})
CARRY_FORWARD_LIMITS = {
    "market_scout_report": 3,
    "research_agenda": 3,
    "tool_manifest_report": 24,
}
CARRY_FORWARD_STAGE_IDS = {
    "market_scout_report": "market_scout",
    "research_agenda": "research_director",
}
STAGE_MECHANIC_FIELDS = frozenset({"status", "tools_used"})
STAGE_OUTPUT_REQUIRED_FIELDS = frozenset({
    "status",
    "tools_used",
    "observations",
    "evidence_status",
    "blockers",
    "confidence",
    "next_actions",
})
EVIDENCE_STATUS_ALIASES = {
    "partially_verified": "partial",
}
MARKET_STATUS_ALIASES = {
    "closed_weekend": "closed",
}
CAPTURE_ORIGIN_ALIASES = {
    "web_source": "host_summary",
    "web_search_result": "host_summary",
    "external_search": "host_summary",
    "web_search": "host_summary",
    "search_result": "host_summary",
    "direct_web_search": "host_summary",
    "direct_web_response": "host_summary",
    "direct_web_result": "host_summary",
    "web_result_summary": "host_summary",
    "direct_file_analysis": "host_transcribed_response",
}
RESULT_ORIGIN_ALIASES = {
    "web_response": "host_summary",
    "web_source": "host_summary",
}
EVIDENCE_TOOL_DEFAULTS = {
    "portfolio": "IBKR",
    "saved_instructions": "IBKR",
    "account_orders": "IBKR",
    "account_trades": "IBKR",
    "market_sessions": "market clock",
    "market_scout": "web.search",
}
DERIVABLE_EVIDENCE_PRODUCERS = frozenset({
    "portfolio",
    "saved_instructions",
    "account_orders",
    "account_trades",
    "market_sessions",
})


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


def _nested_call_source(value: Mapping[str, Any]) -> str | None:
    if not isinstance(value.get("provenance"), Mapping):
        return None
    if isinstance(value.get("call"), Mapping):
        return "call"
    action = value.get("action")
    if isinstance(action, Mapping) and (
        "action" in action or "arguments" in action
    ):
        return "action"
    return None


def _call_source_pointer(
    pointer: str,
    field: str,
    *,
    nested: str | None,
) -> str:
    if not nested:
        return f"{pointer}/{field}"
    if field in {"action", "arguments"}:
        return f"{pointer}/{nested}/{field}"
    if field == "capture_origin":
        return f"{pointer}/provenance/capture/capture_origin"
    if field in {"redactions", "request_redactions"}:
        return f"{pointer}/provenance/capture/{field}"
    if field in {
        "result_origin",
        "observed_at",
        "source_refs",
        "web_sources",
    }:
        return f"{pointer}/provenance/{field}"
    return f"{pointer}/{field}"


def _compact_call_values(value: Mapping[str, Any]) -> dict[str, Any]:
    nested = _nested_call_source(value)
    nested_call = value.get(nested) if nested else None
    provenance = value.get("provenance")
    if (
        isinstance(nested_call, Mapping)
        and isinstance(provenance, Mapping)
    ):
        result = {
            "tool_call_id": value.get("tool_call_id"),
            "kind": value.get("kind"),
            "tool": value.get("tool"),
            "action": nested_call.get("action"),
            "arguments": deepcopy(nested_call.get("arguments")),
            "result": deepcopy(value.get("result")),
        }
    else:
        result = deepcopy(dict(value))
    if isinstance(provenance, Mapping):
        capture = provenance.get("capture")
        capture = capture if isinstance(capture, Mapping) else {}
        provenance_fields = {
            "capture_origin": capture.get("capture_origin"),
            "result_origin": provenance.get("result_origin"),
            "observed_at": provenance.get("observed_at"),
            "source_refs": deepcopy(provenance.get("source_refs")),
            "web_sources": deepcopy(provenance.get("web_sources")),
            "redactions": deepcopy(capture.get("redactions")),
            "request_redactions": deepcopy(
                capture.get("request_redactions")
            ),
        }
        for field, item in provenance_fields.items():
            if field not in result and item is not None:
                result[field] = item
    action = result.get("action")
    if (
        isinstance(action, Mapping)
        and isinstance(action.get("name"), str)
        and action["name"].strip()
        and "arguments" not in result
    ):
        result["action"] = action["name"].strip()
        result["arguments"] = {
            key: deepcopy(item)
            for key, item in action.items()
            if key != "name"
        }
    return result


def _capture_origin(value: Mapping[str, Any]) -> str:
    explicit = value.get("capture_origin")
    if explicit is not None:
        return CAPTURE_ORIGIN_ALIASES.get(
            _text(explicit),
            _text(explicit),
        )
    if (
        _text(value.get("action"))
        and isinstance(value.get("result"), (Mapping, list))
    ):
        return "direct_connector_response"
    return "host_summary"


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
    nested = _nested_call_source(value)
    value = _compact_call_values(value)
    required = (
        "tool_call_id",
        "kind",
        "tool",
        "action",
        "arguments",
        "result",
        "observed_at",
    )
    issues = [
        SemanticIssue(
            "semantic_tool_call_missing",
            _call_source_pointer(pointer, field, nested=nested),
            field,
        )
        for field in required
        if field not in value
    ]
    if issues:
        raise SemanticCandidateError(issues)
    origin = _capture_origin(value)
    if origin not in {
        "direct_connector_response",
        "host_transcribed_response",
        "host_summary",
    }:
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_capture_origin",
                _call_source_pointer(
                    pointer,
                    "capture_origin",
                    nested=nested,
                ),
                origin,
            ),
        ))
    web_sources = _web_sources(
        value.get("web_sources"),
        pointer=_call_source_pointer(
            pointer,
            "web_sources",
            nested=nested,
        ),
    )
    result_origin = (
        value.get("result_origin")
        if value.get("result_origin") is not None
        else "host_summary"
        if origin == "host_summary"
        else "connector_response"
    )
    result_origin = RESULT_ORIGIN_ALIASES.get(
        _text(result_origin),
        _text(result_origin),
    )
    if result_origin not in {"connector_response", "host_summary"}:
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_result_origin",
                _call_source_pointer(
                    pointer,
                    "result_origin",
                    nested=nested,
                ),
                _text(result_origin),
            ),
        ))
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
    pointer_map[f"{canonical_pointer}/call"] = (
        f"{pointer}/call" if nested else pointer
    )
    pointer_map[f"{canonical_pointer}/call/action"] = (
        _call_source_pointer(pointer, "action", nested=nested)
    )
    pointer_map[f"{canonical_pointer}/call/arguments"] = (
        _call_source_pointer(pointer, "arguments", nested=nested)
    )
    pointer_map[f"{canonical_pointer}/provenance"] = (
        f"{pointer}/provenance" if nested else pointer
    )
    pointer_map[f"{canonical_pointer}/provenance/capture"] = (
        f"{pointer}/provenance/capture" if nested else pointer
    )
    pointer_map[
        f"{canonical_pointer}/provenance/capture/capture_origin"
    ] = _call_source_pointer(
        pointer,
        "capture_origin",
        nested=nested,
    )
    pointer_map[f"{canonical_pointer}/provenance/web_sources"] = (
        _call_source_pointer(pointer, "web_sources", nested=nested)
    )
    return call


def _probe_call(value: Any, *, pointer: str) -> list[SemanticIssue]:
    if not isinstance(value, Mapping):
        return [SemanticIssue("semantic_tool_call_object", pointer)]
    nested = _nested_call_source(value)
    compact = _compact_call_values(value)
    issues = [
        SemanticIssue(
            "semantic_tool_call_missing",
            _call_source_pointer(pointer, field, nested=nested),
            field,
        )
        for field in (
            "tool_call_id",
            "kind",
            "tool",
            "action",
            "arguments",
            "result",
            "observed_at",
        )
        if field not in compact
    ]
    explicit_origin = compact.get("capture_origin")
    if explicit_origin is not None:
        origin = CAPTURE_ORIGIN_ALIASES.get(
            _text(explicit_origin),
            _text(explicit_origin),
        )
        if origin not in {
            "direct_connector_response",
            "host_transcribed_response",
            "host_summary",
        }:
            issues.append(SemanticIssue(
                "semantic_capture_origin",
                _call_source_pointer(
                    pointer,
                    "capture_origin",
                    nested=nested,
                ),
                origin,
            ))
    web_sources = compact.get("web_sources")
    web_pointer = _call_source_pointer(
        pointer,
        "web_sources",
        nested=nested,
    )
    if web_sources is not None and not isinstance(web_sources, list):
        issues.append(SemanticIssue(
            "semantic_web_sources_list",
            web_pointer,
        ))
    elif isinstance(web_sources, list):
        for index, item in enumerate(web_sources):
            item_pointer = f"{web_pointer}/{index}"
            if not isinstance(item, Mapping):
                issues.append(SemanticIssue(
                    "semantic_web_source_object",
                    item_pointer,
                ))
                continue
            excerpt = item.get("excerpt")
            if excerpt is not None and not isinstance(excerpt, str):
                issues.append(SemanticIssue(
                    "semantic_web_source_excerpt",
                    f"{item_pointer}/excerpt",
                ))
    return issues


def _probe_call_id_reuse(
    value: Any,
    *,
    pointer: str,
    observed: dict[str, tuple[dict[str, Any], str]],
) -> list[SemanticIssue]:
    """Expose contradictory call IDs before a malformed wrapper stops building."""
    if not isinstance(value, Mapping):
        return []
    compact = _compact_call_values(value)
    call_id = _text(compact.get("tool_call_id"))
    if not call_id:
        return []
    previous = observed.get(call_id)
    if previous is None:
        observed[call_id] = (compact, pointer)
        return []
    prior_call, prior_pointer = previous
    identity_fields = ("action", "arguments", "result", "observed_at")
    if not all(field in prior_call for field in identity_fields):
        if all(field in compact for field in identity_fields):
            observed[call_id] = (compact, pointer)
        return []
    if not all(field in compact for field in identity_fields):
        return []
    if any(
        field in compact
        and field in prior_call
        and not _canonical_equal(compact[field], prior_call[field])
        for field in (*identity_fields, "tool", "kind")
    ):
        return [SemanticIssue(
            "semantic_tool_call_id_conflict",
            _call_source_pointer(
                pointer, "tool_call_id",
                nested=_nested_call_source(value),
            ),
            prior_pointer,
        )]
    return []


def probe_semantic_candidate(
    value: Mapping[str, Any],
    *,
    filename: str,
    records: Sequence[Mapping[str, Any]] = (),
) -> list[SemanticIssue]:
    """Enumerate structural defects without synthesizing a candidate."""
    source = deepcopy(dict(value))
    issues: list[SemanticIssue] = []
    version = source.get("semantic_input_schema_version")
    if version is not None and version != SEMANTIC_INPUT_SCHEMA_VERSION:
        issues.append(SemanticIssue(
            "semantic_schema_version",
            "/semantic_input_schema_version",
        ))
    if "corrects_candidate_id" in source:
        corrects_candidate_id = source.get("corrects_candidate_id")
        if (
            corrects_candidate_id is not None
            and (
                not isinstance(corrects_candidate_id, str)
                or not corrects_candidate_id
                or corrects_candidate_id != corrects_candidate_id.strip()
            )
        ):
            issues.append(SemanticIssue(
                "semantic_corrects_candidate_id",
                "/corrects_candidate_id",
            ))
    if not _text(source.get("cycle_id")):
        try:
            target_name = canonical_target_name(filename)
        except SemanticCandidateError as error:
            issues.extend(error.issues)
        else:
            if not Path(target_name).stem.startswith("cycle-"):
                issues.append(SemanticIssue(
                    "semantic_top_level_missing",
                    "/cycle_id",
                    "cycle_id",
                ))

    declaration = source.get("unchanged_from_prior")
    carry_fields = (
        set(declaration)
        if isinstance(declaration, list)
        else set()
    )
    required = REQUIRED_TOP_LEVEL - {
        field for field in carry_fields
        if field in CARRY_FORWARD_LIMITS
    }
    issues.extend(
        SemanticIssue(
            "semantic_top_level_missing",
            f"/{field}",
            field,
        )
        for field in sorted(required - set(source))
    )

    observed_call_ids: dict[str, tuple[dict[str, Any], str]] = {}
    research = source.get("research")
    if isinstance(research, list):
        for research_index, row in enumerate(research):
            pointer = f"/research/{research_index}"
            if not isinstance(row, Mapping):
                issues.append(SemanticIssue(
                    "semantic_research_object",
                    pointer,
                ))
                continue
            calls = row.get("tool_calls")
            if not isinstance(calls, list):
                issues.append(SemanticIssue(
                    "semantic_tool_calls_list",
                    f"{pointer}/tool_calls",
                ))
                continue
            for call_index, call in enumerate(calls):
                call_pointer = f"{pointer}/tool_calls/{call_index}"
                issues.extend(_probe_call(call, pointer=call_pointer))
                issues.extend(_probe_call_id_reuse(
                    call, pointer=call_pointer, observed=observed_call_ids,
                ))

    scout = source.get("market_scout_report")
    if isinstance(scout, Mapping):
        calls = scout.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            issues.append(SemanticIssue(
                "semantic_tool_calls_list",
                "/market_scout_report/tool_calls",
            ))
        elif isinstance(calls, list):
            for call_index, call in enumerate(calls):
                call_pointer = f"/market_scout_report/tool_calls/{call_index}"
                issues.extend(_probe_call(call, pointer=call_pointer))
                issues.extend(_probe_call_id_reuse(
                    call, pointer=call_pointer, observed=observed_call_ids,
                ))

    evidence_calls = source.get("evidence_calls")
    if isinstance(evidence_calls, list):
        for index, wrapper in enumerate(evidence_calls):
            pointer = f"/evidence_calls/{index}"
            if not isinstance(wrapper, Mapping):
                issues.append(SemanticIssue(
                    "semantic_evidence_call_object",
                    pointer,
                ))
                continue
            producer = _text(wrapper.get("producer"))
            call_input = _evidence_call_input(
                wrapper,
                producer=producer,
            )
            call_pointer = (
                f"{pointer}/call" if "call" in wrapper else pointer
            )
            issues.extend(_probe_call(call_input, pointer=call_pointer))
            issues.extend(_probe_call_id_reuse(
                call_input,
                pointer=call_pointer,
                observed=observed_call_ids,
            ))
            compact = (
                _compact_call_values(call_input)
                if isinstance(call_input, Mapping)
                else {}
            )
            origin = _capture_origin(compact)
            if (
                "projection" not in wrapper
                and origin != "host_summary"
                and producer not in DERIVABLE_EVIDENCE_PRODUCERS
            ):
                issues.append(SemanticIssue(
                    "semantic_evidence_target_missing",
                    pointer,
                    producer,
                ))
            elif producer and producer not in DERIVABLE_EVIDENCE_PRODUCERS:
                # Host summaries skip projection, so an invented producer
                # otherwise surfaces only after the builder, one retry later.
                issues.append(SemanticIssue(
                    "semantic_evidence_producer_invalid",
                    f"{pointer}/producer",
                    producer,
                ))

    agenda = source.get("research_agenda")
    selected: list[str] = []
    selected_rows: list[tuple[int, str]] = []
    if isinstance(agenda, Mapping):
        for candidate_index, candidate in enumerate(
            agenda.get("candidates") or ()
        ):
            if (
                isinstance(candidate, Mapping)
                and candidate.get("selected") is True
                and _text(candidate.get("candidate_id"))
            ):
                candidate_id = _text(candidate.get("candidate_id"))
                selected.append(candidate_id)
                selected_rows.append((candidate_index, candidate_id))
        if not selected:
            issues.append(SemanticIssue(
                "semantic_selected_specialist_required",
                "/research_agenda/candidates",
            ))
    research_stage_ids = {
        _text(row.get("specialist_stage_id"))
        for row in source.get("research") or ()
        if (
            isinstance(row, Mapping)
            and _text(row.get("specialist_stage_id"))
        )
    }
    mismatched_selected = set()
    for candidate_index, candidate_id in selected_rows:
        if candidate_id in research_stage_ids:
            continue
        mismatched_selected.add(candidate_id)
        issues.append(SemanticIssue(
            "semantic_selected_specialist_mismatch",
            f"/research_agenda/candidates/{candidate_index}/candidate_id",
            (
                f"candidate_id={candidate_id};"
                "specialist_stage_ids="
                + "|".join(sorted(research_stage_ids))
            ),
        ))
    stage_outputs = source.get("stage_outputs")
    if isinstance(stage_outputs, Mapping):
        actual = sorted(str(key) for key in stage_outputs)
        for stage_id in [
            *CORE_STAGE_IDS,
            *(item for item in selected if item not in mismatched_selected),
        ]:
            pointer = f"/stage_outputs/{_pointer_token(stage_id)}"
            stage = stage_outputs.get(stage_id)
            if stage is None:
                issues.append(SemanticIssue(
                    "semantic_stage_output_missing",
                    pointer,
                    "actual_keys=" + "|".join(actual),
                ))
                continue
            if not isinstance(stage, Mapping):
                issues.append(SemanticIssue(
                    "semantic_stage_output_object",
                    pointer,
                ))
                continue
            for field in STAGE_OUTPUT_REQUIRED_FIELDS:
                if field not in stage:
                    issues.append(SemanticIssue(
                        "semantic_stage_field_missing",
                        f"{pointer}/{field}",
                        field,
                    ))
    elif stage_outputs is not None:
        issues.append(SemanticIssue(
            "semantic_stage_outputs_object",
            "/stage_outputs",
        ))

    dispositions = source.get("learning_stage_dispositions")
    if isinstance(dispositions, list):
        observed = {
            _text(row.get("stage_id"))
            for row in dispositions
            if isinstance(row, Mapping)
        }
        for stage_id in (
            "learning_audit",
            "meta_research",
            "self_improvement",
        ):
            if stage_id not in observed:
                issues.append(SemanticIssue(
                    "semantic_learning_disposition_missing",
                    "/learning_stage_dispositions",
                    stage_id,
                ))
    elif dispositions is not None:
        issues.append(SemanticIssue(
            "semantic_learning_dispositions_list",
            "/learning_stage_dispositions",
        ))

    unique = {
        (issue.code, issue.pointer, issue.detail): issue
        for issue in issues
    }
    return [
        unique[key] for key in sorted(
            unique,
            key=lambda item: (item[1], item[0], item[2]),
        )
    ]


def _evidence_call_input(
    wrapper: Mapping[str, Any],
    *,
    producer: str,
) -> Any:
    value = wrapper.get("call")
    if value is None:
        value = {
            key: deepcopy(item)
            for key, item in wrapper.items()
            if key not in {"producer", "projection", "market_region"}
        }
    if not isinstance(value, Mapping):
        return value
    result = deepcopy(dict(value))
    result.setdefault("kind", "connector_lookup")
    default_tool = EVIDENCE_TOOL_DEFAULTS.get(producer)
    if default_tool:
        result.setdefault("tool", default_tool)
    return result


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
    if value is None:
        raise SemanticCandidateError((
            SemanticIssue(
                "semantic_stage_output_missing",
                pointer,
                stage_id,
            ),
        ))
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
        for field in STAGE_OUTPUT_REQUIRED_FIELDS
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
        try:
            source_path = _source_path_for_target(
                result,
                target,
                target_path,
            )
        except SemanticCandidateError as error:
            if (
                len(error.issues) == 1
                and error.issues[0].code
                == "semantic_projection_source_ambiguous"
                and error.issues[0].detail == "matches=0"
            ):
                continue
            raise
        bindings.append({
            "source_path": source_path,
            "target_path": target_path,
        })
    if not bindings:
        return None
    return {"extractor": "json_pointer_v1", "bindings": bindings}


def _pointer_exists(value: Mapping[str, Any], path: str) -> bool:
    try:
        json_pointer_value(value, path)
    except (KeyError, ValueError):
        return False
    return True


def _finalized_cycle_ids(
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    return {
        _text((record.get("payload") or {}).get("cycle_id"))
        for record in records
        if record.get("record_type") == "cycle_finalization"
        and isinstance(record.get("payload"), Mapping)
        and _text((record.get("payload") or {}).get("cycle_id"))
    }


def _latest_finalized_stage_field(
    records: Sequence[Mapping[str, Any]],
    *,
    field: str,
) -> tuple[Any, str, int] | None:
    stage_id = CARRY_FORWARD_STAGE_IDS[field]
    finalized = _finalized_cycle_ids(records)
    for record in reversed(records):
        if record.get("record_type") != "cycle_stage":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        cycle_id = _text(payload.get("cycle_id"))
        if (
            cycle_id not in finalized
            or payload.get("agent_id") != stage_id
        ):
            continue
        output = payload.get("output")
        if not isinstance(output, Mapping) or field not in output:
            continue
        carried = output.get("carry_forward")
        carried = carried if isinstance(carried, Mapping) else {}
        metadata = carried.get(field)
        metadata = metadata if isinstance(metadata, Mapping) else {}
        count = metadata.get("count", 0)
        count = count if isinstance(count, int) else 0
        source_cycle_id = _text(
            metadata.get("source_cycle_id")
        ) or cycle_id
        return deepcopy(output[field]), source_cycle_id, count
    return None


def _latest_finalized_tool_inventory(
    records: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], str, int] | None:
    finalized = _finalized_cycle_ids(records)
    for record in reversed(records):
        if record.get("record_type") != "tool_inventory":
            continue
        caused_by = [
            _text(value) for value in record.get("caused_by") or ()
        ]
        cycle_id = next((
            value.removeprefix("cycle-receipt:")
            for value in caused_by
            if value.startswith("cycle-receipt:")
        ), "")
        if cycle_id not in finalized:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        carried = payload.get("carry_forward")
        carried = carried if isinstance(carried, Mapping) else {}
        count = carried.get("count", 0)
        count = count if isinstance(count, int) else 0
        source_cycle_id = _text(
            carried.get("source_cycle_id")
        ) or cycle_id
        return payload, source_cycle_id, count
    return None


def _tool_manifest(
    value: Any,
    records: Sequence[Mapping[str, Any]],
) -> tuple[Any, dict[str, Any] | None]:
    latest_row = _latest_finalized_tool_inventory(records)
    latest = (
        latest_row[0]
        if latest_row is not None
        else next((
            record.get("payload")
            for record in reversed(records)
            if record.get("record_type") == "tool_inventory"
            and isinstance(record.get("payload"), Mapping)
        ), None)
    )
    if value is None:
        if latest_row is None:
            return None, None
        _payload, source_cycle_id, prior_count = latest_row
        count = prior_count + 1
        if count > CARRY_FORWARD_LIMITS["tool_manifest_report"]:
            raise SemanticCandidateError((
                SemanticIssue(
                    "carry_forward_exhausted",
                    "/tool_manifest_report",
                    "tool_manifest_report",
                ),
            ))
        result = deepcopy(dict(latest))
        result.pop("changes", None)
        result["stale"] = True
        result["carry_forward"] = {
            "source_cycle_id": source_cycle_id,
            "count": count,
        }
        return result, {
            "source_cycle_id": source_cycle_id,
            "count": count,
        }
    if not isinstance(value, Mapping):
        return deepcopy(value), None
    connectors = value.get("connectors")
    if not isinstance(connectors, list):
        return deepcopy(value), None
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
    result.pop("carry_forward", None)
    result.pop("stale", None)
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
    return result, None


def _carry_forward_fields(
    value: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    declaration = value.get("unchanged_from_prior")
    if declaration is None:
        fields: list[Any] = []
    elif isinstance(declaration, list):
        fields = declaration
    else:
        raise SemanticCandidateError((
            SemanticIssue(
                "carry_forward_fields_must_be_list",
                "/unchanged_from_prior",
            ),
        ))
    invalid = sorted({
        str(field)
        for field in fields
        if field not in CARRY_FORWARD_LIMITS
    })
    if invalid:
        raise SemanticCandidateError(tuple(
            SemanticIssue(
                "carry_forward_forbidden",
                "/unchanged_from_prior",
                field,
            )
            for field in invalid
        ))
    metadata: dict[str, dict[str, Any]] = {}
    for field in fields:
        if field == "tool_manifest_report" or field in value:
            continue
        prior = _latest_finalized_stage_field(
            records,
            field=field,
        )
        if prior is None:
            raise SemanticCandidateError((
                SemanticIssue(
                    "carry_forward_missing_prior",
                    f"/{field}",
                    field,
                ),
            ))
        prior_value, source_cycle_id, prior_count = prior
        count = prior_count + 1
        if count > CARRY_FORWARD_LIMITS[field]:
            raise SemanticCandidateError((
                SemanticIssue(
                    "carry_forward_exhausted",
                    f"/{field}",
                    field,
                ),
            ))
        value[field] = prior_value
        metadata[field] = {
            "source_cycle_id": source_cycle_id,
            "count": count,
        }
    return metadata


def _carry_forward_guard_issues(
    value: Mapping[str, Any],
    carried: Mapping[str, Any],
) -> list[SemanticIssue]:
    if not carried:
        return []
    issues = []
    if value.get("forecast_registrations"):
        issues.append(SemanticIssue(
            "carry_forward_forecast_forbidden",
            "/forecast_registrations",
        ))
    for index, row in enumerate(
        value.get("order_instruction_activity") or ()
    ):
        if (
            isinstance(row, Mapping)
            and _text(row.get("operation")).lower()
            in {"create", "delete"}
        ):
            issues.append(SemanticIssue(
                "carry_forward_instruction_mutation_forbidden",
                f"/order_instruction_activity/{index}/operation",
            ))
    return issues


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
    value = deepcopy(dict(value))
    if value.get("semantic_input_schema_version") is None:
        value["semantic_input_schema_version"] = (
            SEMANTIC_INPUT_SCHEMA_VERSION
        )
    value.pop("host_input_schema_version", None)
    if not _text(value.get("cycle_id")):
        target_name = canonical_target_name(filename)
        derived_cycle_id = Path(target_name).stem
        if derived_cycle_id.startswith("cycle-"):
            value["cycle_id"] = derived_cycle_id
    carry_forward = _carry_forward_fields(value, records)
    issues = []
    if value.get("semantic_input_schema_version") != (
        SEMANTIC_INPUT_SCHEMA_VERSION
    ):
        issues.append(SemanticIssue(
            "semantic_schema_version",
            "/semantic_input_schema_version",
        ))
    if "corrects_candidate_id" in value:
        corrects_candidate_id = value.get("corrects_candidate_id")
        if (
            corrects_candidate_id is not None
            and (
                not isinstance(corrects_candidate_id, str)
                or not corrects_candidate_id
                or corrects_candidate_id != corrects_candidate_id.strip()
            )
        ):
            issues.append(SemanticIssue(
                "semantic_corrects_candidate_id",
                "/corrects_candidate_id",
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
    if "corrects_candidate_id" in value:
        canonical["corrects_candidate_id"] = value.get(
            "corrects_candidate_id"
        )
    canonical["host_input_schema_version"] = 4
    canonical["evidence_coverage_schema_version"] = 1
    (
        canonical["tool_manifest_report"],
        tool_manifest_carry,
    ) = _tool_manifest(
        canonical.get("tool_manifest_report"),
        records,
    )
    if tool_manifest_carry is not None:
        carry_forward["tool_manifest_report"] = tool_manifest_carry
    issues = _carry_forward_guard_issues(value, carry_forward)
    if issues:
        raise SemanticCandidateError(issues)
    if carry_forward:
        canonical["carry_forward"] = {
            "fields": deepcopy(carry_forward),
        }

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
        call_pointer = (
            f"{pointer}/call"
            if "call" in wrapper
            else pointer
        )
        call = _canonical_call(
            _evidence_call_input(
                wrapper,
                producer=producer,
            ),
            pointer=call_pointer,
            pointer_map=pointer_map,
            canonical_pointer=f"/evidence_calls/{index}/call",
        )
        origin = call["provenance"]["capture"]["capture_origin"]
        projection = (
            deepcopy(wrapper.get("projection"))
            if "projection" in wrapper
            else None
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
            {
                "market_scout_report": scout_report,
                **(
                    {"carry_forward": {
                        "market_scout_report":
                        carry_forward["market_scout_report"],
                    }}
                    if "market_scout_report" in carry_forward
                    else {}
                ),
            }
            if stage_id == "market_scout"
            else {
                "research_agenda": agenda,
                **(
                    {"carry_forward": {
                        "research_agenda":
                        carry_forward["research_agenda"],
                    }}
                    if "research_agenda" in carry_forward
                    else {}
                ),
            }
            if stage_id == "research_director"
            else {
                "decision_status": canonical["decision"].get("status"),
                "rationale": canonical["decision"].get("rationale"),
                **(
                    {
                        "repetition_review":
                        canonical["decision"]["repetition_review"],
                    }
                    if "repetition_review" in canonical["decision"]
                    else {}
                ),
                **(
                    {
                        "forecast_assessment":
                        canonical["decision"]["forecast_assessment"],
                    }
                    if "forecast_assessment" in canonical["decision"]
                    else {}
                ),
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
            if "repetition_review" in canonical["decision"]:
                pointer_map[
                    f"{output_pointer}/repetition_review"
                ] = "/decision/repetition_review"
            if "forecast_assessment" in canonical["decision"]:
                pointer_map[
                    f"{output_pointer}/forecast_assessment"
                ] = "/decision/forecast_assessment"
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
