"""Research-stage primitives.

decide() used to live here. It computed

    score = expected_return - risk + portfolio_fit
    action = "consider" if score > minimum_score else "wait"

which is a strategy, in code, with a threshold defaulted to zero. PHILOSOPHY.md
says the LLM chooses and the runtime checks, and that function did the
choosing. It had no production callers -- only a test -- so it was not even a
strategy the system used; it was one it would have used the moment somebody
wired it up.

candidate_gate remains and is the honest half: it reports whether the evidence
and portfolio inputs needed for a decision are present, and refuses to fill in
the ones that are missing. What to do about a ready candidate is the host's
call. The runtime preserves the host's order and emits no aggregate score,
rank, winner, or recommendation.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Iterable, Mapping

from .portfolio import (
    assignment_ledger, normalize_currency, normalized_assignment_total,
    risk_capacity, scenario_stress,
)


def reconcile_portfolio(snapshot: dict[str, Any], positions: Iterable[dict[str, Any]],
                        fx_to_base: dict[str, float], base: str = "USD",
                        as_of: str | None = None) -> dict[str, Any]:
    """Build the capital/risk state used by every research decision."""
    rows = assignment_ledger(positions, as_of=as_of)
    assignment_base = normalized_assignment_total(rows, fx_to_base, base)
    capacity = risk_capacity(snapshot, assignment_base)
    return {
        "assignment_rows": rows,
        "assignment_by_underlying": {
            key: {"contracts": value["contracts"],
                  "assignment_notional": value["assignment_notional"],
                  "currency": base}
            for key, value in _aggregate(rows, fx_to_base, base).items()
        },
        "capacity": capacity,
    }


def _aggregate(rows, fx_to_base, base="USD"):
    """Aggregate per underlying in BASE currency.

    The sibling total on the same result used normalized_assignment_total,
    which converts, while this summed raw notionals across currencies. One
    dict therefore reported a converted total beside an unconverted
    breakdown: the parts did not sum to the whole, and any reader asking
    *where* the exposure sits saw non-base currencies understated.
    """
    out = {}
    for row in rows:
        item = out.setdefault(row.underlying, {"contracts": 0.0, "assignment_notional": 0.0})
        item["contracts"] += row.contracts
        item["assignment_notional"] += normalize_currency(
            row.assignment_notional, row.currency, fx_to_base, base)
    return out


def candidate_gate(candidate: dict[str, Any], portfolio: dict[str, Any]) -> tuple[bool, list[str]]:
    """Fail closed when evidence or portfolio inputs needed for a decision are absent."""
    blockers: list[str] = []
    for field in ("candidate_id", "expected_return", "risk", "capital_usage"):
        if field not in candidate:
            blockers.append(f"missing:{field}")
    if candidate.get("evidence_status") not in {"verified", "cross_checked"}:
        blockers.append("evidence_not_verified")
    if portfolio.get("capacity", {}).get("assignment_to_nav") is None:
        blockers.append("assignment_capacity_unknown")
    return not blockers, blockers


def evaluate_candidate_readiness(
    candidates: Iterable[dict[str, Any]],
    portfolio: dict[str, Any],
) -> list[dict[str, Any]]:
    """Report evidence/readiness components in the host's supplied order."""
    rows = []
    for candidate in candidates:
        row = dict(candidate)
        ready, blockers = candidate_gate(row, portfolio)
        row["ready"] = ready
        row["blockers"] = blockers
        rows.append(row)
    return rows


def stress_portfolio(snapshot: dict[str, Any], positions: Iterable[dict[str, Any]],
                     scenarios: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run supplied stress scenarios without inventing Greeks or correlations."""
    return scenario_stress(positions, float(snapshot["nav"]), scenarios)


def evaluate_portfolio_mechanics(value: Mapping[str, Any]) -> dict[str, Any]:
    """Compute host-supplied portfolio components without choosing an action."""
    try:
        account = dict(value["account"])
        positions = [
            dict(position) for position in value["positions"]
            if isinstance(position, Mapping)
        ]
        fx_to_base = dict(value.get("fx_to_base") or {})
        base = str(value.get("base_currency", "USD"))
        state = reconcile_portfolio(
            account,
            positions,
            fx_to_base,
            base=base,
            as_of=str(value.get("as_of") or ""),
        )
    except (KeyError, TypeError, ValueError) as error:
        return {
            "valid": False,
            "problems": [f"{type(error).__name__}:{error}"],
        }

    serializable_state = dict(state)
    serializable_state["assignment_rows"] = [
        asdict(row) for row in state["assignment_rows"]
    ]
    result: dict[str, Any] = {
        "valid": True,
        "portfolio": serializable_state,
    }
    candidates = value.get("candidates")
    if isinstance(candidates, Iterable) and not isinstance(
            candidates, (str, bytes, Mapping)):
        result["candidate_readiness"] = evaluate_candidate_readiness(
            [dict(candidate) for candidate in candidates
             if isinstance(candidate, Mapping)],
            state,
        )
    scenarios = value.get("scenarios")
    if isinstance(scenarios, Iterable) and not isinstance(
            scenarios, (str, bytes, Mapping)):
        result["stress"] = stress_portfolio(
            account,
            positions,
            [dict(scenario) for scenario in scenarios
             if isinstance(scenario, Mapping)],
        )
    return result
