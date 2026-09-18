import unittest
from datetime import datetime
from .governance import (
    adversarial_review, choose_wakeup, evaluate_goal_observations,
    goal_direction, goal_terminal_status, grade_goal, integrity_check,
    make_goal, promote_prompt, prompt_variant, rollback_prompt,
    summarise_goal_attribution, summarise_goals, validate_goal_close,
    validate_goal_creation, validate_goal_progress,
)

class GovernanceTests(unittest.TestCase):
    def test_adversarial_divergence(self):
        r = adversarial_review({"action":"consider"},{"action":"reject","divergence":"portfolio_gap","factual_agreement":1,"thesis_agreement":1,"evidence_completeness":1,"counterfactual_quality":1})
        self.assertEqual(r.action,"reject"); self.assertEqual(r.divergence,"portfolio_gap")
    def test_goal_contract_and_grade(self):
        g={"goal_id":"g1","category":"process","statement":"close evidence gaps","deadline":"2026-09-20T00:00:00Z","success_metric":"gaps_closed","success_target":4,"evaluation_rubric":"count closed gaps","metric_type":"controllable","baseline":1,"caused_by":[]}
        self.assertEqual(make_goal(g).goal_id,"g1"); self.assertEqual(grade_goal(g,4,now="2026-09-19T00:00:00Z")["status"],"met")
    def test_prompt_adoption_and_rollback(self):
        p={"prompt_id":"P-X","version":2,"body":"x","state":"testing","baseline":"P-1","hypothesis":"better","success_metric":"quality","counter_metric":"cost","rollback_to":"P-1"}
        self.assertEqual(prompt_variant(p).version,2); self.assertEqual(promote_prompt(p,success=True,counter_metric_ok=True)["state"],"adopted"); self.assertEqual(rollback_prompt(p)["rollback_applied"],"P-1")
    def test_integrity_and_cadence(self):
        r=integrity_check(required_files_ok=True,chain_errors=[],dangling=[],stale_count=1); self.assertFalse(r.ok); self.assertLess(r.confidence_modifier,1)
        w=choose_wakeup(now="2026-09-15T12:00:00Z",urgency_hours=8,data_freshness_hours=24,unresolved_count=6); self.assertEqual(w["recommended_hours"],4.0)

if __name__ == "__main__": unittest.main()


class GoalGradingFollowsDirectionTests(unittest.TestCase):
    """A reduction goal was graded upside down.

    Grading assumed higher_is_better for every goal, so "at most 1 integrity
    error" scored 10 errors as met and 0 errors as missed. The best possible
    result was the failing one, and the worst was a pass.
    """

    NOW = "2026-06-01T00:00:00+00:00"
    DEADLINE = "2026-12-31T00:00:00+00:00"

    def goal(self, **over):
        row = {"goal_id": "g1", "deadline": self.DEADLINE,
               "success_target": 1, "baseline": 8}
        row.update(over)
        return row

    def status(self, observed, **over):
        return grade_goal(self.goal(**over), observed, now=self.NOW)["status"]

    def test_reduction_goal_meeting_target_is_met(self):
        self.assertEqual(self.status(0), "met")
        self.assertEqual(self.status(1), "met")

    def test_reduction_goal_far_past_target_is_missed(self):
        self.assertEqual(self.status(10), "missed")

    def test_increase_goal_still_grades_upward(self):
        self.assertEqual(self.status(12, success_target=10, baseline=4), "met")
        self.assertEqual(self.status(2, success_target=10, baseline=4), "missed")

    def test_direction_is_inferred_from_baseline_and_target(self):
        self.assertEqual(goal_direction(self.goal()), "lower_is_better")
        self.assertEqual(goal_direction(self.goal(success_target=10, baseline=4)),
                         "higher_is_better")

    def test_an_explicit_direction_overrides_the_inference(self):
        goal = self.goal(success_target=10, baseline=4, direction="lower_is_better")
        self.assertEqual(goal_direction(goal), "lower_is_better")
        # 20 is well outside the partial band (target 10 + 25% = 12.5),
        # so under lower_is_better it must miss even though the inferred
        # direction would have called it met.
        self.assertEqual(grade_goal(goal, 20, now=self.NOW)["status"], "missed")
        self.assertEqual(grade_goal({**goal, "direction": "higher_is_better"}, 20,
                                    now=self.NOW)["status"], "met")

    def test_an_ungradable_goal_is_not_silently_passed(self):
        """No basis for a direction must not resolve to "met".

        Guessing is how 10 errors became a pass.
        """
        self.assertEqual(self.status(7, success_target=5, baseline=5),
                         "ungraded_direction_unknown")

    def test_the_grade_reports_the_direction_it_used(self):
        self.assertEqual(grade_goal(self.goal(), 0, now=self.NOW)["direction"],
                         "lower_is_better")


class HostGoalObservationTests(unittest.TestCase):
    def test_non_object_goal_observation_is_reported_not_raised(self):
        self.assertEqual(
            evaluate_goal_observations(["grade"]),
            [{
                "index": 0,
                "valid": False,
                "problems": ["goal_observation_not_an_object"],
            }],
        )

    def test_a_host_authored_goal_is_graded(self):
        goal = {
            "goal_id": "g1",
            "category": "process",
            "statement": "Close adoption debt.",
            "deadline": "2026-09-20T00:00:00Z",
            "success_metric": "unreachable_modules",
            "success_target": 0,
            "evaluation_rubric": "lower is better",
            "metric_type": "controllable",
            "baseline": 15,
            "caused_by": ["finding-1"],
        }
        result = evaluate_goal_observations([{
            "goal": goal,
            "observed_value": 0,
            "now": "2026-09-16T20:00:00Z",
        }])[0]
        self.assertTrue(result["valid"])
        self.assertEqual(result["grade"]["status"], "met")

    def creation(self, **goal_overrides):
        goal = {
            "goal_id": "goal-one",
            "created_at": "2026-09-17T14:00:00Z",
            "category": "research_quality",
            "statement": "Close the selected evidence gap.",
            "deadline": "2026-09-18T14:00:00Z",
            "success_metric": "unresolved_evidence_gaps",
            "success_target": 0,
            "partial_target": 0.5,
            "evaluation_rubric": "Met when the selected gap reaches zero.",
            "metric_type": "controllable",
            "baseline": 1,
            "direction": "lower_is_better",
            "caused_by": ["research_director"],
        }
        goal.update(goal_overrides)
        return {"mode": "create", "goal": goal}

    def test_a_machine_gradable_goal_can_be_opened(self):
        request = self.creation()
        self.assertEqual(
            validate_goal_creation(
                request,
                cycle_as_of="2026-09-17T14:00:00Z",
                allowed_causes=["research_director"],
            ),
            [],
        )
        result = evaluate_goal_observations([request])[0]
        self.assertTrue(result["valid"])
        self.assertEqual(result["event"]["status"], "open")

    def test_unknown_baseline_is_rejected(self):
        problems = validate_goal_creation(
            self.creation(baseline="unknown"),
            cycle_as_of="2026-09-17T14:00:00Z",
            allowed_causes=["research_director"],
        )
        self.assertIn("baseline_not_numeric", problems)

    def test_goal_must_be_caused_by_current_cycle_evidence(self):
        problems = validate_goal_creation(
            self.creation(caused_by=["old-cycle"]),
            cycle_as_of="2026-09-17T14:00:00Z",
            allowed_causes=["research_director"],
        )
        self.assertIn("caused_by_not_in_current_cycle", problems)
        mixed = validate_goal_creation(
            self.creation(caused_by=[
                "research_director", "invented-cause",
            ]),
            cycle_as_of="2026-09-17T14:00:00Z",
            allowed_causes=["research_director"],
        )
        self.assertIn("caused_by_not_in_current_cycle", mixed)

    def test_partial_target_must_follow_declared_direction(self):
        problems = validate_goal_creation(
            self.creation(partial_target=1.5),
            cycle_as_of="2026-09-17T14:00:00Z",
            allowed_causes=["research_director"],
        )
        self.assertIn(
            "partial_target_not_between_baseline_and_target",
            problems,
        )

    def test_open_goal_summary_is_bounded_and_marks_expiry(self):
        records = [{
            "record_type": "goal_event",
            "payload": {
                "goal_id": "goal-one",
                "event": "created",
                "status": "open",
                "source_cycle_id": "cycle-one",
                "goal": self.creation()["goal"],
            },
        }]
        result = summarise_goals(
            records,
            now=datetime.fromisoformat("2026-09-19T00:00:00+00:00"),
        )
        self.assertEqual(result["open_count"], 1)
        self.assertEqual(result["expired_count"], 1)
        self.assertTrue(result["open"][0]["expired"])

    def progress(self, **overrides):
        request = {
            "mode": "progress",
            "goal_id": "goal-one",
            "observed_at": "2026-09-17T15:00:00Z",
            "observed_value": 0.5,
            "assessment": "One of two evidence gaps remains.",
            "evidence": [{
                "evidence_id": "research_director",
                "source": "research director output",
                "finding": "The selected gap count fell from one to 0.5.",
            }],
            "caused_by": ["research_director"],
        }
        request.update(overrides)
        return request

    def open_goal(self):
        return {
            "goal_id": "goal-one",
            "event": "created",
            "status": "open",
            "opened_at": "2026-09-17T14:00:00Z",
            "source_cycle_id": "cycle-one",
            "goal": self.creation()["goal"],
        }

    def test_progress_accepts_a_finite_setback_without_grading(self):
        request = self.progress(observed_value=1.5)
        self.assertEqual(
            validate_goal_progress(
                request,
                cycle_as_of="2026-09-17T15:00:00Z",
                allowed_causes=["research_director"],
                open_goal=self.open_goal(),
            ),
            [],
        )
        result = evaluate_goal_observations([request])[0]
        self.assertTrue(result["valid"])
        self.assertEqual(result["event"]["status"], "open")
        self.assertNotIn("grade", result)

    def test_progress_rejects_nonfinite_and_terminal_claims(self):
        problems = validate_goal_progress(
            self.progress(observed_value=float("inf"), status="met"),
            cycle_as_of="2026-09-17T15:00:00Z",
            allowed_causes=["research_director"],
            open_goal=self.open_goal(),
        )
        self.assertIn("observed_value_not_finite_number", problems)
        self.assertIn("unexpected_field_status", problems)

    def test_progress_rejects_naive_time_without_crashing(self):
        problems = validate_goal_progress(
            self.progress(observed_at="2026-09-17T15:00:00"),
            cycle_as_of="2026-09-17T15:00:00Z",
            allowed_causes=["research_director"],
            open_goal=self.open_goal(),
        )
        self.assertIn("observed_at_timezone_required", problems)

    def test_summary_preserves_creation_and_latest_progress(self):
        goal = self.creation()["goal"]
        records = [
            {
                "record_type": "goal_event",
                "payload": {
                    "goal_id": "goal-one",
                    "event": "created",
                    "status": "open",
                    "opened_at": goal["created_at"],
                    "source_cycle_id": "cycle-one",
                    "goal": goal,
                },
            },
            {
                "record_type": "goal_event",
                "payload": {
                    "goal_id": "goal-one",
                    "event": "progress",
                    "status": "open",
                    "opened_at": goal["created_at"],
                    "observed_at": "2026-09-17T15:00:00Z",
                    "previous_value": 1,
                    "observed_value": 0.5,
                    "delta": -0.5,
                    "remaining_to_target": 0.5,
                    "assessment": "One gap partly closed.",
                    "source_cycle_id": "cycle-two",
                    "goal": goal,
                },
            },
        ]
        row = summarise_goals(
            records,
            now=datetime.fromisoformat("2026-09-17T16:00:00+00:00"),
        )["open"][0]
        self.assertEqual(row["source_cycle_id"], "cycle-one")
        self.assertEqual(row["last_progress_cycle_id"], "cycle-two")
        self.assertEqual(row["latest_observed_value"], 0.5)
        self.assertEqual(row["progress"][0]["delta"], -0.5)

    def close(self, **overrides):
        request = {
            "mode": "close",
            "goal_id": "goal-one",
            "observed_at": "2026-09-18T15:00:00Z",
            "closure_basis": "measurement",
            "observed_value": 0,
            "evidence": [{
                "evidence_id": "research_director",
                "source": "research director output",
                "finding": "No unresolved evidence gaps remain.",
            }],
            "caused_by": ["research_director"],
            "analysis": {
                "causal_summary": "The selected evidence path closed the gap.",
                "worked": ["The specialist followed the selected agenda."],
                "failed": [],
                "counterfactual": "A stale source would have left the gap open.",
                "next_change": "Reuse the bounded evidence comparison.",
            },
        }
        request.update(overrides)
        return request

    def test_terminal_status_uses_declared_partial_boundary(self):
        goal = self.creation()["goal"]
        self.assertEqual(goal_terminal_status(goal, 0), "met")
        self.assertEqual(goal_terminal_status(goal, 0.5), "partially_met")
        self.assertEqual(goal_terminal_status(goal, 0.75), "missed")
        without_partial = dict(goal)
        del without_partial["partial_target"]
        self.assertEqual(goal_terminal_status(without_partial, 0.5), "missed")
        increasing = {
            **goal,
            "baseline": 0,
            "partial_target": 5,
            "success_target": 10,
            "direction": "higher_is_better",
        }
        self.assertEqual(goal_terminal_status(increasing, 10), "met")
        self.assertEqual(
            goal_terminal_status(increasing, 5), "partially_met")
        self.assertEqual(goal_terminal_status(increasing, 4), "missed")

    def test_measured_closure_waits_for_deadline(self):
        problems = validate_goal_close(
            self.close(
                observed_at="2026-09-17T15:00:00Z",
            ),
            cycle_as_of="2026-09-17T15:00:00Z",
            allowed_causes=["research_director"],
            open_goal=self.open_goal(),
            created_goal=self.open_goal(),
        )
        self.assertIn("measurement_before_deadline", problems)

    def test_host_cannot_supply_a_terminal_status(self):
        problems = validate_goal_close(
            self.close(status="met"),
            cycle_as_of="2026-09-18T15:00:00Z",
            allowed_causes=["research_director"],
            open_goal=self.open_goal(),
            created_goal=self.open_goal(),
        )
        self.assertIn("unexpected_field_status", problems)

    def test_invalidation_requires_reason_and_forbids_value(self):
        request = self.close(
            observed_at="2026-09-17T15:00:00Z",
            closure_basis="invalidated",
        )
        problems = validate_goal_close(
            request,
            cycle_as_of="2026-09-17T15:00:00Z",
            allowed_causes=["research_director"],
            open_goal=self.open_goal(),
            created_goal=self.open_goal(),
        )
        self.assertIn(
            "observed_value_forbidden_for_invalidation", problems)
        self.assertIn("invalidation_reason_missing", problems)

    def test_closed_summary_separates_lifecycle_and_grade(self):
        goal = self.creation()["goal"]
        records = [
            {
                "record_type": "goal_event",
                "payload": {
                    "goal_id": "goal-one",
                    "event": "created",
                    "status": "open",
                    "opened_at": goal["created_at"],
                    "source_cycle_id": "cycle-one",
                    "goal": goal,
                },
            },
            {
                "record_type": "goal_event",
                "payload": {
                    "goal_id": "goal-one",
                    "event": "closed",
                    "status": "closed",
                    "terminal_status": "partially_met",
                    "closure_basis": "measurement",
                    "closed_at": "2026-09-18T15:00:00Z",
                    "source_cycle_id": "cycle-close",
                    "observed_value": 0.5,
                    "analysis": self.close()["analysis"],
                    "progress_count": 1,
                    "goal": goal,
                },
            },
        ]
        result = summarise_goals(records)
        self.assertEqual(result["open_count"], 0)
        self.assertEqual(result["closed_count"], 1)
        row = result["recent_closed"][0]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["terminal_status"], "partially_met")
        self.assertEqual(row["created_cycle_id"], "cycle-one")


class GoalAttributionTests(unittest.TestCase):
    def created(self, goal_id, category, causes):
        return {
            "record_type": "goal_event",
            "payload": {
                "goal_id": goal_id,
                "event": "created",
                "status": "open",
                "source_cycle_id": f"create-{goal_id}",
                "goal": {
                    "goal_id": goal_id,
                    "category": category,
                    "metric_type": "controllable",
                    "direction": "lower_is_better",
                    "caused_by": causes,
                },
            },
        }

    def closed(self, goal_id, status, sources, **overrides):
        payload = {
            "goal_id": goal_id,
            "event": "closed",
            "status": "closed",
            "terminal_status": status,
            "closed_at": "2026-09-17T15:00:00Z",
            "source_cycle_id": f"close-{goal_id}",
            "evidence": [
                {
                    "evidence_id": f"{goal_id}-evidence-{index}",
                    "source": source,
                    "finding": "Observed closure evidence.",
                }
                for index, source in enumerate(sources)
            ],
            "analysis": {
                "causal_summary": "The recorded factors explain the result.",
                "worked": ["One process step helped."],
                "failed": ["One process step did not help."],
                "counterfactual": "Different evidence could change it.",
                "next_change": f"Change the next {goal_id} process.",
            },
        }
        payload.update(overrides)
        return {"record_type": "goal_event", "payload": payload}

    def records(self):
        return [
            self.created(
                "goal-met", "evidence_quality",
                ["research_director", "research_director"],
            ),
            self.closed(
                "goal-met", "met", ["SEC", "SEC"],
                goal={"category": "tampered-close-copy"},
            ),
            self.created(
                "goal-partial", "evidence_quality",
                ["research_director"],
            ),
            self.closed("goal-partial", "partially_met", ["SEC"]),
            self.created(
                "goal-missed", "decision_quality", ["portfolio_fit"],
            ),
            self.closed("goal-missed", "missed", ["IBKR"]),
            self.closed("goal-invalidated", "invalidated", ["operator"]),
            self.closed("goal-bad", "excellent", ["unknown"]),
            self.created("goal-open", "open_pattern", ["portfolio"]),
            {
                "record_type": "goal_event",
                "payload": {
                    "goal_id": "goal-open",
                    "event": "progress",
                    "status": "open",
                    "observed_at": "2026-09-17T15:00:00Z",
                },
            },
        ]

    def test_attribution_is_a_deduplicated_closed_goal_census(self):
        records = self.records()
        result = summarise_goal_attribution(records)
        self.assertEqual(result["sample_count"], 4)
        self.assertEqual(
            result["outcome_counts"],
            {
                "met": 1,
                "partially_met": 1,
                "missed": 1,
                "invalidated": 1,
            },
        )
        self.assertEqual(
            result["closures_excluded"],
            {"count": 1, "reasons": {"invalid_terminal_status": 1}},
        )
        self.assertEqual(
            result["coverage"],
            {
                "closures_with_goal_pattern": 3,
                "closures_with_origin_causes": 3,
                "closures_with_evidence_sources": 4,
                "closures_with_next_change": 4,
            },
        )
        patterns = result["goal_patterns"]["rows"]
        self.assertNotIn(
            "tampered-close-copy",
            [row["category"] for row in patterns],
        )
        evidence = {
            row["source"]: row
            for row in result["closure_evidence_sources"]["rows"]
        }
        self.assertEqual(evidence["SEC"]["closed_count"], 2)
        self.assertEqual(evidence["SEC"]["outcomes"]["met"], 1)
        self.assertEqual(evidence["SEC"]["outcomes"]["partially_met"], 1)
        causes = {
            row["cause_id"]: row
            for row in result["origin_causes"]["rows"]
        }
        self.assertEqual(causes["research_director"]["closed_count"], 2)
        for table_name in (
            "goal_patterns",
            "origin_causes",
            "closure_evidence_sources",
        ):
            for row in result[table_name]["rows"]:
                self.assertEqual(
                    sum(row["outcomes"].values()),
                    row["closed_count"],
                )
        self.assertEqual(
            sum(result["outcome_counts"].values()),
            result["sample_count"],
        )
        self.assertEqual(
            summarise_goals(records)["closed_count"],
            result["sample_count"],
        )

    def test_attribution_tables_report_bounded_coverage(self):
        result = summarise_goal_attribution(self.records(), limit=1)
        for table_name in (
            "goal_patterns",
            "origin_causes",
            "closure_evidence_sources",
        ):
            table = result[table_name]
            self.assertEqual(len(table["rows"]), 1)
            self.assertEqual(
                table["not_shown"],
                table["distinct_count"] - 1,
            )

    def test_long_identity_is_bounded_with_a_hash(self):
        records = [
            self.created("goal-long", "quality", ["cause"]),
            self.closed("goal-long", "met", ["s" * 300]),
        ]
        row = summarise_goal_attribution(
            records)["closure_evidence_sources"]["rows"][0]
        self.assertTrue(row["source_truncated"])
        self.assertEqual(len(row["source"]), 200)
        self.assertEqual(len(row["source_sha256"]), 64)

    def test_zero_state_makes_no_quality_claim(self):
        result = summarise_goal_attribution([])
        self.assertEqual(result["sample_count"], 0)
        self.assertIn(
            "no quality attribution claim",
            result["what_this_means"].lower(),
        )
