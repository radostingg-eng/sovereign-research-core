"""Validated host-visible connector and action inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from .timestamps import parse_iso_timestamp


REQUIRED_IBKR_ACTIONS = frozenset({
    "get account orders",
    "get account trades",
    "get account balances",
    "get account positions",
    "get account summary",
    "get alert",
    "get alerts",
    "get combo identifier",
    "get company connections",
    "get company themes",
    "get option data",
    "get option parameters",
    "get order instructions",
    "get pa allocation",
    "get pa performance all periods",
    "get price history",
    "get price snapshot",
    "get theme details",
    "get watchlist",
    "get watchlists",
    "search contracts",
    "search futures",
    "search investment topk",
    "what's new",
    "create alert",
    "create order instruction",
    "create watchlist",
    "delete alert",
    "delete order instruction",
    "delete watchlist",
    "edit watchlist",
    "provide customer feedback",
    "set alert status",
    "update alert",
})
ACTION_MODES = frozenset({"read", "write_nontransmitting", "write", "unknown"})
NONTRANSMITTING_WRITE_ACTIONS = frozenset({
    "create order instruction",
    "delete order instruction",
})
WRITE_ACTIONS = frozenset({
    "create alert",
    "create watchlist",
    "delete alert",
    "delete watchlist",
    "edit watchlist",
    "provide customer feedback",
    "set alert status",
    "update alert",
})
ACTION_ALIASES = {
    "search investment topics": "search investment topk",
}


def normalize_action_name(value: Any) -> str:
    """Normalize UI labels and connector identifiers for comparison only."""
    name = str(value or "").strip().lower().replace("'", "")
    name = re.sub(r"[_-]+", " ", name)
    name = " ".join(name.split())
    return ACTION_ALIASES.get(name, name)


NORMALIZED_NONTRANSMITTING_WRITE_ACTIONS = frozenset(
    normalize_action_name(name) for name in NONTRANSMITTING_WRITE_ACTIONS
)
NORMALIZED_WRITE_ACTIONS = frozenset(
    normalize_action_name(name) for name in WRITE_ACTIONS
)
NORMALIZED_REQUIRED_IBKR_ACTIONS = frozenset(
    normalize_action_name(name) for name in REQUIRED_IBKR_ACTIONS
)


def _parse_timestamp(value: Any) -> bool:
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_tool_manifest_report(value: Any) -> list[str]:
    """Validate what the host says was invokable in its current session."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return ["tool_manifest_report_not_an_object"]
    errors = []
    if not _parse_timestamp(value.get("observed_at")):
        errors.append("tool_manifest_observed_at_invalid")
    if value.get("complete_for_current_session") is not True:
        errors.append("tool_manifest_not_declared_complete")
    connectors = value.get("connectors")
    if not isinstance(connectors, list) or not connectors:
        return errors + ["tool_manifest_connectors_must_be_nonempty_list"]

    ibkr_actions: set[str] = set()
    ibkr_action_rows_valid = True
    for connector_index, connector in enumerate(connectors):
        if not isinstance(connector, Mapping):
            errors.append(
                f"tool_manifest_connector_not_object:{connector_index}")
            continue
        connector_name = str(connector.get("name", "")).strip()
        if not connector_name:
            errors.append(
                f"tool_manifest_connector_name_required:{connector_index}")
        actions = connector.get("actions")
        if not isinstance(actions, list) or not actions:
            errors.append(
                f"tool_manifest_actions_must_be_nonempty_list:"
                f"{connector_name or connector_index}")
            continue
        is_ibkr = connector_name.lower() in {
            "ibkr", "interactive brokers", "interactive brokers (ibkr)",
        }
        if any(not isinstance(action, Mapping) for action in actions):
            errors.append(
                f"tool_manifest_actions_must_be_objects:"
                f"{connector_name or connector_index}")
            if is_ibkr:
                ibkr_action_rows_valid = False
            continue
        names = []
        for action_index, action in enumerate(actions):
            action_name = str(action.get("name", "")).strip()
            if not action_name:
                errors.append(
                    f"tool_manifest_action_name_required:"
                    f"{connector_name or connector_index}:{action_index}")
            else:
                names.append(normalize_action_name(action_name))
            if not isinstance(action.get("inputs"), list):
                errors.append(
                    f"tool_manifest_action_inputs_must_be_list:"
                    f"{connector_name or connector_index}:{action_index}")
            if not str(action.get("returns", "")).strip():
                errors.append(
                    f"tool_manifest_action_returns_required:"
                    f"{connector_name or connector_index}:{action_index}")
            if action.get("mode") not in ACTION_MODES:
                errors.append(
                    f"tool_manifest_action_mode_invalid:"
                    f"{connector_name or connector_index}:{action_index}")
            normalized_name = normalize_action_name(action_name)
            expected_mode = (
                "write_nontransmitting"
                if normalized_name in NORMALIZED_NONTRANSMITTING_WRITE_ACTIONS
                else "write"
                if normalized_name in NORMALIZED_WRITE_ACTIONS
                else "read"
                if normalized_name in NORMALIZED_REQUIRED_IBKR_ACTIONS
                else None
            )
            if expected_mode is not None and action.get("mode") != expected_mode:
                errors.append(
                    f"tool_manifest_known_action_mode_mismatch:"
                    f"{action_name}:{expected_mode}")
        if len(names) != len(set(names)):
            errors.append(
                f"tool_manifest_duplicate_action:"
                f"{connector_name or connector_index}")
        if is_ibkr:
            ibkr_actions.update(names)

    if ibkr_action_rows_valid:
        required_by_normalized = {
            normalize_action_name(action): action
            for action in REQUIRED_IBKR_ACTIONS
        }
        missing_ibkr = sorted(
            canonical
            for normalized, canonical in required_by_normalized.items()
            if normalized not in ibkr_actions
        )
        errors.extend(
            f"tool_manifest_missing_known_ibkr_action:{action}"
            for action in missing_ibkr
        )
    for field in (
        "manifest_discrepancies", "unreachable_manifest_connectors",
    ):
        if not isinstance(value.get(field), list):
            errors.append(f"tool_manifest_field_must_be_list:{field}")
    return errors


def lookalike_manifest_preview(value: Any, key: str) -> list[str]:
    """Describe downstream gaps without accepting a non-canonical key."""
    if not isinstance(value, Mapping):
        return []
    missing_fields = []
    if not _parse_timestamp(value.get("observed_at")):
        missing_fields.append("observed_at")
    if value.get("complete_for_current_session") is not True:
        missing_fields.append("complete_for_current_session")
    connectors = value.get("connectors")
    if not isinstance(connectors, list) or not connectors:
        missing_fields.append("connectors")
    for field in (
        "manifest_discrepancies", "unreachable_manifest_connectors",
    ):
        if not isinstance(value.get(field), list):
            missing_fields.append(field)

    ibkr_actions: set[str] = set()
    if isinstance(connectors, list):
        for connector in connectors:
            if not isinstance(connector, Mapping):
                continue
            if str(connector.get("name", "")).strip().lower() not in {
                "ibkr", "interactive brokers", "interactive brokers (ibkr)",
            }:
                continue
            actions = connector.get("actions")
            if isinstance(actions, list):
                ibkr_actions.update(
                    normalize_action_name(action.get("name"))
                    for action in actions
                    if isinstance(action, Mapping) and action.get("name")
                )
    missing_ibkr = sorted(
        action for action in REQUIRED_IBKR_ACTIONS
        if normalize_action_name(action) not in ibkr_actions
    )
    details = []
    if missing_fields:
        details.append("missing_fields=" + "|".join(missing_fields))
    if missing_ibkr:
        details.append("missing_known_ibkr=" + "|".join(missing_ibkr))
    if not details:
        details.append("canonical_key_only")
    return [
        f"tool_manifest_lookalike_contract_preview:{key}:"
        + ";".join(details)
    ]


def latest_tool_inventory(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Latest persisted capability report for host and operator visibility."""
    for record in reversed(list(records)):
        if record.get("record_type") != "tool_inventory":
            continue
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            return dict(payload)
    return {}


def full_inventory_for_record(
    records: Sequence[Mapping[str, Any]],
    record_id: str,
) -> dict[str, Any]:
    """Return the immutable full inventory named by a feedback digest."""
    for record in records:
        if record.get("record_id") != record_id:
            continue
        if record.get("record_type") != "tool_inventory":
            raise ValueError(f"record_is_not_tool_inventory:{record_id}")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError(f"tool_inventory_payload_invalid:{record_id}")
        return dict(payload)
    raise ValueError(f"tool_inventory_record_not_found:{record_id}")


def _normalized_inputs(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return sorted(
        value,
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _normalized_capabilities(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    connectors = []
    for connector in report.get("connectors") or ():
        if not isinstance(connector, Mapping):
            continue
        actions = []
        for action in connector.get("actions") or ():
            if not isinstance(action, Mapping):
                continue
            actions.append({
                "name": normalize_action_name(action.get("name")),
                "inputs": _normalized_inputs(action.get("inputs")),
                "returns": str(action.get("returns", "")).strip(),
                "mode": action.get("mode"),
            })
        actions.sort(key=lambda row: row["name"])
        connectors.append({
            "name": str(connector.get("name", "")).strip().casefold(),
            "actions": actions,
        })
    connectors.sort(key=lambda row: row["name"])
    return connectors


def canonical_inventory_hash(report: Mapping[str, Any]) -> str:
    """Hash capability content without predecessor-dependent diff metadata."""
    encoded = json.dumps(
        _normalized_capabilities(report),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _action_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "connector": row.get("connector"),
        "action": row.get("action"),
        "mode": row.get("mode"),
    }


def _changed_action_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    before = row.get("before")
    after = row.get("after")
    before = before if isinstance(before, Mapping) else {}
    after = after if isinstance(after, Mapping) else {}
    fields_changed = [
        field for field in ("inputs", "returns", "mode")
        if before.get(field) != after.get(field)
    ]
    return {
        "connector": after.get("connector") or before.get("connector"),
        "action": after.get("action") or before.get("action"),
        "before_mode": before.get("mode"),
        "after_mode": after.get("mode"),
        "fields_changed": fields_changed,
    }


def _bounded_strings(value: Any, *, limit: int = 10) -> dict[str, Any]:
    rows = value if isinstance(value, list) else []
    shown = []
    for row in rows[:limit]:
        text = (
            row if isinstance(row, str)
            else json.dumps(row, ensure_ascii=False, sort_keys=True)
        )
        shown.append(text[:300])
    return {
        "count": len(rows),
        "items": shown,
        "not_shown": max(0, len(rows) - limit),
    }


def tool_inventory_feedback(
    records: Sequence[Mapping[str, Any]],
    *,
    journal_path: str | Path,
    row_limit: int = 20,
) -> dict[str, Any]:
    """Project the full journal inventory into bounded, stale-aware feedback."""
    source_index = None
    source_record = None
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if record.get("record_type") == "tool_inventory":
            source_index = index
            source_record = record
            break
    if source_record is None or source_index is None:
        return {}
    payload = source_record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("latest_tool_inventory_payload_invalid")
    validation_errors = validate_tool_manifest_report(payload)
    if validation_errors:
        raise ValueError(
            "latest_tool_inventory_invalid:" + ",".join(validation_errors))

    source_record_id = str(source_record.get("record_id", ""))
    source_cycle_id = next(
        (
            str(cause).removeprefix("cycle-receipt:")
            for cause in source_record.get("caused_by") or ()
            if str(cause).startswith("cycle-receipt:")
        ),
        None,
    )
    later_receipts = [
        record for record in records[source_index + 1:]
        if record.get("record_type") == "cycle_receipt"
    ]
    cycles_since_observed = len(later_receipts)

    connectors = []
    action_count = 0
    for connector in payload.get("connectors") or ():
        if not isinstance(connector, Mapping):
            continue
        actions = [
            action for action in connector.get("actions") or ()
            if isinstance(action, Mapping)
        ]
        action_count += len(actions)
        mode_counts = {
            mode: sum(1 for action in actions if action.get("mode") == mode)
            for mode in sorted(ACTION_MODES)
            if any(action.get("mode") == mode for action in actions)
        }
        connectors.append({
            "name": connector.get("name"),
            "action_count": len(actions),
            "mode_counts": mode_counts,
        })

    action_map = _action_map(payload)
    ibkr_actions = {
        action_name
        for (connector_name, action_name) in action_map
        if connector_name in {
            "ibkr", "interactive brokers", "interactive brokers (ibkr)",
        }
    }
    missing_ibkr = sorted(
        action for action in REQUIRED_IBKR_ACTIONS
        if normalize_action_name(action) not in ibkr_actions
    )

    changes = payload.get("changes")
    changes = changes if isinstance(changes, Mapping) else {}
    added = [
        row for row in changes.get("added_actions") or ()
        if isinstance(row, Mapping)
    ]
    removed = [
        row for row in changes.get("removed_actions") or ()
        if isinstance(row, Mapping)
    ]
    changed = [
        row for row in changes.get("changed_actions") or ()
        if isinstance(row, Mapping)
    ]
    baseline_established = changes.get("baseline_established")
    if not isinstance(baseline_established, bool):
        baseline_established = (
            len(added) == action_count and not removed and not changed
        )
    added_rows = [] if baseline_established else [
        _action_summary(row) for row in added[:row_limit]
    ]
    changed_rows = [
        _changed_action_summary(row) for row in changed[:row_limit]
    ]
    journal_text = str(journal_path)
    command = (
        "python3 -m runtime.tool_inventory "
        f"--journal {shlex.quote(journal_text)} "
        f"--record-id {shlex.quote(source_record_id)}"
    )
    return {
        "observed_at": payload.get("observed_at"),
        "source_cycle_id": source_cycle_id,
        "source_record_id": source_record_id,
        "journal_path": journal_text,
        "full_inventory_command": command,
        "complete_for_current_session": payload.get(
            "complete_for_current_session") is True,
        "observed_in_latest_cycle": cycles_since_observed == 0,
        "cycles_since_observed": cycles_since_observed,
        "stale": cycles_since_observed > 0,
        "inventory_sha256": canonical_inventory_hash(payload),
        "connector_count": len(connectors),
        "action_count": action_count,
        "connectors": connectors,
        "required_ibkr_actions": {
            "required_count": len(REQUIRED_IBKR_ACTIONS),
            "present_count": (
                len(REQUIRED_IBKR_ACTIONS) - len(missing_ibkr)
            ),
            "missing_actions": missing_ibkr,
        },
        "changes": {
            "baseline_established": baseline_established,
            "baseline_action_count": (
                action_count if baseline_established else None
            ),
            "added_action_count": len(added),
            "added_actions": added_rows,
            "added_actions_not_shown": (
                0 if baseline_established
                else max(0, len(added) - row_limit)
            ),
            "removed_action_count": len(removed),
            "removed_actions": [
                _action_summary(row) for row in removed
            ],
            "changed_action_count": len(changed),
            "changed_actions": changed_rows,
            "changed_actions_not_shown": max(
                0, len(changed) - row_limit),
            "what_this_means": changes.get("what_this_means"),
        },
        "manifest_discrepancies": _bounded_strings(
            payload.get("manifest_discrepancies")),
        "unreachable_manifest_connectors": _bounded_strings(
            payload.get("unreachable_manifest_connectors")),
        "what_this_means": (
            "This is a bounded digest, not the live callable inventory. "
            "The full immutable manifest is retrievable with "
            "full_inventory_command. stale=true means later accepted cycles "
            "did not submit a fresh manifest; inspect the current connector "
            "tools and re-enumerate every exposed action before relying on "
            "capability availability."
        ),
    }


def _action_map(report: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    actions: dict[tuple[str, str], dict[str, Any]] = {}
    for connector in report.get("connectors", ()):
        if not isinstance(connector, Mapping):
            continue
        connector_name = str(connector.get("name", "")).strip()
        for action in connector.get("actions", ()):
            if not isinstance(action, Mapping):
                continue
            action_name = str(action.get("name", "")).strip()
            if connector_name and action_name:
                actions[
                    (connector_name.lower(), normalize_action_name(action_name))
                ] = {
                    "connector": connector_name,
                    "action": action_name,
                    "inputs": list(action.get("inputs") or ()),
                    "returns": action.get("returns"),
                    "mode": action.get("mode"),
                }
    return actions


def inventory_diff(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Return deterministic capability additions, removals, and changes."""
    previous_actions = _action_map(previous or {})
    current_actions = _action_map(current)
    previous_keys = set(previous_actions)
    current_keys = set(current_actions)
    added = [
        current_actions[key] for key in sorted(current_keys - previous_keys)
    ]
    removed = [
        previous_actions[key] for key in sorted(previous_keys - current_keys)
    ]
    changed = []
    for key in sorted(previous_keys & current_keys):
        before = previous_actions[key]
        after = current_actions[key]
        if {
            field: before[field] for field in ("inputs", "returns", "mode")
        } != {
            field: after[field] for field in ("inputs", "returns", "mode")
        }:
            changed.append({"before": before, "after": after})
    return {
        "added_actions": added,
        "removed_actions": removed,
        "changed_actions": changed,
        "what_this_means": (
            "Capability changes are observations, not instructions to call "
            "every tool. Assess new actions for decision value and source "
            "coverage; stop relying on removed actions immediately."
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read one full immutable tool inventory record.")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--record-id", required=True)
    args = parser.parse_args(argv)
    from .audit_store import AuditJournal
    payload = full_inventory_for_record(
        AuditJournal(args.journal).read(),
        args.record_id,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
