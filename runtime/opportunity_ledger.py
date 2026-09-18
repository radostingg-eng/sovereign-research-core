"""Append-only opportunity identities and lifecycle projection.

The host decides which opportunities matter. This module only validates stable
identity, evidence lineage, legal state changes, and durable persistence.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import input_fingerprint
from .audit_store import AuditJournal
from .engine import canonical_json, sha256_text
from .integrity import order_chain
from .timestamps import effective_as_of, parse_iso_timestamp

OPPORTUNITY_SCHEMA_VERSION = 3
OPPORTUNITY_STATES = (
    "new",
    "screened",
    "researching",
    "watch",
    "actionable",
    "rejected",
    "invalidated",
)
TERMINAL_STATES = frozenset({"rejected", "invalidated"})
DIRECTIONS = frozenset({
    "long",
    "short",
    "relative_value",
    "hedge",
    "mixed",
})
TRANSITIONS = {
    "new": {
        "screened", "researching", "watch", "rejected", "invalidated",
    },
    "screened": {
        "researching", "watch", "actionable", "rejected", "invalidated",
    },
    "researching": {
        "watch", "actionable", "rejected", "invalidated",
    },
    "watch": {
        "researching", "actionable", "rejected", "invalidated",
    },
    "actionable": {
        "researching", "watch", "rejected", "invalidated",
    },
    "rejected": {"new"},
    "invalidated": {"new"},
}
IDENTITY_FIELDS = frozenset({
    "instrument",
    "instrument_type",
    "strategy_family",
    "direction",
    "thesis_key",
})
MAX_TEXT_CHARS = 600
MAX_EVIDENCE_REFS = 8
MAX_REF_CHARS = 160
MAX_FEEDBACK_ITEMS = 12
MAX_SOFT_COLLISIONS = 8
MAX_RESEARCH_STATE_ITEMS = 12
RESEARCH_STATE_EFFECTIVE_AT = "2026-09-17T20:24:25Z"
RESEARCH_ITEM_STATUSES = frozenset({"open", "resolved"})
REVIEW_TRIGGER_STATUSES = frozenset({"active", "retired"})
REVISIT_RESULTS = frozenset({
    "resolved",
    "partially_resolved",
    "no_new_information",
    "invalidated",
})

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_EVIDENCE_REF = re.compile(
    r"^(stage|finding):[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _fold(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_identity(value: Mapping[str, Any]) -> dict[str, str]:
    """Return the exact identity form used for deduplication."""
    if not isinstance(value, Mapping):
        raise ValueError("not_object")
    unexpected = sorted(set(map(str, value)) - IDENTITY_FIELDS)
    if unexpected:
        raise ValueError("unexpected_fields:" + ",".join(unexpected))
    missing = [
        field for field in sorted(IDENTITY_FIELDS)
        if not _text(value.get(field))
    ]
    if missing:
        raise ValueError("missing_fields:" + ",".join(missing))

    direction = _fold(_text(value["direction"]))
    if direction not in DIRECTIONS:
        raise ValueError(f"invalid_direction:{direction}")
    family = re.sub(
        r"[\s-]+", "_", _fold(_text(value["strategy_family"])),
    )
    return {
        "instrument": _fold(_text(value["instrument"])),
        "instrument_type": _fold(_text(value["instrument_type"])),
        "strategy_family": family,
        "direction": direction,
        "thesis_key": _fold(_text(value["thesis_key"])),
    }


def identity_fingerprint(identity: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(normalize_identity(identity)))


def soft_identity_fingerprint(identity: Mapping[str, Any]) -> str:
    normalized = normalize_identity(identity)
    normalized.pop("thesis_key")
    return sha256_text(canonical_json(normalized))


def _record_id(event_id: str) -> str:
    return f"opportunity-event:{event_id}"


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
    anchors.update(
        {
            f"finding:{finding_id}": decision_anchor
            for row in data.get("findings") or ()
            if isinstance(row, Mapping)
            and (finding_id := _text(row.get("id")))
        }
    )
    return anchors


def _event_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    exclude_cycle_id: str | None = None,
) -> list[Mapping[str, Any]]:
    ordered, failures = order_chain(records)
    if failures:
        ordered = list(records)
    return [
        record
        for record in ordered
        if record.get("record_type") == "opportunity_event"
        and isinstance(record.get("payload"), Mapping)
        and (
            exclude_cycle_id is None
            or _text(record["payload"].get("cycle_id")) != exclude_cycle_id
        )
    ]


def _research_state_signature(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return sha256_text(canonical_json(value))


def _current_state(
    records: Sequence[Mapping[str, Any]],
    *,
    exclude_cycle_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    current: dict[str, dict[str, Any]] = {}
    exact_identities: dict[str, str] = {}
    for record in _event_rows(records, exclude_cycle_id=exclude_cycle_id):
        payload = dict(record["payload"])
        opportunity_id = _text(payload.get("opportunity_id"))
        fingerprint = _text(payload.get("identity_fingerprint"))
        if not opportunity_id or not fingerprint:
            continue
        previous = current.get(opportunity_id)
        payload["record_id"] = record.get("record_id")
        payload["event_count"] = int(
            previous.get("event_count", 0) if previous else 0
        ) + 1
        no_info_targets = dict(
            previous.get("no_information_target_signatures", {})
            if isinstance(previous, Mapping)
            and isinstance(
                previous.get("no_information_target_signatures"), Mapping,
            )
            else {}
        )
        revisit = payload.get("revisit")
        if (
            isinstance(revisit, Mapping)
            and _text(revisit.get("result")).lower() == "no_new_information"
        ):
            target_id = _text(
                revisit.get("target_missing_information_id")
            )
            signature = _research_state_signature(
                payload.get("research_state")
            )
            if target_id and signature:
                no_info_targets[target_id] = signature
        payload["no_information_target_signatures"] = no_info_targets
        current[opportunity_id] = payload
        exact_identities[fingerprint] = opportunity_id
    return current, exact_identities


def _validate_evidence(
    value: Any,
    *,
    index: int,
    anchors: Mapping[str, str],
    prefix: str = "opportunity_evidence_invalid",
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_EVIDENCE_REFS
    ):
        return [f"{prefix}:{index}:count"]
    errors = []
    seen = set()
    for evidence_index, raw in enumerate(value):
        ref = _text(raw)
        if (
            not ref
            or len(ref) > MAX_REF_CHARS
            or _EVIDENCE_REF.fullmatch(ref) is None
        ):
            errors.append(
                f"{prefix}:{index}:"
                f"invalid_ref:{evidence_index}"
            )
        elif ref in seen:
            errors.append(
                f"{prefix}:{index}:duplicate_ref:{ref}"
            )
        elif ref not in anchors:
            errors.append(
                f"{prefix}:{index}:dangling_ref:{ref}"
            )
        seen.add(ref)
    return errors


def _research_contract_required(
    data: Mapping[str, Any],
    *,
    required: bool,
) -> bool:
    if required:
        return True
    observed = parse_iso_timestamp(effective_as_of(data))
    effective = parse_iso_timestamp(RESEARCH_STATE_EFFECTIVE_AT)
    return (
        observed is not None
        and observed.tzinfo is not None
        and effective is not None
        and effective.tzinfo is not None
        and observed > effective
    )


def _validate_state_rows(
    value: Any,
    *,
    index: int,
    section: str,
    text_fields: tuple[str, ...],
    statuses: frozenset[str],
) -> tuple[list[str], dict[str, Mapping[str, Any]]]:
    if not isinstance(value, list) or len(value) > MAX_RESEARCH_STATE_ITEMS:
        return [
            f"opportunity_research_state_invalid:{index}:{section}:count"
        ], {}
    errors: list[str] = []
    rows: dict[str, Mapping[str, Any]] = {}
    expected = {"id", *text_fields, "status"}
    for row_index, row in enumerate(value):
        if not isinstance(row, Mapping):
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:{section}:{row_index}:object"
            )
            continue
        if set(row) != expected:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:{section}:{row_index}:fields"
            )
        item_id = _text(row.get("id"))
        if not item_id or _ID.fullmatch(item_id) is None:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:{section}:{row_index}:id"
            )
        elif item_id in rows:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:{section}:{row_index}:duplicate_id"
            )
        else:
            rows[item_id] = row
        for field in text_fields:
            text = _text(row.get(field))
            if not text or len(text) > MAX_TEXT_CHARS:
                errors.append(
                    "opportunity_research_state_invalid:"
                    f"{index}:{section}:{row_index}:{field}"
                )
        status = _text(row.get("status")).lower()
        if status not in statuses:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:{section}:{row_index}:status"
            )
    return errors, rows


def _previous_state_rows(
    previous: Mapping[str, Any] | None,
) -> tuple[
    Mapping[str, Any] | None,
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    state = previous.get("research_state") if previous else None
    if not isinstance(state, Mapping):
        return None, {}, {}, {}

    def rows(section: str) -> dict[str, Mapping[str, Any]]:
        value = state.get(section)
        if not isinstance(value, list):
            return {}
        return {
            _text(row.get("id")): row
            for row in value
            if isinstance(row, Mapping) and _text(row.get("id"))
        }

    return (
        state,
        rows("missing_information"),
        rows("uncertainties"),
        rows("review_triggers"),
    )


def _validate_stable_rows(
    *,
    index: int,
    section: str,
    previous: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    stable_fields: tuple[str, ...],
    closed_status: str,
) -> list[str]:
    errors = []
    for item_id, prior in previous.items():
        row = current.get(item_id)
        if row is None:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:{section}:removed:{item_id}"
            )
            continue
        for field in stable_fields:
            if _fold(_text(row.get(field))) != _fold(_text(prior.get(field))):
                errors.append(
                    "opportunity_research_state_invalid:"
                    f"{index}:{section}:changed:{item_id}:{field}"
                )
        if (
            _text(prior.get("status")).lower() == closed_status
            and _text(row.get("status")).lower() != closed_status
        ):
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:{section}:reopened:{item_id}"
            )
    return errors


def _validate_research_state(
    value: Any,
    *,
    index: int,
    to_state: str,
    previous: Mapping[str, Any] | None,
) -> tuple[
    list[str],
    Mapping[str, Any] | None,
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    if not isinstance(value, Mapping):
        return [
            f"opportunity_research_state_invalid:{index}:not_object"
        ], None, {}, {}, {}
    expected = {
        "missing_information",
        "uncertainties",
        "review_triggers",
        "next_question_id",
    }
    errors = []
    if set(value) != expected:
        errors.append(
            f"opportunity_research_state_invalid:{index}:fields"
        )
    row_errors, missing = _validate_state_rows(
        value.get("missing_information"),
        index=index,
        section="missing_information",
        text_fields=("question", "why_it_matters"),
        statuses=RESEARCH_ITEM_STATUSES,
    )
    errors.extend(row_errors)
    row_errors, uncertainties = _validate_state_rows(
        value.get("uncertainties"),
        index=index,
        section="uncertainties",
        text_fields=("description",),
        statuses=RESEARCH_ITEM_STATUSES,
    )
    errors.extend(row_errors)
    row_errors, triggers = _validate_state_rows(
        value.get("review_triggers"),
        index=index,
        section="review_triggers",
        text_fields=("condition",),
        statuses=REVIEW_TRIGGER_STATUSES,
    )
    errors.extend(row_errors)

    next_question_id = value.get("next_question_id")
    next_question_id = (
        _text(next_question_id) if next_question_id is not None else None
    )
    open_missing = {
        item_id for item_id, row in missing.items()
        if _text(row.get("status")).lower() == "open"
    }
    open_uncertainties = {
        item_id for item_id, row in uncertainties.items()
        if _text(row.get("status")).lower() == "open"
    }
    active_triggers = {
        item_id for item_id, row in triggers.items()
        if _text(row.get("status")).lower() == "active"
    }
    needs_next_question = to_state in {
        "new", "screened", "researching", "watch",
    }
    if needs_next_question:
        if not open_missing:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:missing_information:no_open_item"
            )
        if not open_uncertainties:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:uncertainties:no_open_item"
            )
        if not active_triggers:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:review_triggers:no_active_item"
            )
        if next_question_id not in open_missing:
            errors.append(
                "opportunity_research_state_invalid:"
                f"{index}:next_question_id"
            )
    elif next_question_id is not None and next_question_id not in open_missing:
        errors.append(
            f"opportunity_research_state_invalid:{index}:next_question_id"
        )

    (
        previous_state,
        previous_missing,
        previous_uncertainties,
        previous_triggers,
    ) = _previous_state_rows(previous)
    if previous_state is not None:
        errors.extend(_validate_stable_rows(
            index=index,
            section="missing_information",
            previous=previous_missing,
            current=missing,
            stable_fields=("question",),
            closed_status="resolved",
        ))
        errors.extend(_validate_stable_rows(
            index=index,
            section="uncertainties",
            previous=previous_uncertainties,
            current=uncertainties,
            stable_fields=("description",),
            closed_status="resolved",
        ))
        errors.extend(_validate_stable_rows(
            index=index,
            section="review_triggers",
            previous=previous_triggers,
            current=triggers,
            stable_fields=("condition",),
            closed_status="retired",
        ))
    return errors, value, missing, uncertainties, triggers


def _validate_revisit(
    value: Any,
    *,
    index: int,
    anchors: Mapping[str, str],
    previous: Mapping[str, Any],
    current_state: Mapping[str, Any],
    current_missing: Mapping[str, Mapping[str, Any]],
    current_uncertainties: Mapping[str, Mapping[str, Any]],
    current_triggers: Mapping[str, Mapping[str, Any]],
    to_state: str,
) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"opportunity_revisit_invalid:{index}:not_object"]
    expected = {
        "trigger_id",
        "target_missing_information_id",
        "expected_information_gain",
        "result",
        "result_summary",
        "evidence",
        "legacy_state_initialization",
        "retarget_reason",
    }
    errors = []
    if set(value) != expected:
        errors.append(f"opportunity_revisit_invalid:{index}:fields")
    trigger_id = _text(value.get("trigger_id"))
    target_id = _text(value.get("target_missing_information_id"))
    for field, text in (
        ("trigger_id", trigger_id),
        ("target_missing_information_id", target_id),
        (
            "expected_information_gain",
            _text(value.get("expected_information_gain")),
        ),
        ("result_summary", _text(value.get("result_summary"))),
    ):
        if not text or (
            field in {"trigger_id", "target_missing_information_id"}
            and _ID.fullmatch(text) is None
        ) or (
            field not in {"trigger_id", "target_missing_information_id"}
            and len(text) > MAX_TEXT_CHARS
        ):
            errors.append(
                f"opportunity_revisit_invalid:{index}:{field}"
            )
    result = _text(value.get("result")).lower()
    if result not in REVISIT_RESULTS:
        errors.append(f"opportunity_revisit_invalid:{index}:result")
    legacy = value.get("legacy_state_initialization")
    if not isinstance(legacy, bool):
        errors.append(
            "opportunity_revisit_invalid:"
            f"{index}:legacy_state_initialization"
        )
    retarget_reason = value.get("retarget_reason")
    if retarget_reason is not None and (
        not _text(retarget_reason)
        or len(_text(retarget_reason)) > MAX_TEXT_CHARS
    ):
        errors.append(
            f"opportunity_revisit_invalid:{index}:retarget_reason"
        )
    errors.extend(_validate_evidence(
        value.get("evidence"),
        index=index,
        anchors=anchors,
        prefix="opportunity_revisit_evidence_invalid",
    ))

    (
        previous_state,
        previous_missing,
        previous_uncertainties,
        previous_triggers,
    ) = _previous_state_rows(previous)
    if previous_state is None:
        if legacy is not True:
            errors.append(
                f"opportunity_revisit_invalid:{index}:legacy_required"
            )
        if (
            target_id not in current_missing
            or _text(current_missing[target_id].get("status")).lower()
            != "open"
        ):
            errors.append(
                f"opportunity_revisit_invalid:{index}:legacy_target"
            )
        if (
            trigger_id not in current_triggers
            or _text(current_triggers[trigger_id].get("status")).lower()
            != "active"
        ):
            errors.append(
                f"opportunity_revisit_invalid:{index}:legacy_trigger"
            )
        return errors

    if legacy is not False:
        errors.append(
            f"opportunity_revisit_invalid:{index}:legacy_forbidden"
        )
    previous_next = _text(previous_state.get("next_question_id"))
    retargeted = bool(_text(retarget_reason))
    if target_id != previous_next and not retargeted:
        errors.append(
            f"opportunity_revisit_invalid:{index}:target_not_committed"
        )
    if (
        target_id not in previous_missing
        and target_id not in current_missing
    ):
        errors.append(
            f"opportunity_revisit_invalid:{index}:target_unknown"
        )
    elif (
        target_id in previous_missing
        and _text(previous_missing[target_id].get("status")).lower()
        != "open"
    ):
        errors.append(
            f"opportunity_revisit_invalid:{index}:target_not_open"
        )
    prior_trigger_active = (
        trigger_id in previous_triggers
        and _text(previous_triggers[trigger_id].get("status")).lower()
        == "active"
    )
    current_trigger_active = (
        trigger_id in current_triggers
        and _text(current_triggers[trigger_id].get("status")).lower()
        == "active"
    )
    if not prior_trigger_active and not (
        retargeted and current_trigger_active
    ):
        errors.append(
            f"opportunity_revisit_invalid:{index}:trigger_unknown"
        )

    def status_changed(
        prior: Mapping[str, Mapping[str, Any]],
        current: Mapping[str, Mapping[str, Any]],
    ) -> bool:
        return any(
            item_id in current
            and _text(row.get("status")).lower()
            != _text(current[item_id].get("status")).lower()
            for item_id, row in prior.items()
        )

    progress = (
        to_state != _text(previous.get("to_state"))
        or set(current_missing) != set(previous_missing)
        or set(current_uncertainties) != set(previous_uncertainties)
        or set(current_triggers) != set(previous_triggers)
        or status_changed(previous_missing, current_missing)
        or status_changed(previous_uncertainties, current_uncertainties)
        or status_changed(previous_triggers, current_triggers)
    )
    if not progress and result != "no_new_information":
        errors.append(
            f"opportunity_revisit_invalid:{index}:no_state_change"
        )

    prior_revisit = previous.get("revisit")
    prior_no_info_targets = previous.get("no_information_target_signatures")
    prior_no_info_targets = (
        prior_no_info_targets
        if isinstance(prior_no_info_targets, Mapping)
        else {}
    )
    if (
        result == "no_new_information"
        and isinstance(prior_revisit, Mapping)
        and _text(prior_revisit.get("result")).lower()
        == "no_new_information"
        and _text(prior_revisit.get("trigger_id")) == trigger_id
        and _text(
            prior_revisit.get("target_missing_information_id")
        ) == target_id
    ):
        errors.append(
            f"opportunity_revisit_repeated_no_information:{index}"
        )
    elif (
        result == "no_new_information"
        and prior_no_info_targets.get(target_id)
        == _research_state_signature(current_state)
    ):
        errors.append(
            f"opportunity_revisit_repeated_no_information:{index}"
        )
    return errors


def _scout_identities(
    data: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    for stage in data.get("cognitive_stages") or ():
        if (
            not isinstance(stage, Mapping)
            or _text(stage.get("stage_id")) != "market_scout"
        ):
            continue
        output = stage.get("output")
        output = output if isinstance(output, Mapping) else {}
        report = output.get("market_scout_report")
        report = report if isinstance(report, Mapping) else {}
        candidates = report.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        identities = {}
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            candidate_id = _text(candidate.get("candidate_id"))
            try:
                identity = normalize_identity(candidate.get("identity"))
            except ValueError:
                continue
            if candidate_id:
                identities[candidate_id] = identity
        return identities
    return {}


def _selected_agenda_candidates(
    data: Mapping[str, Any],
) -> list[tuple[int, Mapping[str, Any]]]:
    for stage in data.get("cognitive_stages") or ():
        if (
            not isinstance(stage, Mapping)
            or _text(stage.get("stage_id")) != "research_director"
        ):
            continue
        output = stage.get("output")
        output = output if isinstance(output, Mapping) else {}
        agenda = output.get("research_agenda")
        agenda = agenda if isinstance(agenda, Mapping) else {}
        candidates = agenda.get("candidates")
        if not isinstance(candidates, list):
            return []
        return [
            (index, row)
            for index, row in enumerate(candidates)
            if isinstance(row, Mapping) and row.get("selected") is True
        ]
    return []


def _validate_agenda_opportunity_links(
    data: Mapping[str, Any],
    *,
    current: Mapping[str, Mapping[str, Any]],
    updates: Sequence[Mapping[str, Any]],
    strict: bool,
) -> list[str]:
    if not strict:
        return []
    errors = []
    scout_identities = _scout_identities(data)
    exact = {
        _text(row.get("identity_fingerprint")): opportunity_id
        for opportunity_id, row in current.items()
    }
    soft: dict[str, list[str]] = {}
    instrument_family: dict[tuple[str, str], list[str]] = {}
    for opportunity_id, row in current.items():
        fingerprint = _text(row.get("soft_identity_fingerprint"))
        if fingerprint:
            soft.setdefault(fingerprint, []).append(opportunity_id)
        identity = row.get("identity")
        if isinstance(identity, Mapping):
            key = (
                _fold(_text(identity.get("instrument"))),
                re.sub(
                    r"[\s-]+",
                    "_",
                    _fold(_text(identity.get("strategy_family"))),
                ),
            )
            instrument_family.setdefault(key, []).append(opportunity_id)
    updates_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for row in updates:
        opportunity_id = _text(row.get("opportunity_id"))
        if opportunity_id:
            updates_by_id.setdefault(opportunity_id, []).append(row)

    for index, candidate in _selected_agenda_candidates(data):
        opportunity_id = _text(candidate.get("opportunity_id"))
        scout_id = _text(candidate.get("scout_candidate_id"))
        scout_identity = scout_identities.get(scout_id)
        exact_match = (
            exact.get(identity_fingerprint(scout_identity))
            if scout_identity is not None
            else None
        )
        soft_matches = (
            soft.get(soft_identity_fingerprint(scout_identity), [])
            if scout_identity is not None
            else []
        )
        key = (
            _fold(_text(candidate.get("instrument"))),
            re.sub(
                r"[\s-]+",
                "_",
                _fold(_text(candidate.get("strategy_family"))),
            ),
        )
        possible_matches = sorted(set(
            soft_matches or instrument_family.get(key, [])
        ))
        if exact_match and not opportunity_id:
            errors.append(
                "opportunity_agenda_link_invalid:"
                f"{index}:existing_id_required:{exact_match}"
            )
            continue
        if opportunity_id:
            previous = current.get(opportunity_id)
            if previous is None:
                errors.append(
                    "opportunity_agenda_link_invalid:"
                    f"{index}:unknown:{opportunity_id}"
                )
                continue
            if exact_match and exact_match != opportunity_id:
                errors.append(
                    "opportunity_agenda_link_invalid:"
                    f"{index}:identity_mismatch:{opportunity_id}"
                )
            identity = previous.get("identity")
            if isinstance(identity, Mapping) and (
                key != (
                    _fold(_text(identity.get("instrument"))),
                    re.sub(
                        r"[\s-]+",
                        "_",
                        _fold(_text(identity.get("strategy_family"))),
                    ),
                )
            ):
                errors.append(
                    "opportunity_agenda_link_invalid:"
                    f"{index}:agenda_mismatch:{opportunity_id}"
                )
            linked_updates = updates_by_id.get(opportunity_id, [])
            if not any(
                isinstance(row.get("revisit"), Mapping)
                for row in linked_updates
            ):
                errors.append(
                    "opportunity_agenda_revisit_required:"
                    f"{index}:{opportunity_id}"
                )
            continue

        if possible_matches:
            declared = candidate.get("distinct_from_opportunity_ids")
            declared_ids = (
                sorted({_text(value) for value in declared if _text(value)})
                if isinstance(declared, list)
                else []
            )
            if declared_ids != possible_matches:
                errors.append(
                    "opportunity_agenda_link_invalid:"
                    f"{index}:distinct_ids"
                )
            if not _text(candidate.get("distinctness_reason")):
                errors.append(
                    "opportunity_agenda_link_invalid:"
                    f"{index}:distinctness_reason"
                )
    return errors


def validate_opportunity_updates(
    updates: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    require_research_state: bool = False,
) -> list[str]:
    """Validate a full-cycle opportunity event sequence before persistence."""
    if updates is None:
        return []
    if data.get("host_input_schema_version") != OPPORTUNITY_SCHEMA_VERSION:
        return ["opportunity_updates_require_schema_v3"]
    if not isinstance(updates, list):
        return ["opportunity_updates_must_be_a_list"]

    cycle_id = _text(data.get("cycle_id"))
    anchors = _current_evidence_anchors(data)
    current, exact_identities = _current_state(
        records, exclude_cycle_id=cycle_id,
    )
    prior_current = dict(current)
    strict_research_state = _research_contract_required(
        data, required=require_research_state,
    )
    existing_record_ids = {
        _text(record.get("record_id")) for record in records
    }
    seen_event_ids: set[str] = set()
    errors: list[str] = []

    for index, row in enumerate(updates):
        if not isinstance(row, Mapping):
            errors.append(f"opportunity_update_not_object:{index}")
            continue

        event_id = _text(row.get("event_id"))
        opportunity_id = _text(row.get("opportunity_id"))
        for field, value in (
            ("event_id", event_id),
            ("opportunity_id", opportunity_id),
            ("thesis", _text(row.get("thesis"))),
            ("rationale", _text(row.get("rationale"))),
        ):
            if not value:
                errors.append(
                    f"opportunity_update_field_required:{index}:{field}"
                )
            elif field in {"event_id", "opportunity_id"} and (
                _ID.fullmatch(value) is None
            ):
                errors.append(
                    f"opportunity_update_identity_invalid:{index}:{field}"
                )
            elif field in {"thesis", "rationale"} and (
                len(value) > MAX_TEXT_CHARS
            ):
                errors.append(
                    f"opportunity_update_text_too_long:{index}:{field}"
                )

        record_id = _record_id(event_id) if event_id else ""
        if event_id in seen_event_ids:
            errors.append(f"opportunity_event_id_duplicate:{index}:{event_id}")
        elif record_id in existing_record_ids:
            errors.append(f"opportunity_event_id_reused:{index}:{event_id}")
        seen_event_ids.add(event_id)

        try:
            identity = normalize_identity(row.get("identity"))
        except ValueError as error:
            errors.append(
                f"opportunity_update_identity_invalid:{index}:{error}"
            )
            identity = None
        fingerprint = (
            identity_fingerprint(identity) if identity is not None else ""
        )

        errors.extend(_validate_evidence(
            row.get("evidence"), index=index, anchors=anchors,
        ))

        to_state = _text(row.get("to_state")).lower()
        if to_state not in OPPORTUNITY_STATES:
            errors.append(
                f"opportunity_transition_invalid:{index}:unknown:{to_state}"
            )
        previous = current.get(opportunity_id)
        from_state = row.get("from_state")
        from_state = (
            _text(from_state).lower()
            if from_state is not None
            else None
        )

        if previous is None:
            if from_state is not None or to_state != "new":
                errors.append(
                    f"opportunity_initial_state_invalid:{index}:"
                    f"{from_state}->{to_state}"
                )
            duplicate_id = exact_identities.get(fingerprint)
            if fingerprint and duplicate_id and duplicate_id != opportunity_id:
                errors.append(
                    f"opportunity_duplicate_identity:{index}:"
                    f"{opportunity_id}:{duplicate_id}"
                )
        else:
            previous_state = _text(previous.get("to_state"))
            if from_state != previous_state:
                errors.append(
                    f"opportunity_state_mismatch:{index}:"
                    f"{opportunity_id}:{previous_state}!={from_state}"
                )
            if (
                fingerprint
                and fingerprint != previous.get("identity_fingerprint")
            ):
                errors.append(
                    f"opportunity_identity_changed:{index}:{opportunity_id}"
                )
            if (
                to_state in OPPORTUNITY_STATES
                and to_state not in TRANSITIONS.get(previous_state, set())
                and not (
                    to_state == previous_state
                    and isinstance(row.get("revisit"), Mapping)
                )
            ):
                errors.append(
                    f"opportunity_transition_invalid:{index}:"
                    f"{previous_state}->{to_state}"
                )

        reopen = _text(row.get("reopens_event_id"))
        previous_state = _text(previous.get("to_state")) if previous else ""
        if previous_state in TERMINAL_STATES and to_state == "new":
            if reopen != _text(previous.get("record_id")):
                errors.append(
                    f"opportunity_reopen_invalid:{index}:{opportunity_id}"
                )
        elif reopen:
            errors.append(
                f"opportunity_reopen_invalid:{index}:{opportunity_id}"
            )

        prior_state, _, _, _ = _previous_state_rows(previous)
        state_required = strict_research_state or prior_state is not None
        research_state = row.get("research_state")
        state_value: Mapping[str, Any] | None = None
        current_missing: dict[str, Mapping[str, Any]] = {}
        current_uncertainties: dict[str, Mapping[str, Any]] = {}
        current_triggers: dict[str, Mapping[str, Any]] = {}
        if state_required or research_state is not None:
            (
                state_errors,
                state_value,
                current_missing,
                current_uncertainties,
                current_triggers,
            ) = _validate_research_state(
                research_state,
                index=index,
                to_state=to_state,
                previous=previous,
            )
            errors.extend(state_errors)

        revisit = row.get("revisit")
        if previous is None:
            if revisit is not None:
                errors.append(
                    f"opportunity_revisit_invalid:{index}:initial_event"
                )
        elif revisit is not None and state_value is not None:
            errors.extend(_validate_revisit(
                revisit,
                index=index,
                anchors=anchors,
                previous=previous,
                current_state=state_value,
                current_missing=current_missing,
                current_uncertainties=current_uncertainties,
                current_triggers=current_triggers,
                to_state=to_state,
            ))
        elif from_state == to_state:
            errors.append(
                f"opportunity_revisit_required:{index}:{opportunity_id}"
            )

        if (
            opportunity_id
            and identity is not None
            and to_state in OPPORTUNITY_STATES
        ):
            no_info_targets = dict(
                previous.get("no_information_target_signatures", {})
                if isinstance(previous, Mapping)
                and isinstance(
                    previous.get("no_information_target_signatures"),
                    Mapping,
                )
                else {}
            )
            if (
                isinstance(revisit, Mapping)
                and _text(revisit.get("result")).lower()
                == "no_new_information"
            ):
                target_id = _text(
                    revisit.get("target_missing_information_id")
                )
                signature = _research_state_signature(state_value)
                if target_id and signature:
                    no_info_targets[target_id] = signature
            projected = {
                "opportunity_id": opportunity_id,
                "identity": identity,
                "identity_fingerprint": fingerprint,
                "to_state": to_state,
                "record_id": record_id,
                "research_state": state_value,
                "revisit": revisit,
                "no_information_target_signatures": no_info_targets,
                "event_count": int(
                    previous.get("event_count", 0) if previous else 0
                ) + 1,
            }
            current[opportunity_id] = projected
            exact_identities[fingerprint] = opportunity_id

    errors.extend(_validate_agenda_opportunity_links(
        data,
        current=prior_current,
        updates=[
            row for row in updates if isinstance(row, Mapping)
        ],
        strict=strict_research_state,
    ))
    return sorted(set(errors))


def _payload(
    row: Mapping[str, Any],
    *,
    data: Mapping[str, Any],
    cycle_id: str,
) -> dict[str, Any]:
    identity = normalize_identity(row["identity"])
    anchors = _current_evidence_anchors(data)
    evidence = [_text(ref) for ref in row.get("evidence") or ()]
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    payload = {
        "schema_version": (
            2 if isinstance(row.get("research_state"), Mapping) else 1
        ),
        "cycle_id": cycle_id,
        "event_id": _text(row.get("event_id")),
        "opportunity_id": _text(row.get("opportunity_id")),
        "identity": identity,
        "identity_fingerprint": identity_fingerprint(identity),
        "soft_identity_fingerprint": soft_identity_fingerprint(identity),
        "from_state": (
            _text(row.get("from_state")).lower()
            if row.get("from_state") is not None
            else None
        ),
        "to_state": _text(row.get("to_state")).lower(),
        "thesis": _text(row.get("thesis")),
        "rationale": _text(row.get("rationale")),
        "evidence": evidence,
        "evidence_record_ids": list(dict.fromkeys(
            anchors[ref] for ref in evidence
        )),
        "reopens_event_id": _text(row.get("reopens_event_id")) or None,
        "observed_at": effective_as_of(data),
    }
    if payload["schema_version"] == 2:
        payload["research_state"] = row.get("research_state")
        payload["revisit"] = row.get("revisit")
    return payload


def persist_opportunity_updates(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> int:
    """Append missing opportunity events and safely replay partial writes."""
    updates = data.get("opportunity_updates")
    if not isinstance(updates, list) or not updates:
        return 0
    cycle_id = _text(receipt.get("cycle_id"))
    records = journal.read()
    base_records = [
        record for record in records
        if not (
            record.get("record_type") == "opportunity_event"
            and isinstance(record.get("payload"), Mapping)
            and _text(record["payload"].get("cycle_id")) == cycle_id
        )
    ]
    errors = validate_opportunity_updates(
        updates, data=data, records=base_records,
    )
    if errors:
        raise ValueError(
            "opportunity_update_reconciliation_failed:" + ",".join(errors)
        )

    current, _ = _current_state(base_records)
    existing = {
        _text(record.get("record_id")): record
        for record in records
        if record.get("record_type") == "opportunity_event"
    }
    receipt_id = f"cycle-receipt:{cycle_id}"
    added = 0
    for row in updates:
        event_id = _text(row.get("event_id"))
        opportunity_id = _text(row.get("opportunity_id"))
        record_id = _record_id(event_id)
        payload = _payload(row, data=data, cycle_id=cycle_id)
        previous = current.get(opportunity_id)
        caused_by = [receipt_id]
        caused_by.extend(payload["evidence_record_ids"])
        if previous and _text(previous.get("record_id")):
            caused_by.append(_text(previous["record_id"]))
        expected_causes = list(dict.fromkeys(caused_by))

        prior_record = existing.get(record_id)
        if prior_record is not None:
            if (
                prior_record.get("payload") != payload
                or prior_record.get("caused_by") != expected_causes
            ):
                raise ValueError(
                    f"opportunity_event_payload_mismatch:{record_id}"
                )
        else:
            journal.append(
                record_id=record_id,
                record_type="opportunity_event",
                agent="sovereign-host",
                caused_by=expected_causes,
                payload=payload,
            )
            added += 1
        payload["record_id"] = record_id
        payload["event_count"] = int(
            previous.get("event_count", 0) if previous else 0
        ) + 1
        current[opportunity_id] = payload
    return added


def backfill_opportunity_events(
    inputs: Sequence[Path],
    journal: AuditJournal,
) -> int:
    """Recover missing opportunity events for already receipted inputs."""
    candidates: dict[tuple[str, str], Mapping[str, Any]] = {}
    for path in inputs:
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("opportunity_updates"), list)
        ):
            continue
        cycle_id = _text(data.get("cycle_id"))
        if cycle_id:
            candidates[(cycle_id, input_fingerprint(data))] = data

    ordered, failures = order_chain(journal.read())
    if failures:
        raise ValueError("opportunity_backfill_chain_invalid")
    existing_event_ids = {
        _text(record.get("record_id"))
        for record in ordered
        if record.get("record_type") == "opportunity_event"
    }
    added = 0
    for record in ordered:
        if record.get("record_type") != "cycle_receipt":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        cycle_id = _text(payload.get("cycle_id"))
        snapshot_id = _text(payload.get("snapshot_id"))
        fingerprint = snapshot_id.rsplit(":", 1)[-1] if ":" in snapshot_id else ""
        data = candidates.get((cycle_id, fingerprint))
        if data is not None:
            updates = data.get("opportunity_updates") or ()
            expected_ids = {
                _record_id(_text(row.get("event_id")))
                for row in updates
                if isinstance(row, Mapping) and _text(row.get("event_id"))
            }
            if expected_ids and expected_ids.issubset(existing_event_ids):
                continue
            appended = persist_opportunity_updates(data, journal, payload)
            added += appended
            existing_event_ids.update(expected_ids)
    return added


def opportunity_ledger_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = MAX_FEEDBACK_ITEMS,
) -> dict[str, Any]:
    """Return current opportunity state without ranking investment merit."""
    current, _ = _current_state(records)
    ordered = list(current.values())
    ordered.sort(
        key=lambda row: (
            _text(row.get("observed_at")),
            _text(row.get("opportunity_id")),
        ),
        reverse=True,
    )
    state_counts = Counter(_text(row.get("to_state")) for row in ordered)
    soft_groups: dict[str, list[str]] = {}
    for row in ordered:
        soft = _text(row.get("soft_identity_fingerprint"))
        opportunity_id = _text(row.get("opportunity_id"))
        if soft and opportunity_id:
            soft_groups.setdefault(soft, []).append(opportunity_id)
    soft_collisions = [
        {
            "soft_identity_fingerprint": fingerprint,
            "opportunity_ids": sorted(ids),
        }
        for fingerprint, ids in sorted(soft_groups.items())
        if len(ids) > 1
    ][:MAX_SOFT_COLLISIONS]
    revisit_counts: dict[str, Counter[str]] = {}
    revisit_pairs: dict[str, Counter[tuple[str, str]]] = {}
    for record in _event_rows(records):
        payload = record["payload"]
        opportunity_id = _text(payload.get("opportunity_id"))
        revisit = payload.get("revisit")
        if not opportunity_id or not isinstance(revisit, Mapping):
            continue
        result = _text(revisit.get("result")).lower()
        revisit_counts.setdefault(opportunity_id, Counter())["attempts"] += 1
        if result:
            revisit_counts[opportunity_id][result] += 1
        pair = (
            _text(revisit.get("trigger_id")),
            _text(revisit.get("target_missing_information_id")),
        )
        if all(pair):
            revisit_pairs.setdefault(opportunity_id, Counter())[pair] += 1
    items = [
        {
            "opportunity_id": row.get("opportunity_id"),
            "state": row.get("to_state"),
            "identity": row.get("identity"),
            "identity_fingerprint": row.get("identity_fingerprint"),
            "last_transition": {
                "from_state": row.get("from_state"),
                "to_state": row.get("to_state"),
            },
            "last_event_id": row.get("event_id"),
            "last_record_id": row.get("record_id"),
            "event_count": row.get("event_count"),
            "last_cycle_id": row.get("cycle_id"),
            "observed_at": row.get("observed_at"),
            "thesis": _text(row.get("thesis"))[:240],
            "rationale": _text(row.get("rationale"))[:240],
            "evidence": list(row.get("evidence") or ()),
            "evidence_record_ids": list(
                row.get("evidence_record_ids") or ()
            ),
            "research_state": row.get("research_state"),
            "last_revisit": row.get("revisit"),
            "revisit_metrics": {
                "attempts": revisit_counts.get(
                    _text(row.get("opportunity_id")), Counter()
                )["attempts"],
                "no_new_information": revisit_counts.get(
                    _text(row.get("opportunity_id")), Counter()
                )["no_new_information"],
                "attempts_by_trigger_and_target": [
                    {
                        "trigger_id": trigger_id,
                        "target_missing_information_id": target_id,
                        "attempts": count,
                    }
                    for (
                        trigger_id,
                        target_id,
                    ), count in sorted(
                        revisit_pairs.get(
                            _text(row.get("opportunity_id")), Counter()
                        ).items()
                    )[:MAX_RESEARCH_STATE_ITEMS]
                ],
            },
        }
        for row in ordered[:limit]
    ]
    return {
        "total": len(ordered),
        "counts_by_state": dict(sorted(state_counts.items())),
        "items": items,
        "not_shown": max(0, len(ordered) - len(items)),
        "soft_identity_collisions": soft_collisions,
        "what_this_means": (
            "Append-only current opportunity state. Exact identity duplicates "
            "are refused. Soft collisions share instrument, type, family, and "
            "direction but use different thesis keys; the host must decide "
            "whether they are genuinely distinct. This ledger does not rank "
            "or select opportunities."
        ),
    }
