import json
from pathlib import Path
import unittest

from .profile_paths import profile_root
from .tool_inventory import (
    NONTRANSMITTING_WRITE_ACTIONS,
    REQUIRED_IBKR_ACTIONS,
    WRITE_ACTIONS,
    canonical_inventory_hash,
    full_inventory_for_record,
    inventory_diff,
    latest_tool_inventory,
    lookalike_manifest_preview,
    normalize_action_name,
    tool_inventory_feedback,
    validate_tool_manifest_report,
)


def action(name, mode="read"):
    return {
        "name": name,
        "inputs": [],
        "returns": "documented result",
        "mode": mode,
    }


def mode_for(name):
    if name in NONTRANSMITTING_WRITE_ACTIONS:
        return "write_nontransmitting"
    if name in WRITE_ACTIONS:
        return "write"
    return "read"


def report():
    return {
        "observed_at": "2026-09-16T22:34:48Z",
        "complete_for_current_session": True,
        "connectors": [{
            "name": "Interactive Brokers (IBKR)",
            "actions": [
                action(name, mode_for(name))
                for name in sorted(REQUIRED_IBKR_ACTIONS)
            ],
        }],
        "manifest_discrepancies": [],
        "unreachable_manifest_connectors": [],
    }


def inventory_records(value, *, later_cycle=False):
    records = [
        {
            "record_id": "cycle-receipt:cycle-one",
            "record_type": "cycle_receipt",
            "payload": {"cycle_id": "cycle-one"},
        },
        {
            "record_id": "tool-inventory:cycle-one",
            "record_type": "tool_inventory",
            "caused_by": ["cycle-receipt:cycle-one"],
            "payload": value,
        },
    ]
    if later_cycle:
        records.append({
            "record_id": "cycle-receipt:cycle-two",
            "record_type": "cycle_receipt",
            "payload": {"cycle_id": "cycle-two"},
        })
    return records


class ToolManifestValidationTests(unittest.TestCase):
    def test_complete_ibkr_inventory_passes(self):
        self.assertEqual(validate_tool_manifest_report(report()), [])

    def test_nanosecond_observation_timestamp_is_portable(self):
        value = report()
        value["observed_at"] = "2026-09-17T10:31:54.69185424Z"

        self.assertEqual(validate_tool_manifest_report(value), [])

    def test_connector_summary_without_actions_is_refused(self):
        value = report()
        value["connectors"][0].pop("actions")
        self.assertIn(
            "tool_manifest_actions_must_be_nonempty_list:"
            "Interactive Brokers (IBKR)",
            validate_tool_manifest_report(value),
        )

    def test_every_known_ibkr_action_is_required(self):
        value = report()
        value["connectors"][0]["actions"] = value["connectors"][0][
            "actions"][1:]
        errors = validate_tool_manifest_report(value)
        self.assertTrue(
            any(error.startswith("tool_manifest_missing_known_ibkr_action:")
                for error in errors),
            errors,
        )

    def test_string_action_list_reports_one_shape_error_without_cascade(self):
        value = report()
        value["connectors"][0]["actions"] = [
            row["name"] for row in value["connectors"][0]["actions"]
        ]
        errors = validate_tool_manifest_report(value)
        self.assertEqual(
            errors,
            [
                "tool_manifest_actions_must_be_objects:"
                "Interactive Brokers (IBKR)",
            ],
        )

    def test_each_malformed_connector_reports_one_shape_error(self):
        value = report()
        value["connectors"].append({
            "name": "Alpaca",
            "actions": ["get_clock", "get_stock_snapshot"],
        })
        value["connectors"][0]["actions"] = ["get account positions"]
        errors = validate_tool_manifest_report(value)
        self.assertEqual(
            errors,
            [
                "tool_manifest_actions_must_be_objects:"
                "Interactive Brokers (IBKR)",
                "tool_manifest_actions_must_be_objects:Alpaca",
            ],
        )

    def test_action_contract_includes_inputs_returns_and_mode(self):
        value = report()
        value["connectors"][0]["actions"][0].pop("inputs")
        value["connectors"][0]["actions"][1]["returns"] = ""
        value["connectors"][0]["actions"][2]["mode"] = "mystery"
        errors = validate_tool_manifest_report(value)
        self.assertTrue(
            any("action_inputs_must_be_list" in error for error in errors))
        self.assertTrue(
            any("action_returns_required" in error for error in errors))
        self.assertTrue(
            any("action_mode_invalid" in error for error in errors))

    def test_known_action_modes_are_enforced(self):
        value = report()
        target = next(
            row for row in value["connectors"][0]["actions"]
            if row["name"] == "create order instruction")
        target["mode"] = "write"
        self.assertIn(
            "tool_manifest_known_action_mode_mismatch:"
            "create order instruction:write_nontransmitting",
            validate_tool_manifest_report(value),
        )

    def test_operator_confirmed_minimum_has_34_actions(self):
        self.assertEqual(len(REQUIRED_IBKR_ACTIONS), 34)

    def test_connector_identifiers_match_operator_facing_labels(self):
        value = report()
        for row in value["connectors"][0]["actions"]:
            row["name"] = row["name"].replace(" ", "_").replace("'", "")
        topic = next(
            row for row in value["connectors"][0]["actions"]
            if row["name"] == "search_investment_topk")
        topic["name"] = "search_investment_topics"
        self.assertEqual(validate_tool_manifest_report(value), [])

    def test_normalization_does_not_change_persisted_display_name(self):
        self.assertEqual(
            normalize_action_name("Get_Account-Orders"),
            "get account orders",
        )

    def test_latest_persisted_inventory_is_surfaced(self):
        value = report()
        records = [{
            "record_type": "tool_inventory",
            "payload": value,
        }]
        self.assertEqual(latest_tool_inventory(records), value)

    def test_capability_hash_ignores_enumeration_order_and_diff_history(self):
        first = report()
        first["connectors"].append({
            "name": "Research",
            "actions": [{
                "name": "lookup",
                "inputs": ["symbol", "region"],
                "returns": "result",
                "mode": "read",
            }],
        })
        second = json.loads(json.dumps(first))
        second["connectors"].reverse()
        second["connectors"][1]["actions"].reverse()
        second["connectors"][0]["actions"][0]["inputs"].reverse()
        second["changes"] = {"added_actions": [{"history": "ignored"}]}

        self.assertEqual(
            canonical_inventory_hash(first),
            canonical_inventory_hash(second),
        )
        second["connectors"][0]["actions"][0]["mode"] = "write"
        self.assertNotEqual(
            canonical_inventory_hash(first),
            canonical_inventory_hash(second),
        )

    def test_feedback_digest_is_traceable_stale_and_schema_free(self):
        value = report()
        value["changes"] = inventory_diff({}, value)
        value["changes"]["baseline_established"] = True

        digest = tool_inventory_feedback(
            inventory_records(value, later_cycle=True),
            journal_path="audit/journal.jsonl",
        )

        self.assertEqual(digest["source_cycle_id"], "cycle-one")
        self.assertEqual(
            digest["source_record_id"], "tool-inventory:cycle-one")
        self.assertEqual(digest["cycles_since_observed"], 1)
        self.assertTrue(digest["stale"])
        self.assertFalse(digest["observed_in_latest_cycle"])
        self.assertEqual(digest["action_count"], len(REQUIRED_IBKR_ACTIONS))
        self.assertEqual(digest["required_ibkr_actions"]["missing_actions"], [])
        self.assertTrue(digest["changes"]["baseline_established"])
        self.assertEqual(digest["changes"]["added_actions"], [])
        self.assertEqual(
            digest["changes"]["baseline_action_count"],
            len(REQUIRED_IBKR_ACTIONS),
        )
        encoded = json.dumps(digest)
        self.assertNotIn('"inputs"', encoded)
        self.assertNotIn('"returns"', encoded)
        self.assertIn(
            "--record-id tool-inventory:cycle-one",
            digest["full_inventory_command"],
        )

    def test_whole_connector_removal_keeps_every_action_name(self):
        before = report()
        before["connectors"].append({
            "name": "Large Research Connector",
            "actions": [action(f"lookup {index}") for index in range(133)],
        })
        after = report()
        after["changes"] = inventory_diff(before, after)
        after["changes"]["baseline_established"] = False

        digest = tool_inventory_feedback(
            inventory_records(after),
            journal_path="audit/journal.jsonl",
            row_limit=5,
        )

        removed = digest["changes"]["removed_actions"]
        self.assertEqual(len(removed), 133)
        self.assertEqual(digest["changes"]["removed_action_count"], 133)
        self.assertEqual(
            {row["action"] for row in removed},
            {f"lookup {index}" for index in range(133)},
        )
        self.assertNotIn('"inputs"', json.dumps(removed))
        self.assertNotIn('"returns"', json.dumps(removed))

    def test_mode_flip_changes_hash_and_remains_visible(self):
        before = report()
        before["connectors"].append({
            "name": "Research",
            "actions": [action("lookup dataset", "read")],
        })
        after = json.loads(json.dumps(before))
        after["connectors"][1]["actions"][0]["mode"] = "write"
        after["changes"] = inventory_diff(before, after)
        after["changes"]["baseline_established"] = False

        digest = tool_inventory_feedback(
            inventory_records(after),
            journal_path="audit/journal.jsonl",
        )

        self.assertNotEqual(
            canonical_inventory_hash(before),
            canonical_inventory_hash(after),
        )
        self.assertEqual(
            digest["changes"]["changed_actions"],
            [{
                "connector": "Research",
                "action": "lookup dataset",
                "before_mode": "read",
                "after_mode": "write",
                "fields_changed": ["mode"],
            }],
        )

    def test_full_inventory_is_retrievable_by_digest_record_id(self):
        value = report()
        records = inventory_records(value)
        self.assertEqual(
            full_inventory_for_record(
                records, "tool-inventory:cycle-one"),
            value,
        )
        with self.assertRaisesRegex(
            ValueError, "tool_inventory_record_not_found:missing",
        ):
            full_inventory_for_record(records, "missing")

    def test_real_309_action_inventory_projects_below_twenty_kilobytes(self):
        root = profile_root()
        journal_path = sorted((root / "audit").glob("*.jsonl"))[-1]
        from .audit_store import AuditJournal
        records = AuditJournal(journal_path).read()

        digest = tool_inventory_feedback(
            records, journal_path=journal_path)

        self.assertEqual(digest["action_count"], 309)
        self.assertLess(
            len(json.dumps(digest, ensure_ascii=False)),
            20_000,
        )
        self.assertNotIn('"inputs"', json.dumps(digest))
        self.assertNotIn('"returns"', json.dumps(digest))

    def test_new_plugin_actions_are_detected_automatically(self):
        before = report()
        after = report()
        after["connectors"].append({
            "name": "New Research Plugin",
            "actions": [
                action("search new dataset"),
                action("get source document"),
            ],
        })
        changes = inventory_diff(before, after)
        self.assertEqual(
            [row["action"] for row in changes["added_actions"]],
            ["get source document", "search new dataset"],
        )
        self.assertEqual(changes["removed_actions"], [])

    def test_removed_and_changed_actions_are_detected(self):
        before = report()
        after = report()
        removed = after["connectors"][0]["actions"].pop()
        after["connectors"][0]["actions"][0]["returns"] = "new result shape"
        changes = inventory_diff(before, after)
        self.assertEqual(
            [row["action"] for row in changes["removed_actions"]],
            [removed["name"]],
        )
        self.assertEqual(len(changes["changed_actions"]), 1)

    def test_spacing_changes_do_not_create_phantom_inventory_deltas(self):
        before = report()
        after = report()
        for row in after["connectors"][0]["actions"]:
            row["name"] = row["name"].replace(" ", "_").replace("'", "")
        topic = next(
            row for row in after["connectors"][0]["actions"]
            if row["name"] == "search_investment_topk")
        topic["name"] = "search_investment_topics"
        changes = inventory_diff(before, after)
        self.assertEqual(changes["added_actions"], [])
        self.assertEqual(changes["removed_actions"], [])

    def test_lookalike_preview_reports_all_downstream_gaps(self):
        value = {
            "verified_at": "2026-09-17T06:00:30Z",
            "connectors": [{
                "name": "Interactive Brokers (IBKR)",
                "actions": [action("get account positions")],
            }],
        }
        errors = lookalike_manifest_preview(value, "tool_manifest")
        self.assertEqual(len(errors), 1)
        error = errors[0]
        self.assertIn("missing_fields=observed_at", error)
        self.assertIn("complete_for_current_session", error)
        self.assertIn("manifest_discrepancies", error)
        self.assertIn("unreachable_manifest_connectors", error)
        self.assertIn("missing_known_ibkr=create alert", error)
        self.assertNotIn("get account positions|", error)


if __name__ == "__main__":
    unittest.main()
