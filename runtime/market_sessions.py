"""Timezone-aware market-session evidence and overlap mechanics."""
from __future__ import annotations

from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .timestamps import parse_iso_timestamp


MAX_OBSERVATION_SKEW_SECONDS = 15 * 60
MARKET_REGIONS = frozenset({"EU", "US"})
SESSION_STATUSES = frozenset({
    "open",
    "closed",
    "pre_market",
    "post_market",
    "holiday",
    "auction",
    "unknown",
})
OVERLAP_STATES = frozenset({
    "both_open", "eu_only", "us_only", "none_open",
})


def _parse_timestamp(value: Any):
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def derive_overlap(markets: Sequence[Mapping[str, Any]]) -> str:
    """Project source-backed EU/US open flags into one mechanical state."""
    open_by_region = {
        str(market.get("region", "")).upper(): market.get("is_open") is True
        for market in markets
        if isinstance(market, Mapping)
    }
    eu_open = open_by_region.get("EU", False)
    us_open = open_by_region.get("US", False)
    if eu_open and us_open:
        return "both_open"
    if eu_open:
        return "eu_only"
    if us_open:
        return "us_only"
    return "none_open"


def validate_market_sessions(
    value: Any,
    *,
    expected_at: Any = None,
) -> list[str]:
    """Validate session evidence without encoding exchange schedules."""
    if not isinstance(value, Mapping):
        return ["market_sessions_not_an_object"]
    errors = []
    observed_at = _parse_timestamp(value.get("observed_at"))
    if observed_at is None:
        errors.append("market_sessions_observed_at_invalid")
    expected = _parse_timestamp(expected_at)
    if (
        observed_at is not None
        and expected is not None
        and abs((observed_at - expected).total_seconds())
        > MAX_OBSERVATION_SKEW_SECONDS
    ):
        errors.append("market_sessions_observed_at_mismatch")
    markets = value.get("markets")
    if not isinstance(markets, list) or not markets:
        return errors + ["market_sessions_markets_must_be_nonempty_list"]

    seen_regions = set()
    valid_markets = []
    for index, market in enumerate(markets):
        if not isinstance(market, Mapping):
            errors.append(f"market_session_not_object:{index}")
            continue
        valid_markets.append(market)
        region = str(market.get("region", "")).upper()
        if region not in MARKET_REGIONS:
            errors.append(f"market_session_region_invalid:{index}:{region}")
        elif region in seen_regions:
            errors.append(f"market_session_region_duplicate:{region}")
        else:
            seen_regions.add(region)
        if not str(market.get("venue", "")).strip():
            errors.append(f"market_session_venue_required:{index}")
        timezone_name = str(market.get("timezone", "")).strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"market_session_timezone_invalid:{index}")
            timezone = None
        local_time = _parse_timestamp(market.get("local_time"))
        if local_time is None:
            errors.append(f"market_session_local_time_invalid:{index}")
        elif observed_at is not None and timezone is not None:
            expected = observed_at.astimezone(timezone)
            if abs((local_time - expected).total_seconds()) > 60:
                errors.append(f"market_session_local_time_mismatch:{index}")
            elif local_time.utcoffset() != expected.utcoffset():
                errors.append(
                    f"market_session_local_time_offset_mismatch:{index}")
        status = market.get("status")
        if status not in SESSION_STATUSES:
            errors.append(f"market_session_status_invalid:{index}:{status}")
        if not isinstance(market.get("is_open"), bool):
            errors.append(f"market_session_is_open_not_boolean:{index}")
        elif status == "open" and market["is_open"] is not True:
            errors.append(f"market_session_open_status_mismatch:{index}")
        elif (
            status in {
                "closed", "pre_market", "post_market", "holiday", "unknown",
            }
            and market["is_open"] is not False
        ):
            errors.append(f"market_session_closed_status_mismatch:{index}")
        next_times = {}
        for field in ("next_open", "next_close"):
            parsed = _parse_timestamp(market.get(field))
            if parsed is None:
                errors.append(f"market_session_{field}_invalid:{index}")
            else:
                next_times[field] = parsed
                if observed_at is not None and parsed <= observed_at:
                    errors.append(
                        f"market_session_{field}_not_future:{index}")
        if {"next_open", "next_close"} <= next_times.keys():
            next_open = next_times["next_open"]
            next_close = next_times["next_close"]
            is_open = market.get("is_open")
            if is_open is True and next_close >= next_open:
                errors.append(
                    f"market_session_open_sequence_invalid:{index}")
            elif is_open is False and next_open >= next_close:
                errors.append(
                    f"market_session_closed_sequence_invalid:{index}")
        evidence = market.get("evidence")
        call_ids = market.get("evidence_tool_call_ids")
        has_legacy_evidence = (
            isinstance(evidence, list) and bool(evidence)
        )
        has_call_ids = (
            isinstance(call_ids, list)
            and bool(call_ids)
            and all(
                isinstance(value, str) and value.strip()
                for value in call_ids
            )
        )
        if not has_legacy_evidence and not has_call_ids:
            errors.append(f"market_session_evidence_required:{index}")
            continue
        for evidence_index, item in enumerate(evidence or ()):
            if (
                not isinstance(item, Mapping)
                or not str(item.get("tool", "")).strip()
                or "result" not in item
            ):
                errors.append(
                    f"market_session_evidence_invalid:"
                    f"{index}:{evidence_index}")

    missing_regions = sorted(MARKET_REGIONS - seen_regions)
    errors.extend(
        f"market_session_region_missing:{region}"
        for region in missing_regions
    )
    overlap = value.get("overlap")
    if overlap not in OVERLAP_STATES:
        errors.append(f"market_session_overlap_invalid:{overlap}")
    elif not missing_regions and overlap != derive_overlap(valid_markets):
        errors.append(
            f"market_session_overlap_mismatch:"
            f"{overlap}!={derive_overlap(valid_markets)}")
    return errors


def latest_market_sessions(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Latest persisted session state for host and operator visibility."""
    for record in reversed(list(records)):
        if record.get("record_type") != "market_sessions":
            continue
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            return dict(payload)
    return {}
