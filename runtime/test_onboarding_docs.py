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
        self.assertIn("Sovereign Profile Host Cycle", text)
        self.assertIn("single profile validation/execution workflow", text)

    def test_profile_is_bootstrapped_dynamically_not_from_static_genesis(self):
        text = self.text()
        self.assertIn("Sovereign Profile Bootstrap", text)
        self.assertIn("creates a unique genesis", text)
        self.assertNotIn(
            "profile_templates/audit/genesis.jsonl\n  -> audit/genesis.jsonl",
            text,
        )

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


class ExistingProfileRepairIsAutonomousTests(unittest.TestCase):

    def text(self):
        return START_HERE.read_text(encoding="utf-8")

    def test_existing_profile_is_repaired_without_questions(self):
        text = " ".join(self.text().split())
        self.assertIn("one owner-authorized installer action", text)
        self.assertIn("Never ask the operator to name it or paste workflow YAML", text)

    def test_repair_preserves_operator_state(self):
        text = self.text()
        self.assertIn("Preserve every journal record, preference", text)
        self.assertIn("Never truncate\nor rebuild non-empty history", text)

    def test_invalid_genesis_is_not_silently_replaced(self):
        text = self.text()
        self.assertIn(
            "Do not replace a genesis because it looks hand-written",
            text,
        )
        self.assertIn("If it is invalid, stop", text)

    def test_repair_uses_explicit_installer_and_upgrade(self):
        text = self.text()
        self.assertIn("ops/install_profile_repo.py", text)
        self.assertIn("--upgrade-core", text)
        self.assertIn("Routine repair never changes a valid core pin", text)

    def test_nonempty_invalid_history_fails_closed(self):
        text = self.text()
        self.assertIn("Validate the journal", text)
        self.assertIn("exact integrity blocker", text)

    def test_new_profile_uses_private_template_and_ordinary_request(self):
        text = self.text()
        self.assertIn(
            "radostingg-eng/sovereign-research-profile-template",
            text,
        )
        self.assertIn("bootstrap/request.json", text)
        self.assertIn("with exactly `{}`", text)
        self.assertIn("with **private** visibility", text)

    def test_connector_never_writes_workflow_or_pastes_yaml(self):
        text = self.text()
        self.assertIn(
            "The research host does not create or update "
            "`.github/workflows/`",
            text,
        )
        self.assertIn("Do not bypass it", text)
        self.assertIn("ask the operator to paste YAML", text)

    def test_health_precedes_hourly_schedule(self):
        text = self.text()
        self.assertIn("Verify all of these before scheduling research", text)
        self.assertIn("python3 -P -m runtime.profile_health", text)
        self.assertIn("promotion policy and both feedback files", text)


class ProfileWorkflowNotificationTests(unittest.TestCase):

    def text(self):
        return START_HERE.read_text(encoding="utf-8")

    def test_profile_does_not_install_full_code_ci(self):
        text = " ".join(self.text().split())
        self.assertIn(
            "The private profile installs `Sovereign Profile Bootstrap` and",
            text,
        )
        self.assertIn(
            "It does not install the shared code repository's "
            "`Runtime Contract` workflow",
            text,
        )

    def test_expected_refusals_self_heal_without_failed_run(self):
        text = self.text()
        self.assertIn("A candidate refusal is a normal, recoverable result", text)
        self.assertIn("completes\nsuccessfully", text)
        self.assertIn("submits a corrected new candidate without asking", text)

    def test_unexpected_failures_remain_visible(self):
        text = self.text()
        self.assertIn("Unexpected failures remain failed", text)
        self.assertIn("Do not convert those failures into success-shaped", text)

    def test_email_can_be_disabled_without_disabling_workflow(self):
        text = self.text()
        self.assertIn(
            "Settings -> Notifications -> System -> Actions -> Don't notify",
            text,
        )
        self.assertIn("This changes notification delivery only", text)
        self.assertIn(
            "Do not ask the operator to choose a notification preference",
            text,
        )


if __name__ == "__main__":
    unittest.main()
