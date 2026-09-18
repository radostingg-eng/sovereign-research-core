"""Bounded executable strategy discovery across the system's strategy families.

Discovery creates research candidates only. It does not assert that a thesis is
true and cannot submit an order. Each candidate must subsequently pass the
normal evidence, portfolio-fit, risk, tax and counterfactual gates.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping, Sequence

FAMILIES = (
    "deep_value_fcf", "quality_at_discount", "garp", "special_situations",
    "event_driven", "relative_value", "volatility_options", "hedging",
    "factor_macro", "insider_ownership", "institutional_13f",
    "buybacks_capital_return", "restructuring_turnaround", "sentiment_dislocation",
    "momentum_mean_reversion", "source_disagreement", "instrument_substitution",
)

@dataclass(frozen=True)
class ResearchCandidate:
    candidate_id: str
    family: str
    instrument: str
    direction: str
    thesis: str
    required_evidence: tuple[str, ...]
    invalidation: tuple[str, ...]
    expression_types: tuple[str, ...]
    portfolio_question: str


def _slug(value: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in value).strip("_")


def discover_candidates(*, universe: Sequence[Mapping[str, Any]],
                        observations: Mapping[str, Any] | None = None,
                        families: Sequence[str] | None = None,
                        max_candidates: int = 50) -> list[dict[str, Any]]:
    """Generate deterministic research candidates from supplied observations.

    No family is privileged and no numeric thesis is invented. Candidate text
    points the research specialists at the evidence they must establish.
    """
    if max_candidates <= 0:
        return []
    # Defaulting to the whole catalogue is a hardcoded strategy sequence,
    # which PHILOSOPHY.md forbids and which build_plan had to have removed
    # for the same reason. The Research Director selects.
    if families is None:
        raise ValueError(
            "family_selection_required: discover_candidates cannot choose "
            f"research families for you. FAMILIES lists {len(FAMILIES)} known "
            "shapes and the Research Director selects per cycle from decision "
            "value and portfolio gaps. See PHILOSOPHY.md."
        )
    selected = tuple(families)
    if not selected:
        raise ValueError("empty_family_selection: supply at least one research family")
    # Unknown families ARE refused, and this is deliberately NOT the same
    # situation as an invented specialist role. A family is a key into the
    # required-evidence map below: it decides what a candidate must prove
    # before it can pass the evidence gate. An off-catalogue family has no
    # evidence contract, so admitting one would mint candidates that nothing
    # requires evidence for. Inventing a family therefore means supplying its
    # evidence requirements too, which is a real change rather than a looser
    # check.
    unknown = [f for f in selected if f not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown_family:{','.join(unknown)}")
    obs = observations or {}
    candidates: list[ResearchCandidate] = []
    for asset in universe:
        symbol = str(asset.get("symbol", ""))
        if not symbol:
            continue
        instruments = tuple(asset.get("instruments", ("equity",)))
        for family in selected:
            if len(candidates) >= max_candidates:
                break
            if family in {"volatility_options", "instrument_substitution"}:
                types = ("stock", "option", "spread", "future", "future_option")
            elif family == "hedging":
                types = ("future", "etf", "option", "fx")
            else:
                types = ("stock", "etf", "relative_value")
            available = tuple(x for x in types if x in instruments or x in {"stock", "etf", "relative_value"})
            if not available:
                continue
            evidence = {
                "deep_value_fcf": ("valuation", "free_cash_flow", "balance_sheet"),
                "quality_at_discount": ("returns_on_capital", "balance_sheet", "valuation"),
                "garp": ("growth", "valuation", "earnings_revision"),
                "special_situations": ("corporate_action", "catalyst", "valuation"),
                "event_driven": ("event", "probability", "payoff"),
                "relative_value": ("peer_prices", "relative_valuation", "catalyst"),
                "volatility_options": ("option_chain", "iv", "oi", "liquidity"),
                "hedging": ("portfolio_exposure", "correlation", "hedge_cost"),
                "factor_macro": ("factor_exposure", "macro_regime", "valuation"),
                "insider_ownership": ("insider_activity", "ownership", "valuation"),
                "institutional_13f": ("institutional_holdings", "changes", "catalyst"),
                "buybacks_capital_return": ("buybacks", "capital_return", "balance_sheet"),
                "restructuring_turnaround": ("restructuring", "cash_burn", "catalyst"),
                "sentiment_dislocation": ("sentiment", "positioning", "fundamentals"),
                "momentum_mean_reversion": ("price_history", "trend_or_reversion", "liquidity"),
                "source_disagreement": ("source_conflict", "freshness", "independent_confirmation"),
                "instrument_substitution": ("thesis", "instrument_cost", "liquidity", "risk"),
            }[family]
            known = [x for x in evidence if x in obs.get(symbol, {})]
            candidates.append(ResearchCandidate(
                candidate_id=f"{_slug(family)}:{_slug(symbol)}:{len(candidates)+1}",
                family=family,
                instrument=available[0],
                direction="hedge" if family == "hedging" else "long",
                thesis=f"Investigate {family} setup for {symbol}; establish the required evidence before scoring.",
                required_evidence=evidence,
                invalidation=("required evidence cannot be verified", "portfolio fit fails", "counterfactual fails"),
                expression_types=available,
                portfolio_question="Does this improve the portfolio versus hold/wait and alternative capital uses?",
            ))
    return [asdict(c) for c in candidates[:max_candidates]]
