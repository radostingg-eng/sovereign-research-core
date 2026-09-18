import unittest

from .engine import (SourceObservation, arbitrate, backtest_long_only, calibration_summary,
                     dangling_causes, evaluate_backtests,
                     evaluate_source_arbitrations, make_record, mutate_strategy,
                     validate_strategy, verify_chain, walk_forward_splits)
from .portfolio import (assignment_by_expiry_bucket, assignment_ledger, normalized_assignment_total,
                        risk_capacity, stress_nav)
from .opportunity import covered_call_opportunity


class MakeRecordWriteGuardTests(unittest.TestCase):
    """The write site must refuse a malformed prev_hash.

    A host once wrote a prev_hash truncated to 63 characters. Because
    chain order is rebuilt by FOLLOWING prev_hash, that one dropped
    character orphaned six downstream records and surfaced four hops from
    the culprit. Catching it in the reader is necessary; refusing it at
    the writer is what stops it being written at all.
    """

    def test_well_formed_prev_hash_is_accepted(self):
        record = make_record("r", "finding", "t", {}, prev_hash="a" * 64)
        self.assertEqual(record["prev_hash"], "a" * 64)

    def test_none_is_accepted_for_a_chain_root(self):
        self.assertIsNone(make_record("r", "finding", "t", {})["prev_hash"])

    def test_prev_hash_truncated_by_one_char_is_refused(self):
        # The exact incident.
        with self.assertRaises(ValueError) as ctx:
            make_record("r", "finding", "t", {}, prev_hash="a" * 63)
        self.assertIn("malformed_prev_hash", str(ctx.exception))
        self.assertIn("63", str(ctx.exception))

    def test_uppercase_hex_is_refused(self):
        with self.assertRaises(ValueError):
            make_record("r", "finding", "t", {}, prev_hash="A" * 64)

    def test_non_hex_is_refused(self):
        with self.assertRaises(ValueError):
            make_record("r", "finding", "t", {}, prev_hash="z" * 64)

    def test_short_placeholder_is_refused(self):
        # "deadbeef" reads like a valid hash and is not one.
        with self.assertRaises(ValueError):
            make_record("r", "finding", "t", {}, prev_hash="deadbeef")

    def test_refusal_names_the_record_so_the_writer_is_identifiable(self):
        with self.assertRaises(ValueError) as ctx:
            make_record("cycle-receipt:abc", "finding", "t", {}, prev_hash="a")
        self.assertIn("cycle-receipt:abc", str(ctx.exception))


class RuntimeTests(unittest.TestCase):
    def base_strategy(self):
        return {"strategy_id":"s1","family":"test","thesis":"test","signal":["x"],"mechanism":"test","universe":["AAA"],"direction":"long","expression":["shares"],"horizon":"3y","regime":["normal"],"risk_sources":[],"capital_model":"cash","benchmark":"hold","invalidation":["x"],"status":"experimental"}

    def test_audit_chain(self):
        a=make_record("a","finding","test",{"x":1},created_at="2026-01-01T00:00:00Z")
        b=make_record("b","decision","test",{"y":2},["a"],a["record_hash"],"2026-01-01T01:00:00Z")
        self.assertEqual(verify_chain([a,b]),[])
        self.assertEqual(dangling_causes([a,dict(b,caused_by=["missing"])]),["b: missing caused_by missing"])

    def test_source_arbitration_rejects_future_and_stale(self):
        result=arbitrate([SourceObservation("strong","2026-09-15T09:00:00Z",100.0,.9,3),SourceObservation("other","2026-09-15T08:59:00Z",103.0,.8,2),SourceObservation("future","2026-09-15T10:00:00Z",90.0,.9,3),SourceObservation("stale","2026-09-13T00:00:00Z",101.0,.9,3)],"2026-09-15T09:00:00Z",
                       max_age_hours=24.0, numeric_conflict_tolerance=0.01)
        self.assertEqual(result.selected.source,"strong"); self.assertTrue(any("future" in x for x in result.rejected)); self.assertTrue(any("stale" in x for x in result.rejected)); self.assertTrue(result.conflicts)

    def test_strategy_validation_and_mutation(self):
        s=self.base_strategy(); self.assertEqual(validate_strategy(s),[]); m=mutate_strategy(s,{"horizon":"5y"}); self.assertEqual(m["status"],"experimental"); self.assertEqual(m["strategy_version"],1); self.assertEqual(m["parent_version"],0)

    def test_calibration(self):
        r=calibration_summary([1,1,0,1],[1,0,0,1],[.9,.8,.2,.7]); self.assertEqual(r.n,4); self.assertEqual(r.hit_rate,.75); self.assertAlmostEqual(r.brier,.195)

    def test_backtest_and_walk_forward(self):
        bars=[{"timestamp":f"2026-09-0{i+1}T00:00:00Z","close":100+i} for i in range(6)]; r=backtest_long_only(bars,[0,1,1,0,1,1],"2026-09-06T00:00:00Z"); self.assertGreater(r.final_equity,1.0); self.assertEqual(r.trades,3); self.assertEqual(len(walk_forward_splits(20,10,5)),2)
        with self.assertRaises(ValueError): backtest_long_only(bars,[0,1,1,0,1,1],"2026-09-05T00:00:00Z")

    def test_assignment_ledger_and_fx(self):
        rows=assignment_ledger([{"underlying":"BKNG","currency":"USD","contracts":25,"strike":104,"expiry":"2028-01-21","side":"SELL","right":"P"},{"underlying":"BMW","currency":"EUR","contracts":30,"strike":40,"expiry":"2028-06-21","side":"SELL","right":"P"},{"underlying":"AAPL","currency":"USD","contracts":4,"strike":120,"expiry":"2027-12-17","side":"SELL","right":"P"}],"2026-09-15")
        by_underlying={r.underlying:r for r in rows}
        self.assertEqual(by_underlying["BKNG"].assignment_notional,260000)
        self.assertAlmostEqual(normalized_assignment_total(rows,{"EUR":1.1535}),260000+30*40*100*1.1535+48000)
        # This line used to assert the bug: that bucketing sums raw notionals
        # across USD and EUR as though they were one unit. Line 87 above,
        # in this same test, already converted properly for the total.
        fx={"EUR":1.1535}
        self.assertAlmostEqual(sum(assignment_by_expiry_bucket(rows,fx).values()),
                               normalized_assignment_total(rows,fx))
        with self.assertRaises(ValueError):
            assignment_by_expiry_bucket(rows)

    def test_capacity_and_stress(self):
        c=risk_capacity({"nav":1000,"total_cash":20,"available_funds":500,"leverage":1.2,"excess_liquidity":450},800)
        self.assertEqual(c["cash_capacity"],20); self.assertEqual(c["margin_capacity"],500); self.assertEqual(c["assignment_notional"],800); self.assertEqual(c["assignment_to_nav"],.8)
        self.assertEqual(stress_nav(1000,[-.3,0,.2])[0]["nav_after_shock"],700)

    def test_covered_call_opportunity_cost(self):
        r=covered_call_opportunity({"underlying":"GLW","shares":100,"spot":143.40},{"strike":80,"premium":64,"contracts":1,"target_price":180},3633490.59)
        self.assertEqual(r["covered_shares"],100); self.assertTrue(r["is_itm"]); self.assertLess(r["strike_vs_spot_pct"],0); self.assertEqual(r["upside_cap_pct"],0); self.assertEqual(r["assignment_value"],8000); self.assertEqual(r["opportunity_cost_at_target"],10000)


class HostParameterizedMechanicsTests(unittest.TestCase):
    def test_source_arbitration_uses_host_parameters(self):
        results = evaluate_source_arbitrations([{
            "as_of": "2026-09-16T20:00:00Z",
            "max_age_hours": 24,
            "numeric_conflict_tolerance": 0.01,
            "observations": [
                {
                    "source": "primary",
                    "observed_at": "2026-09-16T19:00:00Z",
                    "value": 100.0,
                    "confidence": 0.9,
                    "tier": 3,
                },
                {
                    "source": "future",
                    "observed_at": "2026-09-17T00:00:00Z",
                    "value": 99.0,
                    "confidence": 0.9,
                    "tier": 3,
                },
            ],
        }])
        self.assertTrue(results[0]["valid"])
        self.assertEqual(results[0]["selected"]["source"], "primary")
        self.assertTrue(results[0]["rejected"])

    def test_backtest_runs_a_host_authored_signal_and_splits(self):
        results = evaluate_backtests([{
            "bars": [
                {"timestamp": f"2026-09-0{index + 1}T00:00:00Z",
                 "close": 100.0 + index}
                for index in range(6)
            ],
            "signal": [0, 1, 1, 0, 1, 1],
            "as_of": "2026-09-06T00:00:00Z",
            "fee_bps": 5.0,
            "slippage_bps": 5.0,
            "train_size": 3,
            "test_size": 2,
        }])
        self.assertTrue(results[0]["valid"])
        self.assertEqual(results[0]["backtest"]["trades"], 3)
        self.assertEqual(len(results[0]["walk_forward_splits"]), 1)
        self.assertNotIn("recommended", results[0])

if __name__ == "__main__": unittest.main()


class UncoveredCallTests(unittest.TestCase):
    """An obligation larger than the shares held is a NAKED short call.

    Premium was counted in full against only the covered shares, so 50
    shares written against 2 contracts reported a 12% yield when three
    quarters of that premium was compensation for uncovered risk -- and
    nothing in the output named the naked leg at all.
    """

    def result(self, shares=50, contracts=2):
        return covered_call_opportunity(
            {"underlying": "MSFT", "shares": shares, "spot": 100.0},
            {"strike": 110.0, "premium": 3.0, "contracts": contracts,
             "contract_multiplier": 100, "target_price": 130.0},
            nav=1_000_000)

    def test_the_naked_leg_is_reported(self):
        result = self.result(shares=50, contracts=2)
        self.assertEqual(result["obligated_shares"], 200.0)
        self.assertEqual(result["uncovered_shares"], 150.0)
        self.assertFalse(result["is_fully_covered"])

    def test_premium_is_split_between_covered_and_uncovered(self):
        result = self.result(shares=50, contracts=2)
        self.assertEqual(result["covered_premium_value"], 150.0)
        self.assertEqual(result["uncovered_premium_value"], 450.0)
        self.assertEqual(
            result["covered_premium_value"]
            + result["uncovered_premium_value"],
            result["premium_value"],
        )


class CoveredCallInputsAreEvidenceTests(unittest.TestCase):
    def result(self, shares=50, contracts=2):
        return covered_call_opportunity(
            {"underlying": "MSFT", "shares": shares, "spot": 100.0},
            {"strike": 110.0, "premium": 3.0, "contracts": contracts,
             "contract_multiplier": 100, "target_price": 130.0},
            nav=1_000_000)

    def position(self):
        return {"underlying": "MSFT", "shares": 100, "spot": 490.0}

    def call(self, **over):
        value = {
            "strike": 520.0,
            "premium": 10.0,
            "contracts": 1,
            "target_price": 550.0,
        }
        value.update(over)
        return value

    def test_premium_cannot_default_to_zero(self):
        value = self.call()
        del value["premium"]
        with self.assertRaisesRegex(
                ValueError, "covered_call_premium_required"):
            covered_call_opportunity(
                self.position(), value, nav=1_000_000)

    def test_target_cannot_default_to_spot(self):
        value = self.call()
        del value["target_price"]
        with self.assertRaisesRegex(
                ValueError, "covered_call_target_price_required"):
            covered_call_opportunity(
                self.position(), value, nav=1_000_000)

    def test_yield_reflects_only_covered_income(self):
        # 12% before: $600 of premium over $5,000 of covered stock.
        self.assertAlmostEqual(
            self.result(shares=50, contracts=2)["premium_yield_on_covered_value"], 0.03)

    def test_a_fully_covered_write_reports_no_naked_leg(self):
        result = self.result(shares=200, contracts=2)
        self.assertEqual(result["uncovered_shares"], 0.0)
        self.assertTrue(result["is_fully_covered"])

    def test_excess_shares_do_not_create_negative_uncovered(self):
        result = self.result(shares=500, contracts=2)
        self.assertEqual(result["uncovered_shares"], 0.0)
        self.assertEqual(result["covered_shares"], 200.0)


class BacktestInputValidationTests(unittest.TestCase):
    """A backtest that accepts invalid bars returns a number, and a number
    is what gets believed.

    Verified before adding these: reverse-chronological bars produced a
    plausible 0.83 final equity, duplicate timestamps were accepted, a NaN
    close produced equity=nan, and a NEGATIVE close produced a profit.
    """

    AS_OF = "2026-12-31T00:00:00Z"

    def bars(self, prices, dates=None):
        dates = dates or [f"2026-01-{i + 1:02d}T00:00:00Z" for i in range(len(prices))]
        return [{"timestamp": d, "close": p} for d, p in zip(dates, prices)]

    def test_chronological_bars_still_work(self):
        result = backtest_long_only(self.bars([100.0, 110.0, 120.0]), [1, 1, 0], self.AS_OF)
        self.assertGreater(result.final_equity, 1.0)

    def test_reverse_chronological_bars_are_refused(self):
        reversed_dates = ["2026-01-03T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"]
        with self.assertRaises(ValueError) as ctx:
            backtest_long_only(self.bars([120.0, 110.0, 100.0], reversed_dates),
                               [1, 1, 0], self.AS_OF)
        self.assertIn("ascending", str(ctx.exception))

    def test_duplicate_timestamps_are_refused(self):
        dates = ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]
        with self.assertRaises(ValueError):
            backtest_long_only(self.bars([100.0, 999.0, 110.0], dates), [1, 1, 0], self.AS_OF)

    def test_a_nan_close_is_refused_rather_than_returning_nan_equity(self):
        with self.assertRaises(ValueError) as ctx:
            backtest_long_only(self.bars([100.0, float("nan"), 120.0]), [1, 1, 0], self.AS_OF)
        self.assertIn("finite", str(ctx.exception))

    def test_an_infinite_close_is_refused(self):
        with self.assertRaises(ValueError):
            backtest_long_only(self.bars([100.0, float("inf"), 120.0]), [1, 1, 0], self.AS_OF)

    def test_a_negative_close_is_refused_rather_than_producing_a_profit(self):
        with self.assertRaises(ValueError) as ctx:
            backtest_long_only(self.bars([100.0, -50.0, 120.0]), [1, 1, 0], self.AS_OF)
        self.assertIn("positive", str(ctx.exception))

    def test_a_zero_close_is_refused_rather_than_dividing_by_zero(self):
        with self.assertRaises(ValueError):
            backtest_long_only(self.bars([100.0, 0.0, 120.0]), [1, 1, 0], self.AS_OF)

    def test_a_non_numeric_close_is_refused(self):
        with self.assertRaises(ValueError):
            backtest_long_only(self.bars([100.0, "n/a", 120.0]), [1, 1, 0], self.AS_OF)

    def test_non_positive_initial_equity_is_refused(self):
        with self.assertRaises(ValueError):
            backtest_long_only(self.bars([100.0, 110.0]), [1, 0], self.AS_OF, initial_equity=0.0)


class BacktestCostValidationTests(unittest.TestCase):
    """Costs deserve the validation the bars already got.

    The bar checks exist because a number gets believed where an error would
    not. The costs were left unvalidated on the same reasoning: a NaN fee
    produced equity=nan, an infinite fee produced -inf, and a NEGATIVE fee
    paid the strategy to trade, returning 1.0343 against a 1.0287 baseline on
    identical bars. Negative costs are the cheapest way to improve any
    backtest.
    """

    BARS = [{"timestamp": f"2026-01-0{i}T00:00:00+00:00", "close": 100.0 + i}
            for i in range(1, 5)]
    SIGNAL = [1, 1, 1, 1]
    AS_OF = "2026-02-01T00:00:00+00:00"

    def run_with(self, **over):
        return backtest_long_only(self.BARS, self.SIGNAL, as_of=self.AS_OF, **over)

    def test_nan_fee_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_with(fee_bps=float("nan"))

    def test_infinite_fee_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_with(fee_bps=float("inf"))

    def test_negative_fee_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_with(fee_bps=-50.0)

    def test_negative_slippage_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_with(slippage_bps=-1.0)

    def test_non_positive_initial_equity_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_with(initial_equity=0.0)

    def test_valid_costs_still_run(self):
        self.assertGreater(self.run_with(fee_bps=5.0, slippage_bps=5.0).final_equity, 0.0)

    def test_higher_costs_never_improve_the_result(self):
        cheap = self.run_with(fee_bps=1.0).final_equity
        dear = self.run_with(fee_bps=50.0).final_equity
        self.assertLess(dear, cheap)


class JudgementsAreNotDefaultedTests(unittest.TestCase):
    """A default is how a judgement gets made without anyone choosing it.

    How stale is too stale, and how far apart two sources must be before
    they disagree, are decisions about the world rather than arithmetic.
    PHILOSOPHY.md gives those to the host; they were sitting in a function
    signature as 24.0 and 0.01.
    """

    OBS = [SourceObservation("a", "2026-09-15T09:00:00Z", 100.0, 0.9, 3)]

    def test_the_bounds_must_be_supplied(self):
        with self.assertRaises(TypeError):
            arbitrate(self.OBS, "2026-09-15T09:00:00Z")

    def test_they_cannot_be_passed_positionally_by_accident(self):
        """Keyword-only, so the two cannot be swapped silently."""
        with self.assertRaises(TypeError):
            arbitrate(self.OBS, "2026-09-15T09:00:00Z", 24.0, 0.01)

    def test_supplying_them_works(self):
        result = arbitrate(self.OBS, "2026-09-15T09:00:00Z",
                           max_age_hours=24.0, numeric_conflict_tolerance=0.01)
        self.assertIsNotNone(result)

    def test_the_caller_can_choose_a_different_staleness(self):
        """The point: the bound is the caller's, not the runtime's."""
        strict = arbitrate(self.OBS, "2026-09-16T09:00:00Z",
                           max_age_hours=1.0, numeric_conflict_tolerance=0.01)
        loose = arbitrate(self.OBS, "2026-09-16T09:00:00Z",
                          max_age_hours=48.0, numeric_conflict_tolerance=0.01)
        self.assertNotEqual(bool(strict.rejected), bool(loose.rejected))
