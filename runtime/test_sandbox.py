import difflib
import inspect
import unittest
from pathlib import Path

from .sandbox import (DEFAULT_COMMAND, SandboxError, SandboxReport, run_candidate,
                      verify_sandbox_report)
from .measurement import Measurement, MeasurementSuite, RESEARCH_TOOL
from .self_improvement import (MutationProposal, evaluate_mutation, promote_mutation,
                               proposal_digest)


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "host-standing-schedule.md"
HEADER = '"""Which strategy families the host has actually used, and which it has not.'

COMPILES = ("diff --git a/runtime/strategy_coverage.py b/runtime/strategy_coverage.py\n"
            "--- a/runtime/strategy_coverage.py\n+++ b/runtime/strategy_coverage.py\n"
            "@@ -1 +1,2 @@\n+# candidate marker\n " + HEADER + "\n")

BREAKS_BUILD = ("diff --git a/runtime/strategy_coverage.py b/runtime/strategy_coverage.py\n"
                "--- a/runtime/strategy_coverage.py\n+++ b/runtime/strategy_coverage.py\n"
                "@@ -1 +1,2 @@\n+this is not valid python(((\n " + HEADER + "\n")

DOES_NOT_APPLY = ("diff --git a/runtime/strategy_coverage.py b/runtime/strategy_coverage.py\n"
                  "--- a/runtime/strategy_coverage.py\n+++ b/runtime/strategy_coverage.py\n"
                  "@@ -1 +1,2 @@\n+x\n THIS LINE IS NOT IN THE FILE\n")

ESCAPES_SCOPE = ("diff --git a/SYSTEM.md b/runtime/strategy_coverage.py\n"
                 "rename from SYSTEM.md\nrename to runtime/strategy_coverage.py\n")


def proposal(patch, *, targets=("runtime/strategy_coverage.py",), mutation_id="m1", **over):
    signature = inspect.signature(MutationProposal)
    kw = dict(mutation_id=mutation_id, parent_version="v1", targets=targets, patch=patch,
              failure_ids=("f1",), expected_effect="e", counter_metrics=("cost",),
              rollback_condition="r", sample_requirement=30)
    kw = {k: v for k, v in kw.items() if k in signature.parameters}
    for name in signature.parameters:
        if name not in kw:
            kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
    kw.update(over)
    return MutationProposal(**kw)


def prompt_patch(*, invalid):
    original = PROMPT_PATH.read_text(encoding="utf-8")
    line = "Never claim a tool was consulted when it was not."
    if invalid:
        assert original.count(line) == 1
        modified = original.replace(line, "Tool claims need no evidence.")
    else:
        modified = original + "\n"
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile="a/prompts/host-standing-schedule.md",
        tofile="b/prompts/host-standing-schedule.md",
        n=1,
    ))


class SandboxActuallyRunsTests(unittest.TestCase):
    """`sandbox_passed` used to be a boolean the caller supplied.

    Nothing verified a sandbox had ever run, so the cheapest way to clear the
    sandbox gate was to pass True. These tests exercise a real checkout, a
    real patch application and a real command.
    """

    def test_a_compiling_candidate_passes(self):
        report = run_candidate(proposal(COMPILES), repo_root=REPO_ROOT)
        self.assertTrue(report.passed)
        self.assertEqual(report.exit_code, 0)

    def test_a_candidate_that_breaks_the_build_fails(self):
        report = run_candidate(proposal(BREAKS_BUILD), repo_root=REPO_ROOT)
        self.assertFalse(report.passed)
        self.assertNotEqual(report.exit_code, 0)

    def test_passed_is_derived_from_the_exit_code(self):
        good = run_candidate(proposal(COMPILES), repo_root=REPO_ROOT)
        bad = run_candidate(proposal(BREAKS_BUILD), repo_root=REPO_ROOT)
        self.assertEqual(good.passed, good.exit_code == 0)
        self.assertEqual(bad.passed, bad.exit_code == 0)

    def test_different_candidates_produce_different_tree_digests(self):
        good = run_candidate(proposal(COMPILES), repo_root=REPO_ROOT)
        bad = run_candidate(proposal(BREAKS_BUILD), repo_root=REPO_ROOT)
        self.assertNotEqual(good.tree_digest, bad.tree_digest)

    def test_the_report_names_the_command_it_ran(self):
        report = run_candidate(proposal(COMPILES), repo_root=REPO_ROOT)
        self.assertEqual(report.command, DEFAULT_COMMAND)

    def test_the_working_tree_is_untouched(self):
        """The candidate runs against an export of HEAD, not the repo."""
        target = REPO_ROOT / "runtime" / "strategy_coverage.py"
        before = target.read_text(encoding="utf-8")
        run_candidate(proposal(COMPILES), repo_root=REPO_ROOT)
        after = target.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertNotIn("# candidate marker", after)

    def test_prompt_invariant_runs_even_when_requested_command_passes(self):
        original = PROMPT_PATH.read_text(encoding="utf-8")
        with self.assertRaisesRegex(
            SandboxError, "candidate_prompt_invalid:.*no_fabricated_tool_use",
        ):
            run_candidate(
                proposal(
                    prompt_patch(invalid=True),
                    targets=("prompts/host-standing-schedule.md",),
                ),
                repo_root=REPO_ROOT,
                allowed_prefixes=("prompts/",),
                command=("python3", "-c", "print('passed')"),
            )
        self.assertEqual(PROMPT_PATH.read_text(encoding="utf-8"), original)

    def test_valid_prompt_candidate_remains_testable(self):
        report = run_candidate(
            proposal(
                prompt_patch(invalid=False),
                targets=("prompts/host-standing-schedule.md",),
            ),
            repo_root=REPO_ROOT,
            allowed_prefixes=("prompts/",),
            command=("python3", "-c", "print('passed')"),
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.exit_code, 0)

    def test_command_cannot_weaken_prompt_after_preflight(self):
        change_prompt = (
            "from pathlib import Path; "
            "p=Path('prompts/host-standing-schedule.md'); "
            "t=p.read_text(); "
            "p.write_text(t.replace("
            "'Never claim a tool was consulted when it was not.',"
            "'Tool claims need no evidence.'))"
        )
        report = run_candidate(
            proposal(
                prompt_patch(invalid=False),
                targets=("prompts/host-standing-schedule.md",),
            ),
            repo_root=REPO_ROOT,
            allowed_prefixes=("prompts/",),
            command=("python3", "-c", change_prompt),
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.exit_code, 126)
        self.assertIn("candidate_prompt_invalid_after_command", report.stdout_tail)


class NotRunIsNotTheSameAsFailedTests(unittest.TestCase):
    """A patch that never applied has not been tested.

    Reporting that as a failed run would be as wrong as reporting it as a
    pass: in both cases the evidence does not exist.
    """

    def test_a_patch_that_does_not_apply_raises_rather_than_failing(self):
        with self.assertRaises(SandboxError) as ctx:
            run_candidate(proposal(DOES_NOT_APPLY), repo_root=REPO_ROOT)
        self.assertIn("patch_did_not_apply", str(ctx.exception))

    def test_an_out_of_scope_patch_is_refused_before_it_runs(self):
        """Executing an out-of-scope patch to see what happens is how a
        sandbox becomes the thing that applies the change."""
        with self.assertRaises(SandboxError) as ctx:
            run_candidate(proposal(ESCAPES_SCOPE), repo_root=REPO_ROOT)
        self.assertIn("immutable_target:SYSTEM.md", str(ctx.exception))

    def test_an_empty_patch_is_refused(self):
        with self.assertRaises(SandboxError):
            run_candidate(proposal(""), repo_root=REPO_ROOT)


class ReportsAreBoundToTheirProposalTests(unittest.TestCase):

    def report(self):
        return run_candidate(proposal(COMPILES), repo_root=REPO_ROOT)

    def test_a_matching_report_verifies(self):
        self.assertEqual(verify_sandbox_report(self.report(), proposal(COMPILES)), [])

    def test_a_report_cannot_be_spent_on_another_proposal(self):
        errors = verify_sandbox_report(self.report(), proposal(COMPILES + "\n# other\n"))
        self.assertTrue(any(e.startswith("sandbox_report_for_other_proposal") for e in errors))

    def test_a_report_claiming_success_beside_a_failure_is_refused(self):
        import dataclasses
        forged = dataclasses.replace(self.report(), passed=True, exit_code=1)
        errors = verify_sandbox_report(forged, proposal(COMPILES))
        self.assertTrue(any(e.startswith("sandbox_report_inconsistent") for e in errors))

    def test_a_missing_report_is_refused(self):
        self.assertEqual(verify_sandbox_report(None, proposal(COMPILES)),
                         ["sandbox_report_missing"])


class EndToEndPromotionTests(unittest.TestCase):
    """run_candidate -> evaluate_mutation -> promote_mutation.

    The seam is the point: a report earned by a real run has to satisfy the
    verdict gate, and the verdict has to satisfy the promotion gate.
    """

    EVIDENCE = dict(sample_size=40, primary_delta=0.5,
                    counter_metric_deltas={"cost": -0.1},
                    counter_metric_directions={"cost": "lower_is_better"},
                    out_of_sample=True, sandbox_passed=True, rollback_triggered=False)

    def test_a_real_passing_run_reaches_promotion(self):
        candidate = proposal(COMPILES)
        report = run_candidate(candidate, repo_root=REPO_ROOT)
        verdict = evaluate_mutation(candidate, sandbox_report=report,
                                    measurement_suite=_measured(candidate),
                                    **self.EVIDENCE)
        self.assertEqual(verdict.status, "eligible")
        from .deployment import DeploymentReport
        from .self_improvement import proposal_digest
        deployment = DeploymentReport(
            proposal_digest=proposal_digest(candidate), parent_sha="a" * 40,
            candidate_sha="b" * 40, applied=True, verified=True, verify_exit_code=0,
            command=DEFAULT_COMMAND, rolled_back=False, restored_sha=None,
            head_after="b" * 40, reason="verified", at="2026-09-16T00:00:00+00:00")
        promoted = promote_mutation({}, candidate, verdict,
                                    deployment_report=deployment)
        self.assertEqual(promoted["self_improvement"]["active_mutation_id"], "m1")

    def test_a_real_failing_run_is_rejected(self):
        candidate = proposal(BREAKS_BUILD)
        report = run_candidate(candidate, repo_root=REPO_ROOT)
        verdict = evaluate_mutation(candidate, sandbox_report=report,
                                    measurement_suite=_measured(candidate),
                                    **self.EVIDENCE)
        self.assertEqual(verdict.status, "rejected")
        self.assertIn("sandbox_failed", verdict.reason)

    def test_no_report_is_testing_rather_than_eligible(self):
        """Untested is not failed. Those are different findings."""
        verdict = evaluate_mutation(proposal(COMPILES), **self.EVIDENCE)
        self.assertEqual(verdict.status, "testing")
        self.assertIn("sandbox_evidence_required", verdict.reason)

    def test_a_report_from_another_candidate_does_not_authorize_promotion(self):
        report = run_candidate(proposal(COMPILES), repo_root=REPO_ROOT)
        other = proposal(COMPILES, targets=("runtime/research.py",))
        verdict = evaluate_mutation(other, sandbox_report=report, **self.EVIDENCE)
        self.assertNotEqual(verdict.status, "eligible")


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
