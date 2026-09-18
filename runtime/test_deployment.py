import inspect
import pathlib
import subprocess
import tempfile
import unittest

from .deployment import (DeploymentError, deploy_candidate, head_sha, rollback_to,
                         verify_deployment_report, working_tree_is_clean)
from .measurement import Measurement, MeasurementSuite, RESEARCH_TOOL
from .self_improvement import MutationProposal


COMMAND = ("python3", "-m", "compileall", "-q", "runtime")

GOOD = ("diff --git a/runtime/mod.py b/runtime/mod.py\n--- a/runtime/mod.py\n"
        "+++ b/runtime/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n")
BREAKS = ("diff --git a/runtime/mod.py b/runtime/mod.py\n--- a/runtime/mod.py\n"
          "+++ b/runtime/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = (((\n")
STALE = ("diff --git a/runtime/mod.py b/runtime/mod.py\n--- a/runtime/mod.py\n"
         "+++ b/runtime/mod.py\n@@ -1 +1 @@\n-NOT THE CONTENT\n+VALUE = 3\n")
ESCAPES = ("diff --git a/SYSTEM.md b/runtime/mod.py\n"
           "rename from SYSTEM.md\nrename to runtime/mod.py\n")


def proposal(patch, *, targets=("runtime/mod.py",), **over):
    signature = inspect.signature(MutationProposal)
    kw = dict(mutation_id="m1", parent_version="v1", targets=targets, patch=patch,
              failure_ids=("f1",), expected_effect="e", counter_metrics=("cost",),
              rollback_condition="r", sample_requirement=30)
    kw = {k: v for k, v in kw.items() if k in signature.parameters}
    for name in signature.parameters:
        if name not in kw:
            kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
    kw.update(over)
    return MutationProposal(**kw)


class DeploymentTestCase(unittest.TestCase):
    """Each test gets a scratch repository. The real one is never touched."""

    def scratch_repo(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="sovereign-deploy-test-"))
        (root / "runtime").mkdir()
        (root / "runtime" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "init", "-q"], capture_output=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                        "user.email=t@t", "commit", "-q", "-m", "init"], capture_output=True)
        return root

    def value(self, root):
        return (root / "runtime" / "mod.py").read_text(encoding="utf-8").strip()


class DeploymentAppliesAndVerifiesTests(DeploymentTestCase):
    """promote_mutation updates a dictionary. Nothing went live.

    A "promoted" mutation changed a state file and nothing else, so the
    system could report a promotion while the code it promoted had never
    been applied anywhere.
    """

    def test_a_good_candidate_is_applied_and_verified(self):
        root = self.scratch_repo()
        before = head_sha(root)
        report = deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)
        self.assertTrue(report.applied)
        self.assertTrue(report.verified)
        self.assertNotEqual(report.head_after, before)

    def test_the_change_is_actually_on_disk_afterwards(self):
        root = self.scratch_repo()
        deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)
        self.assertEqual(self.value(root), "VALUE = 2")

    def test_the_reported_shas_come_from_git(self):
        root = self.scratch_repo()
        parent = head_sha(root)
        report = deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)
        self.assertEqual(report.parent_sha, parent)
        self.assertEqual(report.candidate_sha, head_sha(root))
        self.assertNotEqual(report.parent_sha, report.candidate_sha)


class FailedVerificationRollsBackTests(DeploymentTestCase):
    """Rollback is not best-effort.

    A rollback that silently failed would leave a broken candidate live
    while reporting it removed, which is worse than never rolling back.
    """

    def test_a_breaking_candidate_is_rolled_back(self):
        root = self.scratch_repo()
        report = deploy_candidate(proposal(BREAKS), repo_root=root, command=COMMAND)
        self.assertTrue(report.applied)
        self.assertFalse(report.verified)
        self.assertTrue(report.rolled_back)

    def test_head_returns_to_the_parent_commit(self):
        root = self.scratch_repo()
        parent = head_sha(root)
        report = deploy_candidate(proposal(BREAKS), repo_root=root, command=COMMAND)
        self.assertEqual(head_sha(root), parent)
        self.assertEqual(report.restored_sha, parent)

    def test_the_file_contents_are_restored(self):
        root = self.scratch_repo()
        deploy_candidate(proposal(BREAKS), repo_root=root, command=COMMAND)
        self.assertEqual(self.value(root), "VALUE = 1")

    def test_the_failure_exit_code_is_reported(self):
        root = self.scratch_repo()
        report = deploy_candidate(proposal(BREAKS), repo_root=root, command=COMMAND)
        self.assertNotEqual(report.verify_exit_code, 0)
        self.assertIn("verification_failed", report.reason)


class DeploymentRefusesUnsafeStatesTests(DeploymentTestCase):

    def test_a_dirty_working_tree_is_refused(self):
        """A deployment that cannot tell its own change from someone else's
        cannot honestly roll back: restoring the parent would discard work
        it never made."""
        root = self.scratch_repo()
        (root / "runtime" / "unrelated.py").write_text("X = 1\n", encoding="utf-8")
        self.assertFalse(working_tree_is_clean(root))
        with self.assertRaises(DeploymentError) as ctx:
            deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)
        self.assertIn("working_tree_not_clean", str(ctx.exception))

    def test_an_out_of_scope_proposal_is_refused(self):
        root = self.scratch_repo()
        with self.assertRaises(DeploymentError) as ctx:
            deploy_candidate(proposal(ESCAPES), repo_root=root, command=COMMAND)
        self.assertIn("immutable_target:SYSTEM.md", str(ctx.exception))

    def test_a_patch_that_does_not_apply_leaves_the_tree_clean(self):
        root = self.scratch_repo()
        parent = head_sha(root)
        report = deploy_candidate(proposal(STALE), repo_root=root, command=COMMAND)
        self.assertFalse(report.applied)
        self.assertIn("patch_did_not_apply", report.reason)
        self.assertEqual(head_sha(root), parent)
        self.assertTrue(working_tree_is_clean(root))


class ExplicitRollbackTests(DeploymentTestCase):

    def test_rollback_restores_a_known_commit_and_verifies_it(self):
        root = self.scratch_repo()
        original = head_sha(root)
        deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)
        self.assertEqual(self.value(root), "VALUE = 2")
        report = rollback_to(original, repo_root=root, command=COMMAND)
        self.assertTrue(report.rolled_back)
        self.assertTrue(report.verified)
        self.assertEqual(head_sha(root), original)
        self.assertEqual(self.value(root), "VALUE = 1")

    def test_a_malformed_sha_is_refused(self):
        root = self.scratch_repo()
        with self.assertRaises(DeploymentError):
            rollback_to("abc", repo_root=root, command=COMMAND)


class DeploymentReportsAreBoundTests(DeploymentTestCase):

    def test_a_verified_deployment_report_validates(self):
        root = self.scratch_repo()
        report = deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)
        self.assertEqual(verify_deployment_report(report, proposal(GOOD)), [])

    def test_a_rolled_back_deployment_is_not_evidence_of_going_live(self):
        root = self.scratch_repo()
        report = deploy_candidate(proposal(BREAKS), repo_root=root, command=COMMAND)
        errors = verify_deployment_report(report, proposal(BREAKS))
        self.assertIn("deployment_was_rolled_back", errors)
        self.assertIn("deployment_not_verified", errors)

    def test_a_report_cannot_be_spent_on_another_proposal(self):
        root = self.scratch_repo()
        report = deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)
        errors = verify_deployment_report(report, proposal(GOOD, targets=("runtime/other.py",)))
        self.assertTrue(any(e.startswith("deployment_report_for_other_proposal")
                            for e in errors), errors)

    def test_a_missing_report_is_refused(self):
        self.assertEqual(verify_deployment_report(None, proposal(GOOD)),
                         ["deployment_report_missing"])


class PromotionConsumesDeploymentEvidenceTests(DeploymentTestCase):
    """A promotion should mean the code went live and ran.

    promote_mutation took production_parent_sha and candidate_sha as strings
    the caller supplied, so it could record a promotion of something never
    applied anywhere. When a deployment report is supplied the SHAs come
    from git instead, and the report has to evidence a verified deployment.
    """

    def deployed(self, patch=GOOD):
        root = self.scratch_repo()
        report = deploy_candidate(proposal(patch), repo_root=root, command=COMMAND)
        return root, report

    def test_the_full_path_runs_from_deployment_to_promotion(self):
        from .self_improvement import promote_mutation
        _, report = self.deployed()
        candidate = proposal(GOOD)
        verdict = _eligible_verdict(candidate)
        promoted = promote_mutation({}, candidate, verdict, deployment_report=report)
        entry = promoted["self_improvement"]["mutations"][-1]
        self.assertEqual(entry["candidate_sha"], report.candidate_sha)
        self.assertEqual(entry["production_parent_sha"], report.parent_sha)

    def test_the_promoted_shas_are_the_ones_git_produced(self):
        from .self_improvement import promote_mutation
        root, report = self.deployed()
        promoted = promote_mutation({}, proposal(GOOD), _eligible_verdict(proposal(GOOD)),
                                    deployment_report=report)
        self.assertEqual(promoted["self_improvement"]["mutations"][-1]["candidate_sha"],
                         head_sha(root))

    def test_a_rolled_back_deployment_cannot_be_promoted(self):
        """The candidate is not live. Recording it as promoted would be a lie
        about the state of the running system."""
        from .self_improvement import promote_mutation
        _, report = self.deployed(BREAKS)
        with self.assertRaises(ValueError) as ctx:
            promote_mutation({}, proposal(BREAKS), _eligible_verdict(proposal(BREAKS)),
                             deployment_report=report)
        self.assertIn("deployment_not_evidenced", str(ctx.exception))

    def test_a_report_from_another_proposal_cannot_be_promoted(self):
        from .self_improvement import promote_mutation
        _, report = self.deployed()
        other = proposal(GOOD, targets=("runtime/other.py",))
        with self.assertRaises(ValueError):
            promote_mutation({}, other, _eligible_verdict(other), deployment_report=report)


def _eligible_verdict(candidate):
    """An eligible verdict bound to `candidate`, with sandbox evidence."""
    from .sandbox import SandboxReport
    from .self_improvement import evaluate_mutation, proposal_digest
    report = SandboxReport(
        proposal_digest=proposal_digest(candidate), passed=True, exit_code=0,
        command=COMMAND, tree_digest="a" * 64, stdout_tail="", duration_s=0.1,
        started_at="2026-09-16T00:00:00+00:00", stage="sandbox",
    )
    return evaluate_mutation(
        candidate, sample_size=40, primary_delta=0.5,
        counter_metric_deltas={"cost": -0.1},
        counter_metric_directions={"cost": "lower_is_better"},
        out_of_sample=True, sandbox_passed=True, rollback_triggered=False,
        sandbox_report=report, measurement_suite=_measured(candidate),
    )

def _measured(candidate):
    """A measurement suite bound to `candidate`, carrying a protected holdout.

    evaluate_mutation now refuses to reach eligible without one, because
    primary_delta was otherwise whatever the caller typed. Built directly so
    these tests stay about their own seam; measure_candidate's real two-tree
    run is covered in test_measurement.py.
    """
    from .self_improvement import proposal_digest
    return MeasurementSuite(
        proposal_digest=proposal_digest(candidate),
        at="2026-09-16T00:00:00+00:00",
        measurements=(
            Measurement("dev", RESEARCH_TOOL, "higher_is_better", False,
                        0.0, 0.5, 0, 0, True, "measured"),
            Measurement("holdout", RESEARCH_TOOL, "higher_is_better", True,
                        0.0, 0.2, 0, 0, True, "measured"),
        ),
    )



class VerificationThatCannotFinishRollsBackTests(DeploymentTestCase):
    """A timeout used to leave the candidate committed and live.

    The exception escaped before the rollback, so the one path where
    verification cannot finish was also the one path that left unverified
    code in place. Any failure to verify must roll back, not just a failing
    exit code.
    """

    def test_a_timeout_restores_the_parent(self):
        root = self.scratch_repo()
        parent = head_sha(root)
        report = deploy_candidate(proposal(GOOD), repo_root=root,
                                  command=("python3", "-c", "import time; time.sleep(30)"),
                                  timeout_s=1)
        self.assertFalse(report.verified)
        self.assertTrue(report.rolled_back)
        self.assertEqual(head_sha(root), parent)
        self.assertIn("timed_out", report.reason)

    def test_the_file_is_restored_after_a_timeout(self):
        root = self.scratch_repo()
        deploy_candidate(proposal(GOOD), repo_root=root,
                         command=("python3", "-c", "import time; time.sleep(30)"),
                         timeout_s=1)
        self.assertEqual(self.value(root), "VALUE = 1")

    def test_a_command_that_cannot_run_also_rolls_back(self):
        root = self.scratch_repo()
        parent = head_sha(root)
        report = deploy_candidate(proposal(GOOD), repo_root=root,
                                  command=("definitely-not-a-real-binary-xyz",))
        self.assertTrue(report.rolled_back)
        self.assertEqual(head_sha(root), parent)


class ContradictoryReportsAreRefusedTests(DeploymentTestCase):
    """A report can claim verified while contradicting itself.

    Exit code 7 beside verified=True, or a HEAD still pointing at the parent,
    is a hand-written assertion wearing a deployment result's clothes.
    """

    def clean_report(self):
        root = self.scratch_repo()
        return deploy_candidate(proposal(GOOD), repo_root=root, command=COMMAND)

    def test_verified_with_a_nonzero_exit_is_refused(self):
        import dataclasses
        forged = dataclasses.replace(self.clean_report(), verify_exit_code=7)
        self.assertIn("deployment_verified_without_clean_exit:7",
                      verify_deployment_report(forged, proposal(GOOD)))

    def test_verified_while_head_is_still_the_parent_is_refused(self):
        import dataclasses
        report = self.clean_report()
        forged = dataclasses.replace(report, head_after=report.parent_sha)
        self.assertTrue([e for e in verify_deployment_report(forged, proposal(GOOD))
                         if e.startswith(
                             "deployment_verified_but_head_is_not_the_candidate")])

    def test_a_genuine_report_still_passes(self):
        self.assertEqual(verify_deployment_report(self.clean_report(), proposal(GOOD)), [])


class AbsentEvidenceIsNotPassingEvidenceTests(DeploymentTestCase):
    """Two holes wearing the shape of a check.

    An ABSENT verify_exit_code was accepted as success, the same mistake as
    reading an absent order_submission_used as a denial: a verification that
    produced no exit code did not demonstrably pass. And head_after was
    compared only against the PARENT, so an absent head, or one pointing at
    some unrelated commit, both counted as proof the candidate was live.
    """

    def clean_report(self):
        return deploy_candidate(proposal(GOOD), repo_root=self.scratch_repo(),
                                command=COMMAND)

    def report(self, **over):
        import dataclasses
        return dataclasses.replace(self.clean_report(), **over)

    def errors(self, **over):
        return verify_deployment_report(self.report(**over), proposal(GOOD))

    def test_an_absent_exit_code_is_not_success(self):
        self.assertTrue([e for e in self.errors(verify_exit_code=None)
                         if e.startswith("deployment_verified_without_clean_exit")])

    def test_an_absent_head_is_not_proof_of_anything(self):
        self.assertIn("deployment_verified_without_head",
                      self.errors(head_after=""))

    def test_an_unrelated_head_does_not_evidence_this_candidate(self):
        """Not the parent, so the old check passed it. Not the candidate either."""
        self.assertTrue([e for e in self.errors(head_after="c" * 40)
                         if e.startswith(
                             "deployment_verified_but_head_is_not_the_candidate")])

    def test_the_candidate_sha_is_what_evidences_it(self):
        report = self.clean_report()
        self.assertEqual(report.head_after, report.candidate_sha)
        self.assertEqual(verify_deployment_report(report, proposal(GOOD)), [])
