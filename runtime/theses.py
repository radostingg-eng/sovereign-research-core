"""Theses the host holds, so "no new actionable thesis" stops being the end.

Four consecutive cycles concluded "external evidence does not establish a new
actionable thesis". That reads like diligence and functions like a dead end,
because nothing persisted a thesis between cycles. Each run asked "is there a
thesis?" from a standing start, found nothing complete enough to act on in
one hour of research, and said so. A thesis that takes a week to mature could
never form.

theses/ exists, THESIS_TEMPLATE.md exists, and both had been dormant since
they were written -- the same shape as ACTIVE_BRAIN.md, designed and never
wired to anything.

Surfacing them changes the question the host is answering. Instead of "can I
find a complete thesis today", it becomes "what did I already believe, has
anything falsified it, and is it now strong enough to act on". Those are
answerable in an hour. The first is not.

This reads and reports. It does not grade a thesis, rank theses, or decide
when one is ready, because all three are exactly the judgement the host
exists to make.
"""

from __future__ import annotations

from .profile_paths import code_root, profile_root
import re
from pathlib import Path
from typing import Any

THESES_DIR = profile_root() / "theses"

# Matched case-insensitively at the start of a line, so the template's
# "- Lifecycle state: `researching`" and the looser "Status: OPEN - ..."
# already in use both parse without rewriting either file.
_FIELDS = {
    "status": re.compile(r"^[-\s]*(?:lifecycle\s+state|status)\s*:\s*(.+)$", re.I),
    "last_reviewed": re.compile(r"^[-\s]*last\s+reviewed\s*:\s*(.+)$", re.I),
    "next_review_trigger": re.compile(r"^[-\s]*next\s+review\s+trigger\s*:\s*(.+)$", re.I),
}


def _clean(value: str) -> str:
    return value.strip().strip("`").strip() or ""


def read_thesis(path: Path) -> dict[str, Any]:
    """Name, state and review markers for one thesis file."""
    row: dict[str, Any] = {"thesis": path.stem, "file": f"theses/{path.name}"}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {**row, "status": "unreadable"}
    for line in text.splitlines():
        for field, pattern in _FIELDS.items():
            if field in row:
                continue
            match = pattern.match(line)
            if match:
                row[field] = _clean(match.group(1))
    row.setdefault("status", "unstated")
    return row


def open_theses(theses_dir: Path | str = THESES_DIR) -> list[dict[str, Any]]:
    directory = Path(theses_dir)
    if not directory.exists():
        return []
    return [read_thesis(p) for p in sorted(directory.glob("*.md"))
            if p.name != "THESIS_TEMPLATE.md"]


def summarise(theses_dir: Path | str = THESES_DIR) -> dict[str, Any]:
    """The block the host reads before asking whether a thesis exists."""
    rows = open_theses(theses_dir)
    return {
        "count": len(rows),
        "theses": rows,
        "what_this_means": (
            "Theses you already hold. Review them before looking for a new "
            "one: has anything falsified this, has a review trigger fired, is "
            "it now strong enough to act on. A thesis that needs a week of "
            "evidence can never form if every cycle starts from nothing, "
            "which is why 'no new actionable thesis' had become the standing "
            "answer. Record a new thesis in theses/<NAME>.md using "
            "THESIS_TEMPLATE.md, update the ones you revisit, and mark a "
            "falsified one falsified rather than deleting it."
        ) if rows else (
            "No theses recorded. If research suggests something worth "
            "tracking but not yet worth acting on, write it to "
            "theses/<NAME>.md rather than discarding it at the end of the "
            "cycle."
        ),
    }
