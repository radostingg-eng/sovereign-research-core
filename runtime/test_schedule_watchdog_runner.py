import json
import unittest
from unittest.mock import patch

from ops.run_schedule_watchdog import main


class ScheduleWatchdogRunnerTests(unittest.TestCase):
    def test_missing_slots_are_published_without_process_failure(self):
        result = {
            "healthy": False,
            "current_incidents": [{
                "slot": "2026-09-22T09:57:00+00:00",
                "status": "missing",
            }],
            "configuration_problems": [],
        }
        with patch(
            "ops.run_schedule_watchdog.account_schedule",
            return_value=result,
        ):
            self.assertEqual(
                main(["--profile-root", ".", "--workflow-version", "2"]),
                0,
            )

    def test_configuration_problem_fails_closed(self):
        result = {
            "healthy": False,
            "current_incidents": [],
            "configuration_problems": [
                "schedule_configuration_mismatch:effective_core_commit",
            ],
        }
        with patch(
            "ops.run_schedule_watchdog.account_schedule",
            return_value=result,
        ):
            self.assertEqual(
                main(["--profile-root", ".", "--workflow-version", "2"]),
                1,
            )

    def test_runtime_error_fails_closed(self):
        with patch(
            "ops.run_schedule_watchdog.account_schedule",
            side_effect=ValueError("schedule_event_chain_invalid"),
        ):
            self.assertEqual(
                main(["--profile-root", ".", "--workflow-version", "2"]),
                1,
            )

    def test_output_retains_health_and_incident_details(self):
        result = {
            "healthy": False,
            "current_incidents": [{"status": "missing"}],
            "configuration_problems": [],
        }
        with (
            patch(
                "ops.run_schedule_watchdog.account_schedule",
                return_value=result,
            ),
            patch("builtins.print") as output,
        ):
            self.assertEqual(
                main(["--profile-root", ".", "--workflow-version", "2"]),
                0,
            )
        encoded = output.call_args.args[0]
        self.assertEqual(json.loads(encoded), result)


if __name__ == "__main__":
    unittest.main()
