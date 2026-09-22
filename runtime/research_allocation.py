"""Research allocation accounting without deterministic investment ranking."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any, Mapping, Sequence

from .market_scout import market_scout_report
from .opportunity_ledger import (
    TERMINAL_STATES,
    opportunity_ledger_summary,
)
from .timestamps import effective_as_of, parse_iso_timestamp

ALLOCATION_CATEGORIES = (
    "new_opportunity",
    "existing_opportunity",
    "portfolio_risk",
    "follow_up",
)
ALLOCATION_FACTOR_FIELDS = (
    "novelty",
    "portfolio_impact",
    "missing_information",
    "expected_information_gain",
)
MAX_ALLOCATION_TEXT_CHARS = 600
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def research_agenda(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    stages = data.get("cognitive_stages")
    if not isinstance(stages, list):
        return None
    for stage in stages:
        if (
            not isinstance(stage, Mapping)
            or _text(stage.get("stage_id")) != "research_director"
        ):
            continue
        output = stage.get("output")
        if not isinstance(output, Mapping):
            return None
        agenda = output.get("research_agenda")
        return agenda if isinstance(agenda, Mapping) else None
    return None


def primary_allocation_category(candidate: Mapping[str, Any]) -> str:
    """Derive one accounting bucket using explicit-link precedence."""
    if _text(candidate.get("follow_up_ref")):
        return "follow_up"
    if _text(candidate.get("portfolio_risk_ref")):
        return "portfolio_risk"
    if _text(candidate.get("opportunity_id")):
        return "existing_opportunity"
    return "new_opportunity"


def primary_allocation_reference(candidate: Mapping[str, Any]) -> str:
    category = primary_allocation_category(candidate)
    if category == "follow_up":
        return _text(candidate.get("follow_up_ref"))
    if category == "portfolio_risk":
        return _text(candidate.get("portfolio_risk_ref"))
    if category == "existing_opportunity":
        return _text(candidate.get("opportunity_id"))
    return (
        _text(candidate.get("scout_candidate_id"))
        or _text(candidate.get("candidate_id"))
    )


def _agenda_candidates(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    agenda = research_agenda(data)
    candidates = agenda.get("candidates") if agenda else None
    if not isinstance(candidates, list):
        return []
    return [row for row in candidates if isinstance(row, Mapping)]


def _open_question_pairs(
    records: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()

    def collect(
        opportunity_id: Any,
        state: Any,
        research_state: Any,
    ) -> None:
        opportunity = _text(opportunity_id)
        if (
            not opportunity
            or _text(state) in TERMINAL_STATES
            or not isinstance(research_state, Mapping)
        ):
            return
        rows = research_state.get("missing_information")
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            question_id = _text(row.get("id"))
            if question_id and _text(row.get("status")).lower() == "open":
                pairs.add((opportunity, question_id))

    summary = opportunity_ledger_summary(records, limit=10**9)
    for item in summary.get("items") or ():
        if not isinstance(item, Mapping):
            continue
        collect(
            item.get("opportunity_id"),
            item.get("state"),
            item.get("research_state"),
        )
    return pairs


def _committed_question_pairs(
    records: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    pairs = set()
    summary = opportunity_ledger_summary(records, limit=10**9)
    for item in summary.get("items") or ():
        if (
            not isinstance(item, Mapping)
            or _text(item.get("state")) in TERMINAL_STATES
        ):
            continue
        opportunity_id = _text(item.get("opportunity_id"))
        research_state = item.get("research_state")
        if not opportunity_id or not isinstance(research_state, Mapping):
            continue
        next_question_id = _text(research_state.get("next_question_id"))
        open_ids = {
            _text(row.get("id"))
            for row in research_state.get("missing_information") or ()
            if (
                isinstance(row, Mapping)
                and _text(row.get("status")).lower() == "open"
            )
        }
        if next_question_id and next_question_id in open_ids:
            pairs.add((opportunity_id, next_question_id))
    return pairs


def _portfolio_risk_references(data: Mapping[str, Any]) -> set[str]:
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    references: set[str] = set()
    if snapshot:
        references.add("portfolio:account")
    for field in ("cash", "total_cash_value", "balances"):
        if field in snapshot:
            references.add("portfolio:cash")
    for field, reference in (
        ("positions", "portfolio:positions"),
        ("open_orders", "portfolio:open_orders"),
        ("order_instructions", "portfolio:instructions"),
    ):
        if field in snapshot:
            references.add(reference)
    positions = snapshot.get("positions")
    positions = positions if isinstance(positions, list) else []
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        for field in (
            "symbol",
            "contract_id_ex",
            "contract_id",
            "conid",
            "contractId",
        ):
            value = _text(position.get(field))
            if value:
                references.add(f"position:{value}".casefold())
    return {reference.casefold() for reference in references}


def _follow_up_references(
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    references: set[str] = set()
    instructions = data.get("order_instructions")
    instructions = instructions if isinstance(instructions, list) else []
    for instruction in instructions:
        if not isinstance(instruction, Mapping):
            continue
        instruction_id = _text(
            instruction.get("id")
            or instruction.get("instruction_id")
            or instruction.get("order_instruction_id")
        )
        if instruction_id:
            references.add(f"instruction:{instruction_id}")

    updates = data.get("opportunity_updates")
    updates = updates if isinstance(updates, list) else []
    for update in updates:
        if not isinstance(update, Mapping):
            continue
        opportunity_id = _text(update.get("opportunity_id"))
        if opportunity_id:
            references.add(f"opportunity:{opportunity_id}")

    for record in records:
        if not isinstance(record, Mapping):
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if record.get("record_type") == "opportunity_event":
            opportunity_id = _text(payload.get("opportunity_id"))
            if opportunity_id:
                references.add(f"opportunity:{opportunity_id}")
        if (
            record.get("record_type") == "cycle_stage"
            and payload.get("agent_id") == "research_director"
        ):
            output = payload.get("output")
            output = output if isinstance(output, Mapping) else {}
            agenda = output.get("research_agenda")
            agenda = agenda if isinstance(agenda, Mapping) else {}
            candidates = agenda.get("candidates")
            candidates = candidates if isinstance(candidates, list) else []
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                candidate_id = _text(candidate.get("candidate_id"))
                if candidate_id:
                    references.add(f"candidate:{candidate_id}")
    return {reference.casefold() for reference in references}


def _follow_up_reference_times(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, datetime]:
    observed: dict[str, datetime] = {}

    def remember(reference: str, value: Any) -> None:
        timestamp = parse_iso_timestamp(value)
        if (
            not reference
            or timestamp is None
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            return
        key = reference.casefold()
        previous = observed.get(key)
        if previous is None or timestamp > previous:
            observed[key] = timestamp

    for record in records:
        if not isinstance(record, Mapping):
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        timestamp = (
            record.get("created_at")
            or payload.get("completed_at")
            or payload.get("observed_at")
        )
        if record.get("record_type") == "opportunity_event":
            opportunity_id = _text(payload.get("opportunity_id"))
            if opportunity_id:
                remember(f"opportunity:{opportunity_id}", timestamp)
        if (
            record.get("record_type") == "cycle_stage"
            and payload.get("agent_id") == "research_director"
        ):
            output = payload.get("output")
            output = output if isinstance(output, Mapping) else {}
            agenda = output.get("research_agenda")
            agenda = agenda if isinstance(agenda, Mapping) else {}
            candidates = agenda.get("candidates")
            candidates = candidates if isinstance(candidates, list) else []
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                candidate_id = _text(candidate.get("candidate_id"))
                if candidate_id:
                    remember(f"candidate:{candidate_id}", timestamp)
    return observed


def derived_research_allocation_usage(
    data: Mapping[str, Any],
) -> dict[str, int]:
    """Count selected specialist work by its mechanically derived purpose."""
    counts = Counter(
        primary_allocation_category(candidate)
        for candidate in _agenda_candidates(data)
        if candidate.get("selected") is True
    )
    return {category: counts[category] for category in ALLOCATION_CATEGORIES}


def derived_research_allocation_coverage(
    data: Mapping[str, Any],
) -> dict[str, int]:
    """Count all considered candidates by their primary accounting purpose."""
    counts = Counter(
        primary_allocation_category(candidate)
        for candidate in _agenda_candidates(data)
    )
    return {category: counts[category] for category in ALLOCATION_CATEGORIES}


def _validate_plan(
    plan: Any,
    *,
    data: Mapping[str, Any],
) -> list[str]:
    if not isinstance(plan, Mapping):
        return ["research_allocation_plan_invalid:not_object"]
    expected = {
        *ALLOCATION_CATEGORIES,
        "market_session_context",
        "rationale",
    }
    errors = []
    if set(plan) != expected:
        errors.append("research_allocation_plan_invalid:fields")
    for category in ALLOCATION_CATEGORIES:
        value = plan.get(category)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(
                f"research_allocation_plan_invalid:{category}"
            )
    sessions = data.get("market_sessions")
    sessions = sessions if isinstance(sessions, Mapping) else {}
    if plan.get("market_session_context") != sessions.get("overlap"):
        errors.append(
            "research_allocation_plan_invalid:market_session_context"
        )
    rationale = _text(plan.get("rationale"))
    if not rationale or len(rationale) > MAX_ALLOCATION_TEXT_CHARS:
        errors.append("research_allocation_plan_invalid:rationale")

    report = market_scout_report(data)
    budget = report.get("budget") if report else None
    budget = budget if isinstance(budget, Mapping) else {}
    specialist_budget = budget.get("specialist_investigations")
    planned = [
        plan.get(category)
        for category in ALLOCATION_CATEGORIES
    ]
    if (
        isinstance(specialist_budget, int)
        and not isinstance(specialist_budget, bool)
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in planned
        )
        and sum(planned) > specialist_budget
    ):
        errors.append(
            "research_allocation_plan_invalid:"
            "exceeds_specialist_investigations"
        )
    return errors


def _validate_candidates(
    data: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
    enforce_reference_chronology: bool,
) -> list[str]:
    errors = []
    selected_references: set[str] = set()
    open_questions = _open_question_pairs(records)
    committed_questions = _committed_question_pairs(records)
    considered_open_questions: set[tuple[str, str]] = set()
    portfolio_references = _portfolio_risk_references(data)
    follow_up_references = _follow_up_references(data, records)
    follow_up_times = _follow_up_reference_times(records)
    current_as_of = parse_iso_timestamp(effective_as_of(data))
    for index, candidate in enumerate(_agenda_candidates(data)):
        opportunity_id = _text(candidate.get("opportunity_id"))
        target_question_id = _text(
            candidate.get("target_missing_information_id")
        )
        if target_question_id and not opportunity_id:
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:target_missing_information_requires_opportunity_id"
            )
        elif target_question_id:
            pair = (opportunity_id, target_question_id)
            if pair not in open_questions:
                errors.append(
                    "research_allocation_candidate_invalid:"
                    f"{index}:target_missing_information_unresolved"
                )
            else:
                considered_open_questions.add(pair)
        for field in ("portfolio_risk_ref", "follow_up_ref"):
            value = candidate.get(field)
            if value is not None and (
                not _text(value) or _SAFE_ID.fullmatch(_text(value)) is None
            ):
                errors.append(
                    f"research_allocation_candidate_invalid:{index}:{field}"
                )
        portfolio_risk_ref = _text(candidate.get("portfolio_risk_ref"))
        if (
            portfolio_risk_ref
            and portfolio_risk_ref.casefold() not in portfolio_references
        ):
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:portfolio_risk_ref_unresolved"
            )
        follow_up_ref = _text(candidate.get("follow_up_ref"))
        if (
            follow_up_ref
            and follow_up_ref.casefold() not in follow_up_references
        ):
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:follow_up_ref_unresolved"
            )
        prior_at = follow_up_times.get(follow_up_ref.casefold())
        if (
            enforce_reference_chronology
            and follow_up_ref
            and prior_at is not None
            and current_as_of is not None
            and current_as_of.tzinfo is not None
            and current_as_of.utcoffset() is not None
            and current_as_of <= prior_at
        ):
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:follow_up_ref_not_prior"
            )
        factors = candidate.get("allocation_factors")
        if not isinstance(factors, Mapping):
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:allocation_factors"
            )
        else:
            if set(factors) != set(ALLOCATION_FACTOR_FIELDS):
                errors.append(
                    "research_allocation_candidate_invalid:"
                    f"{index}:allocation_factors_fields"
                )
            for field in ALLOCATION_FACTOR_FIELDS:
                text = _text(factors.get(field))
                if not text or len(text) > MAX_ALLOCATION_TEXT_CHARS:
                    errors.append(
                        "research_allocation_candidate_invalid:"
                        f"{index}:allocation_factors:{field}"
                    )

        if candidate.get("selected") is not True:
            continue
        category = primary_allocation_category(candidate)
        reference = primary_allocation_reference(candidate)
        if (
            category == "new_opportunity"
            and not _text(candidate.get("scout_candidate_id"))
        ):
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:selected_new_requires_scout_candidate_id"
            )
        if not reference or _SAFE_ID.fullmatch(reference) is None:
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:allocation_reference"
            )
        elif reference in selected_references:
            errors.append(
                "research_allocation_candidate_invalid:"
                f"{index}:duplicate_selected_reference:{reference}"
            )
        selected_references.add(reference)
    if (
        enforce_reference_chronology
        and open_questions
        and not considered_open_questions
    ):
        errors.append("research_direction_open_question_unaddressed")
    if enforce_reference_chronology:
        for opportunity_id, question_id in sorted(
            committed_questions - considered_open_questions
        ):
            errors.append(
                "research_direction_committed_question_unaddressed:"
                f"{opportunity_id}:{question_id}"
            )
    return errors


def _validate_variance(
    value: Any,
    *,
    plan: Mapping[str, Any],
    usage: Mapping[str, int],
) -> list[str]:
    exceeded = sorted(
        category
        for category in ALLOCATION_CATEGORIES
        if (
            isinstance(plan.get(category), int)
            and not isinstance(plan.get(category), bool)
            and usage[category] > int(plan[category])
        )
    )
    if not exceeded:
        if value is not None:
            return ["research_allocation_variance_invalid:unexpected"]
        return []
    if not isinstance(value, Mapping):
        return ["research_allocation_variance_invalid:required"]
    errors = []
    if set(value) != {"exceeded", "rationale"}:
        errors.append("research_allocation_variance_invalid:fields")
    declared = value.get("exceeded")
    if (
        not isinstance(declared, list)
        or sorted({_text(item) for item in declared if _text(item)})
        != exceeded
    ):
        errors.append("research_allocation_variance_invalid:exceeded")
    rationale = _text(value.get("rationale"))
    if not rationale or len(rationale) > MAX_ALLOCATION_TEXT_CHARS:
        errors.append("research_allocation_variance_invalid:rationale")
    return errors


def validate_research_allocation(
    data: Mapping[str, Any],
    *,
    required: bool,
    records: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Validate allocation structure and accounting without choosing work."""
    agenda = research_agenda(data)
    if agenda is None:
        return []
    fields_present = any(
        field in agenda
        for field in ("allocation_plan", "allocation_variance")
    ) or any(
        any(
            field in candidate
            for field in (
                "portfolio_risk_ref",
                "follow_up_ref",
                "allocation_factors",
            )
        )
        for candidate in _agenda_candidates(data)
    )
    if not required and not fields_present:
        return []
    errors = []
    if required and "allocation_plan" not in agenda:
        errors.append("research_allocation_required")
    plan = agenda.get("allocation_plan")
    errors.extend(_validate_plan(plan, data=data))
    errors.extend(_validate_candidates(
        data,
        records=records,
        enforce_reference_chronology=required,
    ))
    if isinstance(plan, Mapping):
        errors.extend(_validate_variance(
            agenda.get("allocation_variance"),
            plan=plan,
            usage=derived_research_allocation_usage(data),
        ))
    return sorted(set(errors))
