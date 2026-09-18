"""Cold start must install the host schedule, not only create directories."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
START_HERE = ROOT / "START_HERE.md"
README = ROOT / "README.md"


class ScheduledHostTaskIsPartOfSetupTests(unittest.TestCase):

    def text(self):
        return START_HERE.read_text(encoding="utf-8")

    def test_setup_names_one_stable_task(self):
        text = self.text()
        self.assertIn("Sovereign Research hourly cycle", text)
        self.assertIn("exactly one enabled task", text)

    def test_setup_uses_part_b_not_bootstrap_part_a(self):
        text = self.text()
        self.assertIn("Its standing instruction is **Part B only**", text)
        self.assertIn("Do not include Part A", text)

    def test_setup_requires_hourly_cadence_and_future_next_run(self):
        text = self.text()
        self.assertIn("its cadence is hourly", text)
        self.assertIn("its next run is in the future", text)

    def test_setup_does_not_execute_a_cycle(self):
        text = " ".join(self.text().split())
        self.assertIn("it has not run as a side effect of setup", text)
        self.assertIn("Do not run a research cycle in the same turn", text)

    def test_missing_scheduler_fails_loudly(self):
        text = self.text()
        self.assertIn("If scheduled tasks are unavailable", text)
        self.assertIn("I have not started a research cycle", text)

    def test_task_reads_profile_and_pinned_core(self):
        text = self.text()
        self.assertIn(
            "the operator's private `sovereign-research-profile` repository",
            text,
        )
        self.assertIn("the pinned `sovereign-research-core` commit", text)

    def test_profile_workflow_processes_hourly_host_output(self):
        text = self.text()
        self.assertIn("profile_templates/.github/workflows/host-cycle.yml", text)
        self.assertIn("Sovereign Profile Host Cycle", text)
        self.assertIn("executes only accepted", text)

    def test_genesis_is_copied_with_its_hash_not_rebuilt_from_prose(self):
        text = self.text()
        self.assertIn("profile_templates/audit/genesis.jsonl", text)
        self.assertIn("Copy these template files byte-for-byte", text)
        self.assertIn("computed `record_hash`", text)

    def test_readme_promises_the_same_cold_start(self):
        text = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("one enabled hourly ChatGPT task", text)
        self.assertIn("Part B of the pinned standing prompt", text)


class SetupDoesNotInterviewTheOperatorTests(unittest.TestCase):

    def text(self):
        return START_HERE.read_text(encoding="utf-8")

    def test_setup_explicitly_asks_no_preference_questions(self):
        text = self.text()
        self.assertIn("Do not ask preference questions", text)
        self.assertIn("Do not ask a follow-up question", text)

    def test_missing_preferences_are_valid_state(self):
        text = self.text()
        self.assertIn("No explicit operator preferences recorded yet", text)
        self.assertIn("It is valid state, not a setup failure", text)

    def test_volunteered_preferences_are_captured_without_prompting(self):
        text = self.text()
        self.assertIn("already volunteered", text)
        self.assertIn("without turning every conversation into an", text)
        self.assertIn("intake interview", text)

    def test_true_blockers_are_explicit_and_bounded(self):
        text = self.text()
        self.assertIn("The only setup blockers are", text)
        self.assertIn("GitHub is unavailable", text)
        self.assertIn("Interactive Brokers is unavailable", text)
        self.assertIn("scheduled tasks are unavailable", text)

    def test_optional_web_search_does_not_block_setup(self):
        text = self.text()
        self.assertIn("A missing optional", text)
        self.assertIn("is not a blocker", text)

    def test_first_cycle_runs_from_schedule_not_another_question(self):
        text = self.text()
        self.assertIn("starts automatically at the verified next", text)


if __name__ == "__main__":
    unittest.main()
