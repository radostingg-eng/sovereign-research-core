"""A standing-prompt proposal can be tested without becoming production."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .host_feedback import CANONICAL_EXAMPLE
from .host_publication import content_sha256, marker_path
from .prompt_invariants import STANDING_PROMPT_TARGET
from .run_host_cycle import (
    evaluate_proposed_mutation,
    main as run_host_cycle_main,
    self_improvement_state,
    validate_mutation_block,
)
from .sandbox import SandboxError
from .self_improvement import (
    FailureRecord,
    MutationProposal,
    mutation_proposal_summary,
    persist_mutation_proposal,
)
from .test_sandbox import COMPILES, PROMPT_PATH, prompt_patch


def proposal(patch, *, targets=(STANDING_PROMPT_TARGET,), mutation_type="prompt"):
    return MutationProposal(
        mutation_id="prompt-test",
        parent_version="baseline-v1",
        mutation_type=mutation_type,
        targets=targets,
        rationale="Test a source-backed prompt change without activating it.",
        failure_ids=("failure-observed",),
        patch=patch,
        expected_effect="Reduce the observed refusal fingerprint.",
        counter_metrics=("total_refusal_rate",),
        sample_requirement=30,
        evaluation_window="Subsequent accepted cycles.",
        rollback_condition="The refusal rate worsens.",
        created_at="2026-09-25T12:00:00Z",
    )


class PromptCandidateEvaluationTests(unittest.TestCase):
    def test_valid_prompt_can_be_evaluated_but_not_promoted(self):
        candidate = proposal(prompt_patch(invalid=False))
        original = PROMPT_PATH.read_bytes()
        self.assertEqual(
            validate_mutation_block({"mutation": candidate.as_dict()}),
            [],
        )

        result = evaluate_proposed_mutation(candidate.as_dict())

        self.assertEqual(result["status"], "testing")
        self.assertEqual(result["gates"]["sandbox"], "passed")
        self.assertEqual(result["gates"]["measurement"], "produced")
        self.assertEqual(PROMPT_PATH.read_bytes(), original)

    def test_without_opt_in_a_prompt_proposal_is_only_recorded(self):
        candidate = proposal(prompt_patch(invalid=False))

        result = self_improvement_state(
            {"mutation": candidate.as_dict()},
        )

        self.assertEqual(result["status"], "proposed_not_evaluated")
        self.assertEqual(
            result["gates"]["candidate_execution"], "not_enabled",
        )

    def test_withheld_prompt_proposal_persists_after_real_receipt(self):
        candidate = proposal(prompt_patch(invalid=False))
        with tempfile.TemporaryDirectory() as directory:
            journal = AuditJournal(Path(directory) / "audit.jsonl")
            failure = FailureRecord(
                failure_id="failure-observed",
                run_id="prior-run",
                stage="self_improvement",
                failure_class="prompt_refusal",
                symptom="A staged correction was refused.",
                evidence=(),
                severity="medium",
                detected_at="2026-09-25T12:00:00Z",
            )
            journal.append(
                record_id="failure:failure-observed",
                record_type="failure_record",
                agent="sovereign-host",
                payload=failure.as_dict(),
            )
            receipt = {
                "cycle_id": "cycle-prompt-test",
                "self_improvement": self_improvement_state(
                    {"mutation": candidate.as_dict()},
                ),
            }
            journal.append(
                record_id="cycle-receipt:cycle-prompt-test",
                record_type="cycle_receipt",
                agent="sovereign-host",
                payload=receipt,
                caused_by=("failure:failure-observed",),
            )

            self.assertTrue(persist_mutation_proposal(
                {"mutation": candidate.as_dict()}, journal, receipt,
            ))
            records = journal.read()
            summary = mutation_proposal_summary(records)
            self.assertEqual(summary["count"], 1)
            self.assertEqual(
                summary["items"][0]["status"], "proposed_not_evaluated",
            )
            self.assertIn(
                "failure:failure-observed",
                records[-1]["caused_by"],
            )

    def test_full_cycle_keeps_prompt_proposal_unexecuted_and_finalized(self):
        candidate = proposal(prompt_patch(invalid=False))
        original = PROMPT_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = copy.deepcopy(CANONICAL_EXAMPLE)
            data["cycle_id"] = "cycle-prompt-proposal"
            data["mutation"] = candidate.as_dict()
            source = root / "cycle.json"
            source.write_text(
                json.dumps(data), encoding="utf-8",
            )
            (root / ".promotion_policy.json").write_text(
                json.dumps({"schema_version": 1, "legacy_files": []}),
                encoding="utf-8",
            )
            marker = marker_path(root, source.name)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                content_sha256(source) + "\n", encoding="utf-8",
            )
            journal_path = root / "audit.jsonl"
            journal = AuditJournal(journal_path)
            journal.append(
                record_id="failure:failure-observed",
                record_type="failure_record",
                agent="sovereign-host",
                payload={"failure_id": "failure-observed"},
            )

            self.assertEqual(run_host_cycle_main([
                "--input-dir", str(root),
                "--journal", str(journal_path),
            ]), 0)
            records = journal.read()
            receipt = next(
                record for record in records
                if record["record_id"]
                == "cycle-receipt:cycle-prompt-proposal"
            )
            self.assertEqual(
                receipt["payload"]["self_improvement"]["status"],
                "proposed_not_evaluated",
            )
            self.assertTrue(any(
                record["record_id"] == "mutation-proposal:prompt-test"
                for record in records
            ))
            self.assertTrue(any(
                record["record_id"]
                == "cycle-finalization:cycle-prompt-proposal"
                for record in records
            ))
        self.assertEqual(PROMPT_PATH.read_bytes(), original)

    def test_invalid_prompt_fails_without_refusing_the_cycle(self):
        candidate = proposal(prompt_patch(invalid=True))
        self.assertEqual(
            validate_mutation_block({"mutation": candidate.as_dict()}),
            [],
        )

        result = self_improvement_state(
            {"mutation": candidate.as_dict()}, allow_execution=True,
        )

        self.assertEqual(result["status"], "evaluation_failed")
        self.assertIn("candidate_prompt_invalid", result["note"])
        self.assertIn("no_fabricated_tool_use", result["note"])

    def test_mixed_prompt_and_runtime_candidate_stays_out_of_scope(self):
        candidate = proposal(
            prompt_patch(invalid=False) + COMPILES,
            targets=(
                STANDING_PROMPT_TARGET,
                "runtime/strategy_coverage.py",
            ),
        )

        errors = validate_mutation_block({"mutation": candidate.as_dict()})

        self.assertIn(
            "mutation_invalid:target_outside_allowlist:"
            "runtime/strategy_coverage.py",
            errors,
        )
        with self.assertRaisesRegex(
            SandboxError, "target_outside_allowlist:runtime/strategy_coverage.py",
        ):
            evaluate_proposed_mutation(candidate.as_dict())

    def test_runtime_candidate_retains_the_existing_scope(self):
        candidate = proposal(
            COMPILES,
            targets=("runtime/strategy_coverage.py",),
            mutation_type="runtime_patch",
        )
        self.assertEqual(
            validate_mutation_block({"mutation": candidate.as_dict()}),
            [],
        )

        result = evaluate_proposed_mutation(candidate.as_dict())

        self.assertEqual(result["status"], "testing")
        self.assertEqual(result["gates"]["sandbox"], "passed")
