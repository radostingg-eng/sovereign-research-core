import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .cycle_receipt import build_receipt
from .engine import hash_record
from .reliability import SCORECARD_BYTE_BUDGET, main, operational_reliability

UNDECLARED = object()


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
    corrects_candidate_id=UNDECLARED,
    completed_at="2026-09-17T12:00:01Z",
    executor_provenance=None,
    stage_origins=None,
):
    stages = [stage()]
    if host_input_schema_version == 3:
        stages = [
            stage("learning_audit", 1),
            stage("meta_research", 2),
            stage("self_improvement", 3),
        ]
    if stage_origins is not None:
        if len(stage_origins) != len(stages):
            raise ValueError("stage_origin_count_mismatch")
        for row, origin in zip(stages, stage_origins):
            row["executor_origin"] = origin
    kwargs = dict(
        cycle_id=cycle_id,
        run_id=f"run-{cycle_id}",
        started_at="2026-09-17T12:00:00Z",
        completed_at=completed_at,
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
    if corrects_candidate_id is not UNDECLARED:
        kwargs.update({
            "corrects_candidate_id": corrects_candidate_id,
            "corrects_candidate_id_declared": True,
        })
    if executor_provenance is not None:
        kwargs["executor_provenance"] = executor_provenance
    return build_receipt(**kwargs)


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
        corrects_candidate_id=UNDECLARED,
        completed_at="2026-09-17T12:00:01Z",
        executor_provenance=None,
        stage_origins=None,
    ):
        value = receipt(
            cycle_id,
            host_input_schema_version=host_input_schema_version,
            evidence_completeness=evidence_completeness,
            corrects_candidate_id=corrects_candidate_id,
            completed_at=completed_at,
            executor_provenance=executor_provenance,
            stage_origins=stage_origins,
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

    def append_refusal(
        self,
        name,
        *,
        pass_id=None,
        reason=None,
        candidate_id=None,
        corrects_candidate_id=UNDECLARED,
        at="2026-09-17T12:00:00Z",
    ):
        payload = {
            "input": name,
            "input_sha256_12": "abc123",
            "candidate_id": (
                candidate_id
                or f"{name}@sha256:" + "a" * 64
            ),
            "reason": reason or "ValueError: secret detail must not leak",
            "at": at,
        }
        if pass_id:
            payload["pass_id"] = pass_id
        if corrects_candidate_id is not UNDECLARED:
            payload["corrects_candidate_id"] = corrects_candidate_id
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

    def test_executor_counts_and_queue_latency_keep_unknown_and_mixed(self):
        self.append_receipt("legacy")
        for cycle_id, writer, stages in (
            ("primary", "local_primary", ["local_primary"]),
            ("fallback", "github_fallback", ["github_fallback"]),
            (
                "mixed", "github_fallback",
                ["local_primary", "github_fallback", "github_fallback"],
            ),
        ):
            self.append_receipt(
                cycle_id,
                host_input_schema_version=3 if cycle_id == "mixed" else None,
                stage_origins=stages,
                executor_provenance={
                    "schema_version": 1,
                    "receipt_writer": writer,
                    "input_commit_sha": "a" * 40,
                    "input_committed_at": "2026-09-17T11:59:00Z",
                },
            )

        score = self.score()
        provenance = score["executor_provenance"]
        self.assertEqual(provenance["complete_receipts"], 4)
        self.assertEqual(provenance["provenance"], "runner_reported")
        self.assertIn(
            "do not independently prove launchd",
            provenance["what_this_means"],
        )
        self.assertEqual(provenance["primary_only"], 1)
        self.assertEqual(provenance["fallback_only"], 1)
        self.assertEqual(provenance["mixed"], 1)
        self.assertEqual(provenance["unknown"], 1)
        self.assertEqual(provenance["fallback_involved"], 2)
        self.assertEqual(provenance["queue_latency_seconds"]["measured_count"], 3)
        self.assertEqual(provenance["queue_latency_seconds"]["median"], 60.0)
        self.assertEqual(provenance["queue_latency_seconds"]["unknown_count"], 1)

    def test_negative_queue_latency_is_unknown_not_zero(self):
        self.append_receipt(
            "clock-skew",
            stage_origins=["local_primary"],
            executor_provenance={
                "schema_version": 1,
                "receipt_writer": "local_primary",
                "input_commit_sha": "b" * 40,
                "input_committed_at": "2026-09-17T12:05:00Z",
            },
        )

        provenance = self.score()["executor_provenance"]

        self.assertEqual(provenance["primary_only"], 1)
        self.assertEqual(
            {
                key: provenance["queue_latency_seconds"][key]
                for key in (
                    "measured_count", "unknown_count", "negative_count",
                    "median", "maximum",
                )
            },
            {
                "measured_count": 0,
                "unknown_count": 1,
                "negative_count": 1,
                "median": None,
                "maximum": None,
            },
        )

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
        self.assertEqual(
            score["retry_attempt_distribution"][
                "partial_receipts_excluded"
            ],
            1,
        )

    def test_first_attempt_acceptance_uses_declared_null_lineage(self):
        self.append_receipt("first", corrects_candidate_id=None)

        score = self.score()

        self.assertEqual(
            score["first_pass_acceptance"]["numerator"],
            1,
        )
        self.assertEqual(
            score["first_pass_acceptance"]["denominator"],
            1,
        )
        self.assertEqual(
            score["retry_attempt_distribution"]["accepted"],
            {"1": 1},
        )

    def test_multi_retry_success_has_exact_attempt_and_elapsed_time(self):
        first = "first.semantic.json@sha256:" + "a" * 64
        second = "second.semantic.json@sha256:" + "b" * 64
        self.append_refusal(
            "cycle-first.semantic.json",
            candidate_id=first,
            corrects_candidate_id=None,
            at="2026-09-17T12:00:00Z",
        )
        self.append_refusal(
            "cycle-second.semantic.json",
            candidate_id=second,
            corrects_candidate_id=first,
            at="2026-09-17T12:01:00Z",
        )
        self.append_receipt(
            "accepted",
            corrects_candidate_id=second,
            completed_at="2026-09-17T12:03:00Z",
        )

        score = self.score()

        self.assertEqual(score["first_pass_acceptance"]["numerator"], 0)
        self.assertEqual(score["first_pass_acceptance"]["denominator"], 1)
        self.assertEqual(score["first_pass_acceptance"]["rate"], 0.0)
        self.assertEqual(
            score["first_pass_acceptance"][
                "legacy_or_unjoinable_count"
            ],
            0,
        )
        self.assertEqual(
            score["retry_attempt_distribution"]["accepted"],
            {"3": 1},
        )
        self.assertEqual(
            score["time_to_convergence_seconds"]["measured_count"],
            1,
        )
        self.assertEqual(
            score["time_to_convergence_seconds"]["median"],
            180.0,
        )

    def test_abandoned_retry_distribution_is_explicit(self):
        first = "first.semantic.json@sha256:" + "a" * 64
        second = "second.semantic.json@sha256:" + "b" * 64
        self.append_refusal(
            "cycle-first.semantic.json",
            candidate_id=first,
            corrects_candidate_id=None,
        )
        self.append_refusal(
            "cycle-second.semantic.json",
            candidate_id=second,
            corrects_candidate_id=first,
        )

        score = self.score()

        self.assertEqual(
            score["retry_attempt_distribution"]["abandoned"],
            {"2": 1},
        )
        self.assertEqual(
            score["first_pass_acceptance"]["denominator"],
            1,
        )

    def test_convergence_time_is_omitted_without_both_timestamps(self):
        first = "first.semantic.json@sha256:" + "a" * 64
        self.append_refusal(
            "cycle-first.semantic.json",
            candidate_id=first,
            corrects_candidate_id=None,
            at=None,
        )
        self.append_receipt(
            "accepted",
            corrects_candidate_id=first,
        )

        score = self.score()["time_to_convergence_seconds"]

        self.assertEqual(score["measured_count"], 0)
        self.assertEqual(score["unmeasured_retry_acceptances"], 1)
        self.assertIsNone(score["median"])

    def test_legacy_records_are_not_inferred_into_lineages(self):
        self.append_refusal("cycle-legacy.semantic.json")
        self.append_receipt("legacy")

        score = self.score()

        self.assertEqual(
            score["first_pass_acceptance"]["denominator"],
            0,
        )
        self.assertEqual(
            score["first_pass_acceptance"][
                "legacy_or_unjoinable_count"
            ],
            2,
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

    def test_large_gate_history_trims_oldest_detail_not_aggregates(self):
        resets = [
            {
                "record_id": f"reset-{index}",
                "created_at": f"2026-09-20T{index:02d}:00:00Z",
                "reason": "R" * 800,
            }
            for index in range(20)
        ]
        gate = {
            "provenance": "host_claimed",
            "activation_at": "2026-09-21T11:57:00+00:00",
            "evaluated_through": "2026-09-21T10:57:00+00:00",
            "reset_count": 20,
            "recent_resets": resets,
            "audit_problems": [],
            "mature_slots": [],
            "mature_slots_not_shown": 0,
            "gate_a": {"status": "pending"},
            "gate_b": {"status": "pending"},
        }

        with patch(
            "runtime.reliability.reliability_gate_summary",
            return_value=gate,
        ):
            score = self.score()

        projected = score["gate_summary"]
        self.assertEqual(projected["reset_count"], 20)
        self.assertEqual(projected["recent_resets"][0]["record_id"], "reset-0")
        self.assertGreater(projected["recent_resets_not_shown"], 0)
        self.assertTrue(score["detail_projection"]["bounded"])
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
