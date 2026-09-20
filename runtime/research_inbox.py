"""Validate and summarize optional external research-worker records."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .timestamps import parse_iso_timestamp

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
        row["_path"] = str(path.relative_to(Path(profile_root)))
        rows.append(row)
    rows.sort(
        key=lambda row: str(row.get("observed_at", "")),
        reverse=True,
    )
    eligible = []
    incomplete_result_count = 0
    seen = set()
    for row in rows:
        if row["stale"]:
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
    items = []
    for item in eligible[:limit]:
        candidate = items + [item]
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > (
            RESEARCH_INBOX_SUMMARY_BYTE_BUDGET
        ):
            break
        items = candidate
    summary = {
        "record_count": len(rows),
        "fresh_count": sum(not row["stale"] for row in rows),
        "stale_count": sum(row["stale"] for row in rows),
        "invalid_count": len(invalid),
        "incomplete_result_count": incomplete_result_count,
        "items": items,
        "not_shown": max(0, len(eligible) - len(items)),
        "invalid": invalid[:4],
        "what_this_means": (
            "Optional worker-attested research only. The phone path remains "
            "authoritative for connector evidence and proceeds when this "
            "section is absent, empty, stale, or error-only. Full worker "
            "records remain available at full_record_path."
        ),
    }
    if len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) > (
        RESEARCH_INBOX_SUMMARY_BYTE_BUDGET
    ):
        raise ValueError("research_inbox_summary_exceeds_byte_budget")
    return summary
