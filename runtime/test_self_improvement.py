import unittest
from copy import deepcopy

from .self_improvement import (
    MutationEvaluation,
    patch_touched_paths,
    MutationProposal,
    evaluate_mutation,
    failure_fingerprint,
    promote_mutation, proposal_digest,
    rollback_mutation,
    validate_mutation,
)


def deployment_for(proposal, *, parent="a" * 40, candidate="b" * 40,  # noqa: D401
                   verified=True, rolled_back=False):
    """A deployment report bound to `proposal`, as deploy_candidate produces.

    Constructed directly so unit tests of the promotion gate stay fast. The
    real path -- deploy_candidate against a scratch git repository feeding
    promote_mutation -- is exercised in test_deployment.py, so this shortcut
    cannot hide a broken seam.
    """
    from .deployment import DeploymentReport
    return DeploymentReport(
        proposal_digest=proposal_digest(proposal), parent_sha=parent,
        candidate_sha=candidate, applied=True, verified=verified,
        verify_exit_code=0 if verified else 1,
        command=("python3", "-m", "compileall", "-q", "runtime"),
        rolled_back=rolled_back, restored_sha=parent if rolled_back else None,
        head_after=parent if rolled_back else candidate,
        reason="verified" if verified else "verification_failed",
        at="2026-09-16T00:00:00+00:00",
    )


def passing_measurements(proposal, *, improvement=0.5, protected_improvement=0.2):
    """A measurement suite bound to `proposal`, as measure_candidate produces.

    Built directly so unit tests of the verdict stay fast. The real path --
    measure_candidate running fixed tasks against two exported trees -- is
    exercised in test_measurement.py, so this cannot hide a broken seam.

    Carries a moving holdout on purpose: a development-only improvement is
    refused as invisible-to-holdout, which is a different test.
    """
    from .measurement import RESEARCH_TOOL, Measurement, MeasurementSuite
    return MeasurementSuite(
        proposal_digest=proposal_digest(proposal),
        at="2026-09-16T00:00:00+00:00",
        measurements=(
            Measurement("dev", RESEARCH_TOOL, "higher_is_better", False,
                        0.0, improvement, 0, 0, True, "measured"),
            Measurement("holdout", RESEARCH_TOOL, "higher_is_better", True,
                        0.0, protected_improvement, 0, 0, True, "measured"),
        ),
    )


def passing_report(proposal, *, exit_code=0):
    """A sandbox report consistent with `proposal`, as run_candidate produces.

    Constructed directly so unit tests of the verdict logic stay fast. The
    end-to-end path -- run_candidate feeding evaluate_mutation feeding
    promote_mutation -- is exercised in test_sandbox.py against a real
    checkout, so this shortcut cannot hide a broken seam.
    """
    from .sandbox import SandboxReport
    return SandboxReport(
        proposal_digest=proposal_digest(proposal),
        passed=exit_code == 0,
        exit_code=exit_code,
        command=("python3", "-m", "compileall", "-q", "runtime"),
        tree_digest="a" * 64,
        stdout_tail="",
        duration_s=0.1,
        started_at="2026-09-16T00:00:00+00:00",
        stage="sandbox",
    )

class SelfImprovementTests(unittest.TestCase):
    def proposal(self, **overrides):
        value = dict(
            mutation_id="mut-1",
            parent_version="engine-v1",
            mutation_type="runtime_patch",
            targets=("runtime/example.py",),
            rationale="fix recurring failure",
            failure_ids=("failure-1",),
            patch="return improved_result()",
            expected_effect="reduce blocked decisions",
            counter_metrics=("integrity_errors", "false_positive_rate"),
            sample_requirement=30,
            evaluation_window="2026-Q4",
            rollback_condition="integrity_errors > 0",
            created_at="2026-09-16T00:00:00+00:00",
        )
        value.update(overrides)
        return MutationProposal(**value)

    def test_failure_fingerprint_is_stable(self):
        a = failure_fingerprint(stage="research", failure_class="stale_data", symptom="old", evidence=("e1",))
        b = failure_fingerprint(stage="research", failure_class="stale_data", symptom="old", evidence=("e1",))
        self.assertEqual(a, b)

    def test_immutable_boundary_is_enforced(self):
        errors = validate_mutation(self.proposal(targets=("SYSTEM.md",)),
                                   allowed_prefixes=("",))
        self.assertIn("immutable_target:SYSTEM.md", errors)

    def test_audit_runtime_is_immutable(self):
        errors = validate_mutation(self.proposal(targets=("runtime/audit_store.py",)),
                                   allowed_prefixes=("",))
        self.assertIn("immutable_target:runtime/audit_store.py", errors)

    def test_broker_execution_mutation_is_rejected(self):
        errors = validate_mutation(self.proposal(patch="submit_order(order)"),
                                   allowed_prefixes=("",))
        self.assertIn("forbidden_mutation_token:submit_order", errors)

    def test_small_sample_stays_testing(self):
        result = evaluate_mutation(
            self.proposal(), sample_size=29, primary_delta=1.0,
            counter_metric_deltas={"integrity_errors": 0.0, "false_positive_rate": 0.0},
            counter_metric_directions={"integrity_errors": "lower_is_better", "false_positive_rate": "lower_is_better"},
            out_of_sample=True, sandbox_passed=True, rollback_triggered=False,
            sandbox_report=passing_report(self.proposal()),
            measurement_suite=passing_measurements(self.proposal()),
        )
        self.assertEqual(result.status, "testing")

    def test_oos_improvement_becomes_eligible(self):
        result = evaluate_mutation(
            self.proposal(), sample_size=40, primary_delta=0.1,
            counter_metric_deltas={"integrity_errors": 0.0, "false_positive_rate": 0.0},
            counter_metric_directions={"integrity_errors": "lower_is_better", "false_positive_rate": "lower_is_better"},
            out_of_sample=True, sandbox_passed=True, rollback_triggered=False,
            sandbox_report=passing_report(self.proposal()),
            measurement_suite=passing_measurements(self.proposal()),
        )
        self.assertEqual(result.status, "eligible")

    def test_lower_is_better_counter_metric(self):
        result = evaluate_mutation(
            self.proposal(), sample_size=40, primary_delta=0.1,
            counter_metric_deltas={"false_positive_rate": -0.05, "integrity_errors": 0.0},
            counter_metric_directions={"false_positive_rate": "lower_is_better", "integrity_errors": "lower_is_better"},
            out_of_sample=True, sandbox_passed=True, rollback_triggered=False,
            sandbox_report=passing_report(self.proposal()),
            measurement_suite=passing_measurements(self.proposal()),
        )
        self.assertEqual(result.status, "eligible")

    def test_counter_metric_regression_blocks_promotion(self):
        result = evaluate_mutation(
            self.proposal(), sample_size=40, primary_delta=0.1,
            counter_metric_deltas={"false_positive_rate": 0.05, "integrity_errors": 0.0},
            counter_metric_directions={"false_positive_rate": "lower_is_better", "integrity_errors": "lower_is_better"},
            out_of_sample=True, sandbox_passed=True, rollback_triggered=False,
            sandbox_report=passing_report(self.proposal()),
            measurement_suite=passing_measurements(self.proposal()),
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("counter_metric_regressed:false_positive_rate", result.reason)

    def test_missing_direction_blocks_promotion(self):
        result = evaluate_mutation(
            self.proposal(), sample_size=40, primary_delta=0.1,
            counter_metric_deltas={"integrity_errors": 0.0, "false_positive_rate": 0.0},
            counter_metric_directions={}, out_of_sample=True,
            sandbox_passed=True, rollback_triggered=False,
        )
        self.assertEqual(result.status, "rejected")

    def test_promotion_is_versioned_and_retires_previous(self):
        current = {
            "self_improvement": {
                "version": 1,
                "active_mutation_id": "old",
                "mutations": [{"mutation_id": "old", "state": "active"}],
            }
        }
        evaluation = MutationEvaluation(
            "mut-1", "eligible", 40, 0.1, {}, {}, True, True, False, "evidence_gate_passed",
            proposal_digest(self.proposal()),
        )
        result = promote_mutation(
            current, self.proposal(), evaluation,
            deployment_report=deployment_for(self.proposal()),
        )
        self.assertEqual(result["self_improvement"]["version"], 2)
        self.assertEqual(result["self_improvement"]["active_mutation_id"], "mut-1")
        self.assertEqual(result["self_improvement"]["mutations"][0]["state"], "retired")
        self.assertEqual(result["self_improvement"]["mutations"][1]["candidate_sha"], "b" * 40)

    def test_rollback_preserves_reason_and_restore_version(self):
        state = {"self_improvement": {"version": 2, "active_mutation_id": "mut-1",
                                     "mutations": [{"mutation_id": "mut-1", "state": "active"}]}}
        result = rollback_mutation(state, reason="production integrity regression", restore_version="engine-v1")
        self.assertIsNone(result["self_improvement"]["active_mutation_id"])
        self.assertEqual(result["self_improvement"]["mutations"][0]["state"], "rolled_back")
        self.assertEqual(result["self_improvement"]["restore_version"], "engine-v1")


if __name__ == "__main__":
    unittest.main()


class MutationScopeEscapeTests(unittest.TestCase):
    """The mutation allowlist is the guard on autonomous self-modification.

    Scope checks used to be raw string comparisons, so "runtime/../SYSTEM.md"
    cleared the allowlist (starts with "runtime/") AND cleared the immutable
    check (does not equal "SYSTEM.md") while resolving to the constitution.
    Separately, the patch body was only scanned for forbidden tokens, so a
    proposal could declare runtime/ok.py and ship a diff editing SYSTEM.md.

    An autonomous repair loop runs against this repository. These are the
    checks that keep it inside its box.
    """

    ALLOW = ("runtime",)

    def proposal(self, targets=("runtime/ok.py",), patch="return improved_result()"):
        import inspect
        sig = inspect.signature(MutationProposal)
        kw = dict(mutation_id="m1", parent_version="v1", targets=tuple(targets),
                  patch=patch, failure_ids=("f1",), expected_effect="better",
                  counter_metrics=("cost",), rollback_condition="worse",
                  sample_requirement=30)
        kw = {k: v for k, v in kw.items() if k in sig.parameters}
        for name in sig.parameters:
            if name not in kw:
                kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
        return MutationProposal(**kw)

    def scope_errors(self, **kw):
        keys = ("immutable", "allowlist", "absolute", "escapes", "backslash", "patch_touches")
        return [e for e in validate_mutation(self.proposal(**kw), allowed_prefixes=self.ALLOW)
                if any(k in e for k in keys)]

    def test_a_legitimate_in_scope_target_is_allowed(self):
        # Guards against fixing the escape by blocking everything.
        self.assertEqual(self.scope_errors(targets=("runtime/ok.py",)), [])

    def test_dotdot_traversal_to_the_constitution_is_blocked(self):
        self.assertIn("immutable_target:SYSTEM.md",
                      self.scope_errors(targets=("runtime/../SYSTEM.md",)))

    def test_dot_and_dotdot_traversal_is_blocked(self):
        self.assertIn("immutable_target:SYSTEM.md",
                      self.scope_errors(targets=("runtime/./../SYSTEM.md",)))

    def test_multi_level_traversal_to_the_host_contract_is_blocked(self):
        self.assertIn("immutable_target:LLM_HOST_CONTRACT.md",
                      self.scope_errors(targets=("runtime/a/../../LLM_HOST_CONTRACT.md",)))

    def test_absolute_paths_are_refused(self):
        self.assertTrue(any("absolute_target" in e
                            for e in self.scope_errors(targets=("/etc/passwd",))))

    def test_windows_drive_letter_paths_are_refused(self):
        # posixpath.normpath("C:/Windows/x") leaves it unchanged and it does
        # NOT start with "/", so the post-normalization absolute check misses
        # it. Only the drive-letter test catches this one. Found by a
        # surviving mutation: removing that check broke nothing.
        self.assertTrue(any("absolute_target" in e
                            for e in self.scope_errors(targets=("C:/Windows/system.md",))))

    def test_escaping_the_repo_root_is_refused(self):
        self.assertTrue(any("target_escapes_repo" in e
                            for e in self.scope_errors(targets=("../escape.py",))))

    def test_backslash_separators_are_refused(self):
        # Would survive posixpath.normpath untouched and could re-enter as a
        # separator elsewhere.
        self.assertTrue(any("backslash_in_target" in e
                            for e in self.scope_errors(targets=("runtime\\..\\SYSTEM.md",))))

    def test_allowlist_entries_with_a_trailing_slash_still_match(self):
        # "runtime/" previously matched nothing because the check appended
        # another slash, silently rejecting every legitimate target.
        errors = [e for e in validate_mutation(self.proposal(), allowed_prefixes=("runtime/",))
                  if "allowlist" in e]
        self.assertEqual(errors, [])

    def test_a_patch_touching_an_undeclared_file_is_refused(self):
        errors = self.scope_errors(targets=("runtime/ok.py",),
                                   patch="--- a/SYSTEM.md\n+++ b/SYSTEM.md\n+backdoor\n")
        self.assertIn("patch_touches_undeclared_target:SYSTEM.md", errors)

    def test_a_patch_touching_an_immutable_file_is_refused_twice_over(self):
        errors = self.scope_errors(targets=("runtime/ok.py",),
                                   patch="--- a/SYSTEM.md\n+++ b/SYSTEM.md\n+backdoor\n")
        self.assertIn("immutable_target:SYSTEM.md", errors)

    def test_a_patch_matching_its_declared_target_is_allowed(self):
        self.assertEqual(
            self.scope_errors(targets=("runtime/ok.py",),
                              patch="--- a/runtime/ok.py\n+++ b/runtime/ok.py\n+x = 1\n"),
            [])

    def test_dev_null_headers_are_ignored(self):
        # New and deleted files legitimately carry /dev/null on one side.
        self.assertEqual(
            self.scope_errors(targets=("runtime/new.py",),
                              patch="--- /dev/null\n+++ b/runtime/new.py\n+x = 1\n"),
            [])

    def test_patch_traversal_is_normalized_before_comparison(self):
        errors = self.scope_errors(
            targets=("runtime/ok.py",),
            patch="--- a/runtime/../SYSTEM.md\n+++ b/runtime/../SYSTEM.md\n+x\n")
        self.assertIn("immutable_target:SYSTEM.md", errors)

    def test_an_opaque_patch_cannot_be_cross_checked_and_is_not_claimed_to_be(self):
        # Free-form patch text carries no diff headers. patch_touched_paths
        # returns empty rather than inventing a false clean bill of health;
        # the applier must enforce scope at apply time.
        self.assertEqual(patch_touched_paths("return improved_result()"), set())


class PromotionBindingTests(unittest.TestCase):
    """Approval must bind to the exact proposal, evaluation and commits.

    An autonomous repair loop promotes its own work. If approval is not
    bound to a specific artifact, "this was evaluated and passed" and
    "this is what got promoted" are two unrelated claims.
    """

    PARENT = "a" * 40
    CANDIDATE = "b" * 40

    def proposal(self, mutation_id="mut-1"):
        import inspect
        sig = inspect.signature(MutationProposal)
        kw = dict(mutation_id=mutation_id, parent_version="v1",
                  targets=("runtime/ok.py",), patch="x", failure_ids=("f1",),
                  expected_effect="better", counter_metrics=("cost",),
                  rollback_condition="worse", sample_requirement=30)
        kw = {k: v for k, v in kw.items() if k in sig.parameters}
        for name in sig.parameters:
            if name not in kw:
                kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
        return MutationProposal(**kw)

    def evaluation(self, mutation_id="mut-1", status="eligible"):
        return MutationEvaluation(mutation_id, status, 40, 0.1, {}, {}, True, True, False, "ok",
                                  proposal_digest(self.proposal(mutation_id)))

    def promote(self, proposal_id="mut-1", evaluation_id="mut-1",
                parent=PARENT, candidate=CANDIDATE, state=None):
        target = self.proposal(proposal_id)
        return promote_mutation(state if state is not None else {},
                                target, self.evaluation(evaluation_id),
                                deployment_report=deployment_for(
                                    target, parent=parent, candidate=candidate))

    def test_a_correctly_bound_promotion_succeeds(self):
        # Guards against fixing the binding by refusing everything.
        result = self.promote()
        self.assertEqual(result["self_improvement"]["active_mutation_id"], "mut-1")

    def test_another_mutations_evaluation_cannot_promote_this_one(self):
        # MutationEvaluation has carried a mutation_id all along; nothing
        # compared it to the proposal being promoted.
        with self.assertRaises(ValueError) as ctx:
            self.promote(proposal_id="mut-1", evaluation_id="mut-2")
        self.assertIn("evaluation_mutation_mismatch", str(ctx.exception))

    def test_promotion_without_commit_identity_is_refused(self):
        # A record with no SHAs identifies nothing executable, and leaves a
        # later rollback with no version to restore. The SHAs now arrive on
        # the deployment report, so that is where the check lives.
        for parent, candidate in ((None, self.CANDIDATE), (self.PARENT, None), (None, None)):
            with self.subTest(parent=parent, candidate=candidate):
                with self.assertRaises(ValueError) as ctx:
                    self.promote(parent=parent, candidate=candidate)
                self.assertIn("deployment_report_missing_", str(ctx.exception))

    def test_blank_sha_is_refused(self):
        with self.assertRaises(ValueError):
            self.promote(parent="   ")

    def test_malformed_sha_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.promote(parent="prod-sha")
        self.assertIn("deployment_report_missing_parent_sha", str(ctx.exception))

    def test_candidate_equal_to_parent_is_refused(self):
        # Promoting the parent onto itself is a no-op dressed as a change.
        with self.assertRaises(ValueError) as ctx:
            self.promote(parent=self.PARENT, candidate=self.PARENT)
        self.assertIn("deployment_candidate_equals_parent", str(ctx.exception))

    def test_ineligible_evaluation_is_still_refused(self):
        with self.assertRaises(ValueError) as ctx:
            promote_mutation({}, self.proposal(), self.evaluation(status="blocked"),
                             deployment_report=deployment_for(self.proposal()))
        self.assertIn("mutation_not_eligible", str(ctx.exception))

    def test_the_stored_evaluation_records_which_mutation_it_belongs_to(self):
        entry = self.promote()["self_improvement"]["mutations"][-1]
        self.assertEqual(entry["evaluation"]["mutation_id"], "mut-1")

    def test_promotion_does_not_mutate_the_caller_state(self):
        # list(...) copied the list and shared every dict inside it, so
        # retiring prior mutations wrote through into the caller's history.
        prior = {"self_improvement": {"version": 1, "active_mutation_id": "old",
                                      "mutations": [{"mutation_id": "old", "state": "active"}]}}
        before = deepcopy(prior)
        self.promote(state=prior)
        self.assertEqual(prior, before)

    def test_rollback_does_not_mutate_the_caller_state(self):
        prior = {"self_improvement": {"version": 2, "active_mutation_id": "mut-1",
                                      "mutations": [{"mutation_id": "mut-1", "state": "active"}]}}
        before = deepcopy(prior)
        rollback_mutation(prior, reason="regression", restore_version="v1")
        self.assertEqual(prior, before)

    def test_rollback_still_records_the_transition_in_its_result(self):
        prior = {"self_improvement": {"version": 2, "active_mutation_id": "mut-1",
                                      "mutations": [{"mutation_id": "mut-1", "state": "active"}]}}
        result = rollback_mutation(prior, reason="regression", restore_version="v1")
        entry = result["self_improvement"]["mutations"][0]
        self.assertEqual(entry["state"], "rolled_back")
        self.assertEqual(entry["rollback_reason"], "regression")
        self.assertIsNone(result["self_improvement"]["active_mutation_id"])


class MutationEvidenceValidityTests(unittest.TestCase):
    """Unmeasurable evidence must not read as `evidence_gate_passed`.

    Every NaN comparison is False under IEEE 754, so `primary_delta <= min`
    and the counter-metric regression checks both fell through and a mutation
    with no measurable effect reported eligible. The same defect was fixed in
    experiments.evaluate_variant; this is its twin on the mutation path, which
    is the path a self-modifying loop actually promotes through.

    A proposal also names the counter-metrics it agrees to be judged against.
    Supplying none of them skipped the regression loop entirely, making
    "measure nothing" the cheapest way to pass the counter-metric gate.
    """

    def proposal(self, **over):
        import inspect
        sig = inspect.signature(MutationProposal)
        kw = dict(mutation_id="m1", parent_version="v1", targets=("runtime/ok.py",),
                  patch="x", failure_ids=("f1",), expected_effect="better",
                  counter_metrics=("cost",), rollback_condition="worse",
                  sample_requirement=30)
        kw = {k: v for k, v in kw.items() if k in sig.parameters}
        for name in sig.parameters:
            if name not in kw:
                kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
        kw.update(over)
        return MutationProposal(**kw)

    def evaluate(self, **over):
        kw = dict(sample_size=40, primary_delta=0.5, counter_metric_deltas={"cost": -0.1},
                  counter_metric_directions={"cost": "lower_is_better"},
                  out_of_sample=True, sandbox_passed=True, rollback_triggered=False)
        kw.update(over)
        kw.setdefault("sandbox_report", passing_report(self.proposal()))
        kw.setdefault("measurement_suite", passing_measurements(self.proposal()))
        return evaluate_mutation(self.proposal(), **kw)

    def test_nan_primary_delta_is_rejected(self):
        # Delivered through the suite, because a measured delta is the only
        # way a primary_delta reaches the gate now. A caller-typed NaN is no
        # longer read at all, so injecting it there would prove nothing.
        result = self.evaluate(
            measurement_suite=passing_measurements(self.proposal(),
                                                   improvement=float("nan")))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "non_finite_measurement:primary_delta")

    def test_infinite_primary_delta_is_rejected(self):
        result = self.evaluate(
            measurement_suite=passing_measurements(self.proposal(),
                                                   improvement=float("inf")))
        self.assertEqual(result.status, "rejected")

    def test_nan_counter_metric_is_rejected(self):
        result = self.evaluate(counter_metric_deltas={"cost": float("nan")})
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "non_finite_measurement:counter_metric:cost")

    def test_declared_counter_metric_must_be_measured(self):
        result = self.evaluate(counter_metric_deltas={}, counter_metric_directions={})
        self.assertEqual(result.status, "testing")
        self.assertEqual(result.reason, "counter_metric_not_measured:cost")

    def test_partially_measured_counter_metrics_are_refused(self):
        proposal = self.proposal(counter_metrics=("cost", "latency"))
        result = evaluate_mutation(
            proposal, sample_size=40, primary_delta=0.5,
            counter_metric_deltas={"cost": -0.1},
            counter_metric_directions={"cost": "lower_is_better"},
            out_of_sample=True, sandbox_passed=True, rollback_triggered=False)
        self.assertEqual(result.reason, "counter_metric_not_measured:latency")

    def test_genuine_finite_improvement_is_still_eligible(self):
        result = self.evaluate()
        self.assertEqual(result.status, "eligible")
        self.assertEqual(result.reason, "evidence_gate_passed")


class RenameEscapesScopeTests(unittest.TestCase):
    """Moving a file is a write to both paths.

    A pure git rename carries no ---/+++ hunk headers, so the scan that
    cross-checks a patch against its declared targets returned an empty set
    and validate_mutation reported zero errors for a patch that moves a
    protected file out from under the immutable check.
    """

    def proposal(self, patch):
        import inspect
        sig = inspect.signature(MutationProposal)
        kw = dict(mutation_id="m1", parent_version="v1", targets=("runtime/ok.py",),
                  patch=patch, failure_ids=("f1",), expected_effect="e",
                  counter_metrics=("cost",), rollback_condition="r", sample_requirement=30)
        kw = {k: v for k, v in kw.items() if k in sig.parameters}
        for name in sig.parameters:
            if name not in kw:
                kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
        return MutationProposal(**kw)

    RENAME = ("diff --git a/SYSTEM.md b/runtime/ok.py\n"
              "similarity index 100%\n"
              "rename from SYSTEM.md\n"
              "rename to runtime/ok.py\n")
    COPY = ("diff --git a/SYSTEM.md b/runtime/ok.py\n"
            "copy from SYSTEM.md\n"
            "copy to runtime/ok.py\n")
    ORDINARY = ("diff --git a/runtime/ok.py b/runtime/ok.py\n"
                "--- a/runtime/ok.py\n+++ b/runtime/ok.py\n@@ -1 +1 @@\n-a\n+b\n")

    def test_rename_source_is_seen_as_touched(self):
        self.assertIn("SYSTEM.md", patch_touched_paths(self.RENAME))

    def test_rename_of_protected_file_is_refused(self):
        errors = validate_mutation(self.proposal(self.RENAME), allowed_prefixes=("runtime/",))
        self.assertIn("immutable_target:SYSTEM.md", errors)
        self.assertIn("patch_touches_undeclared_target:SYSTEM.md", errors)

    def test_copy_of_protected_file_is_refused(self):
        errors = validate_mutation(self.proposal(self.COPY), allowed_prefixes=("runtime/",))
        self.assertIn("immutable_target:SYSTEM.md", errors)

    def test_diff_git_header_alone_is_parsed(self):
        self.assertEqual(patch_touched_paths("diff --git a/runtime/x.py b/runtime/y.py\n"),
                         {"runtime/x.py", "runtime/y.py"})

    NO_PREFIX = ("diff --git SYSTEM.md runtime/ok.py\n"
                 "similarity index 100%\n"
                 "rename from SYSTEM.md\n"
                 "rename to runtime/ok.py\n")

    def test_no_prefix_rename_is_still_caught(self):
        """git diff --no-prefix defeats the a/ b/ header pattern.

        The rename lines are then the only place the paths appear, so
        dropping them leaves a real and easily reached bypass.
        """
        self.assertIn("SYSTEM.md", patch_touched_paths(self.NO_PREFIX))
        errors = validate_mutation(self.proposal(self.NO_PREFIX), allowed_prefixes=("runtime/",))
        self.assertIn("immutable_target:SYSTEM.md", errors)

    def test_custom_diff_prefixes_are_still_caught(self):
        patch = ("diff --git src/SYSTEM.md dst/runtime/ok.py\n"
                 "rename from SYSTEM.md\nrename to runtime/ok.py\n")
        self.assertIn("SYSTEM.md", patch_touched_paths(patch))

    def test_ordinary_in_scope_patch_still_passes(self):
        self.assertEqual(validate_mutation(self.proposal(self.ORDINARY),
                                           allowed_prefixes=("runtime/",)), [])


class ApprovalIsBoundToContentTests(unittest.TestCase):
    """An approval must attest to the code actually being shipped.

    mutation_id is a name. Binding approval to the name alone let a proposal
    be evaluated with one patch, keep its id, have its patch swapped, and then
    be promoted on the earlier evaluation.
    """

    PARENT = "a" * 40
    CANDIDATE = "b" * 40

    def proposal(self, **over):
        import inspect
        sig = inspect.signature(MutationProposal)
        kw = dict(mutation_id="m1", parent_version="v1", targets=("runtime/ok.py",),
                  patch="X", failure_ids=("f1",), expected_effect="e",
                  counter_metrics=("cost",), rollback_condition="r", sample_requirement=30)
        kw = {k: v for k, v in kw.items() if k in sig.parameters}
        for name in sig.parameters:
            if name not in kw:
                kw[name] = () if ("ids" in name or "metrics" in name or "targets" in name) else ""
        kw.update(over)
        return MutationProposal(**kw)

    def evaluated(self, proposal):
        return evaluate_mutation(proposal, sample_size=40, primary_delta=0.5,
                                 counter_metric_deltas={"cost": -0.1},
                                 counter_metric_directions={"cost": "lower_is_better"},
                                 out_of_sample=True, sandbox_passed=True,
                                 rollback_triggered=False,
                                 sandbox_report=passing_report(proposal),
                                 measurement_suite=passing_measurements(proposal))

    def test_promoting_the_evaluated_proposal_succeeds(self):
        proposal = self.proposal()
        result = promote_mutation({}, proposal, self.evaluated(proposal),
                             deployment_report=deployment_for(proposal))
        self.assertEqual(result["self_improvement"]["active_mutation_id"], "m1")

    def test_swapping_the_patch_invalidates_the_approval(self):
        approval = self.evaluated(self.proposal())
        with self.assertRaises(ValueError) as ctx:
            promote_mutation({}, self.proposal(patch="MALICIOUS"), approval,
                             deployment_report=deployment_for(self.proposal(patch="MALICIOUS")))
        self.assertIn("evaluation_content_mismatch", str(ctx.exception))

    def test_changing_the_declared_targets_invalidates_the_approval(self):
        approval = self.evaluated(self.proposal())
        with self.assertRaises(ValueError):
            promote_mutation({}, self.proposal(targets=("runtime/other.py",)), approval,
                             production_parent_sha=self.PARENT, candidate_sha=self.CANDIDATE)

    def test_hand_built_evaluation_cannot_authorize_a_promotion(self):
        forged = MutationEvaluation("m1", "eligible", 40, 0.5, {}, {}, True, True, False, "ok")
        with self.assertRaises(ValueError) as ctx:
            promote_mutation({}, self.proposal(), forged,
                             deployment_report=deployment_for(self.proposal()))
        self.assertIn("evaluation_content_mismatch", str(ctx.exception))

    def test_prose_edits_do_not_invalidate_a_completed_evaluation(self):
        approval = self.evaluated(self.proposal())
        result = promote_mutation({}, self.proposal(rationale="reworded"), approval,
                             deployment_report=deployment_for(self.proposal(rationale="reworded")))
        self.assertEqual(result["self_improvement"]["version"], 1)


class FingerprintGroupsRecurrencesTests(unittest.TestCase):
    """A fingerprint must survive the occurrence that produced it.

    Evidence record ids were hashed into the fingerprint. Evidence is
    occurrence-specific by definition, so the SAME recurring failure produced
    a new fingerprint every time it recurred, which defeats the one thing a
    fingerprint exists to do. Grouping is also how a fix gets proven: if
    every occurrence is a new identity, nothing can be shown to have stopped
    happening.
    """

    FAILURE = dict(stage="research", failure_class="stale_data",
                   symptom="quote older than 24h")

    def test_the_same_failure_groups_across_different_evidence(self):
        first = failure_fingerprint(**self.FAILURE, evidence=("rec-1",))
        second = failure_fingerprint(**self.FAILURE, evidence=("rec-2",))
        third = failure_fingerprint(**self.FAILURE, evidence=("rec-1", "rec-2"))
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_evidence_is_optional(self):
        self.assertEqual(failure_fingerprint(**self.FAILURE),
                         failure_fingerprint(**self.FAILURE, evidence=("rec-9",)))

    def test_a_different_symptom_is_a_different_failure(self):
        other = failure_fingerprint(stage="research", failure_class="stale_data",
                                    symptom="quote missing entirely")
        self.assertNotEqual(failure_fingerprint(**self.FAILURE), other)

    def test_a_different_stage_is_a_different_failure(self):
        other = failure_fingerprint(stage="decision", failure_class="stale_data",
                                    symptom="quote older than 24h")
        self.assertNotEqual(failure_fingerprint(**self.FAILURE), other)


class MeasurementEvidenceIsRequiredTests(unittest.TestCase):
    """primary_delta was a number the caller typed.

    verify_sandbox_report already refused to take sandbox_passed on trust,
    because a boolean saying "it ran" is not a run. The same hole was left one
    level up: with no measurement suite the improvement itself was asserted
    rather than produced, so a candidate that merely compiled could reach
    eligible on figures nothing generated.

    out_of_sample had the same shape. It is a fact about which tasks were
    measured, so it is read off the suite instead of being claimed beside it.
    """

    def proposal(self):
        import inspect
        sig = inspect.signature(MutationProposal)
        kw = dict(mutation_id="m1", parent_version="v1", targets=("runtime/ok.py",),
                  patch="x", failure_ids=("f1",), expected_effect="better",
                  counter_metrics=("cost",), rollback_condition="worse",
                  sample_requirement=30)
        kw = {k: v for k, v in kw.items() if k in sig.parameters}
        for name in sig.parameters:
            if name not in kw:
                kw[name] = () if ("ids" in name or "metrics" in name
                                  or "targets" in name) else ""
        return MutationProposal(**kw)

    def evaluate(self, **over):
        p = self.proposal()
        kw = dict(sample_size=40, primary_delta=0.5,
                  counter_metric_deltas={"cost": -0.1},
                  counter_metric_directions={"cost": "lower_is_better"},
                  out_of_sample=True, sandbox_passed=True,
                  rollback_triggered=False, sandbox_report=passing_report(p))
        kw.update(over)
        kw.setdefault("measurement_suite", passing_measurements(p))
        return evaluate_mutation(p, **kw)

    def test_an_unmeasured_candidate_cannot_be_eligible(self):
        verdict = self.evaluate(measurement_suite=None, primary_delta=0.9)
        self.assertEqual(verdict.status, "testing")
        self.assertIn("measurement_evidence_required", verdict.reason)

    def test_a_measured_candidate_is_eligible(self):
        self.assertEqual(self.evaluate().status, "eligible")

    def test_the_sandbox_is_still_reported_before_the_measurement(self):
        """"It never ran" is the more actionable of the two answers."""
        verdict = self.evaluate(sandbox_report=None, measurement_suite=None)
        self.assertIn("sandbox_evidence_required", verdict.reason)

    def test_claiming_out_of_sample_without_a_holdout_does_not_survive(self):
        p = self.proposal()
        from .measurement import RESEARCH_TOOL, Measurement, MeasurementSuite
        development_only = MeasurementSuite(
            proposal_digest=proposal_digest(p), at="2026-09-16T00:00:00+00:00",
            measurements=(Measurement("dev", RESEARCH_TOOL, "higher_is_better",
                                      False, 0.0, 5.0, 0, 0, True, "measured"),))
        verdict = self.evaluate(measurement_suite=development_only,
                                out_of_sample=True)
        self.assertFalse(verdict.out_of_sample)
        self.assertNotEqual(verdict.status, "eligible")

    def test_an_unusable_holdout_does_not_count_as_out_of_sample(self):
        """A crashed holdout is not a holdout that agreed."""
        p = self.proposal()
        from .measurement import RESEARCH_TOOL, Measurement, MeasurementSuite
        crashed = MeasurementSuite(
            proposal_digest=proposal_digest(p), at="2026-09-16T00:00:00+00:00",
            measurements=(
                Measurement("dev", RESEARCH_TOOL, "higher_is_better", False,
                            0.0, 5.0, 0, 0, True, "measured"),
                Measurement("holdout", RESEARCH_TOOL, "higher_is_better", True,
                            None, None, 0, 1, False, "candidate_exit=1")))
        self.assertFalse(self.evaluate(measurement_suite=crashed,
                                       out_of_sample=True).out_of_sample)

    def test_sample_size_is_not_silently_taken_from_the_task_count(self):
        """The suite holds one row per evaluation task, not per observation.

        Deriving sample_size from it would put every proposal below any
        realistic sample_requirement and brick promotion permanently, which
        is a different bug from the one this gate closes.
        """
        self.assertEqual(self.evaluate(sample_size=40).sample_size, 40)


class ABrokenEvaluationTaskBlocksPromotionTests(unittest.TestCase):
    """A task that crashed on the candidate was silently discarded.

    Only usable rows were averaged, so a candidate that destroyed a protected
    evaluation task could still reach eligible on the tasks it did not break.
    A crash on the candidate while the baseline ran it fine is not missing
    data; it is the candidate breaking something, which is the strongest
    evidence the suite can produce.
    """

    def proposal(self):
        import inspect
        sig = inspect.signature(MutationProposal)
        kw = dict(mutation_id="m1", parent_version="v1", targets=("runtime/x.py",),
                  patch="p", failure_ids=("f1",), expected_effect="better",
                  counter_metrics=("cost",), rollback_condition="worse",
                  sample_requirement=30)
        kw = {k: v for k, v in kw.items() if k in sig.parameters}
        for name in sig.parameters:
            if name not in kw:
                kw[name] = () if ("ids" in name or "metrics" in name
                                  or "targets" in name) else ""
        return MutationProposal(**kw)

    def verdict(self, measurements):
        from .measurement import MeasurementSuite
        p = self.proposal()
        return evaluate_mutation(
            p, sandbox_report=passing_report(p),
            measurement_suite=MeasurementSuite(proposal_digest(p), measurements,
                                               "2026-09-16T00:00:00Z"),
            sample_size=40, primary_delta=None,
            counter_metric_deltas={"cost": -0.1},
            counter_metric_directions={"cost": "lower_is_better"},
            out_of_sample=True, sandbox_passed=True, rollback_triggered=False)

    def rows(self, *extra):
        from .measurement import RESEARCH_TOOL, Measurement
        return (
            Measurement("dev", RESEARCH_TOOL, "higher_is_better", False,
                        0.0, 0.5, 0, 0, True, "measured"),
            Measurement("holdout", RESEARCH_TOOL, "higher_is_better", True,
                        0.0, 0.3, 0, 0, True, "measured"),
        ) + extra

    def test_a_task_the_candidate_broke_rejects_it(self):
        from .measurement import RESEARCH_TOOL, Measurement
        broke = Measurement("holdout_crash", RESEARCH_TOOL, "higher_is_better",
                            True, 1.0, None, 0, 1, False, "candidate_exit=1")
        verdict = self.verdict(self.rows(broke))
        self.assertEqual(verdict.status, "rejected")
        self.assertIn("candidate_broke_evaluation_task:holdout_crash",
                      verdict.reason)

    def test_a_clean_suite_is_still_eligible(self):
        self.assertEqual(self.verdict(self.rows()).status, "eligible")

    def test_a_task_broken_in_both_trees_is_not_blamed_on_the_candidate(self):
        """The task itself is broken. That is not this candidate's doing."""
        from .measurement import RESEARCH_TOOL, Measurement
        already_broken = Measurement("both_fail", RESEARCH_TOOL,
                                     "higher_is_better", True, None, None,
                                     1, 1, False, "baseline_exit=1")
        self.assertNotEqual(self.verdict(self.rows(already_broken)).status,
                            "rejected")

    def test_a_broken_development_task_also_blocks(self):
        """Breaking a non-protected task is still breaking something."""
        from .measurement import RESEARCH_TOOL, Measurement
        broke = Measurement("dev_crash", RESEARCH_TOOL, "higher_is_better",
                            False, 1.0, None, 0, 2, False, "candidate_exit=2")
        self.assertEqual(self.verdict(self.rows(broke)).status, "rejected")

    def test_a_holdout_that_exits_cleanly_with_junk_output_still_blocks(self):
        """The exit code is not the signal.

        The first fix only caught a nonzero exit, so a task that exited 0 and
        emitted NaN or nonnumeric text was still silently dropped from
        grading. What matters is that the BASELINE produced a usable number
        and the candidate did not, however it failed to.
        """
        from .measurement import RESEARCH_TOOL, Measurement
        for label, candidate_value in (("nonnumeric", None),
                                       ("nan", float("nan")),
                                       ("infinite", float("inf"))):
            with self.subTest(output=label):
                junk = Measurement("holdout_junk", RESEARCH_TOOL,
                                   "higher_is_better", True, 1.0, candidate_value,
                                   0, 0, False, f"{label}_output")
                verdict = self.verdict(self.rows(junk))
                self.assertEqual(verdict.status, "rejected")
                self.assertIn("candidate_broke_evaluation_task:holdout_junk",
                              verdict.reason)
