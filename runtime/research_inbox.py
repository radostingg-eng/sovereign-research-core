"""Validate and summarize optional external research-worker records."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .timestamps import parse_iso_timestamp

RESEARCH_INBOX_SCHEMA_VERSION = 1
MAX_INBOX_BYTES = 32_000
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


def research_inbox_summary(
    profile_root: Path | str,
    *,
    now: datetime | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    root = Path(profile_root) / "research_inbox"
    observed_now = now or datetime.now(timezone.utc)
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
        expires = parse_iso_timestamp(row.get("expires_at"))
        row["stale"] = (
            expires is None
            or expires.astimezone(timezone.utc) <= observed_now
        )
        rows.append(row)
    rows.sort(
        key=lambda row: str(row.get("observed_at", "")),
        reverse=True,
    )
    items = [{
        "record_id": row.get("record_id"),
        "worker_id": row.get("worker_id"),
        "status": row.get("status"),
        "observed_at": row.get("observed_at"),
        "expires_at": row.get("expires_at"),
        "stale": row.get("stale"),
        "deployment": row.get("deployment"),
        "selection": row.get("selection"),
        "target": row.get("target"),
        "result": row.get("result"),
        "error": row.get("error"),
    } for row in rows[:limit]]
    return {
        "record_count": len(rows),
        "fresh_count": sum(not row["stale"] for row in rows),
        "stale_count": sum(row["stale"] for row in rows),
        "invalid_count": len(invalid),
        "items": items,
        "not_shown": max(0, len(rows) - limit),
        "invalid": invalid[:4],
        "what_this_means": (
            "Optional worker-attested research only. The phone path remains "
            "authoritative for connector evidence and proceeds when this "
            "section is absent, empty, stale, or error-only."
        ),
    }
