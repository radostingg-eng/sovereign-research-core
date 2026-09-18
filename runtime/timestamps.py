"""Portable ISO 8601 timestamp parsing for host-supplied evidence."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any


_ISO_TIMESTAMP = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:(?P<separator>[Tt ])"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<fraction>[.,]\d+)?"
    r"(?P<offset>Z|z|[+-]\d{2}(?::?\d{2})?)?"
    r")?$"
)


def normalize_iso_timestamp(value: Any) -> str | None:
    """Normalize supported ISO timestamps before stdlib parsing.

    Python 3.9 only accepts three or six fractional digits while newer
    versions accept arbitrary precision. Normalize once so validation does
    not depend on the interpreter that happens to execute the cycle.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    match = _ISO_TIMESTAMP.fullmatch(value.strip())
    if match is None:
        return None
    if match.group("separator") is None:
        return match.group("date")

    fraction = match.group("fraction")
    normalized_fraction = ""
    if fraction:
        digits = fraction[1:]
        normalized_fraction = "." + (digits + "000000")[:6]

    offset = match.group("offset") or ""
    if offset in {"Z", "z"}:
        offset = "+00:00"
    elif offset and ":" not in offset:
        offset = (
            f"{offset}:00"
            if len(offset) == 3
            else f"{offset[:3]}:{offset[3:]}"
        )

    return (
        f"{match.group('date')}T{match.group('hour')}:"
        f"{match.group('minute')}:{match.group('second')}"
        f"{normalized_fraction}{offset}"
    )


def parse_iso_timestamp(value: Any) -> datetime | None:
    """Return a parsed timestamp, or None when the value is unsupported."""
    normalized = normalize_iso_timestamp(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
