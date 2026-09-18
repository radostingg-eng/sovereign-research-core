import inspect
import pathlib
import subprocess
import tempfile
import unittest

from .measurement import (INVESTMENT_RETURN, RESEARCH_TOOL, Task, default_tasks,
                          domain_delta,
                          investment_claim_support, load_tasks, measure_candidate,
                          measurement_tampering_suspected,
                          verify_measurement_suite)
from .sandbox import SandboxError
from .self_improvement import MutationProposal, evaluate_mutation


IMPROVE = ("diff --git a/runtime/metric.py b/runtime/metric.py\n--- a/runtime/metric.py\n"
           "+++ b/runtime/metric.py\n@@ -1 +1 @@\n-print(10.0)\n+print(14.0)\n")
REGRESS = ("diff --git a/runtime/metric.py b/runtime/metric.py\n--- a/runtime/metric.py\n"
           "+++ b/runtime/metric.py\n@@ -1 +1 @@\n-print(10.0)\n+print(6.0)\n")
BREAKS = ("diff --git a/runtime/metric.py b/runtime/metric.py\n--- a/runtime/metric.py\n"
          "+++ b/runtime/metric.py\n@@ -1 +1 @@\n-print(10.0)\n+raise SystemExit(3)\n")


def proposal(patch, **over):
    signature = inspect.signature(MutationProposal)
    kw = dict(mutation_id="m1", parent_version="v1", targets=("runtime/metric.py",),
              patch=patch, failure_ids=("f1",), expected_effect="e",
              counter_metrics=("cost",), rollback_condition="r", sample_requirement=30)
    kw = {k: v for k, v in kw.items() if k in signature.parameters}
    for name in signature.parameters:
        if name not in kw:
            kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
    kw.update(over)
    return MutationProposal(**kw)


def task(task_id, domain, *, protected=False, direction="higher_is_better"):
    return Task(task_id, domain, direction, protected, ("python3", "runtime/metric.py"))


class MeasurementTestCase(unittest.TestCase):

    def scratch_repo(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="sovereign-measure-test-"))
        (root / "runtime").mkdir()
        (root / "runtime" / "metric.py").write_text("print(10.0)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "init", "-q"], capture_output=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                        "user.email=t@t", "commit", "-q", "-m", "init"], capture_output=True)
        return root

    def suite(self, patch, tasks):
        return measure_candidate(proposal(patch), tasks, repo_root=self.scratch_repo())


class MeasurementsComeFromRealRunsTests(MeasurementTestCase):
    """primary_delta used to be a number the caller supplied.

    Nothing produced it, so the evidence gate was only ever as honest as
    whoever filled in the form.
    """

    def test_baseline_and_candidate_are_both_executed(self):
        suite = self.suite(IMPROVE, [task("t1", RESEARCH_TOOL)])
        measurement = suite.measurements[0]
        self.assertEqual(measurement.baseline_value, 10.0)
        self.assertEqual(measurement.candidate_value, 14.0)

    def test_improvement_respects_higher_is_better(self):
        suite = self.suite(IMPROVE, [task("t1", RESEARCH_TOOL)])
        self.assertEqual(suite.measurements[0].improvement, 4.0)

    def test_improvement_respects_lower_is_better(self):
        """The same numbers mean the opposite for a cost metric."""
        suite = self.suite(IMPROVE, [task("t1", RESEARCH_TOOL, direction="lower_is_better")])
        self.assertEqual(suite.measurements[0].improvement, -4.0)

    def test_a_regression_is_reported_as_negative(self):
        suite = self.suite(REGRESS, [task("t1", RESEARCH_TOOL)])
        self.assertEqual(suite.measurements[0].improvement, -4.0)

    def test_a_task_that_fails_is_unusable_not_zero(self):
        """Scoring a crashed task as zero is how a broken candidate looks neutral."""
        suite = self.suite(BREAKS, [task("t1", RESEARCH_TOOL)])
        measurement = suite.measurements[0]
        self.assertFalse(measurement.usable)
        self.assertIsNone(measurement.improvement)
        self.assertIn("task_failed", measurement.reason)

    def test_an_empty_task_list_is_refused(self):
        with self.assertRaises(SandboxError):
            measure_candidate(proposal(IMPROVE), [], repo_root=self.scratch_repo())


class DomainsDoNotMixTests(MeasurementTestCase):
    """A research tool getting faster is not evidence that returns improved.

    They were interchangeable as long as every measurement fed one
    undifferentiated primary_delta, which let a tooling win be spent as an
    economic one.
    """

    def mixed_suite(self):
        return self.suite(IMPROVE, [task("tool", RESEARCH_TOOL),
                                    task("money", INVESTMENT_RETURN, protected=True)])

    def test_each_domain_is_summarised_separately(self):
        suite = self.mixed_suite()
        self.assertEqual(domain_delta(suite, RESEARCH_TOOL)["n"], 1)
        self.assertEqual(domain_delta(suite, INVESTMENT_RETURN)["n"], 1)

    def test_an_unknown_domain_is_refused_rather_than_scored_zero(self):
        """A typo'd domain silently scoring zero is indistinguishable from a
        real absence of effect."""
        with self.assertRaises(ValueError):
            domain_delta(self.mixed_suite(), "reserch_tool")

    def test_a_tooling_only_suite_cannot_support_an_investment_claim(self):
        suite = self.suite(IMPROVE, [task("tool", RESEARCH_TOOL, protected=True)])
        support = investment_claim_support(suite)
        self.assertFalse(support["supported"])
        self.assertEqual(support["reason"], "no_investment_return_tasks")

    def test_unprotected_investment_tasks_cannot_support_the_claim(self):
        """Tuning against a case and then citing it is the oldest way to
        manufacture an out-of-sample result."""
        suite = self.suite(IMPROVE, [task("money", INVESTMENT_RETURN, protected=False)])
        support = investment_claim_support(suite)
        self.assertFalse(support["supported"])
        self.assertEqual(support["reason"], "no_protected_investment_measurements")

    def test_a_protected_investment_task_supports_the_claim(self):
        suite = self.suite(IMPROVE, [task("money", INVESTMENT_RETURN, protected=True)])
        self.assertTrue(investment_claim_support(suite)["supported"])

    def test_protected_only_excludes_development_cases(self):
        suite = self.suite(IMPROVE, [task("dev", RESEARCH_TOOL, protected=False),
                                     task("holdout", RESEARCH_TOOL, protected=True)])
        self.assertEqual(domain_delta(suite, RESEARCH_TOOL)["n"], 2)
        self.assertEqual(domain_delta(suite, RESEARCH_TOOL, protected_only=True)["n"], 1)


class SuitesAreBoundToTheirProposalTests(MeasurementTestCase):

    def test_a_matching_suite_verifies(self):
        suite = self.suite(IMPROVE, [task("t1", RESEARCH_TOOL)])
        self.assertEqual(verify_measurement_suite(suite, proposal(IMPROVE)), [])

    def test_a_suite_cannot_be_spent_on_another_proposal(self):
        suite = self.suite(IMPROVE, [task("t1", RESEARCH_TOOL)])
        errors = verify_measurement_suite(suite, proposal(IMPROVE, targets=("runtime/x.py",)))
        self.assertTrue(any(e.startswith("measurement_suite_for_other_proposal")
                            for e in errors), errors)

    def test_a_suite_with_no_usable_measurements_is_refused(self):
        suite = self.suite(BREAKS, [task("t1", RESEARCH_TOOL)])
        self.assertIn("measurement_suite_has_no_usable_measurements",
                      verify_measurement_suite(suite, proposal(BREAKS)))

    def test_a_missing_suite_is_refused(self):
        self.assertEqual(verify_measurement_suite(None, proposal(IMPROVE)),
                         ["measurement_suite_missing"])


class TaskDefinitionsAreValidatedTests(unittest.TestCase):

    def write(self, rows):
        path = pathlib.Path(tempfile.mkdtemp()) / "tasks.json"
        import json
        path.write_text(json.dumps({"tasks": rows}), encoding="utf-8")
        return path

    def row(self, **over):
        base = {"task_id": "t1", "domain": RESEARCH_TOOL,
                "direction": "higher_is_better", "protected": False,
                "command": ["python3", "runtime/metric.py"]}
        base.update(over)
        return base

    def test_a_valid_definition_loads(self):
        self.assertEqual(len(load_tasks(self.write([self.row()]))), 1)

    def test_an_invalid_domain_is_refused(self):
        with self.assertRaises(ValueError):
            load_tasks(self.write([self.row(domain="vibes")]))

    def test_an_invalid_direction_is_refused(self):
        with self.assertRaises(ValueError):
            load_tasks(self.write([self.row(direction="bigger")]))

    def test_a_duplicate_task_id_is_refused(self):
        """Two tasks sharing an id makes the suite's own count a lie."""
        with self.assertRaises(ValueError):
            load_tasks(self.write([self.row(), self.row()]))

    def test_a_task_without_a_command_is_refused(self):
        with self.assertRaises(ValueError):
            load_tasks(self.write([self.row(command=[])]))


class TheGateConsumesMeasurementsTests(MeasurementTestCase):
    """Wiring, not decoration: a supplied suite replaces primary_delta."""

    EVIDENCE = dict(sample_size=40, counter_metric_deltas={"cost": -0.1},
                    counter_metric_directions={"cost": "lower_is_better"},
                    out_of_sample=True, sandbox_passed=True, rollback_triggered=False)

    def sandbox_report(self, candidate):
        from .sandbox import SandboxReport
        from .self_improvement import proposal_digest
        return SandboxReport(proposal_digest(candidate), True, 0,
                             ("python3",), "a" * 64, "", 0.1,
                             "2026-09-16T00:00:00+00:00", "sandbox")

    def test_a_measured_improvement_reaches_the_verdict(self):
        candidate = proposal(IMPROVE)
        # A protected task is present because out_of_sample is now read off
        # the suite: claiming it while measuring only development tasks is
        # the thing the derivation exists to refuse.
        suite = self.suite(IMPROVE, [task("t1", RESEARCH_TOOL),
                                     task("t2", RESEARCH_TOOL, protected=True)])
        verdict = evaluate_mutation(candidate, primary_delta=None,
                                    sandbox_report=self.sandbox_report(candidate),
                                    measurement_suite=suite, **self.EVIDENCE)
        self.assertEqual(verdict.primary_delta, 4.0)
        self.assertEqual(verdict.status, "eligible")

    def test_a_measured_regression_is_not_eligible(self):
        candidate = proposal(REGRESS)
        suite = self.suite(REGRESS, [task("t1", RESEARCH_TOOL)])
        verdict = evaluate_mutation(candidate, primary_delta=0.9,
                                    sandbox_report=self.sandbox_report(candidate),
                                    measurement_suite=suite, **self.EVIDENCE)
        self.assertNotEqual(verdict.status, "eligible")

    def test_an_investment_claim_without_investment_tasks_is_refused(self):
        candidate = proposal(IMPROVE)
        suite = self.suite(IMPROVE, [task("t1", RESEARCH_TOOL)])
        verdict = evaluate_mutation(candidate, primary_delta=None,
                                    sandbox_report=self.sandbox_report(candidate),
                                    measurement_suite=suite,
                                    claim_domain=INVESTMENT_RETURN, **self.EVIDENCE)
        self.assertEqual(verdict.status, "testing")
        self.assertIn("investment_claim_unsupported", verdict.reason)


class TheCommittedSuiteIsFixedTests(unittest.TestCase):
    """measurement.py could run tasks and nothing shipped any.

    Every caller had to invent its own, and tasks invented per run are
    neither fixed nor protected: whoever picks them can pick ones that
    flatter the candidate.
    """

    def test_the_committed_suite_loads(self):
        self.assertGreaterEqual(len(default_tasks()), 4)

    def test_it_contains_a_protected_holdout(self):
        self.assertTrue(any(t.protected for t in default_tasks()))

    def test_it_contains_development_cases_too(self):
        """A suite that is entirely holdout leaves nothing to iterate on."""
        self.assertTrue(any(not t.protected for t in default_tasks()))

    def test_protected_and_development_tasks_do_not_overlap(self):
        """A holdout measuring what you already optimised is not a holdout."""
        tasks = default_tasks()
        protected = {t.task_id for t in tasks if t.protected}
        development = {t.task_id for t in tasks if not t.protected}
        self.assertEqual(protected & development, set())

    def test_no_investment_task_exists_yet(self):
        """There are no attributable outcomes.

        Shipping an investment_return task before they exist would let a
        tooling improvement be spent as an economic claim, which is what the
        domain split exists to prevent.
        """
        self.assertEqual([t for t in default_tasks() if t.domain == INVESTMENT_RETURN], [])

    def test_every_task_counts_something_that_should_fall(self):
        """No task rewards volume: a candidate can always add more tests."""
        self.assertTrue(all(t.direction == "lower_is_better" for t in default_tasks()))

    def test_every_task_emits_exactly_one_number(self):
        import subprocess
        for task in default_tasks():
            result = subprocess.run(list(task.command), capture_output=True, text=True,
                                    cwd=str(pathlib.Path(__file__).resolve().parent.parent))
            self.assertEqual(result.returncode, 0, task.task_id)
            lines = [x for x in result.stdout.strip().splitlines() if x.strip()]
            self.assertEqual(len(lines), 1, f"{task.task_id}: {lines}")
            float(lines[0])

    def test_measure_candidate_uses_the_committed_suite_by_default(self):
        """Wiring, not availability: the default must be the fixed suite.

        Runs against THIS repository, so the patch has to be one that really
        applies here rather than to a scratch fixture.
        """
        header = '"""Which strategy families the host has actually used, and which it has not.'
        patch = ("diff --git a/runtime/strategy_coverage.py b/runtime/strategy_coverage.py\n"
                 "--- a/runtime/strategy_coverage.py\n+++ b/runtime/strategy_coverage.py\n"
                 "@@ -1 +1,2 @@\n+# candidate marker\n " + header + "\n")
        candidate = proposal(patch, targets=("runtime/strategy_coverage.py",))
        suite = measure_candidate(candidate, repo_root=self.repo())
        self.assertEqual({m.task_id for m in suite.measurements},
                         {t.task_id for t in default_tasks()})

    def test_the_default_suite_produces_usable_measurements_here(self):
        """A corpus that cannot run against its own repository is decoration."""
        header = '"""Which strategy families the host has actually used, and which it has not.'
        patch = ("diff --git a/runtime/strategy_coverage.py b/runtime/strategy_coverage.py\n"
                 "--- a/runtime/strategy_coverage.py\n+++ b/runtime/strategy_coverage.py\n"
                 "@@ -1 +1,2 @@\n+# candidate marker\n " + header + "\n")
        candidate = proposal(patch, targets=("runtime/strategy_coverage.py",))
        suite = measure_candidate(candidate, repo_root=self.repo())
        self.assertTrue(all(m.usable for m in suite.measurements),
                        [(m.task_id, m.reason) for m in suite.measurements])

    def repo(self):
        return pathlib.Path(__file__).resolve().parent.parent


class HoldoutCatchesSelfMeasurementTests(MeasurementTestCase):
    """A self-improving system measures itself with its own detectors.

    So a candidate can improve a metric by WEAKENING the code that reports
    it rather than by fixing anything. Deleting one integrity check scored
    +3.0 on open findings while the sixteen stranded records it stopped
    reporting were still stranded. The sandbox passes: weakened code still
    compiles, so execution alone never sees it.
    """

    def suite_with(self, development_improvement, protected_improvement):
        """A suite shaped like the outcome, without needing a real patch."""
        from .measurement import Measurement, MeasurementSuite
        return MeasurementSuite(
            proposal_digest="d", at="2026-09-16T00:00:00+00:00",
            measurements=(
                Measurement("dev", RESEARCH_TOOL, "higher_is_better", False,
                            0.0, development_improvement, 0, 0, True, "measured"),
                Measurement("holdout", RESEARCH_TOOL, "higher_is_better", True,
                            0.0, protected_improvement, 0, 0, True, "measured"),
            ),
        )

    def test_improvement_no_holdout_can_see_is_flagged(self):
        result = measurement_tampering_suspected(self.suite_with(3.0, 0.0))
        self.assertTrue(result["suspected"])
        self.assertEqual(result["development"], ["dev"])

    def test_an_improvement_the_holdout_shares_is_not_flagged(self):
        self.assertFalse(measurement_tampering_suspected(self.suite_with(3.0, 1.0))["suspected"])

    def test_a_regression_is_not_flagged_as_tampering(self):
        """Nothing is being claimed, so there is nothing to disbelieve."""
        self.assertFalse(measurement_tampering_suspected(self.suite_with(-3.0, 0.0))["suspected"])

    def test_no_holdout_means_no_comparison_rather_than_a_pass(self):
        from .measurement import Measurement, MeasurementSuite
        suite = MeasurementSuite(
            proposal_digest="d", at="2026-09-16T00:00:00+00:00",
            measurements=(Measurement("dev", RESEARCH_TOOL, "higher_is_better", False,
                                      0.0, 3.0, 0, 0, True, "measured"),))
        result = measurement_tampering_suspected(suite)
        self.assertFalse(result["suspected"])
        self.assertEqual(result["reason"], "no_protected_measurements_to_compare")

    def test_the_verdict_withholds_eligibility(self):
        """It cannot prove tampering. It refuses to treat an unseen
        improvement as evidence, which is the honest half."""
        from .sandbox import SandboxReport
        from .self_improvement import evaluate_mutation, proposal_digest
        candidate = proposal(IMPROVE)
        suite = self.suite_with(3.0, 0.0)
        suite = type(suite)(proposal_digest=proposal_digest(candidate),
                            measurements=suite.measurements, at=suite.at)
        report = SandboxReport(proposal_digest(candidate), True, 0, ("python3",),
                               "a" * 64, "", 0.1, "2026-09-16T00:00:00+00:00", "sandbox")
        verdict = evaluate_mutation(
            candidate, sandbox_report=report, measurement_suite=suite, sample_size=40,
            counter_metric_deltas={"cost": -0.1},
            counter_metric_directions={"cost": "lower_is_better"},
            out_of_sample=True, sandbox_passed=True, rollback_triggered=False)
        self.assertEqual(verdict.status, "testing")
        self.assertIn("improvement_invisible_to_holdout", verdict.reason)
