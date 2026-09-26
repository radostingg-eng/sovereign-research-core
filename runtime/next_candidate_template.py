"""Build a pre-filled skeleton for the host's *next* semantic candidate.

The host is a weaker model that tends to copy its previous candidate file
forward and edit it in place. That candidate carries a stale ``cycle_id``,
stale ``schedule_context`` timestamps, resolved-but-still-listed opportunity
slots, and stale evidence references -- the single largest source of
refusals (``schedule_context_task_id``, ``opportunity_research_state_invalid``,
``worker_research_disposition_missing``, ``decision_repetition_prior_cycle_
mismatch``, and friends).

This module builds a fresh skeleton instead, using ONLY values the runtime
already knows from the journal: the schedule contract's ``task_id``, the
worker records the host is currently required to dispose of, the ledger's
current state for every open opportunity, the last *finalized* (not merely
accepted) prior cycle id, the venues used by the most recent accepted
``market_sessions`` payload, and the current carry-forward eligibility for
``market_scout_report``/``research_agenda``. Every field the runtime cannot
know -- because it requires the host's own reasoning about markets, risk, or
evidence -- is filled with a ``"<FILL: ...>"`` placeholder that names the
exact instruction and, where useful, the allowed values.

Nothing here is committed as an input. It is advisory feedback only: the
host is instructed to copy this object, replace every placeholder, and NOT
carry over field values from its own previous (possibly stale) candidate for
any field this template already covers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .decision_repetition import latest_prior_finalized_decision_selection
from .opportunity_ledger import (
    REVISIT_RESULTS,
    TERMINAL_STATES,
    TRANSITIONS,
    _current_state,
)
from .research_allocation import _committed_question_pairs, _open_question_pairs
from .schedule_ledger import INTERVENTIONS, TRIGGERS, load_schedule_contract
from .semantic_candidate import (
    CARRY_FORWARD_LIMITS,
    CARRY_FORWARD_STAGE_IDS,
    SEMANTIC_INPUT_SCHEMA_VERSION,
    _latest_finalized_stage_field,
)
from .worker_research_dispositions import WORKER_RESEARCH_DISPOSITIONS

# Target upper bound on the serialized template. Approximate, not exact: the
# per-section limits below keep the actual payload well under this.
TARGET_MAX_SERIALIZED_BYTES = 15_000

MAX_OPPORTUNITY_REVISIT_ROWS = 3
MAX_RESEARCH_STATE_ROWS = 4
MAX_WORKER_DISPOSITION_ROWS = 8
MAX_QUESTION_HINTS = 12
LEARNING_STAGE_IDS = ("learning_audit", "meta_research", "self_improvement")


def _fill(instruction: str) -> str:
    return f"<FILL: {instruction}>"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _expected_slot_rule() -> str:
    return (
        "slot derived from started_at: let anchor = schedule contract "
        "anchor_at, cadence = contract cadence_minutes (minutes), grace = "
        "contract grace_minutes (minutes). If started_at < anchor: "
        "expected_slot = anchor (only valid if anchor - started_at <= "
        "grace). Otherwise: slot_number = floor((started_at - anchor) / "
        "cadence); preceding = anchor + slot_number * cadence; following = "
        "preceding + cadence; expected_slot = following if "
        "(following - started_at) <= grace else preceding. This must be the "
        "cadence-aligned instant nearest started_at within grace, not a "
        "copy of any prior candidate's expected_slot."
    )


def _schedule_context_template(
    profile_root: Path | str | None,
) -> dict[str, Any] | None:
    if profile_root is None:
        return None
    try:
        contract = load_schedule_contract(profile_root)
    except (OSError, ValueError):
        return None
    if not contract or contract.get("enabled") is not True:
        return None
    return {
        "schema_version": 2,
        "task_id": contract["task_id"],
        "platform_run_id": None,
        "platform_run_id_status": "unavailable",
        "expected_slot": _fill(_expected_slot_rule()),
        "started_at": _fill(
            "this cycle's actual UTC start timestamp, ISO 8601 with "
            "offset"
        ),
        "source_observed_at": _fill(
            "UTC timestamp your primary source (e.g. the IBKR snapshot) "
            "was observed at, ISO 8601 with offset"
        ),
        "trigger": _fill(f"one of {sorted(TRIGGERS)}"),
        "intervention": _fill(f"one of {sorted(INTERVENTIONS)}"),
    }


def _worker_disposition_rows(
    research_inbox: Mapping[str, Any] | None,
) -> tuple[str | None, list[dict[str, Any]], int]:
    if not isinstance(research_inbox, Mapping):
        return None, [], 0
    projection_id = research_inbox.get("projection_id")
    required_ids = [
        _text(value)
        for value in research_inbox.get("adoption_required_record_ids") or ()
        if _text(value)
    ]
    truncated = max(0, len(required_ids) - MAX_WORKER_DISPOSITION_ROWS)
    rows = [
        {
            "worker_record_id": worker_record_id,
            "disposition": _fill("see worker_research_dispositions_legend"),
            "evidence": [_fill("see legend; [] if not used_as_lead")],
            "rationale": _fill("1-2 sentence rationale"),
            "revisit_condition": _fill("see legend; null unless deferred"),
        }
        for worker_record_id in required_ids[:MAX_WORKER_DISPOSITION_ROWS]
    ]
    return (
        _text(projection_id) or None,
        rows,
        truncated,
    )


def _worker_research_dispositions_legend() -> dict[str, Any]:
    return {
        "disposition": (
            "one of " + str(sorted(WORKER_RESEARCH_DISPOSITIONS))
        ),
        "evidence": (
            "list of stage:<id>/finding:<id> refs from THIS cycle's "
            "cognitive_stages/findings; required (non-empty) when "
            "disposition is used_as_lead, must be [] otherwise"
        ),
        "revisit_condition": (
            "condition text (<=600 chars) when disposition is deferred; "
            "must be null for every other disposition"
        ),
        "note": (
            "one row per id in worker_research_projection_id's "
            "adoption_required_record_ids -- every listed id needs a row "
            "or the cycle is refused (worker_research_disposition_missing)"
        ),
    }


def _stable_state_rows(
    rows: Any,
    *,
    text_field: str,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(rows, list):
        return [], 0
    usable = [row for row in rows if isinstance(row, Mapping)]
    truncated = max(0, len(usable) - limit)
    return [
        {
            "id": row.get("id"),
            text_field: row.get(text_field),
            "status": row.get("status"),
        }
        for row in usable[:limit]
    ], truncated


def _active_review_trigger_ids(review_triggers: Any) -> list[str]:
    if not isinstance(review_triggers, list):
        return []
    return [
        _text(row.get("id"))
        for row in review_triggers
        if isinstance(row, Mapping)
        and _text(row.get("status")).lower() == "active"
        and _text(row.get("id"))
    ]


def _opportunity_revisit_rows(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    current, _identities = _current_state(records)
    open_opportunities = [
        payload for payload in current.values()
        if _text(payload.get("to_state")) not in TERMINAL_STATES
    ]
    open_opportunities.sort(
        key=lambda payload: (
            _text(payload.get("observed_at")),
            _text(payload.get("opportunity_id")),
        ),
    )
    truncated = max(0, len(open_opportunities) - MAX_OPPORTUNITY_REVISIT_ROWS)
    rows = []
    for payload in open_opportunities[:MAX_OPPORTUNITY_REVISIT_ROWS]:
        opportunity_id = payload.get("opportunity_id")
        from_state = payload.get("to_state")
        research_state = payload.get("research_state")
        research_state = (
            research_state if isinstance(research_state, Mapping) else {}
        )
        missing, missing_truncated = _stable_state_rows(
            research_state.get("missing_information"),
            text_field="question",
            limit=MAX_RESEARCH_STATE_ROWS,
        )
        uncertainties, uncertainties_truncated = _stable_state_rows(
            research_state.get("uncertainties"),
            text_field="description",
            limit=MAX_RESEARCH_STATE_ROWS,
        )
        triggers, triggers_truncated = _stable_state_rows(
            research_state.get("review_triggers"),
            text_field="condition",
            limit=MAX_RESEARCH_STATE_ROWS,
        )
        next_question_id = _text(research_state.get("next_question_id"))
        active_trigger_ids = _active_review_trigger_ids(
            research_state.get("review_triggers")
        )
        allowed_next_states = sorted(
            TRANSITIONS.get(_text(from_state), set())
        )
        rows.append({
            "opportunity_id": opportunity_id,
            "identity": payload.get("identity"),
            "from_state": from_state,
            "to_state": _fill(
                f"from_state ({from_state!r}) to stay a revisit, else one "
                f"of {allowed_next_states}; see opportunity_updates_legend"
            ),
            "rationale": _fill("why this update is made this cycle"),
            "evidence": [_fill("stage:<id>/finding:<id> from THIS cycle")],
            "research_state": {
                "missing_information": (
                    missing
                    + ([{
                        "_truncated_rows_not_shown": missing_truncated,
                    }] if missing_truncated else [])
                ),
                "uncertainties": (
                    uncertainties
                    + ([{
                        "_truncated_rows_not_shown": uncertainties_truncated,
                    }] if uncertainties_truncated else [])
                ),
                "review_triggers": (
                    triggers
                    + ([{
                        "_truncated_rows_not_shown": triggers_truncated,
                    }] if triggers_truncated else [])
                ),
                "next_question_id": next_question_id or None,
            },
            "revisit": {
                "trigger_id": (
                    _fill(f"one active id from {active_trigger_ids}")
                    if active_trigger_ids
                    else _fill(
                        "no active review_triggers; add/reactivate one "
                        "this cycle before revisiting"
                    )
                ),
                "target_missing_information_id": (
                    next_question_id
                    or _fill(
                        "no committed next_question_id; only set if "
                        "retargeting with a retarget_reason"
                    )
                ),
                "expected_information_gain": _fill(
                    "what evidence would resolve the target question"
                ),
                "result": _fill(f"one of {sorted(REVISIT_RESULTS)}"),
                "result_summary": _fill("<=600 chars: what this found"),
                "evidence": [
                    _fill("stage:<id>/finding:<id> from THIS cycle"),
                ],
                "legacy_state_initialization": False,
                "retarget_reason": None,
            },
        })
    return rows, truncated


def _opportunity_updates_legend() -> dict[str, Any]:
    return {
        "research_state": (
            "id/question/description/condition are copied verbatim from "
            "the ledger; they are stable and must not be reworded. "
            "'status' may change to 'resolved'/'retired' but the text "
            "fields must not."
        ),
        "revisit": (
            "required (non-null) only when to_state stays equal to "
            "from_state -- a pure revisit with no state change; may be "
            "omitted (set the whole key to null) when to_state changes"
        ),
        "to_state": (
            "must equal from_state for a pure revisit (then 'revisit' is "
            "required), or a state reachable per the opportunity lifecycle "
            "transitions; each row already lists its own allowed values"
        ),
    }


def _question_hint_pairs(
    pairs: set[tuple[str, str]],
) -> tuple[list[dict[str, str]], int]:
    ordered = sorted(pairs)
    truncated = max(0, len(ordered) - MAX_QUESTION_HINTS)
    return [
        {"opportunity_id": opportunity_id, "question_id": question_id}
        for opportunity_id, question_id in ordered[:MAX_QUESTION_HINTS]
    ], truncated


def _decision_repetition_hint(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    selection = latest_prior_finalized_decision_selection(
        records,
        current_cycle_id="",
    )
    committed, committed_truncated = _question_hint_pairs(
        _committed_question_pairs(records)
    )
    open_pairs, open_truncated = _question_hint_pairs(
        _open_question_pairs(records)
    )
    return {
        "prior_finalized_cycle_id": selection["latest_cycle_id"],
        "prior_finalized_decision_status": (
            selection["latest_decision_status"]
        ),
        "note": (
            "Last FINALIZED cycle (not last accepted/attempted). If "
            "decision.status == prior_finalized_decision_status, "
            "decision.repetition_review is required with prior_cycle_id "
            "== prior_finalized_cycle_id exactly, else "
            "decision_repetition_prior_cycle_mismatch. committed_questions "
            "= (opportunity_id, question_id) already promised as "
            "next_question_id; open_questions = every open question. A "
            "deliberate_wait review lists unresolved ids from "
            "open_questions."
        ),
        "committed_questions": committed,
        "committed_questions_not_shown": committed_truncated,
        "open_questions": open_pairs,
        "open_questions_not_shown": open_truncated,
    }


def _carry_forward_eligibility(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    for field, limit in CARRY_FORWARD_LIMITS.items():
        if field not in CARRY_FORWARD_STAGE_IDS:
            continue
        prior = _latest_finalized_stage_field(records, field=field)
        if prior is None:
            hints[field] = {
                "eligible": False,
                "reason": "no prior finalized value exists to carry",
            }
            continue
        _value, source_cycle_id, count = prior
        next_count = count + 1
        hints[field] = {
            "eligible": next_count <= limit,
            "source_cycle_id": source_cycle_id,
            "prior_carry_count": count,
            "would_become_count": next_count,
            "limit": limit,
        }
    hints["_note"] = (
        "Add a field name here to 'unchanged_from_prior' only if its "
        "'eligible' is true and its content is genuinely unchanged this "
        "cycle. The runtime substitutes the prior finalized value; do not "
        "also supply that field yourself."
    )
    return hints


def _market_sessions_template(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    from .market_sessions import latest_market_sessions

    latest = latest_market_sessions(records)
    markets = latest.get("markets") if isinstance(latest, Mapping) else None
    if not isinstance(markets, list) or not markets:
        return None
    template_markets = []
    for market in markets:
        if not isinstance(market, Mapping):
            continue
        template_markets.append({
            "region": market.get("region"),
            "venue": market.get("venue"),
            "timezone": market.get("timezone"),
            "status": _fill(
                "one of open|closed|pre_market|post_market|holiday|"
                "auction|unknown, from a fresh calendar check this cycle"
            ),
            "is_open": _fill("boolean matching status above"),
            "next_open": _fill("ISO 8601 timestamp, strictly future"),
            "next_close": _fill("ISO 8601 timestamp, strictly future"),
            "evidence": [_fill(
                "{tool, result} from a calendar/clock check made THIS "
                "cycle; local_time is derived by the builder, omit it"
            )],
        })
    return {
        "observed_at": _fill(
            "UTC timestamp this cycle's session evidence was observed at"
        ),
        "markets": template_markets,
        "overlap": _fill(
            "one of both_open|eu_only|us_only|none_open, consistent with "
            "is_open above"
        ),
        "_note": (
            "region/venue/timezone are carried from the last accepted "
            "market_sessions payload; local_time is a pure function of "
            "observed_at and timezone and is filled in by the builder, "
            "never supply it yourself."
        ),
    }


def build_next_candidate_template(
    records: Sequence[Mapping[str, Any]],
    *,
    research_inbox: Mapping[str, Any] | None = None,
    profile_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return a deterministic, journal-derived skeleton for the next candidate.

    Deterministic given ``records``/``research_inbox``/``profile_root``: no
    wall-clock reads. Every value the runtime cannot derive mechanically is a
    ``"<FILL: ...>"`` placeholder string naming the exact instruction.
    """
    projection_id, worker_rows, worker_truncated = _worker_disposition_rows(
        research_inbox
    )
    opportunity_rows, opportunity_truncated = _opportunity_revisit_rows(
        records
    )
    template: dict[str, Any] = {
        "instructions": (
            "Copy this as the start of your NEXT candidate. Replace every "
            "\"<FILL: ...>\" placeholder per its instruction. Do NOT copy "
            "values from your own previous candidate for any field present "
            "here -- this already reflects current ledger/schedule/worker "
            "state, and a stale copy is the top cause of refusal. Fields "
            "not present here (source, as_of, snapshot, findings, "
            "decision, research, market_scout_report unless carried "
            "forward, stage_outputs, evidence_calls, research_agenda) need "
            "your own reasoning and are intentionally omitted."
        ),
        "semantic_input_schema_version": SEMANTIC_INPUT_SCHEMA_VERSION,
        "cycle_id": _fill(
            "cycle-<UTC compact timestamp of this cycle's actual start, "
            "YYYYMMDDTHHMMSSZ>-<short unique suffix>; must be unique, "
            "never reused from a refused or corrected attempt"
        ),
        "corrects_candidate_id": None,
        "unchanged_from_prior": [],
        "unchanged_from_prior_eligibility": _carry_forward_eligibility(
            records
        ),
        "worker_research_projection_id": projection_id,
        "worker_research_dispositions": worker_rows,
        "worker_research_dispositions_not_shown": worker_truncated,
        "worker_research_dispositions_legend": (
            _worker_research_dispositions_legend()
        ),
        "schedule_context": _schedule_context_template(profile_root),
        "opportunity_updates": opportunity_rows,
        "opportunity_updates_not_shown": opportunity_truncated,
        "opportunity_updates_legend": _opportunity_updates_legend(),
        "decision_repetition": _decision_repetition_hint(records),
        "market_sessions": _market_sessions_template(records),
        "learning_stage_dispositions": [
            {
                "stage_id": stage_id,
                "disposition": _fill(
                    "one of no_change|... per LLM_HOST_CONTRACT.md"
                ),
                "rationale": _fill(
                    "why this disposition for this learning stage"
                ),
                "evidence": [_fill("stage:<id> or finding:<id> ref")],
            }
            for stage_id in LEARNING_STAGE_IDS
        ],
    }
    return template
