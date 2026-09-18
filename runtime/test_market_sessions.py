import unittest

from .market_sessions import derive_overlap, validate_market_sessions


def market_sessions(eu_open=True, us_open=True):
    return {
        "observed_at": "2026-09-16T14:00:00Z",
        "markets": [
            {
                "region": "EU",
                "venue": "XETRA",
                "timezone": "Europe/Berlin",
                "local_time": "2026-09-16T16:00:00+02:00",
                "status": "open" if eu_open else "closed",
                "is_open": eu_open,
                "next_open": "2026-09-17T09:00:00+02:00",
                "next_close": (
                    "2026-09-16T17:30:00+02:00"
                    if eu_open
                    else "2026-09-17T17:30:00+02:00"
                ),
                "evidence": [{
                    "tool": "official Xetra calendar",
                    "result": {"is_open": eu_open},
                }],
            },
            {
                "region": "US",
                "venue": "NYSE",
                "timezone": "America/New_York",
                "local_time": "2026-09-16T10:00:00-04:00",
                "status": "open" if us_open else "closed",
                "is_open": us_open,
                "next_open": "2026-09-17T09:30:00-04:00",
                "next_close": (
                    "2026-09-16T16:00:00-04:00"
                    if us_open
                    else "2026-09-17T16:00:00-04:00"
                ),
                "evidence": [{
                    "tool": "Alpaca get clock",
                    "result": {"is_open": us_open},
                }],
            },
        ],
        "overlap": (
            "both_open" if eu_open and us_open
            else "eu_only" if eu_open
            else "us_only" if us_open
            else "none_open"
        ),
    }


class MarketSessionValidationTests(unittest.TestCase):
    def test_both_markets_open(self):
        value = market_sessions()
        self.assertEqual(validate_market_sessions(value), [])
        self.assertEqual(derive_overlap(value["markets"]), "both_open")

    def test_each_overlap_state_is_derived(self):
        self.assertEqual(
            derive_overlap(market_sessions(True, False)["markets"]),
            "eu_only",
        )
        self.assertEqual(
            derive_overlap(market_sessions(False, True)["markets"]),
            "us_only",
        )
        self.assertEqual(
            derive_overlap(market_sessions(False, False)["markets"]),
            "none_open",
        )

    def test_local_time_must_match_iana_timezone(self):
        value = market_sessions()
        value["markets"][0]["local_time"] = "2026-09-16T15:00:00+02:00"
        self.assertIn(
            "market_session_local_time_mismatch:0",
            validate_market_sessions(value),
        )

    def test_local_time_offset_must_match_iana_timezone(self):
        value = market_sessions()
        value["markets"][0]["local_time"] = "2026-09-16T15:00:00+01:00"
        self.assertIn(
            "market_session_local_time_offset_mismatch:0",
            validate_market_sessions(value),
        )

    def test_observation_must_belong_to_the_same_cycle(self):
        self.assertIn(
            "market_sessions_observed_at_mismatch",
            validate_market_sessions(
                market_sessions(),
                expected_at="2026-09-16T15:00:00Z",
            ),
        )

    def test_holidays_are_closed(self):
        value = market_sessions(False, False)
        value["markets"][0]["status"] = "holiday"
        value["markets"][1]["status"] = "holiday"
        self.assertEqual(validate_market_sessions(value), [])

    def test_overlap_must_match_market_flags(self):
        value = market_sessions()
        value["overlap"] = "none_open"
        self.assertIn(
            "market_session_overlap_mismatch:none_open!=both_open",
            validate_market_sessions(value),
        )

    def test_next_session_times_must_be_future_and_ordered(self):
        value = market_sessions()
        value["markets"][0]["next_close"] = "2026-09-17T17:30:00+02:00"
        self.assertIn(
            "market_session_open_sequence_invalid:0",
            validate_market_sessions(value),
        )
        value = market_sessions(False, False)
        value["markets"][0]["next_open"] = "2026-09-15T09:00:00+02:00"
        errors = validate_market_sessions(value)
        self.assertIn("market_session_next_open_not_future:0", errors)
        value = market_sessions(False, False)
        value["markets"][0]["next_open"] = "2026-09-18T09:00:00+02:00"
        errors = validate_market_sessions(value)
        self.assertIn("market_session_closed_sequence_invalid:0", errors)

    def test_non_regular_sessions_are_not_reported_open(self):
        for status in ("pre_market", "post_market", "holiday", "unknown"):
            with self.subTest(status=status):
                value = market_sessions()
                value["markets"][0]["status"] = status
                self.assertIn(
                    "market_session_closed_status_mismatch:0",
                    validate_market_sessions(value),
                )

    def test_both_regions_and_source_evidence_are_required(self):
        value = market_sessions()
        value["markets"] = value["markets"][:1]
        value["markets"][0]["evidence"] = []
        errors = validate_market_sessions(value)
        self.assertIn("market_session_region_missing:US", errors)
        self.assertIn("market_session_evidence_required:0", errors)


if __name__ == "__main__":
    unittest.main()
