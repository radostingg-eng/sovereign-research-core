"""Validate and summarize optional external research-worker records."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from .timestamps import parse_iso_timestamp
from .worker_health_incidents import safe_error_code, FAILURE_STATUSES

RESEARCH_INBOX_SCHEMA_VERSION = 1
MAX_INBOX_BYTES = 128_000
RESEARCH_INBOX_SUMMARY_BYTE_BUDGET = 40_000
WORKER_STATUSES = frozenset({
    "completed",
    "no_work",
    "quota_exhausted",
    "auth_error",
    "model_error",
    "configuration_error",
})
_SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}")
_REQUIRED_FIELDS = frozenset({
    "schema_version",
    "record_id",
    "worker_id",
    "status",
    "origin",
    "observed_at",
    "expires_at",
})
_FORBIDDEN_KEYS = frozenset({
    "account",
    "account_id",
    "cash",
    "net_liquidation_value",
    "positions",
    "quantity",
})
_TOKEN_USAGE_PATHS = {
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "total_tokens": ("total_tokens",),
    "cached_input_tokens": ("input_tokens_details", "cached_tokens"),
    "cache_write_input_tokens": (
        "input_tokens_details",
        "cache_write_tokens",
    ),
    "reasoning_output_tokens": (
        "output_tokens_details",
        "reasoning_tokens",
    ),
}
_RATE_LIMIT_HEADER_FIELDS = {
    "x-ratelimit-limit-requests": ("requests", "limit"),
    "x-ratelimit-remaining-requests": ("requests", "remaining"),
    "x-ratelimit-reset-requests": (
        "requests",
        "reset_after_seconds",
    ),
    "x-ratelimit-limit-tokens": ("tokens", "limit"),
    "x-ratelimit-remaining-tokens": ("tokens", "remaining"),
    "x-ratelimit-reset-tokens": ("tokens", "reset_after_seconds"),
}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_MAX_SAFE_INTEGER = 9_223_372_036_854_775_807


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > 20 or re.fullmatch(r"\d+", text) is None:
            return None
        parsed = int(text)
    else:
        return None
    if parsed < 0 or parsed > _MAX_SAFE_INTEGER:
        return None
    return parsed


def _duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip().casefold()
        if not text or len(text) > 64:
            return None
        try:
            seconds = float(text)
        except ValueError:
            seconds = 0.0
            position = 0
            factors = {
                "ms": 0.001,
                "s": 1.0,
                "m": 60.0,
                "h": 3600.0,
            }
            for match in _DURATION_PART.finditer(text):
                if match.start() != position:
                    return None
                seconds += float(match.group(1)) * factors[match.group(2)]
                position = match.end()
            if position != len(text):
                return None
    else:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return round(seconds, 6)


def _header_values(headers: Any) -> dict[str, Any]:
    items = getattr(headers, "items", None)
    if not callable(items):
        return {}
    return {
        str(key).casefold(): value
        for key, value in items()
        if str(key).casefold() in (
            set(_RATE_LIMIT_HEADER_FIELDS) | {"retry-after"}
        )
    }


def _usage_value(
    usage: Mapping[str, Any],
    field: str,
    path: tuple[str, ...],
) -> Any:
    if field in usage:
        return usage.get(field)
    value: Any = usage
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def safe_worker_telemetry(
    *,
    response_headers: Any = None,
    response_usage: Any = None,
) -> dict[str, Any]:
    """Project only explicitly allowlisted numeric worker metadata."""
    usage = response_usage if isinstance(response_usage, Mapping) else {}
    telemetry = {
        "usage": {
            field: _nonnegative_int(_usage_value(usage, field, path))
            for field, path in _TOKEN_USAGE_PATHS.items()
        },
        "rate_limits": {
            dimension: {
                "limit": None,
                "remaining": None,
                "reset_after_seconds": None,
            }
            for dimension in ("requests", "tokens")
        },
        "retry_after_seconds": None,
    }
    headers = _header_values(response_headers)
    for header, (dimension, field) in _RATE_LIMIT_HEADER_FIELDS.items():
        raw_value = headers.get(header)
        normalized = (
            _duration_seconds(raw_value)
            if field == "reset_after_seconds"
            else _nonnegative_int(raw_value)
        )
        telemetry["rate_limits"][dimension][field] = normalized
    telemetry["retry_after_seconds"] = _duration_seconds(
        headers.get("retry-after")
    )
    return telemetry


def normalize_worker_telemetry(value: Any) -> dict[str, Any]:
    """Revalidate an already-projected telemetry mapping."""
    row = value if isinstance(value, Mapping) else {}
    usage = row.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    rate_limits = row.get("rate_limits")
    rate_limits = (
        rate_limits
        if isinstance(rate_limits, Mapping)
        else {}
    )
    normalized = safe_worker_telemetry(response_usage=usage)
    for dimension in ("requests", "tokens"):
        observed = rate_limits.get(dimension)
        observed = observed if isinstance(observed, Mapping) else {}
        normalized["rate_limits"][dimension] = {
            "limit": _nonnegative_int(observed.get("limit")),
            "remaining": _nonnegative_int(observed.get("remaining")),
            "reset_after_seconds": _duration_seconds(
                observed.get("reset_after_seconds")
            ),
        }
    normalized["retry_after_seconds"] = _duration_seconds(
        row.get("retry_after_seconds")
    )
    return normalized


def _forbidden_paths(value: Any, prefix: str = "") -> list[str]:
    paths = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}/{key}"
            if str(key).casefold() in _FORBIDDEN_KEYS:
                paths.append(path)
            paths.extend(_forbidden_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_forbidden_paths(child, f"{prefix}/{index}"))
    return paths


def _telemetry_validation_errors(value: Mapping[str, Any]) -> list[str]:
    errors = []
    telemetry = value.get("telemetry")
    if telemetry is None:
        return errors
    if (
        not isinstance(telemetry, Mapping)
        or telemetry != normalize_worker_telemetry(telemetry)
    ):
        errors.append("research_inbox_telemetry")
        return errors
    request = value.get("request")
    request = request if isinstance(request, Mapping) else {}
    attempts = request.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping):
            continue
        observed = attempt.get("telemetry")
        if (
            not isinstance(observed, Mapping)
            or observed != normalize_worker_telemetry(observed)
        ):
            errors.append(
                f"research_inbox_attempt_telemetry:{index}"
            )
    response = value.get("response")
    response = response if isinstance(response, Mapping) else {}
    if (
        "usage" in response
        and response.get("usage") != telemetry["usage"]
    ):
        errors.append("research_inbox_response_usage")
    return errors


def validate_inbox_record(
    value: Any,
    *,
    byte_length: int,
) -> list[str]:
    if byte_length > MAX_INBOX_BYTES:
        return [f"research_inbox_too_large:{byte_length}"]
    if not isinstance(value, Mapping):
        return ["research_inbox_record_not_object"]
    errors = [
        f"research_inbox_missing:{field}"
        for field in sorted(_REQUIRED_FIELDS - set(value))
    ]
    if value.get("schema_version") != RESEARCH_INBOX_SCHEMA_VERSION:
        errors.append("research_inbox_schema_version")
    for field in ("record_id", "worker_id"):
        text = str(value.get(field, "")).strip()
        if not text or _SAFE_ID.fullmatch(text) is None:
            errors.append(f"research_inbox_{field}")
    if value.get("origin") != "worker_attested":
        errors.append("research_inbox_origin")
    status = value.get("status")
    if status not in WORKER_STATUSES:
        errors.append("research_inbox_status")
    observed = parse_iso_timestamp(value.get("observed_at"))
    expires = parse_iso_timestamp(value.get("expires_at"))
    if observed is None:
        errors.append("research_inbox_observed_at")
    if expires is None:
        errors.append("research_inbox_expires_at")
    elif observed is not None and expires <= observed:
        errors.append("research_inbox_expiry_order")
    if status == "completed":
        for field in ("deployment", "selection", "target", "request", "result"):
            if not isinstance(value.get(field), Mapping):
                errors.append(f"research_inbox_completed_{field}")
    elif not isinstance(value.get("error"), Mapping):
        errors.append("research_inbox_error_marker")
    errors.extend(
        f"research_inbox_forbidden:{path}"
        for path in _forbidden_paths(value)
    )
    errors.extend(_telemetry_validation_errors(value))
    return sorted(set(errors))


def load_inbox_record(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"research_inbox_invalid_json:{path.name}:{error.pos}"
        ) from error
    errors = validate_inbox_record(value, byte_length=len(content))
    if errors:
        raise ValueError(
            f"research_inbox_invalid:{path.name}:"
            + ",".join(errors)
        )
    return dict(value)


def prune_expired_inbox(
    profile_root: Path | str,
    *,
    now: datetime | None = None,
) -> list[str]:
    root = Path(profile_root) / "research_inbox"
    observed_now = now or datetime.now(timezone.utc)
    removed = []
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else ():
        try:
            row = load_inbox_record(path)
        except (OSError, ValueError):
            continue
        expires = parse_iso_timestamp(row.get("expires_at"))
        if (
            expires is not None
            and expires.astimezone(timezone.utc) <= observed_now
        ):
            path.unlink()
            removed.append(str(path.relative_to(Path(profile_root))))
    return removed


def _result_digest(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, Mapping):
        return None
    required = {
        "summary",
        "hypotheses",
        "evidence_needed",
        "counterevidence",
        "uncertainties",
        "suggested_next_question",
    }
    if set(result) != required:
        return None
    if not all(
        isinstance(result.get(field), str)
        and result[field].strip()
        for field in ("summary", "suggested_next_question")
    ):
        return None
    if any(
        not isinstance(result.get(field), list)
        or any(not isinstance(item, str) or not item.strip()
               for item in result[field])
        for field in (
            "hypotheses",
            "evidence_needed",
            "counterevidence",
            "uncertainties",
        )
    ):
        return None
    digest = {
        "summary": str(result.get("summary", ""))[:1200],
        "suggested_next_question": str(
            result.get("suggested_next_question", "")
        )[:600],
    }
    for field in (
        "hypotheses",
        "evidence_needed",
        "counterevidence",
        "uncertainties",
    ):
        items = result.get(field)
        if not isinstance(items, list):
            digest[field] = []
            continue
        digest[field] = [
            str(item)[:600]
            for item in items[:3]
        ]
    return digest


def _has_usage(value: Mapping[str, Any]) -> bool:
    return any(
        value.get(field) is not None
        for field in _TOKEN_USAGE_PATHS
    )


def _has_rate_limit(value: Mapping[str, Any]) -> bool:
    limits = value.get("rate_limits")
    limits = limits if isinstance(limits, Mapping) else {}
    return (
        value.get("retry_after_seconds") is not None
        or any(
            isinstance(limits.get(dimension), Mapping)
            and any(
                limits[dimension].get(field) is not None
                for field in (
                    "limit",
                    "remaining",
                    "reset_after_seconds",
                )
            )
            for dimension in ("requests", "tokens")
        )
    )


def _record_telemetry(row: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("telemetry"), Mapping):
        return normalize_worker_telemetry(row["telemetry"])
    response = row.get("response")
    response = response if isinstance(response, Mapping) else {}
    return safe_worker_telemetry(response_usage=response.get("usage"))


def _record_retried(row: Mapping[str, Any]) -> bool:
    quality = row.get("quality")
    quality = quality if isinstance(quality, Mapping) else {}
    if quality.get("retried") is True:
        return True
    request = row.get("request")
    request = request if isinstance(request, Mapping) else {}
    attempts = request.get("attempts")
    return isinstance(attempts, list) and len(attempts) > 1


def _worker_health(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_worker: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["future"]:
            continue
        worker_id = str(row.get("worker_id", "")).strip()
        if not worker_id:
            continue
        health = by_worker.setdefault(worker_id, {
            "worker_id": worker_id,
            "latest_record": None,
            "last_success": None,
            "counts": {
                "records": 0,
                "completed": 0,
                "schema_complete": 0,
                "retried": 0,
            },
            "latest_usage": None,
            "latest_rate_limits": None,
        })
        health["counts"]["records"] += 1
        if health["latest_record"] is None:
            latest_rec = {
                "record_id": row.get("record_id"),
                "status": row.get("status"),
                "observed_at": row.get("observed_at"),
                "expires_at": row.get("expires_at"),
                "stale": row.get("stale", False),
            }
            error_code = safe_error_code(row)
            if error_code:
                latest_rec["error_code"] = error_code
            health["latest_record"] = latest_rec
        quality = row.get("quality")
        quality = quality if isinstance(quality, Mapping) else {}
        if row.get("status") == "completed":
            health["counts"]["completed"] += 1
            if health["last_success"] is None:
                health["last_success"] = {
                    "record_id": row.get("record_id"),
                    "observed_at": row.get("observed_at"),
                }
        if quality.get("result_schema_complete") is True:
            health["counts"]["schema_complete"] += 1
        if _record_retried(row):
            health["counts"]["retried"] += 1
        telemetry = _record_telemetry(row)
        usage = telemetry["usage"]
        if health["latest_usage"] is None and _has_usage(usage):
            health["latest_usage"] = {
                "record_id": row.get("record_id"),
                "observed_at": row.get("observed_at"),
                **usage,
            }
        if (
            health["latest_rate_limits"] is None
            and _has_rate_limit(telemetry)
        ):
            health["latest_rate_limits"] = {
                "record_id": row.get("record_id"),
                "observed_at": row.get("observed_at"),
                "requests": telemetry["rate_limits"]["requests"],
                "tokens": telemetry["rate_limits"]["tokens"],
                "retry_after_seconds": telemetry[
                    "retry_after_seconds"
                ],
            }
    return list(by_worker.values())


def _worker_alerts(
    health: list[dict[str, Any]],
    limit: int | None = 40,
) -> list[dict[str, Any]]:
    """Project worker alerts from health data.

    Alerts surface explicit failures and stale records:
    - Explicit failure statuses (auth_error, quota_exhausted, etc.): alert
    - Stale records (expires_at in past): alert

    Non-stale completed/no_work records do not alert (healthy status).

    Args:
        health: Worker health projection from _worker_health()
        limit: Maximum alerts to return; None means all (unbounded)

    Returns: alert dicts with:
    - worker_id, condition (status or "stale"), source_record_id,
      observed_at, expires_at, error_code (optional, only for failures)
    """
    alerts = []
    for worker in health:
        latest = worker.get("latest_record")
        if latest is None:
            continue

        worker_id = worker.get("worker_id", "")
        if not worker_id:
            continue

        status = latest.get("status", "")
        is_stale = latest.get("stale", False)

        # Alert on explicit failure status
        if status in FAILURE_STATUSES:
            alert = {
                "worker_id": worker_id,
                "condition": status,
                "source_record_id": latest.get("record_id", ""),
                "observed_at": latest.get("observed_at"),
                "expires_at": latest.get("expires_at"),
            }
            if latest.get("error_code"):
                alert["error_code"] = latest["error_code"]
            alerts.append(alert)

        # Alert on stale record
        # For completed/no_work: only alert if stale (healthy when fresh)
        # For other statuses: alert if stale
        elif is_stale:
            alert = {
                "worker_id": worker_id,
                "condition": "stale",
                "source_record_id": latest.get("record_id", ""),
                "observed_at": latest.get("observed_at"),
                "expires_at": latest.get("expires_at"),
            }
            alerts.append(alert)

    if limit is None:
        return alerts
    return alerts[:max(0, limit)]


def _load_summary_rows(
    profile_root: Path | str,
    *,
    observed_now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    root = Path(profile_root) / "research_inbox"
    rows = []
    invalid = []
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else ():
        try:
            row = load_inbox_record(path)
        except (OSError, ValueError) as error:
            invalid.append({
                "path": str(path.relative_to(root.parent)),
                "error": str(error)[:300],
            })
            continue
        observed = parse_iso_timestamp(row.get("observed_at"))
        expires = parse_iso_timestamp(row.get("expires_at"))
        row["future"] = (
            observed is None
            or observed.astimezone(timezone.utc) > observed_now
        )
        row["stale"] = (
            expires is None
            or expires.astimezone(timezone.utc) <= observed_now
        )
        row["_path"] = str(path.relative_to(Path(profile_root)))
        rows.append(row)
    rows.sort(
        key=lambda row: str(row.get("observed_at", "")),
        reverse=True,
    )
    return rows, invalid


def research_inbox_alerts(
    profile_root: Path | str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Extract all worker alerts from research inbox (unbounded).

    Public API for incident persistence. Returns all safe alerts across
    all discovered workers with no numeric limit, using the same canonical
    row-loading and stale/future logic as research_inbox_summary.

    Args:
        profile_root: Root directory containing research_inbox subdir
        now: Current time for stale/future checks (default: now UTC)

    Returns: Complete list of alert dicts, each with:
    - worker_id, condition, source_record_id, observed_at, expires_at,
      error_code (optional)
    """
    observed_now = now or datetime.now(timezone.utc)
    rows, _invalid = _load_summary_rows(
        profile_root,
        observed_now=observed_now,
    )
    health = _worker_health(rows)
    return _worker_alerts(health, limit=None)


def research_inbox_summary(
    profile_root: Path | str,
    *,
    now: datetime | None = None,
    limit: int = 12,
    active_incidents: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    observed_now = now or datetime.now(timezone.utc)
    rows, invalid = _load_summary_rows(
        profile_root,
        observed_now=observed_now,
    )
    eligible = []
    incomplete_result_count = 0
    seen = set()
    for row in rows:
        if row["stale"] or row["future"]:
            continue
        if row.get("status") == "completed":
            digest = _result_digest(row.get("result"))
            quality = row.get("quality")
            quality = quality if isinstance(quality, Mapping) else {}
            if digest is None or quality.get("result_schema_complete") is False:
                incomplete_result_count += 1
                continue
        else:
            digest = None
        target = row.get("target")
        target = target if isinstance(target, Mapping) else {}
        deployment = row.get("deployment")
        deployment = deployment if isinstance(deployment, Mapping) else {}
        identity = (
            str(target.get("question_id", "")),
            str(deployment.get("deployment", "")),
            str(row.get("worker_id", "")),
        )
        if identity in seen:
            continue
        seen.add(identity)
        eligible.append({
            "record_id": row.get("record_id"),
            "worker_id": row.get("worker_id"),
            "status": row.get("status"),
            "observed_at": row.get("observed_at"),
            "expires_at": row.get("expires_at"),
            "deployment": row.get("deployment"),
            "selection": row.get("selection"),
            "target": row.get("target"),
            "result": digest,
            "quality": row.get("quality"),
            "error": row.get("error"),
            "full_record_path": row.get("_path"),
        })
    bounded_limit = max(0, limit)
    health = _worker_health(rows)
    alerts = _worker_alerts(health, limit=40)
    summary = {
        "record_count": len(rows),
        "fresh_count": sum(
            not row["stale"] and not row["future"]
            for row in rows
        ),
        "stale_count": sum(row["stale"] for row in rows),
        "future_count": sum(row["future"] for row in rows),
        "invalid_count": len(invalid),
        "incomplete_result_count": incomplete_result_count,
        "worker_health": [],
        "worker_health_not_shown": len(health),
        "worker_alerts": [],
        "worker_alerts_not_shown": len(alerts),
        "worker_incidents": {
            "active_count": len(active_incidents) if active_incidents else 0,
            "incidents": [],
            "not_shown": len(active_incidents) if active_incidents else 0,
        },
        "items": [],
        "adoption_required_record_ids": [],
        "not_shown": len(eligible),
        "invalid": invalid[:4],
        "what_this_means": (
            "Optional worker-attested leads only, ordered by recency rather "
            "than decision rank. worker_health reports bounded measured "
            "status and explicitly supplied usage/rate-limit observations, "
            "not inferred capacity or a spending recommendation. "
            "worker_alerts surfaces explicit failure statuses and stale "
            "records per worker without hardcoded thresholds. Only "
            "adoption_required_record_ids need a host disposition. They "
            "never establish connector, forecast, instruction, order, or "
            "factual decision evidence. The phone path proceeds when this "
            "section is absent, empty, stale, or error-only."
        ),
    }
    for worker in health[:bounded_limit]:
        candidate = dict(summary)
        candidate["worker_health"] = summary["worker_health"] + [worker]
        candidate["worker_health_not_shown"] = (
            len(health) - len(candidate["worker_health"])
        )
        if len(json.dumps(
            candidate,
            ensure_ascii=False,
        ).encode("utf-8")) > RESEARCH_INBOX_SUMMARY_BYTE_BUDGET:
            break
        summary = candidate
    for alert in alerts[:bounded_limit]:
        candidate = dict(summary)
        candidate["worker_alerts"] = summary["worker_alerts"] + [alert]
        candidate["worker_alerts_not_shown"] = (
            len(alerts) - len(candidate["worker_alerts"])
        )
        if len(json.dumps(
            candidate,
            ensure_ascii=False,
        ).encode("utf-8")) > RESEARCH_INBOX_SUMMARY_BYTE_BUDGET:
            break
        summary = candidate
    for item in eligible[:bounded_limit]:
        candidate = dict(summary)
        candidate["items"] = summary["items"] + [item]
        candidate["adoption_required_record_ids"] = list(dict.fromkeys(
            str(row["record_id"])
            for row in candidate["items"]
            if row.get("status") == "completed"
            and str(row.get("record_id", "")).strip()
        ))
        candidate["not_shown"] = len(eligible) - len(candidate["items"])
        if len(json.dumps(
            candidate,
            ensure_ascii=False,
        ).encode("utf-8")) > RESEARCH_INBOX_SUMMARY_BYTE_BUDGET:
            break
        summary = candidate
    if active_incidents is None:
        active_incidents = {}
    incident_list = list(active_incidents.values())
    for incident in incident_list[:bounded_limit]:
        candidate = dict(summary)
        incidents_so_far = summary["worker_incidents"]["incidents"] + [incident]
        candidate["worker_incidents"] = {
            "active_count": len(incident_list),
            "incidents": incidents_so_far,
            "not_shown": len(incident_list) - len(incidents_so_far),
        }
        if len(json.dumps(
            candidate,
            ensure_ascii=False,
        ).encode("utf-8")) > RESEARCH_INBOX_SUMMARY_BYTE_BUDGET:
            break
        summary = candidate

    return summary
