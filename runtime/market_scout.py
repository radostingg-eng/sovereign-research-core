"""Host-authored market discovery with deterministic evidence accounting."""

from __future__ import annotations

from datetime import datetime
import re
from collections import Counter
from typing import Any, Mapping, Sequence

from .opportunity_ledger import (
    identity_fingerprint,
    normalize_identity,
    opportunity_ledger_summary,
)
from .timestamps import effective_as_of
from .tool_provenance import validate_tool_call_provenance

MARKET_SCOUT_STAGE_ID = "market_scout"
MARKET_SCOUT_REPORT_KEY = "market_scout_report"
MARKET_SCOUT_BUDGET_FIELDS = (
    "specialist_investigations",
    "external_searches",
    "deep_dives",
    "opportunity_updates",
)
MARKET_SCOUT_TOOL_KINDS = frozenset({
    "connector_lookup",
    "external_search",
    "deep_dive",
})
MAX_SCOUT_EVIDENCE_REFS = 12
MAX_SCOUT_FEEDBACK_ITEMS = 12
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _stage_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = data.get("cognitive_stages")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def market_scout_stage(
    data: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    return next((
        row for row in _stage_rows(data)
        if _text(row.get("stage_id")) == MARKET_SCOUT_STAGE_ID
    ), None)


def market_scout_report(
    data: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    stage = market_scout_stage(data)
    if not isinstance(stage, Mapping):
        return None
    output = stage.get("output")
    if not isinstance(output, Mapping):
        return None
    report = output.get(MARKET_SCOUT_REPORT_KEY)
    return report if isinstance(report, Mapping) else None


def _selected_specialist_count(data: Mapping[str, Any]) -> int:
    director = next((
        row for row in _stage_rows(data)
        if _text(row.get("stage_id")) == "research_director"
    ), None)
    if not isinstance(director, Mapping):
        return 0
    output = director.get("output")
    output = output if isinstance(output, Mapping) else {}
    agenda = output.get("research_agenda")
    agenda = agenda if isinstance(agenda, Mapping) else {}
    candidates = agenda.get("candidates")
    if not isinstance(candidates, list):
        return 0
    return sum(
        1 for row in candidates
        if isinstance(row, Mapping) and row.get("selected") is True
    )


def derived_market_scout_usage(
    data: Mapping[str, Any],
) -> dict[str, int]:
    """Derive budget use from committed work instead of host totals."""
    report = market_scout_report(data) or {}
    calls = report.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    kinds = Counter(
        _text(call.get("kind"))
        for call in calls
        if isinstance(call, Mapping)
    )
    updates = data.get("opportunity_updates")
    updates = updates if isinstance(updates, list) else []
    opportunity_ids = {
        _text(row.get("opportunity_id"))
        for row in updates
        if isinstance(row, Mapping) and _text(row.get("opportunity_id"))
    }
    return {
        "specialist_investigations": _selected_specialist_count(data),
        "external_searches": kinds["external_search"],
        "deep_dives": kinds["deep_dive"],
        "opportunity_updates": len(opportunity_ids),
    }


def _validate_scope(scope: Any) -> list[str]:
    if not isinstance(scope, Mapping):
        return ["market_scout_report_invalid:scope:not_object"]
    errors = []
    if set(scope) != {"description", "limitations"}:
        errors.append("market_scout_report_invalid:scope:fields")
    description = scope.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("market_scout_report_invalid:scope:description")
    limitations = scope.get("limitations")
    if not isinstance(limitations, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in limitations
    ):
        errors.append("market_scout_report_invalid:scope:limitations")
    return errors


def _validate_budget(budget: Any) -> list[str]:
    if not isinstance(budget, Mapping):
        return ["market_scout_budget_invalid:not_object"]
    errors = []
    expected = set(MARKET_SCOUT_BUDGET_FIELDS) | {"rationale"}
    if set(budget) != expected:
        errors.append("market_scout_budget_invalid:fields")
    for field in MARKET_SCOUT_BUDGET_FIELDS:
        value = budget.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"market_scout_budget_invalid:{field}")
    rationale = budget.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("market_scout_budget_invalid:rationale")
    return errors


def _validate_tool_calls(
    calls: Any,
    *,
    cycle_as_of: Any,
    schema_version: int | None,
    validation_now: datetime | None,
) -> tuple[list[str], set[str]]:
    if not isinstance(calls, list) or not calls:
        return ["market_scout_tool_calls_invalid:empty"], set()
    errors = []
    call_ids: set[str] = set()
    expected = {
        "tool_call_id",
        "kind",
        "tool",
        "call",
        "result",
        "provenance",
    }
    for index, call in enumerate(calls):
        if not isinstance(call, Mapping):
            errors.append(f"market_scout_tool_call_invalid:{index}:object")
            continue
        if set(call) != expected:
            errors.append(f"market_scout_tool_call_invalid:{index}:fields")
        call_id = _text(call.get("tool_call_id"))
        if not _SAFE_ID.fullmatch(call_id):
            errors.append(f"market_scout_tool_call_invalid:{index}:id")
        elif call_id in call_ids:
            errors.append(f"market_scout_tool_call_invalid:{index}:duplicate")
        call_ids.add(call_id)
        if call.get("kind") not in MARKET_SCOUT_TOOL_KINDS:
            errors.append(f"market_scout_tool_call_invalid:{index}:kind")
        if not _text(call.get("tool")):
            errors.append(f"market_scout_tool_call_invalid:{index}:tool")
        if "call" not in call:
            errors.append(f"market_scout_tool_call_invalid:{index}:call")
        if "result" not in call:
            errors.append(f"market_scout_tool_call_invalid:{index}:result")
        for problem in validate_tool_call_provenance(
            call,
            cycle_as_of=cycle_as_of,
            schema_version=schema_version,
            validation_now=validation_now,
        ):
            errors.append(
                f"market_scout_tool_provenance_invalid:{index}:{problem}"
            )
    return errors, call_ids


def _validate_candidates(
    candidates: Any,
    *,
    call_ids: set[str],
) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not isinstance(candidates, list):
        return ["market_scout_candidates_invalid:not_list"], {}
    errors = []
    candidate_ids: set[str] = set()
    candidate_identities: dict[str, dict[str, str]] = {}
    identity_owners: dict[str, str] = {}
    expected = {
        "candidate_id",
        "identity",
        "trigger",
        "rationale",
        "evidence_tool_call_ids",
    }
    # rediscovery_of is optional: present only when this exact identity
    # matches an unpromoted candidate from a prior receipted cycle. Its
    # presence/absence is validated separately in candidate_registry.py,
    # against the durable registry rather than this cycle in isolation.
    allowed = expected | {"rediscovery_of"}
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            errors.append(f"market_scout_candidate_invalid:{index}:object")
            continue
        present = set(candidate)
        if not expected.issubset(present) or not present.issubset(allowed):
            errors.append(f"market_scout_candidate_invalid:{index}:fields")
        candidate_id = _text(candidate.get("candidate_id"))
        if not _SAFE_ID.fullmatch(candidate_id):
            errors.append(f"market_scout_candidate_invalid:{index}:id")
        elif candidate_id in candidate_ids:
            errors.append(
                f"market_scout_candidate_invalid:{index}:duplicate_id"
            )
        candidate_ids.add(candidate_id)
        identity = candidate.get("identity")
        if not isinstance(identity, Mapping):
            errors.append(
                f"market_scout_candidate_invalid:{index}:identity"
            )
        else:
            try:
                normalized = normalize_identity(identity)
            except ValueError as exc:
                errors.append(
                    "market_scout_candidate_invalid:"
                    f"{index}:identity:{exc}"
                )
            else:
                fingerprint = identity_fingerprint(identity)
                owner = identity_owners.get(fingerprint)
                if owner is not None and owner != candidate_id:
                    errors.append(
                        "market_scout_candidate_invalid:"
                        f"{index}:duplicate_identity:{owner}"
                    )
                identity_owners[fingerprint] = candidate_id
                if _SAFE_ID.fullmatch(candidate_id):
                    candidate_identities[candidate_id] = normalized
        for field in ("trigger", "rationale"):
            value = candidate.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"market_scout_candidate_invalid:{index}:{field}"
                )
        evidence = candidate.get("evidence_tool_call_ids")
        if not isinstance(evidence, list) or not evidence:
            errors.append(
                f"market_scout_candidate_evidence_invalid:{index}:empty"
            )
            continue
        if len(evidence) > MAX_SCOUT_EVIDENCE_REFS:
            errors.append(
                f"market_scout_candidate_evidence_invalid:{index}:count"
            )
        normalized_evidence = [_text(value) for value in evidence]
        if (
            any(not value for value in normalized_evidence)
            or len(normalized_evidence) != len(set(normalized_evidence))
        ):
            errors.append(
                f"market_scout_candidate_evidence_invalid:{index}:values"
            )
        for ref in normalized_evidence:
            if ref not in call_ids:
                errors.append(
                    "market_scout_candidate_evidence_invalid:"
                    f"{index}:dangling:{ref}"
                )
    return errors, candidate_identities


def _validate_budget_variance(
    variance: Any,
    *,
    budget: Mapping[str, Any],
    usage: Mapping[str, int],
) -> list[str]:
    exceeded = sorted(
        field for field in MARKET_SCOUT_BUDGET_FIELDS
        if isinstance(budget.get(field), int)
        and usage[field] > int(budget[field])
    )
    if not exceeded:
        if variance is not None:
            return ["market_scout_budget_variance_invalid:unexpected"]
        return []
    if not isinstance(variance, Mapping):
        return ["market_scout_budget_variance_invalid:required"]
    errors = []
    if set(variance) != {"exceeded", "rationale"}:
        errors.append("market_scout_budget_variance_invalid:fields")
    declared = variance.get("exceeded")
    if not isinstance(declared, list) or sorted(map(_text, declared)) != exceeded:
        errors.append("market_scout_budget_variance_invalid:exceeded")
    rationale = variance.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("market_scout_budget_variance_invalid:rationale")
    return errors


def validate_market_scout(
    data: Mapping[str, Any],
    *,
    required: bool,
    validation_now: datetime | None = None,
) -> list[str]:
    """Validate Market Scout structure, provenance, links, and accounting."""
    stage = market_scout_stage(data)
    if stage is None:
        return ["market_scout_required"] if required else []
    errors = []
    if stage.get("phase") != "discovery":
        errors.append("market_scout_stage_invalid:phase")
    if stage.get("depends_on") != ["portfolio"]:
        errors.append("market_scout_stage_invalid:depends_on")
    if stage.get("required") is not True:
        errors.append("market_scout_stage_invalid:required")
    if stage.get("status") != "completed":
        errors.append("market_scout_stage_invalid:status")

    director = next((
        row for row in _stage_rows(data)
        if _text(row.get("stage_id")) == "research_director"
    ), None)
    if (
        not isinstance(director, Mapping)
        or director.get("depends_on") != [MARKET_SCOUT_STAGE_ID]
    ):
        errors.append("market_scout_stage_invalid:director_dependency")

    report = market_scout_report(data)
    if report is None:
        return errors + ["market_scout_report_invalid:missing"]
    expected = {
        "scope",
        "budget",
        "tool_calls",
        "candidates",
        "budget_variance",
    }
    if set(report) != expected:
        errors.append("market_scout_report_invalid:fields")
    errors.extend(_validate_scope(report.get("scope")))
    errors.extend(_validate_budget(report.get("budget")))
    call_errors, call_ids = _validate_tool_calls(
        report.get("tool_calls"),
        cycle_as_of=effective_as_of(data),
        schema_version=data.get("host_input_schema_version"),
        validation_now=validation_now,
    )
    errors.extend(call_errors)
    candidate_errors, candidate_identities = _validate_candidates(
        report.get("candidates"),
        call_ids=call_ids,
    )
    errors.extend(candidate_errors)

    budget = report.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}
    usage = derived_market_scout_usage(data)
    errors.extend(_validate_budget_variance(
        report.get("budget_variance"),
        budget=budget,
        usage=usage,
    ))

    output = director.get("output") if isinstance(director, Mapping) else {}
    output = output if isinstance(output, Mapping) else {}
    agenda = output.get("research_agenda")
    agenda = agenda if isinstance(agenda, Mapping) else {}
    agenda_candidates = agenda.get("candidates")
    if isinstance(agenda_candidates, list):
        for index, candidate in enumerate(agenda_candidates):
            if not isinstance(candidate, Mapping):
                continue
            scout_id = candidate.get("scout_candidate_id")
            if scout_id is None:
                continue
            if (
                not isinstance(scout_id, str)
                or scout_id not in candidate_identities
            ):
                errors.append(
                    f"market_scout_agenda_link_invalid:{index}:unknown"
                )
                continue
            identity = candidate_identities[scout_id]
            if _text(candidate.get("instrument")).casefold() != (
                identity["instrument"]
            ):
                errors.append(
                    f"market_scout_agenda_link_invalid:{index}:instrument"
                )
            if _text(candidate.get("strategy_family")).casefold() != (
                identity["strategy_family"]
            ):
                errors.append(
                    "market_scout_agenda_link_invalid:"
                    f"{index}:strategy_family"
                )
    return sorted(set(errors))


def _bounded(value: Any, limit: int = 280) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[:limit]


def market_scout_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project the latest durable scout pass without ranking candidates."""
    stages = [
        row for row in records
        if row.get("record_type") == "cycle_stage"
        and isinstance(row.get("payload"), Mapping)
        and row["payload"].get("agent_id") == MARKET_SCOUT_STAGE_ID
    ]
    if not stages:
        return {
            "available": False,
            "what_this_means": (
                "No durable Market Scout stage exists yet."
            ),
        }
    latest = stages[-1]
    payload = latest["payload"]
    output = payload.get("output")
    output = output if isinstance(output, Mapping) else {}
    report = output.get(MARKET_SCOUT_REPORT_KEY)
    report = report if isinstance(report, Mapping) else {}
    calls = report.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    candidates = report.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    cycle_id = _text(payload.get("cycle_id"))

    cycle_stages = [
        row for row in records
        if row.get("record_type") == "cycle_stage"
        and isinstance(row.get("payload"), Mapping)
        and _text(row["payload"].get("cycle_id")) == cycle_id
    ]
    opportunity_events = [
        row for row in records
        if row.get("record_type") == "opportunity_event"
        and isinstance(row.get("payload"), Mapping)
        and _text(row["payload"].get("cycle_id")) == cycle_id
    ]
    kinds = Counter(
        _text(call.get("kind"))
        for call in calls
        if isinstance(call, Mapping)
    )
    director = next((
        row["payload"] for row in cycle_stages
        if row["payload"].get("agent_id") == "research_director"
    ), {})
    director_output = director.get("output")
    director_output = (
        director_output if isinstance(director_output, Mapping) else {}
    )
    agenda = director_output.get("research_agenda")
    agenda = agenda if isinstance(agenda, Mapping) else {}
    agenda_candidates = agenda.get("candidates")
    agenda_candidates = (
        agenda_candidates if isinstance(agenda_candidates, list) else []
    )
    usage = {
        "specialist_investigations": sum(
            1 for row in agenda_candidates
            if isinstance(row, Mapping) and row.get("selected") is True
        ),
        "external_searches": kinds["external_search"],
        "deep_dives": kinds["deep_dive"],
        "opportunity_updates": len({
            _text(row["payload"].get("opportunity_id"))
            for row in opportunity_events
            if _text(row["payload"].get("opportunity_id"))
        }),
    }
    ledger = opportunity_ledger_summary(
        records,
        limit=max(1, len(records)),
    )
    ledger_owners: dict[str, list[str]] = {}
    for item in ledger.get("items") or ():
        if not isinstance(item, Mapping):
            continue
        fingerprint = _text(item.get("identity_fingerprint"))
        opportunity_id = _text(item.get("opportunity_id"))
        if fingerprint and opportunity_id:
            ledger_owners.setdefault(fingerprint, []).append(opportunity_id)

    projected = []
    for candidate in candidates[:MAX_SCOUT_FEEDBACK_ITEMS]:
        if not isinstance(candidate, Mapping):
            continue
        identity = normalize_identity(candidate.get("identity"))
        fingerprint = identity_fingerprint(identity)
        projected.append({
            "candidate_id": candidate.get("candidate_id"),
            "identity": identity,
            "identity_fingerprint": fingerprint,
            "trigger": _bounded(candidate.get("trigger")),
            "rationale": _bounded(candidate.get("rationale")),
            "evidence_tool_call_ids": list(
                candidate.get("evidence_tool_call_ids") or ()
            ),
            "matching_opportunity_ids": sorted(
                ledger_owners.get(fingerprint, [])
            ),
        })
    return {
        "available": True,
        "cycle_id": cycle_id,
        "scope": report.get("scope"),
        "budget": report.get("budget"),
        "usage": usage,
        "budget_variance": report.get("budget_variance"),
        "candidate_count": len(candidates),
        "candidates": projected,
        "not_shown": max(0, len(candidates) - len(projected)),
        "what_this_means": (
            "Latest host-authored discovery pass and mechanically derived "
            "budget use. Candidate order is not an investment ranking."
        ),
    }
