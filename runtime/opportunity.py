"""Deterministic portfolio opportunity primitives.

No market-data or brokerage calls. The research agent supplies verified observations.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def covered_call_opportunity(position: dict[str, Any], call: dict[str, Any], nav: float) -> dict[str, Any]:
    """Quantify covered-call economics, including ITM caps and target opportunity cost."""
    shares = float(position["shares"])
    spot = float(position["spot"])
    strike = float(call["strike"])
    if call.get("premium") is None:
        raise ValueError("covered_call_premium_required")
    premium = float(call["premium"])
    contracts = float(call["contracts"])
    multiplier = float(call.get("contract_multiplier", 100))
    covered_shares = min(shares, contracts * multiplier)
    if covered_shares <= 0 or nav <= 0 or spot <= 0 or strike <= 0:
        raise ValueError("covered shares, nav, spot and strike must be positive")
    # An obligation larger than the shares held is not a covered call on
    # the excess: it is a NAKED short call, with unbounded upside risk.
    # Premium used to be counted in full against only the covered shares,
    # so 50 shares written against 2 contracts reported a 12% yield when
    # three quarters of that premium was compensation for uncovered risk,
    # and nothing in the output named the naked leg at all.
    obligated_shares = contracts * multiplier
    uncovered_shares = max(0.0, obligated_shares - shares)
    strike_vs_spot = strike / spot - 1.0
    premium_value = premium * contracts * multiplier
    covered_premium_value = premium * covered_shares
    uncovered_premium_value = premium * uncovered_shares
    covered_value = spot * covered_shares
    # Yield is premium attributable to the COVERED shares over the value of
    # those shares. Mixing the uncovered premium in inflates it by exactly
    # the ratio of the naked leg.
    premium_yield = covered_premium_value / covered_value
    assignment_value = strike * covered_shares
    if call.get("target_price") is None:
        raise ValueError("covered_call_target_price_required")
    target = float(call["target_price"])
    opportunity_cost_at_target = max(target - strike, 0.0) * covered_shares
    return {
        "underlying": position["underlying"],
        "covered_shares": covered_shares,
        "obligated_shares": obligated_shares,
        "uncovered_shares": uncovered_shares,
        "uncovered_contracts": uncovered_shares / multiplier if multiplier else 0.0,
        "is_fully_covered": uncovered_shares <= 0,
        "covered_premium_value": covered_premium_value,
        "uncovered_premium_value": uncovered_premium_value,
        "strike_vs_spot_pct": strike_vs_spot,
        "is_itm": strike < spot,
        "upside_cap_pct": max(strike_vs_spot, 0.0),
        "premium_value": premium_value,
        "premium_yield_on_covered_value": premium_yield,
        "assignment_value": assignment_value,
        "assignment_pct_nav": assignment_value / nav,
        "opportunity_cost_at_target": opportunity_cost_at_target,
        "premium_less_target_opportunity_cost": premium_value - opportunity_cost_at_target,
    }


def evaluate_covered_calls(entries: Sequence[Mapping[str, Any]]
                           ) -> list[dict[str, Any]]:
    """Compute host-selected covered-call economics without selecting one."""
    results = []
    for index, entry in enumerate(entries):
        try:
            result = covered_call_opportunity(
                dict(entry["position"]),
                dict(entry["call"]),
                float(entry["nav"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            results.append({
                "index": index,
                "valid": False,
                "problems": [f"{type(error).__name__}:{error}"],
            })
            continue
        results.append({"index": index, "valid": True, "economics": result})
    return results
