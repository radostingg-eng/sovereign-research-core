"""Deterministic normalization and completeness checks for IBKR history.

The LLM host fetches the actual IBKR windows. This module never connects to
IBKR and never invents missing records. It filters at account inception,
deduplicates overlapping windows, preserves provenance, and reports exactly
what remains unavailable from the connector.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


def _end_of_day(value: str) -> datetime:
    """Upper bound for a window, inclusive of a bare date's whole day."""
    parsed = _ts(value)
    has_time = "T" in value or " " in value.strip()
    if has_time:
        return parsed
    return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)


def _ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Fields where two windows reporting different values means one source is
# wrong, rather than the same trade being seen twice.
_MATERIAL_TRADE_FIELDS = ("quantity", "price", "symbol", "side", "currency",
                          "commission", "realized_pnl", "trade_time")


@dataclass(frozen=True)
class BackfillResult:
    records: tuple[dict[str, Any], ...]
    duplicate_count: int
    filtered_before_inception: int
    filtered_after_cutoff: int
    earliest: str | None
    latest: str | None
    # Duplicates whose material fields DISAGREE between source windows.
    # A repeated trade_id carrying the same values is a harmless overlap
    # between windows; one carrying a different quantity or price means a
    # source is wrong, and "first window wins" silently picks a winner.
    conflicts: tuple[dict[str, Any], ...] = ()


def normalize_trades(
    windows: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    inception: str,
    cutoff: str,
) -> BackfillResult:
    """Merge IBKR trade windows without losing source provenance."""
    start = _ts(inception)
    # A bare date cutoff means the END of that day. Parsing "2026-09-16" to
    # midnight dropped every trade made ON the cutoff date, while
    # normalize_performance compares date strings and keeps the same day --
    # so the two datasets disagreed about whether the final day counted, and
    # trade coverage could never actually reach a same-day cutoff.
    end = _end_of_day(cutoff)
    by_id: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    duplicates = 0
    before = 0
    after = 0

    for source_window, rows in windows.items():
        for raw in rows:
            if not raw.get("trade_id") or not raw.get("trade_time"):
                raise ValueError("trade_missing_identity_or_timestamp")
            row = dict(raw)
            observed = _ts(str(row["trade_time"]))
            if observed < start:
                before += 1
                continue
            if observed > end:
                after += 1
                continue
            trade_id = str(row["trade_id"])
            if trade_id in by_id:
                duplicates += 1
                existing = by_id[trade_id]
                disagreements = {
                    field: {"kept": existing.get(field), "discarded": row.get(field)}
                    for field in _MATERIAL_TRADE_FIELDS
                    if field in row and existing.get(field) != row.get(field)
                }
                if disagreements:
                    conflicts.append({
                        "trade_id": trade_id,
                        "kept_from": existing.get("source_windows", []),
                        "discarded_from": source_window,
                        "fields": disagreements,
                    })
                sources = list(existing.get("source_windows", []))
                if source_window not in sources:
                    sources.append(source_window)
                existing["source_windows"] = sorted(sources)
                continue
            row["source_windows"] = [source_window]
            by_id[trade_id] = row

    records = tuple(sorted(by_id.values(), key=lambda x: (str(x["trade_time"]), str(x["trade_id"]))))
    return BackfillResult(
        records=records,
        duplicate_count=duplicates,
        filtered_before_inception=before,
        filtered_after_cutoff=after,
        earliest=records[0]["trade_time"] if records else None,
        latest=records[-1]["trade_time"] if records else None,
        conflicts=tuple(conflicts),
    )


def normalize_performance(
    dates: Sequence[str],
    nav: Sequence[float],
    cps: Sequence[float],
    *,
    inception: str,
    cutoff: str,
) -> list[dict[str, Any]]:
    """Normalize the IBKR daily performance series without interpolation."""
    if not (len(dates) == len(nav) == len(cps)):
        raise ValueError("performance_parallel_arrays_mismatch")
    start = inception[:10].replace("-", "")
    end = cutoff[:10].replace("-", "")
    rows = []
    for date, n, r in zip(dates, nav, cps):
        if start <= date.replace("-", "") <= end:
            rows.append({"date": date, "nav": float(n), "cps": float(r)})
    return rows


def _as_date(value: Any) -> str | None:
    """Best-effort YYYY-MM-DD from either an ISO timestamp or YYYYMMDD."""
    if value is None:
        return None
    text = str(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _day_gap(earlier: Any, later: Any) -> int | None:
    """Days from `earlier` to `later`; negative when `later` precedes it."""
    a, b = _as_date(earlier), _as_date(later)
    if a is None or b is None:
        return None
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def _earliest_of(*values: Any) -> str | None:
    dates = [d for d in (_as_date(v) for v in values) if d]
    return min(dates) if dates else None


def _latest_of(*values: Any) -> str | None:
    dates = [d for d in (_as_date(v) for v in values) if d]
    return max(dates) if dates else None



def _statement_evidence(claim: Any) -> dict[str, Any] | None:
    """The host's completeness assessment, or None when it is only asserted.

    A bare True is refused on purpose. Completeness is a judgement about what
    was NOT returned, which needs a cited source and the period it covers;
    a boolean carries neither and cannot be checked later.
    """
    if not isinstance(claim, Mapping):
        return None
    required = ("source", "covers_from", "covers_to")
    if any(not str(claim.get(field, "") or "").strip() for field in required):
        return None
    return dict(claim)


def coverage_report(
    *,
    inception: str,
    cutoff: str,
    trade_result: BackfillResult,
    performance_rows: Sequence[Mapping[str, Any]],
    statement_reconciled: Any = None,
) -> dict[str, Any]:
    """Produce an explicit completeness state; never upgrade missing statement data."""
    performance_earliest = performance_rows[0]["date"] if performance_rows else None
    performance_latest = performance_rows[-1]["date"] if performance_rows else None
    # Completeness used to mean "some data exists". One day of 2026 trades
    # against a 2012 inception therefore reported connector-complete: a
    # single day standing in for fourteen years. The operator has been
    # explicit that a bounded connector window must never be described as
    # lifetime coverage, and this was the code contradicting that.
    #
    # Completeness now means the data actually SPANS the claimed period.
    has_rows = bool(
        trade_result.records
        and performance_rows
        and trade_result.earliest is not None
        and trade_result.latest is not None
        and performance_earliest is not None
        and performance_latest is not None
    )
    earliest_observed = _earliest_of(trade_result.earliest, performance_earliest)
    latest_observed = _latest_of(trade_result.latest, performance_latest)
    gap_at_start_days = _day_gap(inception, earliest_observed)
    gap_at_end_days = _day_gap(latest_observed, cutoff)

    # Taking the earliest of both datasets and the latest of both datasets
    # builds a span out of two endpoints that never belonged to the same
    # series. One 2012 performance point and one 2026 trade spanned fourteen
    # years between them while each dataset held exactly one row -- the same
    # "a single day standing in for fourteen years" the check above was
    # written to stop, reassembled from two sources instead of one.
    #
    # Each dataset must now reach both ends on its own evidence.
    def _spans(earliest: Any, latest: Any) -> bool:
        start_gap = _day_gap(inception, earliest)
        end_gap = _day_gap(latest, cutoff)
        return (start_gap is not None and start_gap <= 0
                and end_gap is not None and end_gap <= 0)

    trades_span = _spans(trade_result.earliest, trade_result.latest)
    performance_spans = _spans(performance_earliest, performance_latest)
    reaches_inception = gap_at_start_days is not None and gap_at_start_days <= 0
    reaches_cutoff = gap_at_end_days is not None and gap_at_end_days <= 0
    # An unresolved conflict is two records claiming to be the same trade with
    # different contents. One of them is wrong and nothing here can say which,
    # so the history is contradictory at that point. Reporting it in the
    # payload while still calling the period "complete" says both "these data
    # disagree" and "these data are trustworthy" in one report.
    unresolved_conflicts = list(trade_result.conflicts)
    connector_complete = bool(has_rows and trades_span and performance_spans
                              and not unresolved_conflicts)
    # statement_reconciled was a bare boolean with no production caller, so
    # the strongest claim this module can make rested on a flag anyone could
    # pass as True. Same shape as sandbox_passed and order_submission_used
    # before them.
    #
    # And two endpoints cannot establish completeness however dense the middle
    # is: reaching 2012 and 2026 proves the SPAN was observed, not that
    # nothing between them was omitted. No observations-per-year constant
    # fixes that, because the missing thing is evidence, not data points --
    # two trades could genuinely be an account's entire history.
    #
    # So the code reports what it can derive, and completeness is an
    # assessment the host makes against statement or export coverage. Absent
    # that evidence the honest answer is that the span is verified and
    # completeness is not.
    statement_evidence = _statement_evidence(statement_reconciled)
    status = (
        "complete_with_statement"
        if connector_complete and statement_evidence
        else "span_verified_completeness_unverified"
        if connector_complete
        # Rows exist but do not span the claimed period. Distinct from
        # "blocked", which means no usable data at all, because partial
        # coverage is usable for recent analysis and must simply never be
        # described as lifetime history.
        else "connector_conflicted"
        if has_rows and unresolved_conflicts
        else "connector_partial"
        if has_rows
        else "blocked"
    )
    return {
        "status": status,
        "covers_claimed_period": connector_complete,
        "observed_earliest": earliest_observed,
        "observed_latest": latest_observed,
        "gap_at_start_days": gap_at_start_days,
        "gap_at_end_days": gap_at_end_days,
        "trade_conflicts": list(trade_result.conflicts),
        "inception": inception,
        "cutoff": cutoff,
        "trade_count": len(trade_result.records),
        "duplicate_count": trade_result.duplicate_count,
        "filtered_before_inception": trade_result.filtered_before_inception,
        "filtered_after_cutoff": trade_result.filtered_after_cutoff,
        "trades_span_claimed_period": trades_span,
        "performance_spans_claimed_period": performance_spans,
        "trade_earliest": trade_result.earliest,
        "trade_latest": trade_result.latest,
        "performance_count": len(performance_rows),
        "performance_earliest": performance_earliest,
        "performance_latest": performance_latest,
        "statement_evidence": statement_evidence,
        "completeness_assessed_by_host": statement_evidence is not None,
        "limitations": [
            "connector does not expose a full historical activity statement",
            "connector does not expose a historical daily cash_and_margin ledger",
        ],
    }


def evaluate_host_backfill(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize host-fetched history and report only supported coverage."""
    try:
        inception = str(value["inception"])
        cutoff = str(value["cutoff"])
        windows = value["trade_windows"]
        performance = value["performance"]
        if not isinstance(windows, Mapping):
            raise TypeError("trade_windows_must_be_an_object")
        if not isinstance(performance, Mapping):
            raise TypeError("performance_must_be_an_object")
        trades = normalize_trades(
            windows,
            inception=inception,
            cutoff=cutoff,
        )
        performance_rows = normalize_performance(
            performance["dates"],
            performance["nav"],
            performance["cps"],
            inception=inception,
            cutoff=cutoff,
        )
        report = coverage_report(
            inception=inception,
            cutoff=cutoff,
            trade_result=trades,
            performance_rows=performance_rows,
            statement_reconciled=value.get("statement_evidence"),
        )
    except (KeyError, TypeError, ValueError) as error:
        return {
            "valid": False,
            "problems": [f"{type(error).__name__}:{error}"],
        }
    return {
        "valid": True,
        "trades": [dict(row) for row in trades.records],
        "performance": performance_rows,
        "coverage": report,
    }
