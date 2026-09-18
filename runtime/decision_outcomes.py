"""What was decided, and what the portfolio did afterwards.

Thirteen decisions, none graded. effectiveness.py exists, the journal holds
zero outcome records, and every cycle reports "insufficient attributable
outcomes" as though the only way to learn were a filled trade.

It is not. A WAIT is a decision with a consequence. Declining to trim a
position is a position held, and an hour later the portfolio is worth
something different. That difference is attributable to the decision in
exactly the way a fill would be, and it costs nothing to observe: the
snapshots are already in the repository.

What this does NOT do is say whether a decision was good. Net liquidation
moving is not the same as a decision being right -- a WAIT that avoided a
loss and a WAIT that missed a gain look identical in one number, over one
hour, on one path that happened. Judging that is the host's work, and a
runtime that scored decisions would be encoding a view of what good looks
like, which is the thing this codebase exists not to do.

So it pairs each decision with the observable state then and now, and hands
both to the host.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .timestamps import effective_as_of


def _position_values(snapshot: Mapping[str, Any]) -> dict[str, float]:
    """Market value per symbol, whichever shape the host used."""
    # attention_positions is what the host actually sends; positions is the
    # shape the worked example shows. Both are read rather than one being
    # declared correct.
    positions = snapshot.get("positions") or snapshot.get("attention_positions")
    values: dict[str, float] = {}
    if isinstance(positions, Mapping):
        for symbol, row in positions.items():
            if isinstance(row, Mapping) and row.get("market_value") is not None:
                try:
                    values[str(symbol)] = float(row["market_value"])
                except (TypeError, ValueError):
                    continue
    elif isinstance(positions, Sequence) and not isinstance(positions, str):
        for row in positions:
            if not isinstance(row, Mapping):
                continue
            symbol = row.get("symbol")
            value = row.get("market_value")
            if symbol and value is not None:
                try:
                    values[str(symbol)] = float(value)
                except (TypeError, ValueError):
                    continue
    return values


def _net_liquidation(snapshot: Mapping[str, Any]) -> float | None:
    summary = snapshot.get("account_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    for key in ("net_liquidation", "net_liquidation_value", "nlv"):
        value = snapshot.get(key)
        if value is None:
            value = summary.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def observable_state(data: Mapping[str, Any]) -> dict[str, Any]:
    """The facts a later cycle can compare against."""
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    return {
        "as_of": effective_as_of(data),
        "net_liquidation": _net_liquidation(snapshot),
        "positions": _position_values(snapshot),
    }


def ungraded_decisions(inputs: Sequence[Mapping[str, Any]],
                       limit: int = 5) -> list[dict[str, Any]]:
    """Past decisions paired with what changed since, most recent last.

    The latest cycle is excluded: nothing has happened after it yet, so there
    is nothing to compare, and offering it would invite a judgement made from
    no elapsed time at all.
    """
    if len(inputs) < 2:
        return []
    latest = observable_state(inputs[-1])
    rows = []
    for data in inputs[:-1][-limit:]:
        decision = data.get("decision")
        decision = decision if isinstance(decision, Mapping) else {}
        state = observable_state(data)
        moves = {}
        for symbol, then in state["positions"].items():
            now = latest["positions"].get(symbol)
            if now is not None and then:
                moves[symbol] = round((now - then) / then * 100, 3)
        nlv_then, nlv_now = state["net_liquidation"], latest["net_liquidation"]
        rows.append({
            "decided_at": state["as_of"],
            "decision": decision.get("status"),
            "rationale": str(decision.get("rationale", ""))[:140] or None,
            "compared_against": latest["as_of"],
            "position_change_pct": moves,
            "net_liquidation_change_pct": (
                round((nlv_now - nlv_then) / nlv_then * 100, 3)
                if nlv_then and nlv_now else None),
        })
    return rows


def summarise(inputs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The block the host reads when judging its own record."""
    rows = ungraded_decisions(inputs)
    return {
        "count": len(rows),
        "decisions": rows,
        "what_this_means": (
            "Your past decisions and what the portfolio did after each. This "
            "is the attributable evidence you have been reporting as absent: "
            "a WAIT is a decision with a consequence, and declining to trim a "
            "position is a position held. These numbers do NOT say whether a "
            "decision was right. A WAIT that avoided a loss and a WAIT that "
            "missed a gain look identical over one hour on the one path that "
            "happened, and the runtime will not pretend otherwise. Judge them "
            "yourself, say when a judgement is premature, and record what you "
            "conclude as a lesson rather than leaving it in the cycle."
        ) if rows else (
            "Not enough history to compare a decision against what followed."
        ),
    }
