import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .cycle_receipt import build_receipt
from .run_host_cycle import (
    partition_validation_errors,
    run_one,
)
from .test_run_host_cycle import v4_post_effective_full_cycle
from .test_tool_inventory import report
from .tool_artifacts import iter_tool_calls
from .tool_probation import (
    persist_tool_probations,
    tool_probation_summary,
    validate_tool_probations,
)


def prior_inventory_records(value=None):
    value = value or report()
    return [
        {
            "record_id": "cycle-receipt:cycle-inventory",
            "record_type": "cycle_receipt",
            "payload": {"cycle_id": "cycle-inventory"},
        },
        {
            "record_id": "tool-inventory:cycle-inventory",
            "record_type": "tool_inventory",
            "caused_by": ["cycle-receipt:cycle-inventory"],
            "payload": value,
        },
        {
            "record_id": "cycle-finalization:cycle-inventory",
            "record_type": "cycle_finalization",
            "payload": {"cycle_id": "cycle-inventory"},
        },
    ]


def probation_input(*, status="experimental", action="get account positions"):
    data = v4_post_effective_full_cycle()
    data["cycle_id"] = "cycle-tool-probation"
    descriptor = next(iter_tool_calls(data))
    call = descriptor["call"]
    call["tool"] = "Interactive Brokers (IBKR)"
    call["call"]["action"] = action
    data["tool_probations"] = [{
        "probation_id": "probation-ibkr-positions",
        "tool_inventory_record_id": "tool-inventory:cycle-inventory",
        "connector": "Interactive Brokers (IBKR)",
        "action": action,
        "status": status,
        "observed_at": data["as_of"],
        "evidence_tool_call_ids": [call["tool_call_id"]],
        "assessment": {
            "result_summary": "Returned current account positions.",
            "names_sources": True,
            "names_observation_time": True,
            "incremental_value": "Authoritative current account state.",
            "failure_behavior": "No failure observed in this probe.",
        },
        "capability_review": None,
        "rationale": "Keep as the authority for current holdings.",
        "supersedes_probation_id": None,
    }]
    return data


def append_prior_inventory(journal):
    prior = build_receipt(
        cycle_id="cycle-inventory",
        run_id="run-inventory",
        started_at="2026-09-16T22:34:48Z",
        completed_at="2026-09-16T22:34:49Z",
        mode="e2e-smoke-manual",
        snapshot_id="inventory:snapshot",
        stages=[{
            "stage_id": "portfolio",
            "agent_id": "portfolio",
            "status": "completed",
            "execution_order": 1,
            "started_at": "2026-09-16T22:34:48Z",
            "completed_at": "2026-09-16T22:34:49Z",
            "tools_used": [],
        }],
        tools_used=[],
        status="completed",
        decision_status="wait",
        self_improvement={
            "status": "none",
            "mutation_ids": [],
            "gates": {},
        },
        host={
            "host_type": "test",
            "cognitive_execution_claim": "Test inventory cycle.",
        },
    )
    journal.append_cycle_receipt(prior)
    journal.append(
        record_id="tool-inventory:cycle-inventory",
        record_type="tool_inventory",
        agent="sovereign-host",
        caused_by=("cycle-receipt:cycle-inventory",),
        payload=report(),
    )
    journal.append(
        record_id="cycle-finalization:cycle-inventory",
        record_type="cycle_finalization",
        agent="sovereign-host",
        caused_by=("cycle-receipt:cycle-inventory",),
        payload={"cycle_id": "cycle-inventory"},
    )


class ToolProbationValidationTests(unittest.TestCase):
    def test_read_probe_is_bound_to_finalized_inventory_and_call(self):
        data = probation_input()

        self.assertEqual(
            validate_tool_probations(
                data["tool_probations"],
                data=data,
                records=prior_inventory_records(),
            ),
            [],
        )

    def test_unfinalized_inventory_is_refused(self):
        data = probation_input()
        records = [
            row for row in prior_inventory_records()
            if row["record_type"] != "cycle_finalization"
        ]

        self.assertIn(
            "tool_probation_invalid:0:inventory_record",
            validate_tool_probations(
                data["tool_probations"],
                data=data,
                records=records,
            ),
        )

    def test_write_capability_requires_explicit_review(self):
        data = probation_input(
            status="unsafe",
            action="create order instruction",
        )
        data["tool_probations"][0]["evidence_tool_call_ids"] = []

        errors = validate_tool_probations(
            data["tool_probations"],
            data=data,
            records=prior_inventory_records(),
        )

        self.assertIn(
            "tool_probation_invalid:0:capability_review",
            errors,
        )

    def test_partial_cycle_cannot_persist_probation(self):
        data = probation_input()

        blocking, advisory = partition_validation_errors(data, [
            "evidence_call_invalid:0:provenance:capture_missing",
        ])

        self.assertTrue(advisory)
        self.assertIn(
            "partial_cycle_tool_probation_forbidden",
            blocking,
        )

    def test_active_verdict_requires_supersession(self):
        data = probation_input(status="adopted")
        records = prior_inventory_records() + [{
            "record_id": (
                "tool-probation:cycle-old:probation-old"
            ),
            "record_type": "tool_probation",
            "payload": {
                "probation_id": "probation-old",
                "connector": "Interactive Brokers (IBKR)",
                "action": "get account positions",
                "status": "experimental",
            },
        }]

        self.assertIn(
            "tool_probation_supersession_required:0:probation-old",
            validate_tool_probations(
                data["tool_probations"],
                data=data,
                records=records,
            ),
        )


class ToolProbationPersistenceTests(unittest.TestCase):
    def test_probation_is_receipt_caused_and_visible(self):
        directory = Path(tempfile.mkdtemp(prefix="tool-probation-"))
        journal = AuditJournal(directory / "journal.jsonl")
        append_prior_inventory(journal)
        data = probation_input(status="adopted")
        receipt = {"cycle_id": data["cycle_id"]}

        self.assertEqual(
            persist_tool_probations(data, journal, receipt),
            1,
        )

        record = journal.read()[-1]
        self.assertEqual(record["record_type"], "tool_probation")
        self.assertEqual(
            record["caused_by"],
            [f"cycle-receipt:{data['cycle_id']}"],
        )
        evidence = record["payload"]["evidence"][0]
        self.assertEqual(
            evidence["tool_call_id"],
            data["tool_probations"][0]["evidence_tool_call_ids"][0],
        )
        self.assertTrue(evidence["result_sha256"])
        summary = tool_probation_summary(journal.read())
        self.assertEqual(summary["counts_by_status"], {"adopted": 1})

    def test_real_cycle_finalization_requires_probation_record(self):
        directory = Path(tempfile.mkdtemp(prefix="tool-probation-cycle-"))
        path = directory / "cycle.json"
        journal = AuditJournal(directory / "audit" / "journal.jsonl")
        append_prior_inventory(journal)
        data = probation_input(status="adopted")
        path.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            side_effect=lambda: journal.read(),
        ):
            receipt = run_one(path, journal)

        records = journal.read()
        probation = next(
            row for row in records
            if row["record_type"] == "tool_probation"
        )
        finalization = next(
            row for row in records
            if row["record_type"] == "cycle_finalization"
            and row["payload"].get("cycle_id") == receipt["cycle_id"]
        )
        self.assertIn(
            probation["record_id"],
            {
                row["record_id"]
                for row in finalization["payload"]["required_records"]
            },
        )


if __name__ == "__main__":
    unittest.main()
