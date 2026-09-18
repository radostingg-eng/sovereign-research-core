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
        text = self.text()
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

    def test_readme_promises_the_same_cold_start(self):
        text = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("one enabled hourly ChatGPT task", text)
        self.assertIn("Part B of the pinned standing prompt", text)


if __name__ == "__main__":
    unittest.main()
