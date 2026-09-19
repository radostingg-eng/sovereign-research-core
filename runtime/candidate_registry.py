"""Durable registry of Market Scout candidates the host did not select.

`market_scout_summary()` in `market_scout.py` deliberately projects only the
latest scout pass. That leaves a gap: a candidate proposed in one cycle and
not selected has no durable trace once a later cycle runs, so the same
instrument and thesis can be re-proposed as if it were a fresh discovery.

This module is descriptive, plus one narrow fail-closed rule. It does not
rank, score, or recommend action on a candidate -- the host remains free to
re-propose, select, reject, or ignore any of it. It only refuses a candidate
that omits acknowledgment of a durable prior proposal of the exact same
identity when the journal already proves that proposal happened.

Only receipted cycles count. A crash after a Market Scout stage is journaled
but before the cycle receipt would otherwise let an unexecuted partial cycle
force acknowledgment of something that never actually ran.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .opportunity_ledger import identity_fingerprint, opportunity_ledger_summary

MAX_REGISTRY_FEEDBACK_ITEMS = 12


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _receipted_cycle_ids(records: Sequence[Mapping[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for record in records:
        if record.get("record_type") != "cycle_receipt":
            continue
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            cycle_id = _text(payload.get("cycle_id"))
            if cycle_id:
                ids.add(cycle_id)
    return ids


def _scout_stage_payloads(
    records: Sequence[Mapping[str, Any]],
    *,
    receipted: set[str],
) -> list[Mapping[str, Any]]:
    """Every market_scout cycle_stage payload from a receipted cycle."""
    rows = []
    for record in records:
        if record.get("record_type") != "cycle_stage":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("agent_id") != "market_scout":
            continue
        cycle_id = _text(payload.get("cycle_id"))
        if not cycle_id or cycle_id not in receipted:
            continue
        output = payload.get("output")
        output = output if isinstance(output, Mapping) else {}
        carry_forward = output.get("carry_forward")
        if (
            isinstance(carry_forward, Mapping)
            and "market_scout_report" in carry_forward
        ):
            continue
        rows.append(payload)
    return rows


def _selected_scout_ids(
    records: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
) -> set[str]:
    """scout_candidate_id values selected by research_director in one cycle."""
    selected: set[str] = set()
    for record in records:
        if record.get("record_type") != "cycle_stage":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("agent_id") != "research_director":
            continue
        if _text(payload.get("cycle_id")) != cycle_id:
            continue
        output = payload.get("output")
        output = output if isinstance(output, Mapping) else {}
        carry_forward = output.get("carry_forward")
        if (
            isinstance(carry_forward, Mapping)
            and "research_agenda" in carry_forward
        ):
            continue
        agenda = output.get("research_agenda")
        agenda = agenda if isinstance(agenda, Mapping) else {}
        agenda_candidates = agenda.get("candidates")
        if isinstance(agenda_candidates, list):
            for row in agenda_candidates:
                if isinstance(row, Mapping) and row.get("selected") is True:
                    scout_id = row.get("scout_candidate_id")
                    if isinstance(scout_id, str):
                        selected.add(scout_id)
    return selected


def occurrences(
    records: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], str]:
    """Map every (cycle_id, candidate_id) occurrence to its identity
    fingerprint, from receipted cycles only. This is the resolution table a
    `rediscovery_of` reference is checked against."""
    receipted = _receipted_cycle_ids(records)
    result: dict[tuple[str, str], str] = {}
    for stage in _scout_stage_payloads(records, receipted=receipted):
        cycle_id = _text(stage.get("cycle_id"))
        output = stage.get("output")
        output = output if isinstance(output, Mapping) else {}
        report = output.get("market_scout_report")
        report = report if isinstance(report, Mapping) else {}
        candidates = report.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            candidate_id = _text(candidate.get("candidate_id"))
            identity = candidate.get("identity")
            if not candidate_id or not isinstance(identity, Mapping):
                continue
            try:
                fingerprint = identity_fingerprint(identity)
            except ValueError:
                continue
            result[(cycle_id, candidate_id)] = fingerprint
    return result


def candidate_registry(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Durable index of every exact candidate identity ever proposed in a
    receipted cycle, keyed by identity_fingerprint.

    `times_proposed` counts distinct receipted cycles proposing this exact
    identity. `times_selected` counts cycles where the research_director
    agenda selected that same-cycle scout candidate -- selection is not the
    same fact as an opportunity existing; a selected candidate can receive
    specialist work without ever producing an opportunity_update.
    `matching_opportunity_ids` is a same-identity ledger match, not causal
    proof that a specific scout row produced that opportunity.
    """
    receipted = _receipted_cycle_ids(records)
    ledger = opportunity_ledger_summary(records, limit=10**9)
    ledger_owners: dict[str, list[str]] = {}
    for item in ledger.get("items") or ():
        if not isinstance(item, Mapping):
            continue
        fingerprint = _text(item.get("identity_fingerprint"))
        opportunity_id = _text(item.get("opportunity_id"))
        if fingerprint and opportunity_id:
            ledger_owners.setdefault(fingerprint, []).append(opportunity_id)

    registry: dict[str, dict[str, Any]] = {}
    seen_cycles: dict[str, set[str]] = {}
    for stage in _scout_stage_payloads(records, receipted=receipted):
        cycle_id = _text(stage.get("cycle_id"))
        output = stage.get("output")
        output = output if isinstance(output, Mapping) else {}
        report = output.get("market_scout_report")
        report = report if isinstance(report, Mapping) else {}
        candidates = report.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        selected_ids = _selected_scout_ids(records, cycle_id=cycle_id)
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            candidate_id = _text(candidate.get("candidate_id"))
            identity = candidate.get("identity")
            if not isinstance(identity, Mapping):
                continue
            try:
                fingerprint = identity_fingerprint(identity)
            except ValueError:
                continue
            entry = registry.get(fingerprint)
            if entry is None:
                entry = {
                    "identity_fingerprint": fingerprint,
                    "first_seen_cycle_id": cycle_id,
                    "first_seen_candidate_id": candidate_id,
                    "last_seen_cycle_id": cycle_id,
                    "last_seen_candidate_id": candidate_id,
                    "times_proposed": 0,
                    "times_selected": 0,
                }
                registry[fingerprint] = entry
                seen_cycles[fingerprint] = set()
            if cycle_id not in seen_cycles[fingerprint]:
                entry["times_proposed"] += 1
                seen_cycles[fingerprint].add(cycle_id)
            entry["last_seen_cycle_id"] = cycle_id
            entry["last_seen_candidate_id"] = candidate_id
            if candidate_id in selected_ids:
                entry["times_selected"] += 1
    for fingerprint, entry in registry.items():
        entry["matching_opportunity_ids"] = sorted(
            ledger_owners.get(fingerprint, [])
        )
    return registry


def unpromoted_candidate_registry(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Registry entries that never matched an opportunity-ledger identity."""
    return {
        fingerprint: entry
        for fingerprint, entry in candidate_registry(records).items()
        if not entry["matching_opportunity_ids"]
    }


def candidate_registry_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = MAX_REGISTRY_FEEDBACK_ITEMS,
) -> dict[str, Any]:
    """Descriptive-only projection for FEEDBACK.json."""
    unpromoted = unpromoted_candidate_registry(records)
    ordered = sorted(
        unpromoted.values(),
        key=lambda entry: (
            entry["last_seen_cycle_id"], entry["identity_fingerprint"],
        ),
        reverse=True,
    )
    items = ordered[:limit]
    return {
        "available": bool(ordered),
        "unpromoted_total": len(ordered),
        "items": items,
        "not_shown": max(0, len(ordered) - len(items)),
        "what_this_means": (
            "Historical Market Scout proposal evidence only, from receipted "
            "cycles. The runtime does not rank, select, or recommend any "
            "candidate. A new candidate whose exact identity matches an "
            "unpromoted row here must include rediscovery_of naming the "
            "cycle_id and candidate_id shown."
        ),
    }


def validate_rediscovery_candidates(
    data: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
    enforce: bool,
    cycle_id: str,
) -> list[str]:
    """Refuse a market_scout candidate that omits a required rediscovery
    reference, supplies one that does not resolve to the exact same
    identity, or supplies one when no prior unpromoted occurrence exists.

    `cycle_id` must be the same effective cycle_id persistence will use
    (`data.get("cycle_id") or f"cycle-{path.stem}"`), passed in by the
    caller rather than re-derived here, since this module has no path.

    Only enforced for newly staged canonical inputs (`enforce=True`); never
    retroactively refuses historical or replayed cycles, matching the same
    `require_market_scout` boundary the rest of Market Scout validation uses.
    """
    if not enforce:
        return []
    from .market_scout import market_scout_report

    report = market_scout_report(data)
    if not isinstance(report, Mapping):
        return []
    candidates = report.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    if not candidates:
        return []

    current_cycle_id = _text(cycle_id)
    unpromoted = unpromoted_candidate_registry(records)
    occurrence_fingerprints = occurrences(records)

    errors: list[str] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            continue
        identity = candidate.get("identity")
        if not isinstance(identity, Mapping):
            continue
        try:
            fingerprint = identity_fingerprint(identity)
        except ValueError:
            continue
        prior = unpromoted.get(fingerprint)
        is_rediscovery = (
            prior is not None
            and prior["last_seen_cycle_id"] != current_cycle_id
        )
        reference = candidate.get("rediscovery_of")
        if not is_rediscovery:
            if reference is not None:
                errors.append(
                    "market_scout_candidate_rediscovery_invalid:"
                    f"{index}:not_applicable"
                )
            continue
        if not isinstance(reference, Mapping) or set(reference) != {
            "cycle_id", "candidate_id",
        }:
            errors.append(
                "market_scout_candidate_rediscovery_invalid:"
                f"{index}:required:{prior['last_seen_cycle_id']}:"
                f"{prior['last_seen_candidate_id']}"
            )
            continue
        ref_cycle = _text(reference.get("cycle_id"))
        ref_candidate = _text(reference.get("candidate_id"))
        resolved = occurrence_fingerprints.get((ref_cycle, ref_candidate))
        if (
            resolved != fingerprint
            or ref_cycle == current_cycle_id
        ):
            errors.append(
                "market_scout_candidate_rediscovery_invalid:"
                f"{index}:unresolvable"
            )
    return errors
