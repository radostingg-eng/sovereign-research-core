"""Which strategy families the host has actually used, and which it has not.

Nine cycles asked twenty-seven distinct research questions and reached the
same conclusion three times: trim the largest holding. The questions were
varied; the
answer was not. It was not repetition of a question, it was confinement to a
single strategy class -- outright equity, long only, one instrument.

STRATEGY_FAMILIES.md documents eighteen families, and the host had touched
almost none of them. Not because they are forbidden, but because nothing ever
asked. The standing prompt requested "the highest-conviction risk-reducing
action", which is answered by trimming the largest position every single time
a portfolio has a largest position.

So this reports coverage: what has been drawn on, what has not. It does not
rank families, require a rotation, or suggest what to look at next. Which
family fits today's evidence is exactly the judgement the host exists to make,
and a runtime that nudged it toward the unused ones would be choosing strategy
by novelty, which is worse than choosing it by habit.

It only makes the habit visible.
"""

from __future__ import annotations

import re

from typing import Any, Mapping, Sequence

from .research_allocation import (
    ALLOCATION_CATEGORIES,
    derived_research_allocation_coverage,
    derived_research_allocation_usage,
    primary_allocation_category,
    primary_allocation_reference,
)
from .timestamps import effective_as_of, parse_iso_timestamp

MAX_RESEARCH_AGENDA_CANDIDATES = 6
MAX_RESEARCH_AGENDA_TEXT_CHARS = 280
MAX_RESEARCH_ALLOCATION_FACTOR_CHARS = 160

# From STRATEGY_FAMILIES.md, which calls them discovery anchors rather than a
# closed whitelist. The host may invent families outside this list; an
# unrecognised one is reported as used, not rejected.
# Mechanism and the evidence each needs, from STRATEGY_FAMILIES.md. Carried
# here rather than left in the document because a list of bare names is not
# something you can choose from: the host saw seventeen labels and no way to
# tell which its current evidence could actually support.
FAMILY_MECHANISMS: dict[str, str] = {
    "deep_value_fcf":
        "price below normalized cash generation; needs statements, valuation history, filings",
    "quality_at_discount":
        "temporary compression without durable impairment; needs margins, ROIC, balance sheet",
    "garp":
        "growth exceeds what valuation implies; needs growth, estimates, unit economics",
    "special_situations":
        "corporate action creates non-market supply/demand; needs filings, corp actions",
    "event_driven":
        "a catalyst changes the information or cash-flow path; needs the primary event source, timeline, scenarios",
    "relative_value":
        "linked assets diverge beyond a justified range; needs paired fundamentals and a normalized spread",
    "volatility_options":
        "implied/realized mismatch, or skew and term-structure edge; needs the option chain, IV, realized vol, OI, liquidity",
    "hedging":
        "reduce tail risk cheaply; needs correlation, beta, stress scenarios, hedge cost",
    "factor_macro":
        "a regime creates systematic mispricing or protection; needs macro series, factor exposure, regime history",
    "insider_ownership":
        "informed ownership change is informative; needs Form 4 and ownership filings, and persistence",
    "institutional_13f":
        "capital allocation changes that persist; needs holdings and filing changes",
    "buybacks_capital_return":
        "a shrinking share count or changed distribution alters value; needs filings and buyback execution",
    "restructuring_turnaround":
        "self-help changes earnings power; needs debt, segments, guidance, operating metrics",
    "sentiment_dislocation":
        "crowding becomes mispriced against fundamentals; needs Stocktwits plus INDEPENDENT fundamentals",
    "momentum_mean_reversion":
        "price behaviour carries incremental information; needs clean history and regime controls",
    "source_disagreement":
        "credible sources disagree exploitably; needs timestamped multi-source observation",
    "instrument_substitution":
        "the same thesis expressed better by another instrument; needs shares vs options vs spreads vs ETF economics",
}

KNOWN_FAMILIES: tuple[str, ...] = tuple(FAMILY_MECHANISMS)


def _normalise(value: Any) -> str:
    """Fold spelling variants onto one key.

    The host wrote "source-disagreement" and "volatility-options" while the
    registry keys on underscores, so families it had genuinely used were
    reported as never used, and it would have been told to explore what it
    had just explored. Hyphens, spaces and case are all the same family.
    """
    text = str(value or "").strip().lower()
    return re.sub(r"[\s-]+", "_", text)


def families_used(inputs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many cycles drew on each family, counting each cycle once."""
    counts: dict[str, int] = {}
    for data in inputs:
        seen = set()
        for row in data.get("research") or ():
            if not isinstance(row, Mapping):
                continue
            family = _normalise(row.get("strategy_family"))
            if family:
                seen.add(family)
        decision = data.get("decision")
        if isinstance(decision, Mapping):
            family = _normalise(decision.get("strategy_family"))
            if family:
                seen.add(family)
        for family in seen:
            counts[family] = counts.get(family, 0) + 1
    return counts


def coverage(inputs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The block the host reads before choosing what to research."""
    used = families_used(inputs)
    untouched = [f for f in KNOWN_FAMILIES if f not in used]
    return {
        "cycles_examined": len(inputs),
        "families_used": used,
        "families_never_used": {f: FAMILY_MECHANISMS[f] for f in untouched},
        "what_this_means": (
            "Record a strategy_family on each research row and on the "
            "decision, drawn from STRATEGY_FAMILIES.md or invented where the "
            "mechanism is plausible. This is coverage, not a target: a family "
            "is not worth using because it is unused, and trimming the "
            "largest position may genuinely be right again today. But if "
            "every cycle reaches for the same one, that is a habit rather "
            "than a conclusion, and this is where it becomes visible."
        ),
    }


def load_recent_inputs(input_dir: Any, limit: int = 20) -> list[dict[str, Any]]:
    """Recent committed inputs, skipping any that no longer parse."""
    import json
    from pathlib import Path

    directory = Path(input_dir)
    if not directory.exists():
        return []
    paths = sorted((p for p in directory.glob("*.json")
                    if not p.name.startswith(".") and p.name != "FEEDBACK.json"),
                   key=lambda p: p.name)[-limit:]
    loaded = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A refused input is still evidence of what was attempted, but an
            # unparseable one cannot be read at all. Skipped rather than
            # guessed at.
            continue
        if isinstance(data, dict):
            loaded.append(data)
    return loaded


def load_accepted_inputs(
    input_dir: Any,
    records: Sequence[Mapping[str, Any]],
    limit: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recent inputs with a valid completed-or-blocked causal receipt."""
    from pathlib import Path

    from .accepted_inputs import feedback_snapshot_ids, input_fingerprint

    accepted_ids = feedback_snapshot_ids(records)
    accepted_stems = {
        snapshot_id.rsplit(":", 1)[0]
        for snapshot_id in accepted_ids
        if ":" in snapshot_id
    }
    selected = []
    excluded = []
    paths = sorted(
        path for path in Path(input_dir).glob("*.json")
        if not path.name.startswith(".") and path.name != "FEEDBACK.json"
    )
    for path in paths:
        if path.stem not in accepted_stems:
            excluded.append(path.name)
            continue
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            excluded.append(path.name)
            continue
        if not isinstance(data, dict):
            excluded.append(path.name)
            continue
        snapshot_id = f"{path.stem}:{input_fingerprint(data)}"
        if snapshot_id not in accepted_ids:
            excluded.append(path.name)
            continue
        snapshot = data.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        observed_at = effective_as_of(data)
        parsed = parse_iso_timestamp(str(observed_at or ""))
        selected.append((
            parsed.timestamp() if parsed is not None else float("-inf"),
            path.name,
            data,
        ))
    selected.sort(key=lambda row: (row[0], row[1]))
    selected = selected[-limit:]
    return (
        [data for _, _, data in selected],
        {
            "inputs_considered": [name for _, name, _ in selected],
            "inputs_excluded_without_valid_receipt": excluded[-limit:],
            "eligible_receipt_statuses": ["completed", "blocked"],
            "what_this_means": (
                "Only inputs causally matched to a valid completed or blocked "
                "cycle receipt can shape recent reasoning. Refused, malformed, "
                "pending, or rewritten inputs are listed but excluded."
            ),
        },
    )


# From SOURCE_MANIFEST.json. Installed and reachable, whether or not the host
# has ever called them. Four of these had zero calls across ten cycles while
# IBKR had forty-three, which is why every conclusion was an outright equity
# one: a family can only be reached through evidence that reaches it.
INSTALLED_SOURCES: dict[str, str] = {
    "ibkr": "portfolio authority: positions, balances, orders, performance",
    "longbridge": "fundamentals, valuation, filings, holdings, short options, news",
    "alpaca": "independent US market and options crosscheck",
    "next stock": "independent model and ranking context",
    "stocktwits": "sentiment and crowding only, never a fundamental claim",
    "github": "persistent memory and versioning",
}


def sources_used(inputs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many cycles called each source, counting each cycle once."""
    counts: dict[str, int] = {}
    for data in inputs:
        seen = set()
        for row in data.get("research") or ():
            if not isinstance(row, Mapping):
                continue
            for call in row.get("tool_calls") or ():
                if not isinstance(call, Mapping):
                    continue
                tool = str(call.get("tool", "") or "").strip().lower()
                for known in INSTALLED_SOURCES:
                    if known in tool:
                        seen.add(known)
                        break
                else:
                    if tool:
                        seen.add(tool)
        for source in seen:
            counts[source] = counts.get(source, 0) + 1
    return counts


def source_coverage(inputs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Which installed evidence sources have gone unused."""
    used = sources_used(inputs)
    never = {name: purpose for name, purpose in INSTALLED_SOURCES.items()
             if name not in used}
    return {
        "sources_used": used,
        "installed_but_never_used": never,
        "what_this_means": (
            "These are installed and reachable. A strategy family can only be "
            "reached through evidence that reaches it, so a cycle that only "
            "calls the portfolio API will only ever conclude something about "
            "the portfolio. This is coverage, not a quota: calling a source "
            "to have called it is worse than not calling it. But if a family "
            "looks unreachable, check whether its evidence was simply never "
            "requested."
        ),
    }


def recent_reasoning(inputs: Sequence[Mapping[str, Any]],
                     limit: int = 3) -> dict[str, Any]:
    """What the host recently concluded, so it can build rather than repeat.

    Every cycle started cold. The host re-derived its own concentration on
    each run and had no record of what it had already considered and
    set aside, so "I looked at the short-option book last cycle and found
    nothing actionable" was not available to it. Three cycles reached the
    same conclusion partly because none of them knew the others had.

    ACTIVE_BRAIN.md was designed as this surface and nothing ever wrote to
    it. This is the bounded version of the same idea, and it keeps that
    file's admission rule: only what can materially affect the current
    decision, never a transcript.

    Questions are included without their findings on purpose. The host should
    see WHAT it asked, so it can go somewhere new or deliberately revisit,
    without being handed its previous answer to agree with.
    """
    rows = []
    for data in inputs[-limit:]:
        decision = data.get("decision")
        decision = decision if isinstance(decision, Mapping) else {}
        questions = [str(r.get("question", ""))[:110]
                     for r in data.get("research") or ()
                     if isinstance(r, Mapping) and r.get("question")]
        rows.append({
            "cycle_id": data.get("cycle_id"),
            "as_of": effective_as_of(data),
            "decision": decision.get("status"),
            "rationale": str(decision.get("rationale", ""))[:160] or None,
            "experiment": (
                dict(decision["experiment"])
                if isinstance(decision.get("experiment"), Mapping)
                else None
            ),
            "questions_asked": questions,
        })
    statuses = [
        str(data.get("decision", {}).get("status", "")).lower()
        for data in inputs
        if isinstance(data.get("decision"), Mapping)
        and str(data["decision"].get("status", "")).strip()
    ]
    trailing_status = statuses[-1] if statuses else None
    consecutive_same_status = 0
    for status in reversed(statuses):
        if status != trailing_status:
            break
        consecutive_same_status += 1
    return {
        "cycles": rows,
        "trailing_decision_status": trailing_status,
        "consecutive_same_status": consecutive_same_status,
        "what_this_means": (
            "Your own recent reasoning. Use it to avoid re-deriving what you "
            "already established and to notice when you are circling: if the "
            "same decision repeats, do not cite a host-owned missing stress "
            "budget, target exposure, decision threshold, or exit rule as an "
            "external blocker. Author a provisional falsifiable parameter, "
            "collect the missing evidence, or run a bounded experiment. WAIT "
            "is valid when nothing has changed, but repeating it does not "
            "resolve uncertainty. Do not treat a previous conclusion as "
            "settled because you reached it; the evidence may have moved and "
            "you are free to disagree with yourself, as long as you say so."
        ),
    }


def research_agenda_summary(
    inputs: Sequence[Mapping[str, Any]],
    limit: int = 5,
) -> dict[str, Any]:
    """Compact accepted agenda history, so repeated focus is visible."""
    cycles = []
    selection_counts: dict[str, int] = {}
    allocation_selection_counts = {
        category: 0 for category in ALLOCATION_CATEGORIES
    }
    allocation_cycles_examined = 0
    legacy_cycles_excluded = 0

    def bounded(value: Any, limit: int = MAX_RESEARCH_AGENDA_TEXT_CHARS) -> str:
        text = str(value).strip() if value is not None else ""
        if len(text) <= limit:
            return text
        return text[:limit - 3] + "..."

    for data in inputs[-limit:]:
        agenda = None
        for stage in data.get("cognitive_stages") or ():
            if (
                isinstance(stage, Mapping)
                and stage.get("stage_id") == "research_director"
            ):
                output = stage.get("output")
                if isinstance(output, Mapping):
                    candidate = output.get("research_agenda")
                    if isinstance(candidate, Mapping):
                        agenda = candidate
                break
        if agenda is None:
            continue
        allocation_enabled = isinstance(
            agenda.get("allocation_plan"), Mapping,
        )
        if allocation_enabled:
            allocation_cycles_examined += 1
        else:
            legacy_cycles_excluded += 1
        selected = []
        rejected = []
        candidates = [
            candidate
            for candidate in agenda.get("candidates") or ()
            if isinstance(candidate, Mapping)
        ]
        for candidate in candidates[:MAX_RESEARCH_AGENDA_CANDIDATES]:
            row = {
                "candidate_id": candidate.get("candidate_id"),
                "instrument": candidate.get("instrument"),
                "strategy_family": candidate.get("strategy_family"),
                "trigger": bounded(candidate.get("trigger")),
                "reason": bounded(
                    candidate.get("selection_reason")
                    if candidate.get("selected") is True
                    else candidate.get("rejection_reason")
                ),
            }
            if candidate.get("selected") is True:
                if allocation_enabled:
                    category = primary_allocation_category(candidate)
                    factors = candidate.get("allocation_factors")
                    factors = (
                        factors if isinstance(factors, Mapping) else {}
                    )
                    row.update({
                        "allocation_category": category,
                        "allocation_reference":
                            primary_allocation_reference(candidate),
                        "allocation_factors": {
                            field: bounded(
                                factors.get(field),
                                MAX_RESEARCH_ALLOCATION_FACTOR_CHARS,
                            )
                            for field in (
                                "novelty",
                                "portfolio_impact",
                                "missing_information",
                                "expected_information_gain",
                            )
                            if factors.get(field) is not None
                        },
                    })
                    allocation_selection_counts[category] += 1
                selected.append(row)
                key = (
                    f"{candidate.get('instrument')}|"
                    f"{candidate.get('strategy_family')}"
                )
                selection_counts[key] = selection_counts.get(key, 0) + 1
            else:
                rejected.append(row)
        cycles.append({
            "cycle_id": data.get("cycle_id"),
            "as_of": effective_as_of(data),
            "selected": selected,
            "rejected": rejected,
            "candidate_count": len(candidates),
            "not_shown": max(
                0,
                len(candidates)
                - len(selected)
                - len(rejected),
            ),
            "selection_rationale": bounded(
                agenda.get("selection_rationale")
            ),
            "allocation_contract": allocation_enabled,
            "allocation_plan": (
                agenda.get("allocation_plan")
                if allocation_enabled
                else None
            ),
            "allocation_usage": (
                derived_research_allocation_usage(data)
                if allocation_enabled
                else None
            ),
            "allocation_coverage": (
                derived_research_allocation_coverage(data)
                if allocation_enabled
                else None
            ),
            "allocation_variance": (
                agenda.get("allocation_variance")
                if allocation_enabled
                else None
            ),
        })
    return {
        "cycles_examined": len(cycles),
        "recent": cycles,
        "selection_counts": selection_counts,
        "allocation_selection_counts": allocation_selection_counts,
        "allocation_cycles_examined": allocation_cycles_examined,
        "legacy_cycles_excluded": legacy_cycles_excluded,
        "what_this_means": (
            "Historical research-director choices, not instructions for the "
            "next cycle. Allocation categories are derived from explicit "
            "links, not investment rankings. Compare planned ceilings, "
            "actual selected usage, considered coverage, and market-session "
            "context to notice repeated focus or unused research capacity."
        ),
    }


def open_experiments(
    inputs: Sequence[Mapping[str, Any]],
    limit: int = 5,
) -> dict[str, Any]:
    """Accepted experiment decisions that no later decision superseded."""
    superseded = {
        str(value)
        for data in inputs
        for value in (
            data.get("decision", {}).get("supersedes", ())
            if isinstance(data.get("decision"), Mapping)
            else ()
        )
    }
    open_rows = []
    for data in inputs:
        decision = data.get("decision")
        if not isinstance(decision, Mapping):
            continue
        experiment = decision.get("experiment")
        if (
            str(decision.get("status", "")).lower() != "experiment"
            or not isinstance(experiment, Mapping)
        ):
            continue
        experiment_id = str(
            data.get("cycle_id") or effective_as_of(data) or ""
        )
        if experiment_id in superseded:
            continue
        open_rows.append({
            "experiment_id": experiment_id,
            "as_of": effective_as_of(data),
            **dict(experiment),
        })
    open_rows = open_rows[-limit:]
    return {
        "count": len(open_rows),
        "open": open_rows,
        "what_this_means": (
            "These accepted experiments remain open until a later decision "
            "names their experiment_id in supersedes. When an evaluation "
            "window ends, report the measurement and counter-metric evidence "
            "or apply the rollback condition; do not restart the same "
            "experiment without resolving the prior one."
        ),
    }


def candidates_for(inputs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Research candidates in the families the HOST asked to explore.

    discovery.py has produced these since it was written and nothing ever
    called it. It generates a concrete candidate per instrument per family,
    each naming the evidence required to judge it -- which is the missing
    step between "you have used one family of seventeen" and actually working
    in another.

    It refuses to choose families itself, and that refusal is correct: the
    Research Director selects. So this runs only for the families the host
    named in its last cycle via "families_to_explore", and produces nothing
    when it named none. The runtime does not decide what to research; it does
    the enumeration the host cannot, because it cannot run code.
    """
    from .discovery import discover_candidates

    if not inputs:
        return {"candidates": [], "requested_families": []}
    latest = inputs[-1]
    families = [str(f).strip().lower().replace("-", "_")
                for f in (latest.get("families_to_explore") or ())
                if str(f).strip()]
    if not families:
        return {
            "candidates": [], "requested_families": [],
            "what_this_means": (
                "Name the families you want enumerated next cycle via "
                "\"families_to_explore\": [...]. The runtime will not choose "
                "them for you, and produces nothing until you do."
            ),
        }
    snapshot = latest.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    positions = snapshot.get("attention_positions") or snapshot.get("positions") or []
    universe = []
    if isinstance(positions, Sequence) and not isinstance(positions, str):
        for row in positions:
            if isinstance(row, Mapping) and row.get("symbol"):
                universe.append({"symbol": row["symbol"],
                                 "market_value": row.get("market_value"),
                                 "market_price": row.get("market_price")})
    if not universe:
        return {"candidates": [], "requested_families": families}
    try:
        found = discover_candidates(universe=universe, families=families)
    except (ValueError, TypeError) as error:
        return {"candidates": [], "requested_families": families,
                "error": f"{type(error).__name__}: {error}"}
    return {
        "candidates": [dict(c) for c in found][:24],
        "requested_families": families,
        "what_this_means": (
            "Candidates enumerated for the families you asked about, each "
            "with the evidence needed to judge it. They are starting points, "
            "not recommendations: none has been researched and most will not "
            "survive contact with evidence. Discard freely."
        ),
    }
