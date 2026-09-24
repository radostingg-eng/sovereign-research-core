import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .audit_store import AuditJournal
from .host_feedback import parse_reason, write_feedback
from .research_inbox import research_inbox_summary
from .run_host_cycle import (
    required_finalization_record_types,
    validate_input,
)
from .staged_intake import _correction_targets
from .worker_projection import (
    load_worker_projection,
    persist_worker_projection,
)
from .worker_research_dispositions import (
    _adopted_lead_digest,
    _projection_sha256,
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


def v2_worker_record(record_id: str, *, observed_at: str) -> dict:
    value = worker_record(record_id, observed_at=observed_at)
    value["request"] = {
        "sha256": "a" * 64,
        "role": "adversarial_challenge",
        "output_contract": {
            "schema_version": 2,
            "role": "adversarial_challenge",
        },
    }
    value["result"] = {
        "summary": "Challenge the leading thesis.",
        "challenged_claims": ["Demand remains durable."],
        "disconfirming_evidence_needed": [
            "Customer concentration trend.",
        ],
        "failure_modes": ["Capex outruns operating cash flow."],
        "alternative_explanations": ["Revenue growth is pull-forward."],
        "uncertainties": ["Customer mix is incomplete."],
        "suggested_next_question": "What would falsify durability?",
        "falsification_conditions": [
            {
                "claim": f"Claim {index}",
                "condition": f"Condition {index}",
                "evidence_needed": f"Evidence {index}",
            }
            for index in range(3)
        ],
    }
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
            record["payload"]["adopted_lead"]["summary"],
            "A bounded research lead.",
        )
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
        self.assertEqual(summary["adopted_lead_count"], 1)
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
        for path in (self.root / "research_inbox").glob("*/*.json"):
            path.unlink()
        self.assertEqual(
            projected_worker_records(
                self.root,
                source_observed_at="2026-09-20T12:00:00Z",
            ),
            [],
        )
        after_expiry = worker_research_adoption_summary(journal.read())
        self.assertEqual(
            after_expiry["recent"][0]["adopted_lead"]["summary"],
            "A bounded research lead.",
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
        self.assertNotIn(
            "adopted_lead",
            journal.read()[-1]["payload"],
        )

    def test_deferred_lead_does_not_persist_worker_content(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition(
            "lead-one",
            disposition="deferred",
            revisit_condition="Revisit after the next filing.",
        )]
        data = cycle_data(rows)
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
            "adopted_lead",
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

    def test_legacy_used_disposition_replays_without_new_digest(self):
        self.write_record(worker_record(
            "lead-one",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition("lead-one")]
        data = cycle_data(rows)
        projected = projected_worker_records(
            self.root,
            source_observed_at="2026-09-20T12:00:00Z",
        )[0]
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        stage_id = "cycle-stage:cycle-worker-adoption:research_director"
        receipt_id = "cycle-receipt:cycle-worker-adoption"
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
            record_id=receipt_id,
            record_type="cycle_receipt",
            agent="sovereign-host",
            caused_by=(stage_id,),
            payload=receipt,
        )
        journal.append(
            record_id=(
                "worker-research-disposition:"
                "cycle-worker-adoption:lead-one"
            ),
            record_type="worker_research_disposition",
            agent="sovereign-host",
            caused_by=(receipt_id, stage_id),
            payload={
                "schema_version": 1,
                "cycle_id": "cycle-worker-adoption",
                "worker_record_id": "lead-one",
                "worker_id": projected["worker_id"],
                "worker_observed_at": projected["observed_at"],
                "source_observed_at": "2026-09-20T12:00:00Z",
                "projection_sha256": _projection_sha256(projected),
                "disposition": "used_as_lead",
                "evidence": ["stage:research_director"],
                "rationale": (
                    "Independent current-cycle work assessed the lead."
                ),
                "revisit_condition": None,
            },
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
        self.assertNotIn(
            "adopted_lead",
            journal.read()[-1]["payload"],
        )

    def test_v2_adopted_lead_keeps_role_and_falsification(self):
        self.write_record(v2_worker_record(
            "lead-v2",
            observed_at="2026-09-20T11:00:00Z",
        ))
        rows = [self.disposition("lead-v2")]
        data = cycle_data(rows)
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

        lead = journal.read()[-1]["payload"]["adopted_lead"]
        self.assertEqual(lead["role"], "adversarial_challenge")
        self.assertEqual(lead["output_contract_version"], 2)
        self.assertEqual(len(lead["falsification_conditions"]), 1)
        self.assertEqual(
            set(lead["falsification_conditions"][0]),
            {"claim", "condition", "evidence_needed"},
        )
        self.assertEqual(
            lead["counterevidence"],
            [
                "Capex outruns operating cash flow.",
                "Revenue growth is pull-forward.",
            ],
        )

    def test_adopted_lead_digest_enforces_text_and_list_bounds(self):
        digest = _adopted_lead_digest({
            "target": {
                "question_id": "q" * 500,
                "question": "t" * 500,
            },
            "result": {
                "role": "adversarial_challenge",
                "output_contract_version": 2,
                "summary": "s" * 500,
                "suggested_next_question": "n" * 500,
                "evidence_needed": ["e" * 500] * 5,
                "counterevidence": ["c" * 500] * 5,
                "falsification_conditions": [{
                    "claim": "a" * 500,
                    "condition": "b" * 500,
                    "evidence_needed": "d" * 500,
                } for _ in range(5)],
            },
        })

        self.assertIsNotNone(digest)
        assert digest is not None
        self.assertEqual(len(digest["summary"]), 400)
        self.assertEqual(len(digest["suggested_next_question"]), 400)
        self.assertEqual(len(digest["evidence_needed"]), 3)
        self.assertEqual(len(digest["counterevidence"]), 3)
        self.assertTrue(all(
            len(value) == 400
            for value in [
                *digest["evidence_needed"],
                *digest["counterevidence"],
            ]
        ))
        self.assertEqual(len(digest["falsification_conditions"]), 1)
        self.assertTrue(all(
            len(value) == 400
            for value in digest["falsification_conditions"][0].values()
        ))

    def test_feedback_shows_only_three_adopted_leads(self):
        records = [{
            "record_id": f"worker-research-disposition:cycle-{index}:lead",
            "record_type": "worker_research_disposition",
            "payload": {
                "cycle_id": f"cycle-{index}",
                "worker_record_id": f"lead-{index}",
                "worker_id": "worker-a",
                "disposition": "used_as_lead",
                "evidence": [],
                "rationale": "Used.",
                "revisit_condition": None,
                "source_observed_at": "2026-09-20T12:00:00Z",
                "adopted_lead": {"summary": f"Lead {index}"},
            },
        } for index in range(5)]

        summary = worker_research_adoption_summary(records)

        self.assertEqual(summary["adopted_lead_count"], 5)
        self.assertEqual(summary["adopted_lead_not_shown"], 2)
        self.assertEqual(
            sum(
                row["adopted_lead"] is not None
                for row in summary["recent"]
            ),
            3,
        )


    def test_empty_feedback_snapshot_excludes_a_later_worker(self):
        summary = research_inbox_summary(
            self.root,
            now=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
        )
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        projection_id = summary["projection_id"]
        self.assertIsNotNone(projection_id)
        self.assertEqual(persist_worker_projection(summary, journal), projection_id)
        self.assertEqual(persist_worker_projection(summary, journal), projection_id)
        self.assertEqual(len(journal.read()), 1)
        self.assertEqual(journal.read()[0]["payload"]["items"], [])

        self.write_record(worker_record(
            "late", observed_at="2026-09-20T12:01:00Z",
        ))
        data = cycle_data([])
        data["schedule_context"]["source_observed_at"] = (
            "2026-09-20T12:02:00Z"
        )
        data["worker_research_projection_id"] = projection_id
        self.assertEqual(
            validate_worker_research_dispositions(
                [], data=data, profile_root=self.root,
                records=journal.read(), require_projection=True,
            ),
            [],
        )
        del data["worker_research_projection_id"]
        self.assertEqual(
            validate_worker_research_dispositions(
                [], data=data, profile_root=self.root,
                records=journal.read(), require_projection=True,
            ),
            ["worker_research_projection_id_required"],
        )

    def test_feedback_snapshot_survives_worker_rotation_and_raw_pruning(self):
        self.write_record(worker_record(
            "shown",
            observed_at="2026-09-20T11:00:00Z",
            expires_at="2026-09-20T12:10:00Z",
        ))
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        summary = research_inbox_summary(
            self.root,
            now=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
        )
        projection_id = summary["projection_id"]
        self.assertEqual(summary["adoption_required_record_ids"], ["shown"])
        self.assertEqual(persist_worker_projection(summary, journal), projection_id)
        self.assertEqual(persist_worker_projection(summary, journal), projection_id)
        self.assertEqual(len(journal.read()), 1)
        projected = journal.read()[0]["payload"]["items"][0]
        self.assertEqual(projected["result"]["summary"], "A bounded research lead.")
        self.assertNotIn("full_record_path", projected)

        (self.root / "host_input").mkdir()
        feedback = write_feedback(
            self.root / "host_input",
            accepted=[],
            refusals=[],
            skipped=[],
            research_inbox=summary,
        )
        self.assertEqual(
            json.loads(feedback.read_text(encoding="utf-8"))[
                "research_inbox"
            ]["projection_id"],
            projection_id,
        )
        rows = [self.disposition("shown")]
        data = cycle_data(rows)
        data["worker_research_projection_id"] = projection_id
        self.assertEqual(
            validate_worker_research_dispositions(
                rows,
                data=data,
                profile_root=self.root,
                records=journal.read(),
                require_projection=True,
            ),
            [],
        )
        stage_id = "cycle-stage:cycle-worker-adoption:research_director"
        receipt_id = "cycle-receipt:cycle-worker-adoption"
        journal.append(
            record_id=stage_id,
            record_type="cycle_stage",
            agent="sovereign-host",
            payload={
                "cycle_id": data["cycle_id"],
                "stage_id": "research_director",
            },
        )
        receipt = {
            "cycle_id": data["cycle_id"],
            "host_input_schema_version": 4,
        }
        journal.append(
            record_id=receipt_id,
            record_type="cycle_receipt",
            agent="sovereign-host",
            caused_by=(stage_id,),
            payload=receipt,
        )
        self.assertEqual(
            persist_worker_research_dispositions(
                data, journal, receipt, profile_root=self.root,
            ),
            1,
        )
        self.assertEqual(
            persist_worker_research_dispositions(
                data, journal, receipt, profile_root=self.root,
            ),
            0,
        )
        disposition = journal.read()[-1]
        self.assertEqual(disposition["payload"]["worker_research_projection_id"], projection_id)
        self.assertEqual(disposition["payload"]["adopted_lead"]["summary"], "A bounded research lead.")
        self.assertIn(projection_id, disposition["caused_by"])
        self.assertTrue(journal.validate()["valid"])

    def test_snapshot_identity_is_required_for_new_inputs_and_fail_closed(self):
        self.write_record(worker_record(
            "shown",
            observed_at="2026-09-20T11:00:00Z",
        ))
        journal = AuditJournal(
            self.root / "audit" / "2026" / "09-20.jsonl"
        )
        summary = research_inbox_summary(
            self.root,
            now=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
        )
        projection_id = persist_worker_projection(summary, journal)
        rows = [self.disposition("shown", disposition="rejected")]
        data = cycle_data(rows)
        self.assertEqual(
            validate_worker_research_dispositions(
                rows,
                data=data,
                profile_root=self.root,
                records=journal.read(),
                require_projection=True,
            ),
            ["worker_research_projection_id_required"],
        )
        self.assertEqual(
            validate_worker_research_dispositions(
                rows,
                data=data,
                profile_root=self.root,
                records=journal.read(),
            ),
            [],
        )
        for invalid_id, expected in (
            (
                projection_id[:-1]
                + ("0" if projection_id[-1] != "0" else "1"),
                "worker_research_projection_unknown",
            ),
            ("forged", "worker_research_projection_invalid"),
        ):
            data["worker_research_projection_id"] = invalid_id
            self.assertEqual(
                validate_worker_research_dispositions(
                    rows, data=data, profile_root=self.root,
                    records=journal.read(), require_projection=True,
                ),
                [expected],
            )
            self.assertIn(expected, parse_reason(expected)[0]["code"])
            target = _correction_targets(expected, data)[0]
            self.assertEqual(
                (target["json_pointer"], target["required_state"]),
                ("/worker_research_projection_id", "non_empty_string"),
            )


if __name__ == "__main__":
    unittest.main()
