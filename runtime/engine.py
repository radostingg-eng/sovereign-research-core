"""Deterministic runtime for the Sovereign Investment System.

This package contains reusable plumbing for auditability, source arbitration,
strategy experiments, calibration, walk-forward evaluation, and portfolio
counterfactual comparison. It contains no brokerage order-submission path.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import json
import re
from typing import Any, Mapping, Sequence


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_record(record: dict[str, Any]) -> str:
    body = dict(record)
    body.pop("record_hash", None)
    return sha256_text(canonical_json(body))


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def make_record(record_id: str, record_type: str, agent: str,
                payload: dict[str, Any], caused_by: Sequence[str] = (),
                prev_hash: str | None = None,
                created_at: str | None = None) -> dict[str, Any]:
    # Refuse a malformed prev_hash at the WRITE site. A host once wrote a
    # prev_hash truncated to 63 characters; because chain order is rebuilt
    # by following prev_hash, that single dropped character orphaned six
    # downstream records and surfaced four hops from the culprit. Callers
    # that cannot produce a well-formed parent hash must pass None and be
    # a chain root, not emit a link that resolves to nothing.
    if prev_hash is not None and not _HEX64.match(str(prev_hash)):
        raise ValueError(
            f"malformed_prev_hash:{record_id}: expected 64 lowercase hex "
            f"or None, got {len(str(prev_hash))} chars: {prev_hash!r}"
        )
    record = {
        "record_id": record_id,
        "record_type": record_type,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "payload": payload,
        "caused_by": list(caused_by),
        "prev_hash": prev_hash,
    }
    record["record_hash"] = hash_record(record)
    return record


def verify_chain(records: Sequence[dict[str, Any]]) -> list[str]:
    """Return defects; an empty list means the hash chain is valid."""
    errors: list[str] = []
    seen: set[str] = set()
    by_hash: set[str | None] = set()
    for i, record in enumerate(records):
        rid = record.get("record_id") or f"record[{i}]"
        if rid in seen:
            errors.append(f"{rid}: duplicate record_id")
        seen.add(rid)
        if record.get("record_hash") != hash_record(record):
            errors.append(f"{rid}: record_hash mismatch")
        # Verify against the record's ACTUAL parent, not whatever happened
        # to precede it in the sequence. The two are the same in a linear
        # chain and differ the moment a fork exists: after a branch, the
        # record before this one is a sibling's descendant, not its parent,
        # and comparing against it reports a mismatch that is not there.
        prev = record.get("prev_hash")
        if i == 0:
            if prev is not None:
                errors.append(f"{rid}: prev_hash mismatch")
        elif prev not in by_hash:
            errors.append(f"{rid}: prev_hash mismatch")
        by_hash.add(record.get("record_hash"))
    return errors


def dangling_causes(records: Sequence[dict[str, Any]]) -> list[str]:
    ids = {r.get("record_id") for r in records}
    return [
        f"{r.get('record_id')}: missing caused_by {parent}"
        for r in records
        for parent in r.get("caused_by", [])
        if parent not in ids
    ]


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SourceObservation:
    source: str
    observed_at: str
    value: Any
    confidence: float
    tier: int
    status: str = "ok"


@dataclass(frozen=True)
class ArbitrationResult:
    selected: SourceObservation | None
    conflicts: tuple[str, ...]
    rejected: tuple[str, ...]


def arbitrate(observations: Sequence[SourceObservation], as_of: str, *,
              max_age_hours: float,
              numeric_conflict_tolerance: float) -> ArbitrationResult:
    """Select the best usable observation and expose source conflicts.

    Both bounds are REQUIRED. They were defaulted to 24.0 hours and 0.01, and
    a default is how a judgement gets made by code without anyone choosing
    it: how stale is too stale, and how far apart two sources must be before
    they disagree, are decisions about the world rather than arithmetic.
    Whoever calls this has to state them, and the receipt then records what
    was actually used instead of a constant nobody revisited.

    The mechanics stay here: comparing timestamps and differences is
    deterministic work the runtime should do and an LLM should not.
    """
    cutoff = _parse_ts(as_of)
    usable: list[SourceObservation] = []
    rejected: list[str] = []
    for obs in observations:
        age_hours = (cutoff - _parse_ts(obs.observed_at)).total_seconds() / 3600.0
        if age_hours < 0:
            rejected.append(f"{obs.source}: future observation")
        elif age_hours > max_age_hours:
            rejected.append(f"{obs.source}: stale observation")
        elif obs.status != "ok":
            rejected.append(f"{obs.source}: status={obs.status}")
        else:
            usable.append(obs)
    usable.sort(key=lambda o: (o.tier, o.confidence, _parse_ts(o.observed_at)), reverse=True)
    selected = usable[0] if usable else None
    conflicts: list[str] = []
    if selected is not None and isinstance(selected.value, (int, float)):
        for obs in usable[1:]:
            if isinstance(obs.value, (int, float)):
                diff = abs(float(obs.value) - float(selected.value)) / max(abs(float(selected.value)), 1e-12)
                if diff > numeric_conflict_tolerance:
                    conflicts.append(f"{selected.source} vs {obs.source}: relative_diff={diff:.4f}")
    return ArbitrationResult(selected, tuple(conflicts), tuple(rejected))


STRATEGY_REQUIRED = {
    "strategy_id", "family", "thesis", "signal", "mechanism", "universe",
    "direction", "expression", "horizon", "regime", "risk_sources",
    "capital_model", "benchmark", "invalidation", "status"
}


def validate_strategy(strategy: dict[str, Any]) -> list[str]:
    errors = [f"missing field: {key}" for key in sorted(STRATEGY_REQUIRED - set(strategy))]
    if strategy.get("direction") not in {"long", "short", "relative_value", "hedge", "mixed"}:
        errors.append("invalid direction")
    if strategy.get("status") not in {"experimental", "active", "retired"}:
        errors.append("invalid status")
    for key in ("signal", "universe", "expression", "regime", "risk_sources", "invalidation"):
        if key in strategy and not isinstance(strategy[key], list):
            errors.append(f"{key} must be a list")
    return errors


def mutate_strategy(strategy: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(strategy)
    candidate.update(deepcopy(changes))
    candidate["status"] = "experimental"
    parent = strategy.get("strategy_version", 0)
    candidate["strategy_version"] = parent + 1
    candidate["parent_version"] = parent
    return candidate


def recombine_strategies(a: dict[str, Any], b: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    candidate = deepcopy(a)
    for field in fields:
        if field in b:
            candidate[field] = deepcopy(b[field])
    candidate["status"] = "experimental"
    candidate["strategy_version"] = max(a.get("strategy_version", 0), b.get("strategy_version", 0)) + 1
    candidate["parent_versions"] = [a.get("strategy_version", 0), b.get("strategy_version", 0)]
    return candidate


def invert_direction(strategy: dict[str, Any]) -> dict[str, Any]:
    mapping = {"long": "short", "short": "long", "relative_value": "relative_value", "hedge": "hedge", "mixed": "mixed"}
    if strategy.get("direction") not in mapping:
        raise ValueError("invalid direction")
    candidate = deepcopy(strategy)
    candidate["direction"] = mapping[strategy["direction"]]
    candidate["status"] = "experimental"
    candidate["strategy_version"] = strategy.get("strategy_version", 0) + 1
    candidate["parent_version"] = strategy.get("strategy_version", 0)
    return candidate


@dataclass(frozen=True)
class Calibration:
    n: int
    hit_rate: float
    brier: float | None


def calibration_summary(predictions: Sequence[int], outcomes: Sequence[int],
                        probabilities: Sequence[float] | None = None) -> Calibration:
    if len(predictions) != len(outcomes) or not predictions:
        raise ValueError("predictions and outcomes must have equal non-zero length")
    if any(p not in (0, 1) for p in predictions) or any(o not in (0, 1) for o in outcomes):
        raise ValueError("predictions and outcomes must be binary")
    brier = None
    if probabilities is not None:
        if len(probabilities) != len(outcomes) or any(p < 0 or p > 1 for p in probabilities):
            raise ValueError("probabilities must align with outcomes and be in [0,1]")
        brier = sum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(outcomes)
    return Calibration(len(predictions), sum(p == o for p, o in zip(predictions, outcomes)) / len(predictions), brier)


@dataclass(frozen=True)
class BacktestResult:
    trades: int
    total_return: float
    max_drawdown: float
    turnover: float
    final_equity: float


def backtest_long_only(bars: Sequence[dict[str, Any]], signal: Sequence[int], as_of: str,
                       initial_equity: float = 1.0,
                       fee_bps: float = 5.0, slippage_bps: float = 5.0) -> BacktestResult:
    """Evaluate a caller-supplied binary signal on sequential bars.

    Signal[i] is exposure over bar i -> i+1. Future bars relative to as_of are
    rejected to prevent look-ahead. Fees and slippage are charged only on entry
    or exit transitions.
    """
    if len(bars) != len(signal) or len(bars) < 2:
        raise ValueError("bars and signal must have same length and contain >=2 rows")
    # The bar validation below was added because a number gets believed where
    # an error would not. The COSTS were left unvalidated on the same
    # reasoning and have the same failure: a NaN fee produced equity=nan, an
    # infinite fee produced -inf, and a NEGATIVE fee paid the strategy to
    # trade -- 1.0343 final equity against a 1.0287 baseline on identical
    # bars. Negative costs are the cheapest way to improve any backtest.
    for label, value in (("initial_equity", initial_equity),
                         ("fee_bps", fee_bps), ("slippage_bps", slippage_bps)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{label} must be a finite number, got {value!r}")
        if number < 0:
            raise ValueError(f"{label} must be non-negative, got {value!r}")
    if float(initial_equity) <= 0:
        raise ValueError(f"initial_equity must be positive, got {initial_equity!r}")
    cutoff = _parse_ts(as_of)
    times = [_parse_ts(str(b["timestamp"])) for b in bars]
    if any(t > cutoff for t in times):
        raise ValueError("future bar detected relative to as_of")
    # A backtest that accepts unordered or invalid bars returns a number
    # rather than an error, and a number is what gets believed. Verified
    # before adding this: reverse-chronological bars produced a plausible
    # 0.83 final equity, duplicate timestamps were accepted, a NaN close
    # produced equity=nan, and a NEGATIVE close produced a profit.
    if any(later <= earlier for earlier, later in zip(times, times[1:])):
        raise ValueError("bars must be strictly ordered by ascending timestamp")
    for index, bar in enumerate(bars):
        try:
            close = float(bar["close"])
        except (TypeError, ValueError, KeyError):
            raise ValueError(f"bar {index} has a non-numeric close") from None
        if not math.isfinite(close):
            raise ValueError(f"bar {index} close is not finite: {close!r}")
        if close <= 0:
            raise ValueError(f"bar {index} close must be positive, got {close!r}")
    if not math.isfinite(float(initial_equity)) or float(initial_equity) <= 0:
        raise ValueError("initial_equity must be a positive finite number")
    if any(s not in (0, 1) for s in signal):
        raise ValueError("signals must be 0 or 1")
    equity = float(initial_equity)
    peak = equity
    max_dd = 0.0
    trades = 0
    turnover = 0.0
    previous = 0
    friction_rate = (fee_bps + slippage_bps) / 10000.0
    for i in range(len(bars) - 1):
        pos = signal[i]
        changed = pos != previous
        if changed:
            trades += 1
            turnover += 1.0
            previous = pos
        gross = float(bars[i + 1]["close"]) / float(bars[i]["close"]) - 1.0
        equity *= 1.0 + pos * gross - (friction_rate if changed else 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    return BacktestResult(trades, equity / initial_equity - 1.0, max_dd, turnover, equity)


def walk_forward_splits(n: int, train_size: int, test_size: int, step: int | None = None):
    if min(n, train_size, test_size) <= 0:
        raise ValueError("sizes must be positive")
    step = step or test_size
    if step <= 0:
        raise ValueError("step must be positive")
    start = 0
    out = []
    while start + train_size + test_size <= n:
        out.append((range(start, start + train_size),
                    range(start + train_size, start + train_size + test_size)))
        start += step
    return out


def evaluate_source_arbitrations(
    requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Run host-parameterized source arbitration without choosing sources."""
    results = []
    for index, request in enumerate(requests):
        try:
            observations = [
                SourceObservation(
                    source=str(row["source"]),
                    observed_at=str(row["observed_at"]),
                    value=row.get("value"),
                    confidence=float(row["confidence"]),
                    tier=int(row["tier"]),
                    status=str(row.get("status", "ok")),
                )
                for row in request["observations"]
                if isinstance(row, Mapping)
            ]
            result = arbitrate(
                observations,
                as_of=str(request["as_of"]),
                max_age_hours=float(request["max_age_hours"]),
                numeric_conflict_tolerance=float(
                    request["numeric_conflict_tolerance"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            results.append({
                "index": index,
                "valid": False,
                "problems": [f"{type(error).__name__}:{error}"],
            })
            continue
        results.append({
            "index": index,
            "valid": True,
            "selected": (
                result.selected.__dict__ if result.selected is not None
                else None
            ),
            "conflicts": list(result.conflicts),
            "rejected": list(result.rejected),
        })
    return results


def evaluate_backtests(
    requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Run host-authored signals and split sizes without selecting a strategy."""
    results = []
    for index, request in enumerate(requests):
        try:
            backtest = backtest_long_only(
                request["bars"],
                request["signal"],
                str(request["as_of"]),
                initial_equity=float(request.get("initial_equity", 1.0)),
                fee_bps=float(request["fee_bps"]),
                slippage_bps=float(request["slippage_bps"]),
            )
            splits = walk_forward_splits(
                len(request["bars"]),
                int(request["train_size"]),
                int(request["test_size"]),
                int(request.get("step") or request["test_size"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            results.append({
                "index": index,
                "valid": False,
                "problems": [f"{type(error).__name__}:{error}"],
            })
            continue
        results.append({
            "index": index,
            "valid": True,
            "backtest": backtest.__dict__,
            "walk_forward_splits": [
                {
                    "train": [split.start, split.stop],
                    "test": [test.start, test.stop],
                }
                for split, test in splits
            ],
        })
    return results
