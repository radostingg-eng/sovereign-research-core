"""Silence should not read as health."""

import unittest
from datetime import datetime, timedelta, timezone

from .liveness import (
    check_liveness,
    consecutive_refusals,
    newest_receipt_age_hours,
)


def receipt(hours_ago):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"record_type": "cycle_receipt",
            "payload": {"completed_at": when.isoformat()}}


def refusal():
    return {"record_type": "host_input_refusal", "payload": {}}


class SilenceIsReportedTests(unittest.TestCase):
    """If the host stops committing, or the executor stops running, nothing
    else in this system would say so. Refusals go to the host, receipts go to
    the journal, integrity failures fail the gate. An absence reports to
    nobody."""

    def test_a_recent_cycle_is_healthy(self):
        self.assertEqual(check_liveness([receipt(0.5)]), [])

    def test_a_long_gap_is_reported(self):
        problems = check_liveness([receipt(9)])
        self.assertTrue([p for p in problems if p.startswith("no_cycle_in")])

    def test_never_having_run_is_reported(self):
        self.assertIn("no_cycle_receipt_has_ever_been_persisted",
                      check_liveness([]))

    def test_the_threshold_is_configurable_not_hardcoded(self):
        self.assertEqual(check_liveness([receipt(2)], max_age_hours=4), [])
        self.assertTrue(check_liveness([receipt(2)], max_age_hours=1))


class ARunOfRefusalsIsAStallTests(unittest.TestCase):
    """One refusal is the loop working: the host is told and corrects. An
    unbroken run means the correction is not landing, which is the failure
    that silence hides best."""

    def test_one_refusal_is_not_an_alarm(self):
        self.assertEqual(check_liveness([receipt(0.5), refusal()]), [])

    def test_a_run_of_refusals_is(self):
        records = [receipt(0.5), refusal(), refusal(), refusal()]
        self.assertTrue([p for p in check_liveness(records)
                         if "consecutive_refusals" in p])

    def test_a_receipt_clears_the_run(self):
        """Recovering is what the loop is supposed to do."""
        records = [refusal(), refusal(), refusal(), receipt(0.5)]
        self.assertEqual(consecutive_refusals(records), 0)
        self.assertEqual(check_liveness(records), [])

    def test_the_count_is_since_the_last_success_not_in_total(self):
        records = [refusal(), refusal(), receipt(0.5), refusal()]
        self.assertEqual(consecutive_refusals(records), 1)


class AgeIsReadFromTheReceiptTests(unittest.TestCase):

    def test_the_newest_receipt_wins(self):
        age = newest_receipt_age_hours([receipt(9), receipt(0.25), receipt(4)])
        self.assertLess(age, 1)

    def test_no_receipts_has_no_age(self):
        self.assertIsNone(newest_receipt_age_hours([refusal()]))

    def test_an_unparseable_timestamp_does_not_crash_the_check(self):
        """A monitor that dies on bad input reports nothing at all."""
        broken = {"record_type": "cycle_receipt",
                  "payload": {"completed_at": "not a date"}}
        self.assertIsNone(newest_receipt_age_hours([broken]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
