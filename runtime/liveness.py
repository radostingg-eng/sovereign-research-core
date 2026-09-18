"""Notice when the loop has stopped producing, and say so.

Every part of this system reports to something. Refusals go to the host via
FEEDBACK.json. Receipts go to the journal. Integrity failures fail the gate.
But if the host simply stops committing, or commits garbage for ten cycles
running, or the laptop sleeps and the fallback never fires, nothing reports
anything to the OPERATOR. The loop goes quiet and quiet reads exactly like
healthy.

That is the last thing standing between "runs unattended" and "runs
unattended and you can trust it to". A system that only reports when it is
working is not monitored, it is merely observed when someone happens to look.

This turns silence into a signal. It is deliberately dumb: it asks when the
last receipt landed and whether recent inputs are stuck being refused. It
does not try to diagnose why, because the feedback file already carries that
and a second opinion computed from less evidence would only disagree.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .timestamps import parse_iso_timestamp

# The host runs roughly hourly. Three missed cycles is long enough that a
# transient stall has had time to resolve itself, and short enough that a
# genuine outage is caught the same working day.
DEFAULT_MAX_RECEIPT_AGE_HOURS = 3.0

# One refused input is normal; the host reads the feedback and corrects. A
# run of them means the correction loop is not working, which is the failure
# that silence hides best.
DEFAULT_MAX_CONSECUTIVE_REFUSALS = 3


def _parse(stamp: Any) -> datetime | None:
    parsed = parse_iso_timestamp(stamp)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else None


def newest_receipt_age_hours(records: Sequence[Mapping[str, Any]],
                             *, now: datetime | None = None) -> float | None:
    """Hours since the most recent cycle receipt, or None if there are none."""
    now = now or datetime.now(timezone.utc)
    newest = None
    for record in records:
        if record.get("record_type") != "cycle_receipt":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        stamp = _parse(payload.get("completed_at")) or _parse(record.get("at"))
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    if newest is None:
        return None
    return (now - newest).total_seconds() / 3600.0


def consecutive_refusals(records: Sequence[Mapping[str, Any]]) -> int:
    """Refusals since the last receipt.

    Counted since the last SUCCESS rather than in total, because a refusal
    the host subsequently recovered from is not a stall; it is the loop
    working. Only an unbroken run means the correction is not landing.
    """
    run = 0
    for record in records:
        kind = record.get("record_type")
        if kind == "cycle_receipt":
            run = 0
        elif kind == "host_input_refusal":
            run += 1
    return run


def check_liveness(records: Sequence[Mapping[str, Any]], *,
                   max_age_hours: float = DEFAULT_MAX_RECEIPT_AGE_HOURS,
                   max_refusals: int = DEFAULT_MAX_CONSECUTIVE_REFUSALS,
                   now: datetime | None = None) -> list[str]:
    """What an operator would want waking up for. Empty means healthy."""
    problems: list[str] = []
    age = newest_receipt_age_hours(records, now=now)
    if age is None:
        problems.append("no_cycle_receipt_has_ever_been_persisted")
    elif age > max_age_hours:
        problems.append(
            f"no_cycle_in_{age:.1f}h:the host, the executor or the schedule "
            f"has stopped, and nothing else would have told you")
    run = consecutive_refusals(records)
    if run >= max_refusals:
        problems.append(
            f"{run}_consecutive_refusals_since_the_last_receipt:the host is "
            f"not recovering from the feedback it is being given")
    return problems


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .integrity import load_journal_records

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-hours", type=float,
                        default=DEFAULT_MAX_RECEIPT_AGE_HOURS)
    parser.add_argument("--max-refusals", type=int,
                        default=DEFAULT_MAX_CONSECUTIVE_REFUSALS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    records = load_journal_records()
    problems = check_liveness(records, max_age_hours=args.max_age_hours,
                              max_refusals=args.max_refusals)
    age = newest_receipt_age_hours(records)
    if args.json:
        print(json.dumps({"healthy": not problems, "problems": problems,
                          "newest_receipt_age_hours": age}, indent=2))
    else:
        print(f"newest receipt: "
              f"{'never' if age is None else f'{age:.1f}h ago'}")
        print(f"consecutive refusals: {consecutive_refusals(records)}")
        for problem in problems:
            print(f"  STALLED  {problem}")
        print("\nloop: ok" if not problems else "\nloop: STALLED")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
