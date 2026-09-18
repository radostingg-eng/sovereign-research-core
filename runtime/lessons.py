"""Lessons the host has drawn, so a conclusion outlives the cycle that made it.

decision_outcomes tells the host what followed each decision, and the prompt
asks it to record what it concludes. There was nowhere to put one. evolution.py
has derived lessons since it was written and the journal holds zero of them:
the fourth piece of infrastructure today found designed and never wired, after
ACTIVE_BRAIN.md, theses/ and effectiveness.py.

Without this, a conclusion drawn in one cycle dies with it, and the next run
re-derives it or does not. That is the same cold-start problem as memory and
theses, one level up: memory is what I concluded, theses are what I believe,
lessons are what I have learned about deciding.

A lesson is a cognitive act, so the host writes it. This persists it, keeps it
bounded, and surfaces it. It does not generate lessons, rank them, or decide
when one has been learned.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# ACTIVE_BRAIN.md's admission rule: only what can materially affect current or
# recurring decisions. A lesson list that grows without limit becomes the raw
# archive that file explicitly refuses to be, and the host pays to read it
# every cycle.
MAX_SURFACED = 12

REQUIRED_FIELDS = ("lesson", "evidence")


def validate_lesson(row: Any) -> list[str]:
    """Why one proposed lesson cannot be recorded."""
    if not isinstance(row, Mapping):
        return ["lesson_not_an_object"]
    problems = []
    for field in REQUIRED_FIELDS:
        if not str(row.get(field, "") or "").strip():
            problems.append(f"lesson_missing_{field}")
    # A lesson nothing could ever contradict is a slogan. Requiring the
    # condition that would overturn it is the same discipline theses get, and
    # for the same reason: an unfalsifiable belief accumulates forever and
    # quietly shapes every later decision.
    if not str(row.get("falsified_if", "") or "").strip():
        problems.append("lesson_missing_falsified_if")
    return problems


def validate_lessons(data: Mapping[str, Any]) -> list[str]:
    """Check any lessons attached to a cycle. Absent is fine; empty is fine."""
    lessons = data.get("lessons")
    if lessons is None:
        return []
    if not isinstance(lessons, Sequence) or isinstance(lessons, str):
        return ["lessons_must_be_a_list"]
    problems = []
    for index, row in enumerate(lessons):
        problems.extend(f"{p}:{index}" for p in validate_lesson(row))
    return problems


def recorded_lessons(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Lessons in the journal, newest last, superseded ones dropped."""
    lessons: dict[str, dict[str, Any]] = {}
    superseded: set[str] = set()
    for record in records:
        if record.get("record_type") != "lesson":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        lesson_id = str(payload.get("lesson_id", "")) or str(record.get("record_id", ""))
        lessons[lesson_id] = {
            "lesson_id": lesson_id,
            "lesson": payload.get("lesson"),
            "evidence": payload.get("evidence"),
            "falsified_if": payload.get("falsified_if"),
            "at": payload.get("at") or record.get("at"),
            "status": payload.get("status", "held"),
        }
        for earlier in payload.get("supersedes") or ():
            superseded.add(str(earlier))
    return [row for lesson_id, row in lessons.items()
            if lesson_id not in superseded][-MAX_SURFACED:]


def summarise(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The block the host reads before drawing the same conclusion again."""
    rows = recorded_lessons(records)
    return {
        "count": len(rows),
        "lessons": rows,
        "what_this_means": (
            "What you have concluded about deciding, not about any one "
            "position. Check whether a lesson's falsified_if has come true "
            "before relying on it, and say so when one has. Record a new one "
            "by sending \"lessons\": [{\"lesson\": ..., \"evidence\": ..., "
            "\"falsified_if\": ...}] with your cycle; add \"supersedes\" when "
            "it replaces an earlier one. A lesson nothing could contradict is "
            "a slogan, which is why falsified_if is required."
        ) if rows else (
            "No lessons recorded yet. When decision_outcomes lets you "
            "conclude something about how you decide, record it rather than "
            "leaving it in the cycle, or the next run starts from nothing."
        ),
    }
