import unittest
from .orchestrator import SPECIALISTS, AgentJob, build_plan, execution_layers, run_plan, specialist_branches, validate_plan


class OrchestratorTests(unittest.TestCase):
    def test_a_selected_plan_is_complete_and_required(self):
        jobs=build_plan(ibkr_current=True,research_needed=True,specialist_ids=("value_fcf","options_volatility"))
        self.assertEqual(validate_plan(jobs),[])
        self.assertEqual([j.agent_id for j in specialist_branches(jobs)],["value_fcf","options_volatility"])
        self.assertTrue(all(j.required for j in specialist_branches(jobs)))
        self.assertTrue(next(j for j in jobs if j.agent_id=="memory_retrieval").required)
        self.assertTrue(next(j for j in jobs if j.agent_id=="learning_audit").required)
        self.assertTrue(next(j for j in jobs if j.agent_id=="meta_research").required)
        self.assertTrue(next(j for j in jobs if j.agent_id=="self_improvement").required)

    def test_no_research_still_runs_memory_and_learning_after_governance(self):
        jobs=build_plan(ibkr_current=False,research_needed=False)
        self.assertEqual(validate_plan(jobs),[])
        self.assertEqual(next(j for j in jobs if j.agent_id=="market_scout").depends_on,("portfolio",))
        self.assertEqual(next(j for j in jobs if j.agent_id=="research_director").depends_on,("market_scout",))
        self.assertEqual(next(j for j in jobs if j.agent_id=="memory_retrieval").depends_on,("research_director",))
        self.assertEqual(next(j for j in jobs if j.agent_id=="governance_review").depends_on,("memory_retrieval",))
        self.assertEqual(next(j for j in jobs if j.agent_id=="decision").depends_on,("governance_review",))
        self.assertEqual(next(j for j in jobs if j.agent_id=="learning_audit").depends_on,("decision",))
        self.assertEqual(next(j for j in jobs if j.agent_id=="meta_research").depends_on,("learning_audit",))
        self.assertEqual(next(j for j in jobs if j.agent_id=="self_improvement").depends_on,("meta_research",))
        self.assertTrue(next(j for j in jobs if j.agent_id=="learning_audit").required)

    def test_partial_mode_is_explicit(self):
        jobs=build_plan(ibkr_current=True,research_needed=True,specialist_ids=("value_fcf",),full_execution=False,weekly_learning=False)
        self.assertFalse(next(j for j in jobs if j.agent_id=="value_fcf").required)
        self.assertFalse(next(j for j in jobs if j.agent_id=="learning_audit").required)
        self.assertFalse(next(j for j in jobs if j.agent_id=="self_improvement").required)

    def test_layers_expose_memory_then_specialist_and_learning_sequence(self):
        jobs=build_plan(ibkr_current=True,research_needed=True,specialist_ids=("value_fcf","options_volatility"))
        layers=execution_layers(jobs); ids=[{j.agent_id for j in layer} for layer in layers]
        self.assertEqual(ids[0],{"portfolio"})
        self.assertEqual(ids[1],{"market_scout"})
        self.assertEqual(ids[2],{"research_director"})
        self.assertEqual(ids[3],{"memory_retrieval"})
        self.assertEqual(ids[4],{"value_fcf","options_volatility"})
        positions={name: i for i, group in enumerate(ids) for name in group}
        self.assertLess(positions["governance_review"], positions["decision"])
        self.assertLess(positions["decision"], positions["learning_audit"])
        self.assertLess(positions["learning_audit"], positions["meta_research"])
        self.assertLess(positions["meta_research"], positions["self_improvement"])

    def test_deep_memory_is_after_self_improvement_and_optional_by_default(self):
        normal=build_plan(ibkr_current=True,research_needed=False,deep_memory=False)
        self.assertNotIn("memory_distillation",[j.agent_id for j in normal])
        deep=build_plan(ibkr_current=True,research_needed=False,deep_memory=True)
        self.assertEqual(validate_plan(deep),[])
        job=next(j for j in deep if j.agent_id=="memory_distillation")
        self.assertEqual(job.depends_on,("self_improvement",)); self.assertTrue(job.required)

    def test_duplicate_and_cycle_are_rejected(self):
        self.assertTrue(validate_plan([AgentJob("a","x"),AgentJob("a","x")]))
        self.assertIn("cyclic_dependency",validate_plan([AgentJob("a","x",("b",)),AgentJob("b","x",("a",))]))

    def test_runner_executes_dependencies_and_fails_closed(self):
        jobs=build_plan(ibkr_current=True,research_needed=True,specialist_ids=("value_fcf","options_volatility"))
        seen=[]
        def handler(job,context,deps): seen.append((job.agent_id,tuple(deps))); return {"agent":job.agent_id}
        result=run_plan(jobs=jobs,handlers={j.agent_id:handler for j in jobs},context={"run_id":"test"})
        self.assertEqual(result.blocked,()); self.assertEqual(result.errors,()); self.assertTrue(result.review_only); self.assertEqual(set(result.outputs),{j.agent_id for j in jobs}); self.assertEqual(dict(seen)["evidence_arbitration"],("value_fcf","options_volatility")); self.assertEqual(dict(seen)["market_scout"],("portfolio",)); self.assertEqual(dict(seen)["research_director"],("market_scout",)); self.assertEqual(dict(seen)["memory_retrieval"],("research_director",)); self.assertEqual(dict(seen)["self_improvement"],("meta_research",))
        handlers={j.agent_id:handler for j in jobs}; handlers.pop("options_volatility"); result=run_plan(jobs=jobs,handlers=handlers); self.assertIn("options_volatility",result.blocked); self.assertIn("evidence_arbitration",result.blocked); self.assertIn("decision",result.blocked); self.assertIn("learning_audit",result.blocked); self.assertIn("meta_research",result.blocked); self.assertIn("self_improvement",result.blocked)


if __name__ == "__main__": unittest.main()


class SpecialistSelectionTests(unittest.TestCase):
    """The Research Director selects; code must not select for it.

    build_plan used to default specialist_ids to the entire SPECIALISTS
    catalogue, every pass required, producing a 23-stage all-or-nothing
    plan. That does not fit in one host wake, so an honest host marked
    every stage blocked and no complete cycle receipt could ever exist.
    The deadlock was this default, not a host limitation.

    AGENT_ORCHESTRATOR.md calls the catalogue "a capability map, not a
    mandatory list and not a ceiling" and names host capacity as a
    selection input. PHILOSOPHY.md forbids hardcoded sequences. A silent
    default is both a contradiction of the doc and a hardcoded sequence.
    """

    def test_omitting_the_selection_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            build_plan(ibkr_current=True, research_needed=True)
        self.assertIn("specialist_selection_required", str(ctx.exception))

    def test_the_refusal_explains_who_decides(self):
        # The message is the teaching surface; a bare error would just get
        # worked around by passing the full catalogue.
        with self.assertRaises(ValueError) as ctx:
            build_plan(ibkr_current=True, research_needed=True)
        message = str(ctx.exception)
        self.assertIn("Research Director", message)
        self.assertIn("host capacity", message)

    def test_empty_selection_is_refused_and_points_at_the_alternative(self):
        with self.assertRaises(ValueError) as ctx:
            build_plan(ibkr_current=True, research_needed=True, specialist_ids=())
        self.assertIn("empty_specialist_selection", str(ctx.exception))
        self.assertIn("research_needed=False", str(ctx.exception))

    def test_a_bounded_selection_produces_a_plan_that_fits_a_wake(self):
        # The point of the whole change: the plan scales with the
        # selection instead of with the catalogue.
        small = build_plan(ibkr_current=True, research_needed=True,
                           specialist_ids=("value_fcf",))
        larger = build_plan(ibkr_current=True, research_needed=True,
                            specialist_ids=("value_fcf", "options_volatility"))
        self.assertEqual(len(larger) - len(small), 1)
        self.assertLess(len(small), 23)

    def test_selecting_the_whole_catalogue_is_still_allowed_when_explicit(self):
        # Not a ceiling. The Director may choose all of them; it just has
        # to say so rather than have code assume it.
        plan = build_plan(ibkr_current=True, research_needed=True,
                          specialist_ids=SPECIALISTS)
        self.assertEqual(
            len([j for j in plan if j.phase == "specialist"]), len(SPECIALISTS))
        self.assertEqual(validate_plan(plan), [])

    def test_a_no_research_cycle_needs_no_selection(self):
        plan = build_plan(ibkr_current=True, research_needed=False)
        self.assertEqual(validate_plan(plan), [])
        self.assertEqual([j for j in plan if j.phase == "specialist"], [])

    def test_an_invented_role_is_allowed_because_the_map_is_not_a_ceiling(self):
        # AGENT_ORCHESTRATOR.md: "New research roles may be invented when
        # justified." Refusing off-catalogue names would turn the
        # capability map back into the mandatory list this change removed.
        plan = build_plan(ibkr_current=True, research_needed=True,
                          specialist_ids=("credit_cycle_positioning",))
        self.assertIn("credit_cycle_positioning",
                      [j.agent_id for j in plan if j.phase == "specialist"])
        self.assertEqual(validate_plan(plan), [])

    def test_a_reserved_pipeline_id_cannot_be_used_as_a_specialist(self):
        # Inventing roles is open; colliding with a pipeline stage is not,
        # because that would silently re-wire the dependency graph.
        with self.assertRaises(ValueError) as ctx:
            build_plan(ibkr_current=True, research_needed=True,
                       specialist_ids=("decision",))
        self.assertIn("invalid_specialist", str(ctx.exception))

    def test_an_empty_role_name_is_refused(self):
        with self.assertRaises(ValueError):
            build_plan(ibkr_current=True, research_needed=True,
                       specialist_ids=("",))


class ReportedBlockingStatusTests(unittest.TestCase):
    """A stage that reports itself blocked must block its dependents.

    run_plan used to record ANY non-raising handler return as completion.
    A stage could return {"status": "blocked"} and be recorded completed,
    with no error, and the decision stage would run on research that had
    just said it could not be done. That defeats the one thing the host
    reliably gets right: refusing to claim work it did not do.
    """

    def plan(self):
        return build_plan(ibkr_current=True, research_needed=True,
                          specialist_ids=("value_fcf",))

    def run_with(self, blocking_result, required_stage="value_fcf"):
        jobs = self.plan()
        ran = []

        def mk(name):
            def handler(job, ctx, deps):
                ran.append(name)
                return blocking_result if name == required_stage else {"ok": True}
            return handler

        result = run_plan(jobs=jobs,
                          handlers={j.agent_id: mk(j.agent_id) for j in jobs},
                          context={})
        return result, ran

    def test_status_blocked_is_not_recorded_as_completed(self):
        result, _ = self.run_with({"status": "blocked", "blockers": ["no data"]})
        self.assertNotIn("value_fcf", result.completed)
        self.assertIn("value_fcf", result.blocked)

    def test_status_blocked_stops_the_decision_stage(self):
        # The load-bearing assertion: no decision on blocked research.
        _, ran = self.run_with({"status": "blocked"})
        self.assertNotIn("decision", ran)

    def test_the_error_names_the_stage_and_the_reported_status(self):
        result, _ = self.run_with({"status": "blocked"})
        self.assertIn("stage_reported_blocked:value_fcf", result.errors)

    def test_failed_and_error_also_block(self):
        for status in ("failed", "error"):
            with self.subTest(status=status):
                result, ran = self.run_with({"status": status})
                self.assertIn("value_fcf", result.blocked)
                self.assertNotIn("decision", ran)

    def test_status_is_matched_case_insensitively(self):
        result, _ = self.run_with({"status": "BLOCKED"})
        self.assertIn("value_fcf", result.blocked)

    def test_a_handler_with_no_status_key_still_completes(self):
        # Backward compatibility: plain payloads are success.
        result, ran = self.run_with({"findings": ["something"]})
        self.assertIn("value_fcf", result.completed)
        self.assertIn("decision", ran)

    def test_a_non_blocking_status_still_completes(self):
        result, ran = self.run_with({"status": "completed"})
        self.assertIn("value_fcf", result.completed)
        self.assertIn("decision", ran)

    def test_a_non_mapping_result_still_completes(self):
        result, ran = self.run_with(["a", "list"])
        self.assertIn("value_fcf", result.completed)
        self.assertIn("decision", ran)


class StageIsolationTests(unittest.TestCase):
    """Stages must not share mutable state.

    Context and dependency outputs were passed by reference, so a
    specialist mutating a nested value leaked it into every LATER
    specialist and into the caller's own dict.

    Two invariants broke at once. AGENT_ORCHESTRATOR.md promises each pass
    "the same immutable portfolio snapshot", and it was neither immutable
    nor necessarily the same. And it is an anchoring channel: sibling
    conclusions must stay hidden until Evidence Arbitration, and a shared
    mutable context is a side-door the dependency graph does not show.
    """

    SPECIALISTS = ("value_fcf", "options_volatility", "special_situations")

    def plan(self):
        return build_plan(ibkr_current=True, research_needed=True,
                          specialist_ids=self.SPECIALISTS)

    def test_a_specialist_mutation_does_not_reach_later_specialists(self):
        seen = {}

        def mk(name):
            def handler(job, ctx, deps):
                seen[name] = dict(ctx.get("snapshot", {}).get("positions", {}))
                if name == "value_fcf":
                    ctx["snapshot"]["positions"]["INJECTED"] = "leak"
                return {"ok": True}
            return handler

        jobs = self.plan()
        run_plan(jobs=jobs, handlers={j.agent_id: mk(j.agent_id) for j in jobs},
                 context={"snapshot": {"positions": {"MSFT": 100}}})
        for later in ("options_volatility", "special_situations"):
            self.assertNotIn("INJECTED", seen[later],
                             f"{later} saw a sibling's mutation before arbitration")

    def test_a_stage_cannot_mutate_the_callers_context(self):
        jobs = self.plan()

        def handler(job, ctx, deps):
            ctx["snapshot"]["positions"]["INJECTED"] = "leak"
            return {"ok": True}

        context = {"snapshot": {"positions": {"MSFT": 100}}}
        run_plan(jobs=jobs, handlers={j.agent_id: handler for j in jobs}, context=context)
        self.assertEqual(context, {"snapshot": {"positions": {"MSFT": 100}}})

    def test_every_specialist_receives_an_identical_snapshot(self):
        # "the same immutable portfolio snapshot" -- assert the sameness,
        # not only the immutability. Captured on ENTRY with a copy, because
        # a stage mutating its own copy afterwards is allowed and would
        # otherwise make this compare post-mutation state.
        from copy import deepcopy
        on_entry = {}

        def mk(name):
            def handler(job, ctx, deps):
                on_entry[name] = deepcopy(ctx.get("snapshot"))
                if name == "value_fcf":
                    ctx["snapshot"]["positions"]["INJECTED"] = "leak"
                return {"ok": True}
            return handler

        jobs = self.plan()
        run_plan(jobs=jobs, handlers={j.agent_id: mk(j.agent_id) for j in jobs},
                 context={"snapshot": {"positions": {"MSFT": 100}}})
        received = [on_entry[s] for s in self.SPECIALISTS]
        self.assertTrue(all(snap == received[0] for snap in received), received)
        self.assertEqual(received[0], {"positions": {"MSFT": 100}})

    def test_a_stage_cannot_mutate_a_dependency_output(self):
        jobs = self.plan()
        captured = {}

        def handler(job, ctx, deps):
            for dep_id, value in deps.items():
                if isinstance(value, dict) and "payload" in value:
                    value["payload"]["TAMPERED"] = True
            captured[job.agent_id] = {k: dict(v) for k, v in deps.items()
                                      if isinstance(v, dict)}
            return {"payload": {"from": job.agent_id}}

        result = run_plan(jobs=jobs, handlers={j.agent_id: handler for j in jobs},
                          context={})
        for value in result.outputs.values():
            if isinstance(value, dict) and "payload" in value:
                self.assertNotIn("TAMPERED", value["payload"])


class NonExecutionStatusBlocksDependentsTests(unittest.TestCase):
    """A stage that did not execute must not feed its dependents.

    The orchestrator used a denylist of failure words, which is the wrong
    polarity for a safety gate: any status the host invented that was not
    listed counted as success. `skipped`, `deferred` and `not_run` all
    reported a stage that never ran, and the decision stage consumed their
    output anyway -- exactly what AGENT_ORCHESTRATOR.md forbids when it says
    to record skipped passes and never claim work that did not execute.
    """

    def jobs(self):
        return [AgentJob("portfolio", "x"), AgentJob("decision", "x", ("portfolio",))]

    def decision_ran(self, upstream_status):
        def handler(job, ctx, results):
            if job.agent_id == "portfolio":
                return {"status": upstream_status}
            return {"status": "completed"}
        result = run_plan(jobs=self.jobs(),
                          handlers={"portfolio": handler, "decision": handler},
                          context={})
        return "decision" in result.completed

    def test_skipped_upstream_blocks_the_decision_stage(self):
        self.assertFalse(self.decision_ran("skipped"))

    def test_deferred_and_not_run_upstream_block_dependents(self):
        self.assertFalse(self.decision_ran("deferred"))
        self.assertFalse(self.decision_ran("not_run"))

    def test_blocked_and_failed_still_block(self):
        self.assertFalse(self.decision_ran("blocked"))
        self.assertFalse(self.decision_ran("failed"))

    def test_unrecognised_status_fails_closed(self):
        self.assertFalse(self.decision_ran("partially_done"))
        self.assertFalse(self.decision_ran("weird_new_word"))

    def test_completed_upstream_still_runs_dependents(self):
        self.assertTrue(self.decision_ran("completed"))

    def test_handler_returning_plain_data_is_not_treated_as_failure(self):
        def handler(job, ctx, results):
            return {"data": 123}
        result = run_plan(jobs=self.jobs(),
                          handlers={"portfolio": handler, "decision": handler},
                          context={})
        self.assertIn("decision", result.completed)
