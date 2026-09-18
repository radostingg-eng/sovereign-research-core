"""A bug report must be publishable without publishing a portfolio."""

import unittest

from .contribution import (
    PrivateEvidenceFound,
    check_report,
    render_issue,
    scan,
)


def _clean_report(**over):
    report = {
        "violated_contract": "rediscovered candidates must require "
                             "rediscovery_of",
        "expected_behavior": "The second proposal is refused without a "
                             "rediscovery_of reference.",
        "observed_behavior": "The second proposal was accepted as new.",
        "synthetic_reproduction": {
            "cycle_one": {"candidate_id": "scout-acme-one"},
            "cycle_two": {"candidate_id": "scout-acme-two"},
        },
    }
    report.update(over)
    return report


class PrivateEvidenceIsCaughtTests(unittest.TestCase):

    def test_a_cycle_id_is_private(self):
        self.assertTrue(scan("failed in cycle-20260918T073638Z-goala3"))

    def test_a_journal_record_id_is_private(self):
        self.assertTrue(scan("opportunity-event:opportunity-vst-power-1"))

    def test_an_account_number_is_private(self):
        self.assertTrue(scan("account U1234567 rejected the order"))

    def test_a_money_amount_is_private(self):
        self.assertTrue(scan("position was worth $2,058,760"))

    def test_a_bare_position_size_is_private(self):
        self.assertTrue(scan("net liquidation 2,058,760.42"))

    def test_a_home_path_is_private(self):
        self.assertTrue(scan("/Users/someone/.local/share/profile"))

    def test_a_profile_repo_name_is_private(self):
        self.assertTrue(scan("pushed to sovereign-research-radostin"))

    def test_a_github_token_is_a_credential(self):
        self.assertTrue(scan("ghp_abcdefghijklmnop1234"))

    def test_a_signed_url_query_is_a_credential(self):
        self.assertTrue(scan("https://example.com/x?token=abc123secret"))

    def test_the_core_repo_name_is_not_private(self):
        self.assertEqual(scan("sovereign-research-core"), [])

    def test_a_bare_ticker_is_not_private(self):
        self.assertEqual(scan("the ACME candidate was rejected"), [])

    def test_a_small_number_is_not_private(self):
        self.assertEqual(scan("quantity 6, limit 10.0"), [])


class ReportShapeTests(unittest.TestCase):

    def test_a_clean_report_has_no_problems(self):
        self.assertEqual(check_report(_clean_report()), [])

    def test_a_missing_field_is_reported(self):
        report = _clean_report()
        del report["expected_behavior"]
        self.assertIn(
            "missing_field:expected_behavior", check_report(report),
        )

    def test_an_empty_field_is_treated_as_missing(self):
        self.assertIn(
            "missing_field:observed_behavior",
            check_report(_clean_report(observed_behavior="   ")),
        )

    def test_an_unexpected_field_is_reported(self):
        self.assertIn(
            "unexpected_field:portfolio_snapshot",
            check_report(_clean_report(portfolio_snapshot={"cash": 1})),
        )


class RenderingFailsClosedTests(unittest.TestCase):

    def test_a_clean_report_renders(self):
        body = render_issue(_clean_report())
        self.assertIn("Violated contract", body)
        self.assertIn("scout-acme-one", body)

    def test_a_report_carrying_a_cycle_id_is_refused(self):
        report = _clean_report(
            observed_behavior="cycle-20260918T073638Z-goala3 was accepted",
        )
        with self.assertRaises(PrivateEvidenceFound):
            render_issue(report)

    def test_private_evidence_nested_in_the_reproduction_is_refused(self):
        report = _clean_report(synthetic_reproduction={
            "cycle_one": {"candidate_id": "scout-acme-one",
                          "account": "U1234567"},
        })
        with self.assertRaises(PrivateEvidenceFound):
            render_issue(report)

    def test_refusal_names_what_was_found(self):
        report = _clean_report(notes="see /Users/someone/profile/audit")
        with self.assertRaises(PrivateEvidenceFound) as caught:
            render_issue(report)
        self.assertIn("home_path", str(caught.exception))

    def test_a_missing_field_blocks_rendering(self):
        report = _clean_report()
        del report["violated_contract"]
        with self.assertRaises(PrivateEvidenceFound):
            render_issue(report)

    def test_optional_fields_are_rendered_when_present(self):
        body = render_issue(_clean_report(
            core_version="abc1234",
            suggested_test="assert the second proposal is refused",
        ))
        self.assertIn("abc1234", body)
        self.assertIn("Suggested test", body)


if __name__ == "__main__":
    unittest.main()
