"""Detect a truncated audit journal, which the chain cannot do alone.

Rewriting a record is caught: every record's hash covers the one before it,
so altering record 10 breaks record 11. Truncation is not. Dropping the last
N records leaves a shorter chain that is perfectly valid, because a prefix of
a valid chain is a valid chain. Verified before this module was written:

    23 records, integrity: ok
    drop the last record   -> integrity: ok
    drop the last five     -> integrity: ok
    rewrite record 10      -> integrity: FAIL

That is the dangerous direction. The newest records are the receipts saying
what the system just decided, so the cheapest way to erase an inconvenient
decision was to delete the tail, and nothing would have noticed.

The fix cannot live inside the journal, because the journal is the thing
being truncated. A mark is kept beside it recording how many records have
existed and which record was the tip. It only ever grows. Lowering it means
editing a tracked file, which is a deliberate, reviewable act in the commit
history rather than a silent deletion.

SELF_INTEGRITY.md calls this "no unexplained overwrite of historical
records". Issue #1, the tampered record, was this class, and it was caught
only because the chain happened to cover that particular edit.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

MARK_FILENAME = ".high_water.json"


def mark_path(audit_dir: str | Path) -> Path:
    return Path(audit_dir) / MARK_FILENAME


def observed_state(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the journal currently claims about its own size and tip."""
    return {
        "records": len(records),
        "tip_hash": str(records[-1].get("record_hash", "")) if records else "",
    }


def read_mark(audit_dir: str | Path) -> dict[str, Any] | None:
    """The recorded high-water mark, or None if there is not one yet."""
    path = mark_path(audit_dir)
    if not path.exists():
        return None
    try:
        mark = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # An unreadable mark is not an absent one. Treating it as absent
        # would make "corrupt the mark" the way around the check.
        return {"records": -1, "tip_hash": "", "unreadable": True}
    return mark if isinstance(mark, dict) else {"records": -1, "tip_hash": "",
                                                "unreadable": True}


def check_high_water(records: Sequence[Mapping[str, Any]],
                     audit_dir: str | Path) -> list[str]:
    """Problems with the journal's size relative to its recorded high water."""
    mark = read_mark(audit_dir)
    if mark is None:
        # No mark yet is not a violation; it is the state before the first
        # one is written. It is reported so it cannot be a permanent excuse.
        return []
    if mark.get("unreadable"):
        return ["high_water_mark_unreadable"]
    observed = observed_state(records)
    errors: list[str] = []
    recorded = mark.get("records")
    if not isinstance(recorded, int):
        return ["high_water_mark_malformed"]
    if observed["records"] < recorded:
        errors.append(
            f"journal_truncated:had={recorded}:now={observed['records']}")
    tip = str(mark.get("tip_hash", ""))
    if tip and not any(str(r.get("record_hash", "")) == tip for r in records):
        # The recorded tip is gone even though the count did not fall, which
        # is what a rewritten tail looks like once it has been padded back
        # out to the original length.
        errors.append(f"high_water_tip_missing:{tip[:16]}")
    return errors


def update_mark(records: Sequence[Mapping[str, Any]],
                audit_dir: str | Path) -> dict[str, Any]:
    """Advance the mark. Never retreats it.

    Refusing to lower it here means a truncation cannot be laundered by
    running the updater afterwards; the gate stays failed until a human
    edits the tracked file and explains why in the commit.
    """
    observed = observed_state(records)
    existing = read_mark(audit_dir)
    # Advancing over an unsatisfied mark erases the very warning it exists to
    # raise. The runner calls this BEFORE the integrity gate, so a tail that
    # was replaced and padded back to the original length was detected, then
    # overwritten, then reported clean: the detector laundering the tamper it
    # had just found. A corrupt mark was overwritten the same way.
    #
    # So the current mark must be SATISFIED before it may be replaced. A
    # journal that fails the check keeps failing until a human looks.
    if existing is not None and check_high_water(records, audit_dir):
        return existing
    if existing and not existing.get("unreadable"):
        recorded = existing.get("records")
        if isinstance(recorded, int) and observed["records"] < recorded:
            return existing
        # Nothing changed, so writing would only move updated_at. That made
        # every idle pass dirty the tree and the scheduler push a one-line
        # commit, which is the same bug that was just fixed in the feedback
        # file, reintroduced here.
        if (recorded == observed["records"]
                and str(existing.get("tip_hash", "")) == observed["tip_hash"]):
            return existing
    payload = {
        "records": observed["records"],
        "tip_hash": observed["tip_hash"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "note": ("Monotonic. The journal may grow, never shrink. A decrease "
                 "means records were deleted, which the hash chain cannot "
                 "detect because a prefix of a valid chain is still valid."),
    }
    path = mark_path(audit_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload
