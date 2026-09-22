import json
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .host_feedback import write_feedback
from .run_host_cycle import (
    required_finalization_record_types,
    validate_input,
)
from .worker_research_dispositions import (
    persist_worker_research_dispositions,
    projected_worker_records,
    validate_worker_research_dispositions,
    worker_research_adoption_summary,
)


def worker_record(
    record_id: str,
    *,
    observed_at: str,
    expires_at: str = "2026-09-20T14:00:00Z",
    status: str = "completed",
    question_id: str | None = None,
) -> dict:
    value = {
        "schema_version": 1,
        "record_id": record_id,
        "worker_id": f"worker-{record_id.replace(':', '-')}",
        "status": status,
        "origin": "worker_attested",
        "observed_at": observed_at,
        "expires_at": expires_at,
    }
    if status == "completed":
        value.update({
            "deployment": {"deployment": "gpt-test"},
            "selection": {"rule": "bounded"},
            "target": {
                "question_id": question_id or record_id,
                "question": "What could change the decision?",
            },
            "request": {"sha256": "a" * 64},
            "result": {
                "summary": "A bounded research lead.",
                "hypotheses": ["One hypothesis."],
                "evidence_needed": ["Independent verification."],
                "counterevidence": ["One challenge."],
                "uncertainties": ["One uncertainty."],
                "suggested_next_question": "What source can verify this?",
            },
            "quality": {"result_schema_complete": True},
        })
    else:
        value["error"] = {"kind": status}
    return value


def cycle_data(dispositions=None) -> dict:
    value = {
        "host_input_schema_version": 4,
        "cycle_id": "cycle-worker-adoption",
        "schedule_context": {
            "source_observed_at": "2026-09-20T12:00:00Z",
        },
        "cognitive_stages": [
            {"stage_id": "research_director"},
            {"stage_id": "decision"},
        ],
        "findings": [{"id": "verified-finding"}],
    }
    if dispositions is not None:
        value["worker_research_dispositions"] = dispositions
    return value


class WorkerResearchDispositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="worker-research-dispositions-"
        )
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_record(self, value: dict) -> None:
        path = (
            self.root
            / "research_inbox"
            / value["worker_id"]
            / f"{value['record_id'].replace(':', '-')}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def disposition(
        self,
        record_id: str,
        *,
        disposition: str = "used_as_lead",
        evidence=None,
        revisit_condition=None,
    ) -> dict:
        return {
            "worker_record_id": record_id,
            "disposition": disposition,
            "evidence": (
                ["stage:research_director"]
                if evidence is None and disposition == "used_as_lead"
                else evidence or []
            ),
            "rationale": "Independent current-cycle work assessed the lead.",
            "revisit_condition": revisit_condition,
        }

    def test_projection_uses_source_time_and_excludes_non_leads(self):
        self.write_record(worker_record(
            "visible",
            observed_at="2026-09-20T11:00:00Z",
        ))
        self.write_record(worker_record(
            "late",
            observed_at="2026-09-20T12:01:00Z",
        ))
        self.write_record(worker_record(
            "stale",
            observed_at="2026-09-20T10:00:00Z",
            expires_at="2026-09-20T11:59:00Z",
        ))
        self.write_record(worker_record(
            "error-only",
            observed_at="2026-09-20T11:30:00Z",
            status="model_error",
        ))

        projected = projected_worker_records(
            self.root,
            source_observed_at="2026-09-20T12:00:00Z",
        )

        self.assertEqual(
            [row["record_id"] for row in projected],
            ["visible"],
        )

    def test_context_limit_defines_exact_required_set(self):
        for index in range(13):
            self.write_record(worker_record(
                f"record-{index:02d}",
                observed_at=f"2026-09-20T11:{index:02d}:00Z",
                question_id=f"question-{index:02d}",
            ))
        projected = projected_worker_records(
            self.root,
            source_observed_at="2026-09-20T12:00:00Z",
        )
        self.assertEqual(len(projected), 12)
        rows = [
            self.disposition(row["record_id"], disposition="rejected")
            for row in projected
        ]

        self.assertEqual(
            validate_worker_research_dispositions(
                rows,
                data=cycle_data(rows),
                profile_root=self.root,
            ),
            [],
        )
        rows.append(self.disposition(
            "record-00",
            disposition="rejected",
        ))
        self.assertIn(
            "worker_research_disposition_unexpected:record-00",
            validate_worker_research_dispositions(
                rows,
                data=cycle_data(rows),
                profile_root=self.root,
            ),
        )

    def test_empty_stale_or_error_only_inbox_does_not_require_rows(self):
        self.write_record(worker_record(
            "stale",
            observed_at="2026-09-20T10:00:00Z",
            expires_at="2026-09-20T11:00:00Z",
        ))
        self.write_record(worker_record(
            "error",
            observed_at="2026-09-20T11:00:00Z",
            status="quota_exhausted",
        ))

        self.assertEqual(
            validate_worker_research_dispositions(
                None,
                data=cycle_data(),
                profile_root=self.root,
            ),
            [],
        )

    def test_each_projected_record_is_covered_exactly_once(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))

        self.assertIn(
            "worker_research_dispositions_required",
            validate_worker_research_dispositions(
                None,
                data=cycle_data(),
                profile_root=self.root,
            ),
        )
        duplicate = [
            self.disposition("lead-one"),
            self.disposition("lead-one", disposition="rejected"),
        ]
        self.assertIn(
            "worker_research_disposition_duplicate:lead-one",
            validate_worker_research_dispositions(
                duplicate,
                data=cycle_data(duplicate),
                profile_root=self.root,
            ),
        )

    def test_host_input_validation_uses_profile_projection(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))

        errors = validate_input(
            cycle_data(),
            "candidate.json",
            input_dir=self.root / "host_input",
            require_full_schema=True,
        )

        self.assertIn("worker_research_dispositions_required", errors)

    def test_disposition_specific_evidence_and_revisit_rules(self):
        for index, record_id in enumerate(("used", "rejected", "deferred")):
            self.write_record(worker_record(
                record_id,
                observed_at=f"2026-09-20T11:0{index}:00Z",
            ))
        rows = [
            self.disposition("used"),
            self.disposition("rejected", disposition="rejected"),
            self.disposition(
                "deferred",
                disposition="deferred",
                revisit_condition="Revisit after the issuer files results.",
            ),
        ]
        self.assertEqual(
            validate_worker_research_dispositions(
                rows,
                data=cycle_data(rows),
                profile_root=self.root,
            ),
            [],
        )

        rows[0]["evidence"] = []
        rows[2]["revisit_condition"] = None
        errors = validate_worker_research_dispositions(
            rows,
            data=cycle_data(rows),
            profile_root=self.root,
        )
        self.assertIn(
            "worker_research_disposition_invalid:0:evidence_required",
            errors,
        )
        self.assertIn(
            "worker_research_disposition_invalid:2:revisit_condition",
            errors,
        )

    def test_worker_record_id_cannot_be_an_evidence_reference(self):
        self.write_record(worker_record(
            "stage:research_director",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition(
            "stage:research_director",
            evidence=["stage:research_director"],
        )]

        self.assertIn(
            "worker_research_disposition_invalid:0:"
            "worker_record_is_not_evidence:0",
            validate_worker_research_dispositions(
                rows,
                data=cycle_data(rows),
                profile_root=self.root,
            ),
        )

    def test_worker_record_id_cannot_supply_decision_authority(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition("lead-one", disposition="rejected")]
        data = cycle_data(rows)
        data["decision"] = {
            "status": "wait",
            "rationale": "Wait for independent evidence.",
            "rests_on": ["lead-one"],
        }

        errors = validate_worker_research_dispositions(
            rows,
            data=data,
            profile_root=self.root,
        )

        self.assertIn(
            "worker_research_record_authority_forbidden:"
            "/decision/rests_on/0",
            errors,
        )

    def test_persistence_is_idempotent_and_links_receipt_and_evidence(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition("lead-one")]
        data = cycle_data(rows)
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        stage_id = (
            "cycle-stage:cycle-worker-adoption:research_director"
        )
        journal.append(
            record_id=stage_id,
            record_type="cycle_stage",
            agent="sovereign-host",
            payload={
                "cycle_id": "cycle-worker-adoption",
                "stage_id": "research_director",
            },
        )
        receipt = {
            "cycle_id": "cycle-worker-adoption",
            "host_input_schema_version": 4,
            "evidence_completeness": "partial",
            "self_improvement": {},
        }
        journal.append(
            record_id="cycle-receipt:cycle-worker-adoption",
            record_type="cycle_receipt",
            agent="sovereign-host",
            caused_by=(stage_id,),
            payload=receipt,
        )

        self.assertEqual(
            persist_worker_research_dispositions(
                data,
                journal,
                receipt,
                profile_root=self.root,
            ),
            1,
        )
        self.assertEqual(
            persist_worker_research_dispositions(
                data,
                journal,
                receipt,
                profile_root=self.root,
            ),
            0,
        )

        record = journal.read()[-1]
        self.assertEqual(
            record["caused_by"],
            [
                "cycle-receipt:cycle-worker-adoption",
                stage_id,
            ],
        )
        self.assertTrue(record["record_hash"])
        required = required_finalization_record_types(data, receipt)
        self.assertEqual(
            required[
                "worker-research-disposition:"
                "cycle-worker-adoption:lead-one"
            ],
            "worker_research_disposition",
        )

        summary = worker_research_adoption_summary(journal.read())
        self.assertEqual(summary["total_records"], 1)
        self.assertEqual(
            summary["counts_by_disposition"]["used_as_lead"],
            1,
        )
        (self.root / "host_input").mkdir()
        feedback = write_feedback(
            self.root / "host_input",
            accepted=[],
            refusals=[],
            skipped=[],
            worker_research_adoption=summary,
        )
        payload = json.loads(feedback.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["worker_research_adoption"]["recent"][0][
                "worker_record_id"
            ],
            "lead-one",
        )

    def test_used_lead_links_exact_question_to_brain_opportunity_event(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition("lead-one")]
        data = cycle_data(rows)
        data["opportunity_updates"] = [{
            "event_id": "adopt-worker-question",
            "opportunity_id": "opportunity-one",
            "research_state": {
                "missing_information": [{
                    "id": "source-verification",
                    "question": "What source can verify this?",
                    "why_it_matters": (
                        "Independent evidence determines whether the lead "
                        "deserves another research pass."
                    ),
                    "status": "open",
                }],
            },
        }]
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        stage_id = "cycle-stage:cycle-worker-adoption:research_director"
        receipt_id = "cycle-receipt:cycle-worker-adoption"
        opportunity_record_id = (
            "opportunity-event:adopt-worker-question"
        )
        journal.append(
            record_id=stage_id,
            record_type="cycle_stage",
            agent="sovereign-host",
            payload={
                "cycle_id": "cycle-worker-adoption",
                "stage_id": "research_director",
            },
        )
        receipt = {
            "cycle_id": "cycle-worker-adoption",
            "host_input_schema_version": 4,
            "evidence_completeness": "partial",
            "self_improvement": {},
        }
        journal.append(
            record_id=receipt_id,
            record_type="cycle_receipt",
            agent="sovereign-host",
            caused_by=(stage_id,),
            payload=receipt,
        )
        journal.append(
            record_id=opportunity_record_id,
            record_type="opportunity_event",
            agent="sovereign-host",
            caused_by=(receipt_id,),
            payload={
                "cycle_id": "cycle-worker-adoption",
                "event_id": "adopt-worker-question",
                "opportunity_id": "opportunity-one",
            },
        )

        self.assertEqual(
            persist_worker_research_dispositions(
                data,
                journal,
                receipt,
                profile_root=self.root,
            ),
            1,
        )

        disposition = journal.read()[-1]
        self.assertIn(opportunity_record_id, disposition["caused_by"])
        self.assertEqual(
            disposition["payload"]["question_adoptions"],
            [{
                "opportunity_id": "opportunity-one",
                "opportunity_event_record_id": opportunity_record_id,
                "missing_information_id": "source-verification",
                "question": "What source can verify this?",
                "worker_question": "What source can verify this?",
                "worker_question_source": "suggested_next_question",
            }],
        )
        summary = worker_research_adoption_summary(journal.read())
        self.assertEqual(summary["question_adoption_count"], 1)
        self.assertEqual(
            summary["recent"][0]["question_adoptions"][0][
                "missing_information_id"
            ],
            "source-verification",
        )

    def test_rejected_lead_does_not_claim_question_adoption(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition("lead-one", disposition="rejected")]
        data = cycle_data(rows)
        data["opportunity_updates"] = [{
            "event_id": "same-text-coincidence",
            "opportunity_id": "opportunity-one",
            "research_state": {
                "missing_information": [{
                    "id": "source-verification",
                    "question": "What source can verify this?",
                    "why_it_matters": "A host-authored question may coincide.",
                    "status": "open",
                }],
            },
        }]
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        stage_id = "cycle-stage:cycle-worker-adoption:research_director"
        receipt = {
            "cycle_id": "cycle-worker-adoption",
            "host_input_schema_version": 4,
            "evidence_completeness": "partial",
            "self_improvement": {},
        }
        journal.append(
            record_id=stage_id,
            record_type="cycle_stage",
            agent="sovereign-host",
            payload={
                "cycle_id": "cycle-worker-adoption",
                "stage_id": "research_director",
            },
        )
        journal.append(
            record_id="cycle-receipt:cycle-worker-adoption",
            record_type="cycle_receipt",
            agent="sovereign-host",
            caused_by=(stage_id,),
            payload=receipt,
        )

        persist_worker_research_dispositions(
            data,
            journal,
            receipt,
            profile_root=self.root,
        )

        self.assertNotIn(
            "question_adoptions",
            journal.read()[-1]["payload"],
        )

    def test_carried_question_is_not_misattributed_to_new_worker(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition("lead-one")]
        data = cycle_data(rows)
        data["opportunity_updates"] = [{
            "event_id": "carry-worker-question",
            "opportunity_id": "opportunity-one",
            "research_state": {
                "missing_information": [{
                    "id": "source-verification",
                    "question": "What source can verify this?",
                    "why_it_matters": "The unresolved gap remains material.",
                    "status": "open",
                }],
            },
        }]
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        stage_id = "cycle-stage:cycle-worker-adoption:research_director"
        receipt = {
            "cycle_id": "cycle-worker-adoption",
            "host_input_schema_version": 4,
            "evidence_completeness": "partial",
            "self_improvement": {},
        }
        journal.append(
            record_id="opportunity-event:prior-event",
            record_type="opportunity_event",
            agent="sovereign-host",
            payload={
                "cycle_id": "cycle-prior",
                "event_id": "prior-event",
                "opportunity_id": "opportunity-one",
                "to_state": "researching",
                "identity_fingerprint": "a" * 64,
                "research_state": data["opportunity_updates"][0][
                    "research_state"
                ],
            },
        )
        journal.append(
            record_id=stage_id,
            record_type="cycle_stage",
            agent="sovereign-host",
            payload={
                "cycle_id": "cycle-worker-adoption",
                "stage_id": "research_director",
            },
        )
        journal.append(
            record_id="cycle-receipt:cycle-worker-adoption",
            record_type="cycle_receipt",
            agent="sovereign-host",
            caused_by=(stage_id,),
            payload=receipt,
        )

        persist_worker_research_dispositions(
            data,
            journal,
            receipt,
            profile_root=self.root,
        )

        self.assertNotIn(
            "question_adoptions",
            journal.read()[-1]["payload"],
        )

    def test_worker_target_echo_is_not_question_adoption(self):
        record = worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        )
        record["target"]["question"] = "What source can verify this?"
        self.write_record(record)
        rows = [self.disposition("lead-one")]
        data = cycle_data(rows)
        data["opportunity_updates"] = [{
            "event_id": "echoed-target",
            "opportunity_id": "opportunity-one",
            "research_state": {
                "missing_information": [{
                    "id": "source-verification",
                    "question": "What source can verify this?",
                    "why_it_matters": "The host already assigned this target.",
                    "status": "open",
                }],
            },
        }]
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        stage_id = "cycle-stage:cycle-worker-adoption:research_director"
        receipt = {
            "cycle_id": "cycle-worker-adoption",
            "host_input_schema_version": 4,
            "evidence_completeness": "partial",
            "self_improvement": {},
        }
        journal.append(
            record_id=stage_id,
            record_type="cycle_stage",
            agent="sovereign-host",
            payload={
                "cycle_id": "cycle-worker-adoption",
                "stage_id": "research_director",
            },
        )
        journal.append(
            record_id="cycle-receipt:cycle-worker-adoption",
            record_type="cycle_receipt",
            agent="sovereign-host",
            caused_by=(stage_id,),
            payload=receipt,
        )

        persist_worker_research_dispositions(
            data,
            journal,
            receipt,
            profile_root=self.root,
        )

        self.assertNotIn(
            "question_adoptions",
            journal.read()[-1]["payload"],
        )


if __name__ == "__main__":
    unittest.main()
