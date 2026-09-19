import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from .audit_store import AuditJournal
from .cycle_receipt import build_receipt
from .engine import hash_record
from .reliability import SCORECARD_BYTE_BUDGET, main, operational_reliability


def stage(stage_id="portfolio", order=1):
    return {
        "stage_id": stage_id,
        "agent_id": stage_id,
        "status": "completed",
        "execution_order": order,
        "started_at": "2026-09-17T12:00:00Z",
        "completed_at": "2026-09-17T12:00:01Z",
        "tools_used": [],
    }


def receipt(
    cycle_id,
    *,
    host_input_schema_version=None,
    evidence_completeness=None,
):
    stages = [stage()]
    if host_input_schema_version == 3:
        stages = [
            stage("learning_audit", 1),
            stage("meta_research", 2),
            stage("self_improvement", 3),
        ]
    return build_receipt(
        cycle_id=cycle_id,
        run_id=f"run-{cycle_id}",
        started_at="2026-09-17T12:00:00Z",
        completed_at="2026-09-17T12:00:01Z",
        mode="e2e-smoke-manual",
        snapshot_id=f"{cycle_id}:fingerprint",
        stages=stages,
        tools_used=[],
        status="completed",
        decision_status="wait",
        self_improvement={"status": "none", "mutation_ids": [], "gates": {}},
        host={
            "host_type": "test",
            "cognitive_execution_claim": (
                f"Test host executed the declared cycle {cycle_id}."
            ),
        },
        host_input_schema_version=host_input_schema_version,
        evidence_completeness=evidence_completeness,
        evidence_advisories=(
            ["evidence_call_invalid:0:provenance:capture_missing"]
            if evidence_completeness == "partial"
            else ()
        ),
    )


class OperationalReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="reliability-"))
        self.path = self.root / "journal.jsonl"
        self.journal = AuditJournal(self.path)

    def append_receipt(
        self,
        cycle_id,
        *,
        host_input_schema_version=None,
        finalized=True,
        evidence_completeness=None,
    ):
        value = receipt(
            cycle_id,
            host_input_schema_version=host_input_schema_version,
            evidence_completeness=evidence_completeness,
        )
        record = self.journal.append_cycle_receipt(value)
        if finalized:
            self.journal.append(
                record_id=f"cycle-finalization:{cycle_id}",
                record_type="cycle_finalization",
                agent="test",
                caused_by=(f"cycle-receipt:{cycle_id}",),
                payload={
                    "schema_version": 1,
                    "cycle_id": cycle_id,
                    "input": {"canonical_sha256": "a" * 64},
                    "receipt": {
                        "record_id": f"cycle-receipt:{cycle_id}",
                    },
                },
            )
        return record

    def append_v3_dispositions(self, cycle_id):
        receipt_id = f"cycle-receipt:{cycle_id}"
        for stage_id in (
            "learning_audit",
            "meta_research",
            "self_improvement",
        ):
            self.journal.append(
                record_id=f"cycle-stage:{cycle_id}:{stage_id}",
                record_type="cycle_stage",
                agent="test",
                payload={
                    "cycle_id": cycle_id,
                    "stage_id": stage_id,
                    "output": {},
                },
            )
            self.journal.append(
                record_id=f"learning-disposition:{cycle_id}:{stage_id}",
                record_type="learning_disposition",
                agent="test",
                caused_by=(receipt_id,),
                payload={
                    "host_input_schema_version": 3,
                    "stage_id": stage_id,
                    "disposition": "no_change",
                    "rationale": "Current evidence supports no durable change.",
                    "evidence": [f"stage:{stage_id}"],
                    "artifact_refs": [],
                },
            )

    def append_refusal(self, name, *, pass_id=None, reason=None):
        payload = {
            "input": name,
            "input_sha256_12": "abc123",
            "reason": reason or "ValueError: secret detail must not leak",
            "at": "2026-09-17T12:00:00Z",
        }
        if pass_id:
            payload["pass_id"] = pass_id
        self.journal.append(
            record_id=f"refusal:{name}:{len(self.journal.read())}",
            record_type="host_input_refusal",
            agent="runtime",
            payload=payload,
        )

    def score(self):
        return operational_reliability(
            self.journal.read(), journal_path=self.path)

    def test_chain_order_drives_streak_and_acceptance(self):
        self.append_receipt("c1")
        self.append_refusal("cycle-bad.json", pass_id="pass-one")
        self.append_receipt("c2")
        rows = self.journal.read()
        timestamps = [
            "2026-09-17T15:00:00Z",
            "2026-09-17T15:01:00Z",
            "2026-09-17T12:00:00Z",
            "2026-09-17T14:00:00Z",
            "2026-09-17T14:01:00Z",
        ]
        previous = None
        for row, created_at in zip(rows, timestamps):
            row["created_at"] = created_at
            row["prev_hash"] = previous
            row["record_hash"] = hash_record(row)
            previous = row["record_hash"]

        score = operational_reliability(rows, journal_path=self.path)

        self.assertEqual(score["candidate_attempts"]["total"], 3)
        self.assertEqual(
            score["candidate_attempts"]["attempt_acceptance_rate"], 0.6667)
        self.assertEqual(score["accepted_candidate_streak"]["current"], 1)
        self.assertEqual(score["accepted_candidate_streak"]["maximum"], 1)

    def test_partial_receipt_is_research_only_and_resets_streak(self):
        self.append_receipt("complete-one")
        self.append_receipt(
            "partial-one",
            evidence_completeness="partial",
        )

        score = self.score()

        self.assertEqual(
            score["candidate_attempts"]["accepted_receipts"],
            1,
        )
        self.assertEqual(
            score["candidate_attempts"]["research_only_receipts"],
            1,
        )
        self.assertEqual(
            score["candidate_attempts"]["attempt_acceptance_rate"],
            0.5,
        )
        self.assertEqual(
            score["candidate_attempts"]["research_only_rate"],
            0.5,
        )
        self.assertEqual(
            score["accepted_candidate_streak"]["current"],
            0,
        )

    def test_new_receipt_without_manifest_is_runtime_incident(self):
        self.append_receipt("incomplete", finalized=False)

        score = self.score()

        self.assertEqual(score["candidate_attempts"]["total"], 0)
        self.assertEqual(
            score["runtime_incidents"]["receipts_pending_finalization"],
            1,
        )

    def test_refusal_windows_distinguish_records_from_known_passes(self):
        self.append_refusal("cycle-one.json", pass_id="pass-one")
        self.append_refusal("cycle-two.json", pass_id="pass-one")
        self.append_receipt("c1")

        window = self.score()["refusal_windows"]["recent"][0]

        self.assertEqual(window["refusal_records"], 2)
        self.assertEqual(window["known_refusal_passes"], 1)
        self.assertEqual(window["legacy_refusals_without_pass_id"], 0)

    def test_non_cycle_refusals_are_excluded_and_reasons_are_omitted(self):
        self.append_refusal("FEEDBACK.json")
        self.append_refusal("cycle-bad.json")

        score = self.score()
        encoded = json.dumps(score)

        self.assertEqual(score["candidate_attempts"]["total"], 1)
        self.assertEqual(
            score["candidate_attempts"]["excluded_non_cycle_refusals"], 1)
        self.assertNotIn("secret detail", encoded)

    def test_replay_compatibility_refusals_are_incidents_not_attempts(self):
        self.append_receipt("c1")
        self.append_refusal(
            "cycle-old.json",
            reason=(
                "ValueError: tool_provenance_payload_mismatch:"
                "tool-provenance:cycle-old"
            ),
        )

        score = self.score()

        self.assertEqual(score["candidate_attempts"]["total"], 1)
        self.assertEqual(
            score["candidate_attempts"][
                "excluded_replay_compatibility_refusals"
            ],
            1,
        )
        self.assertEqual(
            score["runtime_incidents"][
                "historical_replay_compatibility_refusals"
            ],
            1,
        )
        self.assertEqual(score["accepted_candidate_streak"]["current"], 1)
        self.assertEqual(
            score["cognitive_qualification_streak"]["refusal_resets"],
            0,
        )

    def test_detailed_or_later_provenance_mismatch_is_a_real_failure(self):
        self.append_receipt("c1")
        self.append_refusal(
            "cycle-tampered.json",
            reason=(
                "ValueError: tool_provenance_payload_mismatch:"
                "tool-provenance:cycle-tampered:call_0:result_sha256"
            ),
        )

        score = self.score()

        self.assertEqual(score["candidate_attempts"]["total"], 2)
        self.assertEqual(
            score["candidate_attempts"][
                "excluded_replay_compatibility_refusals"
            ],
            0,
        )
        self.assertEqual(score["accepted_candidate_streak"]["current"], 0)
        self.assertEqual(
            score["cognitive_qualification_streak"]["refusal_resets"],
            1,
        )

    def test_current_receipt_validation_failures_remain_visible(self):
        self.append_receipt("c1")
        rows = self.journal.read()
        rows[0]["payload"]["receipt_hash"] = "broken"

        score = operational_reliability(rows, journal_path=self.path)

        self.assertEqual(
            score["receipt_validation"]["failing_current_validator"], 1)
        self.assertIn(
            "receipt_hash_mismatch",
            score["receipt_validation"]["recent_failures"][0]["error_codes"],
        )

    def test_output_is_bounded(self):
        for index in range(20):
            self.append_refusal(
                f"cycle-{index}.json", pass_id=f"pass-{index}")
            self.append_receipt(f"c{index}")

        score = self.score()

        self.assertEqual(len(score["refusal_windows"]["recent"]), 8)
        self.assertEqual(score["refusal_windows"]["not_shown"], 12)
        self.assertLessEqual(
            len(json.dumps(score, separators=(",", ":")).encode("utf-8")),
            SCORECARD_BYTE_BUDGET,
        )

    def test_v2_receipts_do_not_increment_or_reset_cognitive_streak(self):
        self.append_receipt("v3-one", host_input_schema_version=3)
        self.append_v3_dispositions("v3-one")
        self.append_receipt("v2-history", host_input_schema_version=2)
        self.append_receipt("v3-two", host_input_schema_version=3)
        self.append_v3_dispositions("v3-two")

        score = self.score()["cognitive_qualification_streak"]

        self.assertEqual(score["current"], 2)
        self.assertEqual(score["maximum"], 2)
        self.assertEqual(score["qualifying_v3_receipts"], 2)
        self.assertEqual(score["not_scoreable_receipts"], 1)

    def test_nonqualifying_v3_and_refusal_reset_cognitive_streak(self):
        self.append_receipt("v3-one", host_input_schema_version=3)
        self.append_v3_dispositions("v3-one")
        self.append_receipt("v3-missing", host_input_schema_version=3)
        self.append_receipt("v3-two", host_input_schema_version=3)
        self.append_v3_dispositions("v3-two")
        self.append_refusal("cycle-bad.json", pass_id="pass-one")

        score = self.score()["cognitive_qualification_streak"]

        self.assertEqual(score["current"], 0)
        self.assertEqual(score["maximum"], 1)
        self.assertEqual(score["nonqualifying_v3_receipts"], 1)
        self.assertEqual(score["refusal_resets"], 1)

    def test_cli_prints_named_journal_scope(self):
        self.append_receipt("c1")
        output = StringIO()

        with redirect_stdout(output):
            self.assertEqual(main(["--journal", str(self.path)]), 0)

        self.assertEqual(
            json.loads(output.getvalue())["scope"]["journal"],
            self.path.name,
        )
