import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .learning_dispositions import (
    disposition_reconciliation_errors,
    learning_disposition_summary,
    persist_learning_dispositions,
    validate_learning_dispositions,
)
from .reliability import operational_reliability
from .run_host_cycle import (
    FULL_HOST_INPUT_SCHEMA_VERSION,
    SUPPORTED_FULL_CYCLE_VERSIONS,
    _full_cycle,
    is_full_cycle,
    load_input,
    run_one,
    schema_version,
    validate_input,
)
from .test_run_host_cycle import add_market_scout, post_effective_full_cycle


def dispositions(**overrides):
    rows = [
        {
            "stage_id": stage_id,
            "disposition": "no_change",
            "rationale": f"No durable change supported for {stage_id}.",
            "evidence": [f"stage:{stage_id}"],
        }
        for stage_id in (
            "learning_audit",
            "meta_research",
            "self_improvement",
        )
    ]
    for row in rows:
        row.update(overrides.get(row["stage_id"], {}))
    return rows


def v3_input(**overrides):
    data = post_effective_full_cycle(
        host_input_schema_version=3,
        learning_stage_dispositions=dispositions(),
    )
    data.update(overrides)
    return add_market_scout(data)


class LearningDispositionValidationTests(unittest.TestCase):
    def test_supported_versions_share_the_full_cycle_path(self):
        self.assertEqual(FULL_HOST_INPUT_SCHEMA_VERSION, 4)
        self.assertEqual(
            SUPPORTED_FULL_CYCLE_VERSIONS,
            frozenset({2, 3, 4}),
        )
        self.assertEqual(schema_version({"host_input_schema_version": "2"}), 2)
        self.assertTrue(is_full_cycle({"host_input_schema_version": 2}))
        self.assertTrue(is_full_cycle({"host_input_schema_version": 3}))
        self.assertTrue(is_full_cycle({"host_input_schema_version": 4}))

    def test_v3_requires_exactly_one_disposition_per_learning_stage(self):
        data = v3_input(learning_stage_dispositions=None)
        self.assertIn(
            "learning_dispositions_required",
            validate_input(data, "v3.json"),
        )

        data = v3_input()
        data["learning_stage_dispositions"].append(
            dict(data["learning_stage_dispositions"][0]))
        errors = validate_input(data, "v3.json")
        self.assertIn(
            "learning_disposition_invalid:3:duplicate_stage:learning_audit",
            errors,
        )

        data = v3_input()
        data["learning_stage_dispositions"][0]["stage_id"] = "unknown"
        errors = validate_input(data, "v3.json")
        self.assertIn(
            "learning_disposition_invalid:0:unknown_stage:unknown",
            errors,
        )
        self.assertIn(
            "learning_disposition_missing_stage:learning_audit",
            errors,
        )

    def test_evidence_is_a_closed_current_cycle_reference(self):
        data = v3_input()
        data["learning_stage_dispositions"][0]["evidence"] = [
            "the stage looked good",
            "finding:missing",
        ]
        errors = validate_input(data, "v3.json")
        self.assertIn(
            "learning_disposition_evidence_invalid:0:invalid_ref:0",
            errors,
        )
        self.assertIn(
            "learning_disposition_evidence_invalid:0:"
            "dangling_ref:finding:missing",
            errors,
        )

    def test_no_change_forbids_artifacts_and_artifact_refs_must_be_declared(self):
        data = v3_input()
        data["learning_stage_dispositions"][0]["artifact_refs"] = []
        self.assertIn(
            "learning_disposition_artifact_invalid:0:"
            "forbidden_for_no_change",
            validate_input(data, "v3.json"),
        )

        data = v3_input()
        data["learning_stage_dispositions"][0].update({
            "disposition": "artifact",
            "artifact_refs": ["lesson:not-declared"],
        })
        self.assertIn(
            "learning_disposition_artifact_invalid:0:"
            "dangling_ref:lesson:not-declared",
            validate_input(data, "v3.json"),
        )

    def test_declared_lesson_id_cannot_reuse_a_journal_record(self):
        data = v3_input(lessons=[{
            "lesson_id": "lesson-one",
            "lesson": "Use current-cycle evidence.",
            "evidence": ["stage:learning_audit"],
            "falsified_if": "Later evidence disproves it.",
        }])
        data["learning_stage_dispositions"][0].update({
            "disposition": "artifact",
            "artifact_refs": ["lesson:lesson-one"],
        })
        records = [{
            "record_id": "lesson:lesson-one",
            "record_type": "lesson",
            "payload": {"lesson_id": "lesson-one"},
        }]
        self.assertIn(
            "learning_artifact_id_reused:lesson:lesson-one",
            validate_input(data, "v3.json", records=records),
        )

    def test_closed_artifact_reference_grammar_accepts_supported_ids(self):
        data = {
            "cognitive_stages": [
                {"stage_id": stage_id}
                for stage_id in (
                    "learning_audit",
                    "meta_research",
                    "self_improvement",
                )
            ],
            "findings": [{"id": "finding-one"}],
            "lessons": [{"lesson_id": "lesson-one"}],
            "memory_distillation": {"distillation_id": "distillation-one"},
            "goal_observations": [
                {"mode": "create", "goal": {"goal_id": "goal-new"}},
                {"mode": "progress", "goal_id": "goal-open"},
                {"mode": "close", "goal_id": "goal-close"},
            ],
            "mutation": {"mutation_id": "mutation-one"},
        }
        rows = dispositions(
            learning_audit={
                "disposition": "artifact",
                "evidence": ["finding:finding-one"],
                "artifact_refs": [
                    "lesson:lesson-one",
                    "goal:goal-new",
                ],
            },
            meta_research={
                "disposition": "artifact",
                "artifact_refs": [
                    "memory-distillation:distillation-one",
                    "goal:goal-open:progress",
                    "goal:goal-close:closed",
                ],
            },
            self_improvement={
                "disposition": "artifact",
                "artifact_refs": ["mutation:mutation-one"],
            },
        )
        self.assertEqual(
            validate_learning_dispositions(
                rows, data=data, records=[]),
            [],
        )

    def test_staged_v2_is_rejected_but_runtime_v2_remains_supported(self):
        data = post_effective_full_cycle(host_input_schema_version=2)
        self.assertNotIn(
            "unsupported_host_input_schema_version:2",
            validate_input(data, "historical-v2.json"),
        )
        self.assertIn(
            "staged_host_input_schema_version_required:2",
            validate_input(
                data,
                "new-v2.json",
                require_full_schema=True,
            ),
        )

    def test_no_bare_canonical_version_comparisons_return(self):
        source = Path(__file__).with_name(
            "run_host_cycle.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            source,
            r"(?:==|!=)\s*FULL_HOST_INPUT_SCHEMA_VERSION|"
            r"FULL_HOST_INPUT_SCHEMA_VERSION\s*(?:==|!=)",
        )

    def test_committed_receipted_v2_inputs_remain_full_cycle_replays(self):
        from .integrity import load_journal_records

        root = Path(__file__).resolve().parent.parent
        records = load_journal_records()
        receipted = {
            str((record.get("payload") or {}).get("cycle_id"))
            for record in records
            if record.get("record_type") == "cycle_receipt"
        }
        checked = 0
        for path in sorted((root / "host_input").glob("*.json")):
            if path.name == "FEEDBACK.json":
                continue
            try:
                data = load_input(path)
            except (ValueError, json.JSONDecodeError):
                continue
            if (
                data.get("host_input_schema_version") != 2
                or str(data.get("cycle_id")) not in receipted
            ):
                continue
            self.assertTrue(is_full_cycle(data), path.name)
            jobs, handlers = _full_cycle(data)
            self.assertIn("learning_audit", handlers, path.name)
            self.assertIn("self_improvement", handlers, path.name)
            self.assertGreaterEqual(len(jobs), 12, path.name)
            checked += 1
        self.assertGreater(checked, 0)


class LearningDispositionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="learning-dispositions-"))
        self.input_path = self.root / "cycle-v3.json"
        self.journal = AuditJournal(self.root / "journal.jsonl")

    def test_executor_only_v3_cycle_persists_three_and_qualifies(self):
        self.input_path.write_text(
            json.dumps(v3_input(cycle_id="cycle-v3")),
            encoding="utf-8",
        )

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            receipt = run_one(self.input_path, self.journal)

        records = self.journal.read()
        disposition_records = [
            row for row in records
            if row.get("record_type") == "learning_disposition"
        ]
        self.assertEqual(receipt["host_input_schema_version"], 3)
        self.assertEqual(len(disposition_records), 3)
        self.assertEqual(
            {
                row["record_id"] for row in disposition_records
            },
            {
                "learning-disposition:cycle-v3:learning_audit",
                "learning-disposition:cycle-v3:meta_research",
                "learning-disposition:cycle-v3:self_improvement",
            },
        )
        receipt_record = next(
            row for row in records
            if row.get("record_id") == "cycle-receipt:cycle-v3"
        )
        self.assertEqual(
            disposition_reconciliation_errors(receipt_record, records),
            [],
        )
        score = operational_reliability(
            records, journal_path=self.journal.path)
        self.assertEqual(
            score["cognitive_qualification_streak"]["current"], 1)
        self.assertEqual(
            score["cognitive_qualification_streak"][
                "qualifying_v3_receipts"
            ],
            1,
        )
        persist_learning_dispositions(
            v3_input(cycle_id="cycle-v3"),
            self.journal,
            receipt,
        )
        self.assertEqual(
            sum(
                row.get("record_type") == "learning_disposition"
                for row in self.journal.read()
            ),
            3,
        )
        changed = v3_input(cycle_id="cycle-v3")
        changed["learning_stage_dispositions"][0]["rationale"] = (
            "Different persisted meaning."
        )
        with self.assertRaisesRegex(
            ValueError,
            "learning_disposition_payload_mismatch:"
            "learning-disposition:cycle-v3:learning_audit",
        ):
            persist_learning_dispositions(
                changed,
                self.journal,
                receipt,
            )

    def test_full_cycle_persists_finding_evidence_for_reconciliation(self):
        data = v3_input(
            cycle_id="cycle-finding-evidence",
            findings=[{
                "id": "fresh-opportunity",
                "claim": "Current evidence supports further research.",
            }],
        )
        data["learning_stage_dispositions"][0]["evidence"] = [
            "finding:fresh-opportunity",
            "stage:learning_audit",
        ]
        self.input_path.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(self.input_path, self.journal)

        records = self.journal.read()
        decision_stage = next(
            row for row in records
            if row.get("record_id") == (
                "cycle-stage:cycle-finding-evidence:decision"
            )
        )
        self.assertEqual(
            decision_stage["payload"]["output"]["findings"],
            data["findings"],
        )
        receipt_record = next(
            row for row in records
            if row.get("record_id") == (
                "cycle-receipt:cycle-finding-evidence"
            )
        )
        self.assertEqual(
            disposition_reconciliation_errors(receipt_record, records),
            [],
        )

    def test_artifact_disposition_reconciles_to_same_cycle_lesson(self):
        data = v3_input(
            cycle_id="cycle-artifact",
            lessons=[{
                "lesson_id": "lesson-one",
                "lesson": "Keep evidence identifiers closed.",
                "evidence": ["stage:learning_audit"],
                "falsified_if": "A free-text reference becomes verifiable.",
            }],
        )
        data["learning_stage_dispositions"][0].update({
            "disposition": "artifact",
            "artifact_refs": ["lesson:lesson-one"],
        })
        self.input_path.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(self.input_path, self.journal)

        records = self.journal.read()
        lesson = next(
            row for row in records
            if row.get("record_id") == "lesson:lesson-one"
        )
        self.assertIn(
            "cycle-receipt:cycle-artifact", lesson["caused_by"])
        summary = learning_disposition_summary(records)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(
            summary["recent"][0]["artifact_refs"],
            ["lesson:lesson-one"],
        )

    def test_reconciliation_fails_before_any_disposition_is_appended(self):
        data = v3_input()
        data["lessons"] = [{
            "lesson_id": "missing-write",
            "lesson": "A declared artifact.",
            "evidence": ["stage:learning_audit"],
            "falsified_if": "The artifact is not persisted.",
        }]
        data["learning_stage_dispositions"][0].update({
            "disposition": "artifact",
            "artifact_refs": ["lesson:missing-write"],
        })
        receipt = {
            "cycle_id": "cycle-missing-artifact",
            "host_input_schema_version": 3,
            "self_improvement": {"mutation_ids": []},
        }
        self.journal.append(
            record_id="cycle-receipt:cycle-missing-artifact",
            record_type="cycle_receipt",
            agent="test",
            payload=receipt,
        )
        for stage_id in (
            "learning_audit",
            "meta_research",
            "self_improvement",
        ):
            self.journal.append(
                record_id=(
                    f"cycle-stage:cycle-missing-artifact:{stage_id}"
                ),
                record_type="cycle_stage",
                agent="test",
                payload={
                    "cycle_id": "cycle-missing-artifact",
                    "stage_id": stage_id,
                    "output": {},
                },
            )

        with self.assertRaisesRegex(
            ValueError,
            "learning_disposition_artifact_unresolved:"
            "learning_audit:lesson:missing-write",
        ):
            persist_learning_dispositions(data, self.journal, receipt)

        self.assertFalse(any(
            row.get("record_type") == "learning_disposition"
            for row in self.journal.read()
        ))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
