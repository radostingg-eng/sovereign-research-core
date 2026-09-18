"""Deterministic checks for the human and machine delivery ledgers."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from typing import Any, Mapping, Sequence

from .cycle_receipt import derive_receipt_status
from .profile_paths import code_root, profile_root


_ROW = re.compile(r"^\|\s*([A-Z]+-\d+)\s*\|[^|]*\|\s*([^|]+?)\s*\|", re.MULTILINE)


def parse_delivery_plan(text: str) -> dict[str, str]:
    """Extract ID/status pairs from delivery tables without interpreting prose."""
    return {match.group(1): match.group(2).strip() for match in _ROW.finditer(text)}


def load_delivery_state(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def reconcile_delivery_state(plan_text: str, state: Mapping[str, Any]) -> list[str]:
    parsed = parse_delivery_plan(plan_text)
    machine: dict[str, str] = {}
    for group in ("hard_haves", "xl_items", "self_improvement", "historical_items"):
        machine.update({str(k): str(v) for k, v in state.get(group, {}).items()})
    errors: list[str] = []
    for item_id, status in parsed.items():
        if item_id not in machine:
            errors.append(f"missing_machine_state:{item_id}")
        elif machine[item_id] != status:
            errors.append(f"status_drift:{item_id}:{status}!={machine[item_id]}")
    # Reconciliation only walked plan -> machine, so an EMPTY plan passed
    # against any machine state at all. Dropping an item from the plan was
    # therefore invisible, and dropping it is exactly how a tracked
    # obligation stops being tracked. Both sides must account for the other.
    for item_id in sorted(set(machine) - set(parsed)):
        errors.append(f"missing_plan_entry:{item_id}:{machine[item_id]}")
    return errors


def assert_delivery_state(plan_path: str | Path, state_path: str | Path) -> None:
    errors = reconcile_delivery_state(
        Path(plan_path).read_text(encoding="utf-8"), load_delivery_state(state_path)
    )
    if errors:
        raise ValueError("delivery_state_drift:" + ",".join(errors))


def reconcile_receipt_status(state: Mapping[str, Any],
                             records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Refuse a curated receipt-status claim the journal does not support.

    STATE.json said "migration_pending_first_persisted_receipt" while eleven
    receipt records existed. Nothing compared the two, so the prose drifted
    from the evidence and then outranked it, because prose is what gets read.
    """
    curated = str(state.get("validation", {}).get("cycle_receipt_status", ""))
    if not curated:
        return ["missing_curated_receipt_status"]
    derived = derive_receipt_status(records)
    if curated != derived:
        return [f"receipt_status_drift:curated={curated}:journal={derived}"]
    return []


def main(argv: list[str] | None = None) -> int:
    """Verify both delivery ledgers and the receipt-status claim."""
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        # Explicit callers keep the historical single-root behavior.
        core = profile = Path(argv[0]).resolve()
    else:
        core = code_root()
        profile = profile_root()
    plan_path = core / "DELIVERY_PLAN.md"
    state_path = profile / "DELIVERY_STATE.json"
    state = load_delivery_state(state_path)
    errors = reconcile_delivery_state(
        plan_path.read_text(encoding="utf-8"), state)

    from .integrity import load_journal_records

    operating_state = load_delivery_state(profile / "STATE.json")
    errors.extend(
        reconcile_receipt_status(operating_state, load_journal_records()))
    if errors:
        for error in errors:
            print(f"DELIVERY DRIFT: {error}")
        return 1
    print("delivery state: reconciled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
