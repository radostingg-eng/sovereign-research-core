"""Common instrument/expression contract for equities, options, futures and FX."""
from __future__ import annotations
import math

from dataclasses import dataclass, asdict
from typing import Any, Mapping, Sequence

ASSET_CLASSES = {"equity", "etf", "option", "future", "future_option", "fx"}
EXPRESSION_TYPES = {"spot", "call", "put", "vertical", "calendar", "straddle", "strangle", "future", "future_option", "fx_spot", "fx_forward", "pair", "hedge"}

@dataclass(frozen=True)
class InstrumentExpression:
    candidate_id: str
    asset_class: str
    symbol: str
    expression_type: str
    direction: str
    expiry: str | None
    strike: float | None
    multiplier: float
    currency: str
    required_fields: tuple[str, ...]
    evidence_status: str = "unknown"


def normalize_expression(data: Mapping[str, Any]) -> dict[str, Any]:
    asset = str(data.get("asset_class", "")).lower()
    expr = str(data.get("expression_type", "")).lower()
    if asset not in ASSET_CLASSES:
        raise ValueError(f"invalid_asset_class:{asset}")
    if expr not in EXPRESSION_TYPES:
        raise ValueError(f"invalid_expression_type:{expr}")
    direction = str(data.get("direction", "long")).lower()
    if direction not in {"long", "short", "relative_value", "hedge", "mixed"}:
        raise ValueError(f"invalid_direction:{direction}")
    # The default was the generic base ("price", "liquidity") while
    # required_fields_for -- eight lines below -- already knew an option
    # needs expiry, strike, implied_volatility, open_interest and margin.
    # An option expression therefore became "ready" with a price and
    # nothing else.
    #
    # A caller may ADD requirements but not remove them: a declared list
    # shorter than the instrument demands is how the gate got shrunk, and
    # the declaring side is the side with the incentive to shrink it.
    computed = required_fields_for(asset, expr)
    declared = tuple(str(x) for x in data.get("required_fields", ()))
    required = computed + tuple(x for x in declared if x not in computed)
    row = InstrumentExpression(str(data["candidate_id"]), asset, str(data["symbol"]), expr, direction,
                               data.get("expiry"), float(data["strike"]) if data.get("strike") is not None else None,
                               float(data.get("multiplier", 1.0)), str(data.get("currency", "USD")), required,
                               str(data.get("evidence_status", "unknown")))
    return asdict(row)


def required_fields_for(asset_class: str, expression_type: str) -> tuple[str, ...]:
    base = ("price", "liquidity")
    if asset_class in {"option", "future_option"}:
        return base + ("expiry", "strike", "implied_volatility", "open_interest", "margin")
    if asset_class == "future":
        return base + ("expiry", "open_interest", "margin")
    if asset_class == "fx":
        return base + ("fx_rate",)
    return base


# Fields whose observation must be a real, usable number. `expiry` is a date
# and is checked for presence only.
_NUMERIC_OBSERVATIONS = frozenset({
    "price", "liquidity", "strike", "implied_volatility",
    "open_interest", "margin", "fx_rate",
})


def expression_ready(expression: Mapping[str, Any], observations: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Readiness against what the INSTRUMENT requires, not what it declared.

    Two ways this said yes when it should not have. It read required_fields
    off the expression itself, so an expression that declared none -- or a
    truncated list -- was checked against nothing. And it only tested for
    None, so a price of -5 or the literal string "REJECT" counted as an
    observation.
    """
    asset = str(expression.get("asset_class", "")).lower()
    expr = str(expression.get("expression_type", "")).lower()
    computed = required_fields_for(asset, expr)
    declared = tuple(str(x) for x in expression.get("required_fields", ()))
    required = computed + tuple(x for x in declared if x not in computed)

    problems: list[str] = []
    for field in required:
        value = observations.get(field)
        if value is None:
            problems.append(field)
            continue
        if field in _NUMERIC_OBSERVATIONS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"{field}:not_numeric")
                continue
            number = float(value)
            if not math.isfinite(number):
                problems.append(f"{field}:not_finite")
            elif number < 0:
                problems.append(f"{field}:negative")
    return (not problems, problems)


def evaluate_expressions(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and check host-selected expressions without ranking them."""
    results = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            results.append({
                "index": index,
                "ready": False,
                "problems": ["expression_entry_not_an_object"],
            })
            continue
        raw = entry.get("expression", entry)
        observations = entry.get("observations", {})
        try:
            expression = normalize_expression(raw)
        except (KeyError, TypeError, ValueError) as error:
            results.append({
                "index": index,
                "ready": False,
                "problems": [f"{type(error).__name__}:{error}"],
            })
            continue
        ready, problems = expression_ready(
            expression,
            observations if isinstance(observations, Mapping) else {},
        )
        results.append({
            "index": index,
            "candidate_id": expression["candidate_id"],
            "expression": expression,
            "ready": ready,
            "problems": problems,
        })
    return results
