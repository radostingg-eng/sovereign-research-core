"""A bug report must be publishable without publishing a portfolio."""

import unittest

from .contribution import (
    PrivateEvidenceFound,
    SIGNAL_FIELDS,
    check_report,
    check_signal,
    render_issue,
    render_signal_issue,
    scan,
    signal_fingerprint,
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


def _clean_signal(**over):
    signal = {
        "kind": "runtime_refusal",
        "core_commit": "a" * 40,
        "error_code": "missing_research",
        "occurrence_bucket": "2-5",
        "synthetic_test_needed": True,
    }
    signal.update(over)
    return signal


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


class AutomaticHostSignalsContainNoPrivateNarrativeTests(unittest.TestCase):

    def test_a_known_runtime_code_is_valid(self):
        self.assertEqual(check_signal(_clean_signal()), [])

    def test_signal_shape_is_exact(self):
        signal = _clean_signal(notes="helpful private context")
        self.assertIn("unexpected_signal_field:notes", check_signal(signal))
        self.assertEqual(set(_clean_signal()), SIGNAL_FIELDS)

    def test_core_commit_must_be_a_full_lowercase_sha(self):
        for value in ("abc123", "A" * 40, "g" * 40):
            with self.subTest(value=value):
                self.assertIn(
                    "invalid_core_commit",
                    check_signal(_clean_signal(core_commit=value)),
                )

    def test_error_code_must_be_known_to_the_shared_runtime(self):
        self.assertIn(
            "unknown_signal_code",
            check_signal(_clean_signal(
                error_code="smci_weight_034_private_context",
            )),
        )

    def test_kind_and_occurrence_are_enums(self):
        self.assertIn(
            "invalid_signal_kind",
            check_signal(_clean_signal(kind="portfolio_story")),
        )
        self.assertIn(
            "invalid_occurrence_bucket",
            check_signal(_clean_signal(occurrence_bucket="17")),
        )

    def test_synthetic_test_flag_is_really_boolean(self):
        self.assertIn(
            "synthetic_test_needed_not_boolean",
            check_signal(_clean_signal(synthetic_test_needed="yes")),
        )

    def test_fingerprint_dedupes_by_commit_and_code(self):
        first = signal_fingerprint(_clean_signal(
            kind="runtime_refusal",
            occurrence_bucket="1",
        ))
        repeated = signal_fingerprint(_clean_signal(
            kind="runtime_error",
            occurrence_bucket="6+",
            synthetic_test_needed=False,
        ))
        self.assertEqual(first, repeated)
        self.assertNotEqual(
            first,
            signal_fingerprint(_clean_signal(
                error_code="missing_decision",
            )),
        )

    def test_rendered_signal_has_no_free_text_fields(self):
        title, body = render_signal_issue(_clean_signal())
        self.assertEqual(title, "[host-signal] missing_research")
        self.assertIn("Signal fingerprint", body)
        self.assertIn("No cycle IDs", body)
        self.assertNotIn("observed_behavior", body)
        self.assertNotIn("synthetic_reproduction", body)

    def test_invalid_signal_cannot_render(self):
        with self.assertRaises(PrivateEvidenceFound):
            render_signal_issue(_clean_signal(
                error_code="position_crwv_4500_shares",
            ))


if __name__ == "__main__":
    unittest.main()
