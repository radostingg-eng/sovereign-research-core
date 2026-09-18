import unittest

from .ibkr_backfill import (
    BackfillResult, coverage_report, evaluate_host_backfill,
    normalize_trades,
)


def trade(trade_id, trade_time, **over):
    row = {"trade_id": trade_id, "trade_time": trade_time, "symbol": "MSFT",
           "quantity": 10, "price": 100.0}
    row.update(over)
    return row


class CoverageSpanTests(unittest.TestCase):
    """Completeness must mean the data SPANS the claimed period.

    It used to mean "some data exists". One day of 2026 trades against a
    2012 inception therefore reported connector-complete: a single day
    standing in for fourteen years.

    The operator has been repeatedly explicit that a bounded connector
    window must never be described as lifetime coverage, and that account
    inception stays unknown until an authoritative statement establishes
    it. This was the code contradicting that.
    """

    INCEPTION = "2012-01-01"
    CUTOFF = "2026-09-16"

    def report(self, trades, performance):
        result = normalize_trades({"w1": trades}, inception=self.INCEPTION,
                                  cutoff=self.CUTOFF)
        return coverage_report(inception=self.INCEPTION, cutoff=self.CUTOFF,
                               trade_result=result, performance_rows=performance)

    def test_one_day_of_data_is_not_fourteen_years_of_coverage(self):
        report = self.report([trade("t1", "2026-09-15T10:00:00Z")],
                             [{"date": "20260915", "nav": 1000.0}])
        self.assertEqual(report["status"], "connector_partial")
        self.assertFalse(report["covers_claimed_period"])

    def test_the_gap_at_the_start_is_quantified(self):
        # Naming the gap is what stops "partial" being read as "nearly all".
        report = self.report([trade("t1", "2026-09-15T10:00:00Z")],
                             [{"date": "20260915", "nav": 1000.0}])
        self.assertGreater(report["gap_at_start_days"], 5000)

    def test_data_spanning_the_period_verifies_the_span_only(self):
        """Renamed from "is_complete", because it never was.

        Reaching both endpoints proves the span was observed. It says nothing
        about what between them was omitted, and the old status name said
        "connector_complete" anyway.
        """
        report = self.report(
            [trade("a", "2012-01-01T10:00:00Z"), trade("b", "2026-09-16T10:00:00Z")],
            [{"date": "20120101", "nav": 1.0}, {"date": "20260916", "nav": 2.0}])
        self.assertEqual(report["status"], "span_verified_completeness_unverified")
        self.assertTrue(report["covers_claimed_period"])
        self.assertLessEqual(report["gap_at_start_days"], 0)

    def test_no_data_at_all_is_blocked_not_partial(self):
        # "blocked" and "partial" are different: partial data is usable for
        # recent analysis, it just is not lifetime history.
        self.assertEqual(self.report([], [])["status"], "blocked")

    def test_partial_is_distinct_from_blocked(self):
        partial = self.report([trade("t1", "2026-09-15T10:00:00Z")],
                              [{"date": "20260915", "nav": 1000.0}])["status"]
        self.assertNotEqual(partial, "blocked")

    def test_statement_reconciliation_cannot_upgrade_partial_coverage(self):
        # A statement cannot vouch for data the connector never returned.
        result = normalize_trades({"w1": [trade("t1", "2026-09-15T10:00:00Z")]},
                                  inception=self.INCEPTION, cutoff=self.CUTOFF)
        report = coverage_report(inception=self.INCEPTION, cutoff=self.CUTOFF,
                                 trade_result=result,
                                 performance_rows=[{"date": "20260915", "nav": 1.0}],
                                 statement_reconciled={"source": "IBKR Flex activity statement",
                                  "covers_from": "2012-01-01",
                                  "covers_to": "2026-09-16"})
        self.assertEqual(report["status"], "connector_partial")

    def test_missing_recent_data_is_also_partial(self):
        report = self.report([trade("a", "2012-01-01T10:00:00Z")],
                             [{"date": "20120101", "nav": 1.0}])
        self.assertEqual(report["status"], "connector_partial")
        self.assertGreater(report["gap_at_end_days"], 5000)


class ConflictingDuplicateTests(unittest.TestCase):
    """A repeated trade_id carrying DIFFERENT values means a source is wrong.

    Dedup kept the first window's version and discarded the second without
    comparing them, so a genuine data conflict was resolved silently by
    "first window wins".
    """

    def normalize(self, windows):
        return normalize_trades(windows, inception="2012-01-01", cutoff="2026-09-16")

    def test_a_conflicting_duplicate_is_reported(self):
        result = self.normalize({
            "w1": [trade("t1", "2026-09-15T10:00:00Z", quantity=10, price=100.0)],
            "w2": [trade("t1", "2026-09-15T10:00:00Z", quantity=999, price=555.0)],
        })
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0]["trade_id"], "t1")

    def test_the_conflicting_fields_are_named(self):
        result = self.normalize({
            "w1": [trade("t1", "2026-09-15T10:00:00Z", quantity=10, price=100.0)],
            "w2": [trade("t1", "2026-09-15T10:00:00Z", quantity=999, price=555.0)],
        })
        self.assertEqual(set(result.conflicts[0]["fields"]), {"quantity", "price"})

    def test_both_sides_of_the_conflict_are_preserved(self):
        # Which value was kept and which discarded is the whole point.
        result = self.normalize({
            "w1": [trade("t1", "2026-09-15T10:00:00Z", quantity=10)],
            "w2": [trade("t1", "2026-09-15T10:00:00Z", quantity=999)],
        })
        field = result.conflicts[0]["fields"]["quantity"]
        self.assertEqual(field["kept"], 10)
        self.assertEqual(field["discarded"], 999)

    def test_an_identical_overlap_is_not_a_conflict(self):
        # Windows legitimately overlap. Only disagreement matters.
        result = self.normalize({
            "w1": [trade("t1", "2026-09-15T10:00:00Z")],
            "w2": [trade("t1", "2026-09-15T10:00:00Z")],
        })
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.conflicts, ())

    def test_conflicts_surface_in_the_coverage_report(self):
        result = self.normalize({
            "w1": [trade("t1", "2026-09-15T10:00:00Z", price=100.0)],
            "w2": [trade("t1", "2026-09-15T10:00:00Z", price=555.0)],
        })
        report = coverage_report(inception="2012-01-01", cutoff="2026-09-16",
                                 trade_result=result,
                                 performance_rows=[{"date": "20260915", "nav": 1.0}])
        self.assertEqual(len(report["trade_conflicts"]), 1)


class CoverageIsPerDatasetTests(unittest.TestCase):
    """A span must come from one series, not two endpoints stitched together.

    coverage_report took the earliest of both datasets and the latest of both
    datasets, so one 2012 performance point and one 2026 trade spanned
    fourteen years between them while each dataset held exactly one row. That
    is the same "a single day standing in for fourteen years" the span check
    was written to stop, reassembled from two sources instead of one.
    """

    INCEPTION = "2012-01-01"
    CUTOFF = "2026-09-16"

    def report(self, trades, performance):
        result = normalize_trades({"w1": trades}, inception=self.INCEPTION,
                                  cutoff=self.CUTOFF)
        return coverage_report(inception=self.INCEPTION, cutoff=self.CUTOFF,
                               trade_result=result, performance_rows=performance)

    def test_endpoints_from_different_datasets_do_not_make_a_span(self):
        report = self.report([trade("t1", "2026-09-15T10:00:00Z")],
                             [{"date": "20120101", "nav": 1.0}])
        self.assertEqual(report["status"], "connector_partial")
        self.assertFalse(report["covers_claimed_period"])

    def test_each_dataset_reports_its_own_span(self):
        report = self.report([trade("t1", "2026-09-15T10:00:00Z")],
                             [{"date": "20120101", "nav": 1.0}])
        self.assertFalse(report["trades_span_claimed_period"])
        self.assertFalse(report["performance_spans_claimed_period"])

    def test_trades_spanning_alone_is_not_enough(self):
        report = self.report(
            [trade("a", "2012-01-01T10:00:00Z"), trade("b", "2026-09-16T10:00:00Z")],
            [{"date": "20200101", "nav": 1.0}])
        self.assertTrue(report["trades_span_claimed_period"])
        self.assertFalse(report["performance_spans_claimed_period"])
        self.assertEqual(report["status"], "connector_partial")

    def test_both_datasets_spanning_is_complete(self):
        report = self.report(
            [trade("a", "2012-01-01T10:00:00Z"), trade("b", "2026-09-16T10:00:00Z")],
            [{"date": "20120101", "nav": 1.0}, {"date": "20260916", "nav": 2.0}])
        self.assertTrue(report["covers_claimed_period"])


class CutoffDateIncludesItsOwnDayTests(unittest.TestCase):
    """A bare date cutoff means the end of that day.

    Trades were compared against the cutoff parsed to midnight, so every
    trade made ON the cutoff date was filtered out, while
    normalize_performance compares date strings and keeps the same day. The
    two datasets disagreed about whether the final day counted, and trade
    coverage could never reach a same-day cutoff at all.
    """

    def test_a_trade_on_the_cutoff_date_is_kept(self):
        result = normalize_trades({"w1": [trade("a", "2026-09-16T10:00:00Z")]},
                                  inception="2012-01-01", cutoff="2026-09-16")
        self.assertEqual(result.filtered_after_cutoff, 0)
        self.assertEqual(len(result.records), 1)

    def test_a_trade_after_the_cutoff_date_is_still_filtered(self):
        result = normalize_trades({"w1": [trade("a", "2026-09-17T00:00:01Z")]},
                                  inception="2012-01-01", cutoff="2026-09-16")
        self.assertEqual(result.filtered_after_cutoff, 1)

    def test_an_explicit_timestamp_cutoff_is_respected_exactly(self):
        result = normalize_trades({"w1": [trade("a", "2026-09-16T10:00:00Z")]},
                                  inception="2012-01-01",
                                  cutoff="2026-09-16T09:00:00Z")
        self.assertEqual(result.filtered_after_cutoff, 1)


class AConflictedHistoryIsNotCompleteTests(unittest.TestCase):
    """The report listed unresolved conflicts and still said complete.

    An unresolved conflict is two records claiming to be the same trade with
    different contents. One of them is wrong and nothing here can say which,
    so the history is contradictory at that point. Saying both "these data
    disagree" and "this period is fully covered" in one report lets the
    second sentence be quoted without the first.
    """

    def report(self, conflicts):
        return coverage_report(
            inception="2012-01-01", cutoff="2026-09-16",
            trade_result=BackfillResult(
                records=({"a": 1}, {"b": 2}), duplicate_count=0,
                filtered_before_inception=0, filtered_after_cutoff=0,
                earliest="2012-01-01", latest="2026-09-16",
                conflicts=conflicts),
            performance_rows=[{"date": "2012-01-01"}, {"date": "2026-09-16"}],
            statement_reconciled={"source": "IBKR Flex activity statement",
                                  "covers_from": "2012-01-01",
                                  "covers_to": "2026-09-16"})

    def test_an_unresolved_conflict_blocks_completeness(self):
        report = self.report(({"id": "x", "reason": "same id, different amount"},))
        self.assertEqual(report["status"], "connector_conflicted")
        self.assertFalse(report["covers_claimed_period"])

    def test_the_conflict_is_still_reported(self):
        """Blocking the claim must not hide the evidence for it."""
        report = self.report(({"id": "x", "reason": "same id, different amount"},))
        self.assertEqual(len(report["trade_conflicts"]), 1)

    def test_clean_data_is_still_complete(self):
        report = self.report(())
        self.assertEqual(report["status"], "complete_with_statement")
        self.assertTrue(report["covers_claimed_period"])


class CompletenessIsAssessedNotDerivedTests(unittest.TestCase):
    """Two endpoints cannot establish completeness.

    Reaching 2012 and 2026 proves the SPAN was observed, not that nothing
    between them was omitted, and no observations-per-year constant fixes
    that: two trades could genuinely be an account's entire history. The
    missing thing is evidence, not data points.

    statement_reconciled was also a bare boolean with no production caller,
    so the strongest claim this module makes rested on a flag anyone could
    pass as True. Same shape as sandbox_passed and order_submission_used.
    """

    def report(self, claim):
        return coverage_report(
            inception="2012-01-01", cutoff="2026-09-16",
            trade_result=BackfillResult(
                records=({"a": 1}, {"b": 2}), duplicate_count=0,
                filtered_before_inception=0, filtered_after_cutoff=0,
                earliest="2012-01-01", latest="2026-09-16", conflicts=()),
            performance_rows=[{"date": "2012-01-01"}, {"date": "2026-09-16"}],
            statement_reconciled=claim)

    def test_an_asserted_boolean_does_not_buy_completeness(self):
        self.assertEqual(self.report(True)["status"],
                         "span_verified_completeness_unverified")

    def test_no_claim_at_all_reports_the_same(self):
        self.assertEqual(self.report(None)["status"],
                         "span_verified_completeness_unverified")

    def test_cited_statement_evidence_does(self):
        report = self.report({"source": "IBKR Flex activity statement",
                              "covers_from": "2012-01-01",
                              "covers_to": "2026-09-16"})
        self.assertEqual(report["status"], "complete_with_statement")
        self.assertTrue(report["completeness_assessed_by_host"])

    def test_partial_evidence_is_not_evidence(self):
        """A source with no period says nothing about what was omitted."""
        self.assertEqual(
            self.report({"source": "IBKR Flex activity statement"})["status"],
            "span_verified_completeness_unverified")

    def test_the_span_itself_is_still_reported(self):
        """Refusing the completeness claim must not hide what WAS observed."""
        report = self.report(None)
        self.assertTrue(report["covers_claimed_period"])
        self.assertEqual(report["observed_earliest"], "2012-01-01")
        self.assertEqual(report["observed_latest"], "2026-09-16")


class HostBackfillAdapterTests(unittest.TestCase):
    def payload(self, **over):
        value = {
            "inception": "2026-09-15",
            "cutoff": "2026-09-16",
            "trade_windows": {
                "1D": [trade("t1", "2026-09-16T10:00:00Z")],
            },
            "performance": {
                "dates": ["20260915", "20260916"],
                "nav": [1000.0, 1005.0],
                "cps": [0.0, 0.005],
            },
            "statement_evidence": None,
        }
        value.update(over)
        return value

    def test_host_fetched_data_is_normalized(self):
        result = evaluate_host_backfill(self.payload())
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(len(result["performance"]), 2)

    def test_missing_parallel_performance_data_is_reported(self):
        value = self.payload()
        value["performance"]["nav"] = [1000.0]
        result = evaluate_host_backfill(value)
        self.assertFalse(result["valid"])
        self.assertIn(
            "performance_parallel_arrays_mismatch",
            result["problems"][0],
        )

    def test_the_adapter_does_not_invent_completeness(self):
        result = evaluate_host_backfill(self.payload())
        self.assertEqual(
            result["coverage"]["status"],
            "connector_partial",
        )
