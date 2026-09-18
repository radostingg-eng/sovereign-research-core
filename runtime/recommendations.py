"""What the host has already recommended and nobody has resolved.

Three cycles in thirty minutes each recommended trimming the same position,
at three slightly different prices. Every one is still open. Nothing marked the
earlier ones superseded, because nothing knew they existed: the host reads
FEEDBACK.json at the start of each run and FEEDBACK.json said nothing about
what it had already proposed.

Left alone this produces twenty-four unresolved recommendations a day for one
trade, and an operator cannot tell which is current. Worse, it is the exact
shape of a system that looks busy while deciding nothing: each cycle is
individually reasonable and the sequence is noise.

The fix is not to suppress duplicates. A repriced recommendation an hour
later may be entirely correct, and silently dropping it would hide a real
change of mind. The fix is that the host must SEE its open recommendations
and say what it means to do about each: supersede it, reaffirm it, or
withdraw it. That is a judgement, so it belongs to the host; surfacing the
facts it needs to make it belongs here.

Nothing here decides anything. It reads the journal and reports.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .lifecycle import (
    RESOLVED_STATES, apply_from_state, event_from_mapping,
)



def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def open_recommendations(records: Sequence[Mapping[str, Any]]
                         ) -> list[dict[str, Any]]:
    """Recommendations with no later record resolving or superseding them.

    Superseding is recognised by a later recommendation naming an earlier
    cycle_id in ``supersedes``. Without that the host cannot express "this
    replaces the one I made an hour ago", and every cycle accumulates.
    """
    recommendations: dict[str, dict[str, Any]] = {}
    superseded: set[str] = set()
    lifecycle_events: dict[str, list[Any]] = {}
    reconciled: dict[str, str] = {}

    for record in records:
        payload = _payload(record)
        if record.get("record_type") == "cycle_receipt":
            if str(payload.get("decision_status")) != "recommended":
                continue
            cycle_id = str(payload.get("cycle_id", ""))
            if not cycle_id:
                continue
            # A staged instruction is a different obligation from a bare
            # recommendation: it exists in IBKR, the operator can see it, and
            # it can still be transmitted days later on a thesis that has
            # since broken. The host can DELETE those, so it needs to know
            # which ones are sitting there.
            staged = payload.get("instruction_staged")
            recommendations[cycle_id] = {
                "cycle_id": cycle_id,
                "at": payload.get("completed_at") or record.get("at"),
                "instruction": payload.get("instruction"),
                "staged_in_ibkr": bool(staged),
                "ibkr_instruction_id": payload.get("ibkr_instruction_id"),
            }
            for earlier in payload.get("supersedes") or ():
                superseded.add(str(earlier))
        elif record.get("record_type") == "order_instruction_event":
            recommendation_id = str(payload.get("cycle_id", ""))
            recommendation = recommendations.get(recommendation_id)
            if recommendation is None:
                continue
            operation = str(payload.get("operation", ""))
            if operation == "create" and payload.get("verified_present") is True:
                recommendation["staged_in_ibkr"] = True
                recommendation["ibkr_instruction_id"] = payload.get(
                    "instruction_id")
                recommendation["instruction"] = payload.get("instruction")
            elif operation == "delete" and payload.get("verified_absent") is True:
                recommendation["staged_in_ibkr"] = False
        elif record.get("record_type") == "lifecycle_event":
            recommendation_id = str(payload.get("recommendation_id", ""))
            try:
                event = event_from_mapping(
                    payload, caused_by=tuple(record.get("caused_by") or ()))
            except ValueError:
                continue
            lifecycle_events.setdefault(recommendation_id, []).append(event)
        elif record.get("record_type") == "instruction_reconciliation":
            recommendation_id = str(payload.get("recommendation_id", ""))
            status = str(payload.get("status", ""))
            if recommendation_id and status:
                reconciled[recommendation_id] = status

    resolved: set[str] = set()
    for recommendation_id, events in lifecycle_events.items():
        if recommendation_id not in recommendations:
            continue
        try:
            final_state = apply_from_state("recommended", events)
        except ValueError:
            continue
        if final_state in RESOLVED_STATES:
            resolved.add(recommendation_id)
    resolved.update(
        recommendation_id
        for recommendation_id, status in reconciled.items()
        if status in {
            "accepted_unchanged",
            "accepted_modified",
            "rejected",
            "deleted_saved_only",
        }
    )

    return [row for cycle_id, row in recommendations.items()
            if cycle_id not in superseded and cycle_id not in resolved]


def summarise(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The block the host reads at the start of its next cycle."""
    rows = open_recommendations(records)
    return {
        "count": len(rows),
        "open": rows,
        "what_this_means": (
            "These recommendations are still open: nothing records them as "
            "executed, rejected, expired or superseded. Before proposing "
            "another, say what happens to each one. Add \"supersedes\": "
            "[\"<cycle_id>\", ...] to your decision when a new recommendation "
            "replaces an earlier one. Reaffirming an unchanged recommendation "
            "is fine; producing a fourth without mentioning the first three "
            "is not, because an operator cannot then tell which is current. "
            "Any marked staged_in_ibkr exist as order instructions the "
            "operator can transmit: if one should no longer stand, DELETE "
            "the instruction rather than leaving it resting, and say you "
            "did."
        ) if rows else (
            "No open recommendations. A new one starts a fresh obligation."
        ),
    }
