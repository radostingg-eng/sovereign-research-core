"""Varied questions, one answer, seventeen families untouched."""

import unittest

from .strategy_coverage import (
    KNOWN_FAMILIES, coverage, families_used, open_experiments,
    recent_reasoning, research_agenda_summary,
)


def cycle(*families, decision_family=None):
    data = {"research": [{"question": "q", "strategy_family": f} for f in families]}
    if decision_family:
        data["decision"] = {"status": "recommended",
                            "strategy_family": decision_family}
    return data


class HabitBecomesVisibleTests(unittest.TestCase):
    """Nine cycles asked twenty-seven distinct questions and reached the same
    conclusion three times. The questions varied; the strategy class did not.
    Nothing measured that, so nothing could notice it."""

    def test_nothing_recorded_means_nothing_used(self):
        result = coverage([{"research": [{"question": "q"}]}])
        self.assertEqual(result["families_used"], {})
        self.assertEqual(len(result["families_never_used"]), len(KNOWN_FAMILIES))

    def test_a_used_family_is_counted(self):
        result = coverage([cycle("volatility_options")])
        self.assertEqual(result["families_used"], {"volatility_options": 1})
        self.assertNotIn("volatility_options", result["families_never_used"])

    def test_a_family_used_twice_in_one_cycle_counts_once(self):
        """Cycles, not rows. Otherwise a chatty cycle looks like broad
        coverage."""
        self.assertEqual(families_used([cycle("hedging", "hedging")]),
                         {"hedging": 1})

    def test_the_decision_family_counts_too(self):
        used = families_used([cycle("hedging", decision_family="relative_value")])
        self.assertEqual(sorted(used), ["hedging", "relative_value"])

    def test_case_and_whitespace_do_not_create_phantom_families(self):
        used = families_used([cycle("  Volatility_Options  ")])
        self.assertEqual(used, {"volatility_options": 1})


class AnInventedFamilyIsAllowedTests(unittest.TestCase):
    """STRATEGY_FAMILIES.md calls the list discovery anchors, not a closed
    whitelist, and says the agent may invent families where the mechanism is
    plausible. Rejecting an unrecognised one would contradict that."""

    def test_an_unknown_family_is_reported_as_used(self):
        result = coverage([cycle("prediction_market_arbitrage")])
        self.assertIn("prediction_market_arbitrage", result["families_used"])

    def test_it_does_not_disturb_the_never_used_list(self):
        result = coverage([cycle("prediction_market_arbitrage")])
        self.assertEqual(len(result["families_never_used"]), len(KNOWN_FAMILIES))


class CoverageIsNotATargetTests(unittest.TestCase):
    """A family is not worth using because it is unused. The guidance has to
    say so, or this becomes a nudge toward novelty, which is a worse way to
    pick a strategy than habit."""

    def test_the_guidance_refuses_to_be_a_target(self):
        text = coverage([])["what_this_means"]
        self.assertIn("not a target", text)
        self.assertIn("may genuinely be right again today", text)

    def test_it_does_not_rank_or_recommend_a_family(self):
        result = coverage([cycle("hedging")])
        self.assertNotIn("suggested", result)
        self.assertNotIn("next", result)


class SpellingVariantsAreOneFamilyTests(unittest.TestCase):
    """The host wrote "source-disagreement" and "volatility-options"; the
    registry keys on underscores. Five families it had genuinely used were
    reported as never used, so it would have been told to go and explore
    what it had just explored."""

    def test_hyphens_fold_onto_underscores(self):
        self.assertEqual(families_used([cycle("source-disagreement")]),
                         {"source_disagreement": 1})

    def test_spaces_fold_too(self):
        self.assertEqual(families_used([cycle("volatility options")]),
                         {"volatility_options": 1})

    def test_variants_of_one_family_are_not_counted_twice(self):
        used = families_used([cycle("source-disagreement", "source_disagreement")])
        self.assertEqual(used, {"source_disagreement": 1})

    def test_a_folded_family_leaves_the_never_used_list(self):
        result = coverage([cycle("volatility-options")])
        self.assertNotIn("volatility_options", result["families_never_used"])


class CandidatesAreEnumeratedOnlyWhenAskedTests(unittest.TestCase):
    """discovery.py has produced candidates since it was written and nothing
    ever called it. It refuses to choose families itself, which is correct --
    the Research Director selects -- so this runs only for families the host
    named."""

    def snapshot_cycle(self, families=None):
        data = {"snapshot": {"attention_positions": [
            {"symbol": "MSFT", "market_value": 2000.0, "market_price": 490.0},
            {"symbol": "META", "market_value": 400.0, "market_price": 700.0}]}}
        if families is not None:
            data["families_to_explore"] = families
        return data

    def test_nothing_is_produced_unasked(self):
        from .strategy_coverage import candidates_for
        result = candidates_for([self.snapshot_cycle()])
        self.assertEqual(result["candidates"], [])

    def test_the_guidance_says_the_host_must_choose(self):
        from .strategy_coverage import candidates_for
        text = candidates_for([self.snapshot_cycle()])["what_this_means"]
        self.assertIn("will not choose", text)

    def test_requested_families_produce_candidates(self):
        from .strategy_coverage import candidates_for
        result = candidates_for([self.snapshot_cycle(["volatility_options"])])
        self.assertTrue(result["candidates"])
        self.assertTrue(all(c["family"] == "volatility_options"
                            for c in result["candidates"]))

    def test_hyphenated_requests_are_normalised(self):
        """The host writes hyphens; discovery keys on underscores."""
        from .strategy_coverage import candidates_for
        result = candidates_for([self.snapshot_cycle(["volatility-options"])])
        self.assertTrue(result["candidates"])

    def test_an_unknown_family_does_not_crash_the_cycle(self):
        from .strategy_coverage import candidates_for
        result = candidates_for([self.snapshot_cycle(["not_a_real_family"])])
        self.assertEqual(result["candidates"], [])
        self.assertIn("error", result)

    def test_no_positions_produces_nothing(self):
        from .strategy_coverage import candidates_for
        result = candidates_for([{"families_to_explore": ["hedging"],
                                  "snapshot": {}}])
        self.assertEqual(result["candidates"], [])


class RecentReasoningStagnationTests(unittest.TestCase):
    def reasoning_cycle(self, status):
        return {
            "as_of": "2026-09-16T00:00:00Z",
            "decision": {"status": status, "rationale": "reason"},
            "research": [{
                "question": "What changed?",
                "finding": "Observed evidence.",
                "tool_calls": [{"tool": "Web", "result": "ok"}],
            }],
        }

    def test_consecutive_waits_are_counted_across_full_history(self):
        result = recent_reasoning([
            self.reasoning_cycle("recommended"),
            self.reasoning_cycle("wait"),
            self.reasoning_cycle("wait"),
            self.reasoning_cycle("wait"),
        ])
        self.assertEqual(result["trailing_decision_status"], "wait")
        self.assertEqual(result["consecutive_same_status"], 3)

    def test_a_changed_decision_resets_the_run(self):
        result = recent_reasoning([
            self.reasoning_cycle("wait"),
            self.reasoning_cycle("wait"),
            self.reasoning_cycle("experiment"),
        ])
        self.assertEqual(result["trailing_decision_status"], "experiment")
        self.assertEqual(result["consecutive_same_status"], 1)

    def test_guidance_assigns_host_owned_parameters_to_the_host(self):
        text = recent_reasoning(
            [self.reasoning_cycle("wait")])["what_this_means"]
        self.assertIn("host-owned", text)
        self.assertIn("provisional", text)
        self.assertIn("bounded experiment", text)

    def test_an_experiment_contract_is_carried_forward_untruncated(self):
        data = self.reasoning_cycle("experiment")
        data["cycle_id"] = "cycle-exp"
        data["decision"]["experiment"] = {
            "hypothesis": "Fresh evidence resolves the uncertainty.",
            "mechanism": "Observe the named causal driver.",
            "measurement": "Compare the source metric.",
            "counter_metric": "Track the strongest adverse metric.",
            "evaluation_window": "next two accepted cycles",
            "rollback_condition": "Abandon if the driver does not move.",
        }
        result = recent_reasoning([data])
        self.assertEqual(
            result["cycles"][0]["experiment"]["hypothesis"],
            "Fresh evidence resolves the uncertainty.",
        )


class ResearchAgendaHistoryTests(unittest.TestCase):
    def agenda_cycle(self, cycle_id, instrument, family):
        return {
            "cycle_id": cycle_id,
            "as_of": "2026-09-17T12:00:00Z",
            "cognitive_stages": [{
                "stage_id": "research_director",
                "output": {
                    "research_agenda": {
                        "candidates": [
                            {
                                "candidate_id": "selected",
                                "instrument": instrument,
                                "strategy_family": family,
                                "trigger": "Fresh evidence.",
                                "selected": True,
                                "selection_reason": "Most relevant.",
                            },
                            {
                                "candidate_id": "rejected",
                                "instrument": "cash",
                                "strategy_family": "relative_value",
                                "trigger": "Alternative considered.",
                                "selected": False,
                                "rejection_reason": "Lower relevance.",
                            },
                        ],
                        "selection_rationale": "Selected current evidence.",
                    },
                },
            }],
        }

    def test_repeated_focus_is_counted_without_recommending_it(self):
        result = research_agenda_summary([
            self.agenda_cycle("one", "HOOD", "event_driven"),
            self.agenda_cycle("two", "HOOD", "event_driven"),
        ])
        self.assertEqual(
            result["selection_counts"]["HOOD|event_driven"],
            2,
        )
        self.assertIn("not instructions", result["what_this_means"])
        self.assertEqual(
            result["recent"][-1]["rejected"][0]["instrument"],
            "cash",
        )
        self.assertEqual(
            result["allocation_selection_counts"]["new_opportunity"],
            0,
        )
        self.assertEqual(result["allocation_cycles_examined"], 0)
        self.assertEqual(result["legacy_cycles_excluded"], 2)
        self.assertIsNone(result["recent"][-1]["allocation_usage"])
        self.assertIn(
            "not investment rankings",
            result["what_this_means"],
        )


class OpenExperimentTests(unittest.TestCase):
    def experiment_cycle(self, cycle_id, supersedes=()):
        return {
            "cycle_id": cycle_id,
            "as_of": "2026-09-16T00:00:00Z",
            "decision": {
                "status": "experiment",
                "rationale": "Resolve uncertainty.",
                "supersedes": list(supersedes),
                "experiment": {
                    "hypothesis": "h",
                    "mechanism": "m",
                    "measurement": "measure",
                    "counter_metric": "counter",
                    "evaluation_window": "two cycles",
                    "rollback_condition": "rollback",
                },
            },
        }

    def test_accepted_experiment_remains_visible(self):
        result = open_experiments([self.experiment_cycle("cycle-exp")])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["open"][0]["experiment_id"], "cycle-exp")

    def test_later_supersession_closes_the_experiment(self):
        later = {
            "decision": {
                "status": "wait",
                "rationale": "Experiment evaluated.",
                "supersedes": ["cycle-exp"],
            },
        }
        result = open_experiments([
            self.experiment_cycle("cycle-exp"),
            later,
        ])
        self.assertEqual(result["open"], [])
