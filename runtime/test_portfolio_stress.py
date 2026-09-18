import unittest
from .portfolio import (correlated_stress, option_delta_exposure, risk_capacity,
                        scenario_stress)
from .portfolio import (aggregate_assignment as assignment_by_underlying,
                        assignment_by_expiry_bucket, assignment_ledger,
                        normalized_assignment_total)

class CorrelatedStressTests(unittest.TestCase):
    def test_factor_shocks_and_correlation_are_reported(self):
        result = correlated_stress(
            [{"symbol":"A","market_value":1000}], 10000,
            {"market":-0.10,"rates":0.02},
            {"A":{"market":1.0,"rates":0.5}},
            {"market":{"market":1.0,"rates":0.2},"rates":{"market":0.2,"rates":1.0}},
        )
        self.assertAlmostEqual(result["pnl"], -90.0)
        self.assertIsNotNone(result["correlated_risk_norm"])
        self.assertFalse(result["unmodeled"])

    def test_missing_loading_is_visible(self):
        result = correlated_stress([], 10000, {"market":-.1}, {})
        self.assertEqual(result["unmodeled"], [])

if __name__ == "__main__": unittest.main()


class OptionDeltaExposureTests(unittest.TestCase):
    """Stress must apply shocks to underlying-equivalent exposure.

    Delta is d(option price)/d(underlying price): it converts a move in the
    UNDERLYING into a move in the option. Stress used
    market_value * delta * shock, applying a percentage to the option's own
    premium. Dimensionally wrong, and it understates badly.

    Ten $100-underlying calls at delta 0.5 and $3 premium carry $50,000 of
    underlying exposure; a 10% move is about $5,000. The old expression
    returned $150, roughly 33x too small. Stress output is exactly where an
    understatement is least survivable.
    """

    def option(self, **over):
        base = {"asset_class": "OPT", "delta": 0.5, "market_value": 3000.0,
                "quantity": 10, "multiplier": 100, "underlying_price": 100.0}
        base.update(over)
        return base

    def test_exposure_is_underlying_equivalent_not_premium(self):
        self.assertEqual(option_delta_exposure(self.option()), 50_000.0)

    def test_pnl_uses_that_exposure(self):
        result = scenario_stress(positions=[self.option()], nav=1_000_000,
                                 scenarios=[{"name": "-10%", "equity_shock": -0.10}])[0]
        self.assertAlmostEqual(result["pnl"], -5_000.0)

    def test_a_short_option_position_has_negative_exposure(self):
        self.assertEqual(option_delta_exposure(self.option(quantity=-10)), -50_000.0)

    def test_short_calls_gain_when_the_underlying_falls(self):
        result = scenario_stress(positions=[self.option(quantity=-10)], nav=1_000_000,
                                 scenarios=[{"name": "-10%", "equity_shock": -0.10}])[0]
        self.assertGreater(result["pnl"], 0)

    def test_a_put_with_negative_delta_is_signed_correctly(self):
        self.assertEqual(option_delta_exposure(self.option(delta=-0.4)), -40_000.0)

    def test_a_precomputed_delta_exposure_is_used_as_given(self):
        self.assertEqual(option_delta_exposure({"delta_exposure": 12_345.0}), 12_345.0)

    def test_missing_inputs_yield_no_exposure_rather_than_a_wrong_one(self):
        for field in ("delta", "quantity", "underlying_price"):
            with self.subTest(missing=field):
                position = {k: v for k, v in self.option().items() if k != field}
                self.assertIsNone(option_delta_exposure(position))

    def test_an_unmodellable_option_counts_as_unmodeled_notional(self):
        # A known gap in coverage beats a confident bad number.
        position = {k: v for k, v in self.option().items() if k != "quantity"}
        result = scenario_stress(positions=[position], nav=1_000_000,
                                 scenarios=[{"name": "-10%", "equity_shock": -0.10}])[0]
        self.assertEqual(result["pnl"], 0.0)
        self.assertEqual(result["unmodeled_notional"], 3000.0)
        self.assertEqual(result["coverage_ratio"], 0.0)

    def test_a_default_multiplier_of_one_hundred_is_assumed(self):
        position = {k: v for k, v in self.option().items() if k != "multiplier"}
        self.assertEqual(option_delta_exposure(position), 50_000.0)


class GeneratorExhaustionTests(unittest.TestCase):
    """Scenario loops must not consume their inputs.

    positions is re-iterated once per scenario. A generator was consumed by
    the FIRST scenario, so every later one saw an empty portfolio and
    reported zero loss. A -50% crash scenario came back as harmless, which
    is the most dangerous direction a stress number can be wrong in.
    """

    POSITIONS = [{"symbol": "MSFT", "asset_class": "STK",
                  "market_value": 100_000.0, "beta": 1.0}]
    SCENARIOS = [{"name": "mild", "equity_shock": -0.10},
                 {"name": "severe", "equity_shock": -0.30},
                 {"name": "crash", "equity_shock": -0.50}]

    def test_a_generator_gives_the_same_result_as_a_list(self):
        from_list = scenario_stress(positions=list(self.POSITIONS), nav=1_000_000,
                                    scenarios=list(self.SCENARIOS))
        from_gen = scenario_stress(positions=(p for p in self.POSITIONS), nav=1_000_000,
                                   scenarios=(s for s in self.SCENARIOS))
        self.assertEqual([r["pnl"] for r in from_list], [r["pnl"] for r in from_gen])

    def test_the_worst_scenario_is_not_silently_zero(self):
        # The load-bearing assertion. Zero loss on a -50% shock is the
        # specific lie this bug told.
        results = scenario_stress(positions=(p for p in self.POSITIONS), nav=1_000_000,
                                  scenarios=(s for s in self.SCENARIOS))
        crash = next(r for r in results if r["name"] == "crash")
        self.assertAlmostEqual(crash["pnl"], -50_000.0)

    def test_losses_increase_monotonically_with_shock_severity(self):
        results = scenario_stress(positions=(p for p in self.POSITIONS), nav=1_000_000,
                                  scenarios=(s for s in self.SCENARIOS))
        pnls = [r["pnl"] for r in results]
        self.assertEqual(pnls, sorted(pnls, reverse=True))

    def test_every_scenario_reports_the_same_modeled_notional(self):
        results = scenario_stress(positions=(p for p in self.POSITIONS), nav=1_000_000,
                                  scenarios=(s for s in self.SCENARIOS))
        notionals = {r["modeled_notional"] for r in results}
        self.assertEqual(notionals, {100_000.0})



class UnknownIsNotZeroTests(unittest.TestCase):
    """Absence of a risk figure is not a risk figure of zero.

    Missing fields defaulted to 0.0, so a snapshot that simply did not
    carry excess_liquidity reported 0.0 -- indistinguishable from an
    account genuinely at zero, which is a margin-call condition. And a
    ratio against zero NAV reported 0.0, saying "no assignment exposure
    relative to capital" for an account with obligations and no capital.
    """

    def test_a_populated_snapshot_reports_real_numbers(self):
        result = risk_capacity({"nav": 1_000_000, "total_cash": 5000,
                                "available_funds": 20000, "leverage": 1.2,
                                "excess_liquidity": 15000}, assignment_base=400_000)
        self.assertEqual(result["cash_capacity"], 5000.0)
        self.assertEqual(result["assignment_to_nav"], 0.4)

    def test_zero_nav_reports_undefined_not_zero_exposure(self):
        result = risk_capacity({"nav": 0.0}, assignment_base=400_000)
        self.assertIsNone(result["assignment_to_nav"])

    def test_negative_nav_reports_undefined(self):
        result = risk_capacity({"nav": -50_000.0}, assignment_base=400_000)
        self.assertIsNone(result["assignment_to_nav"])

    def test_absent_risk_fields_are_unknown_not_zero(self):
        result = risk_capacity({"nav": 1_000_000}, assignment_base=0)
        for field in ("cash_capacity", "margin_capacity", "gross_leverage",
                      "excess_liquidity"):
            with self.subTest(field=field):
                self.assertIsNone(result[field])

    def test_a_genuine_zero_is_still_reported_as_zero(self):
        # The distinction only matters if a real zero survives it.
        result = risk_capacity({"nav": 1_000_000, "excess_liquidity": 0.0},
                               assignment_base=0)
        self.assertEqual(result["excess_liquidity"], 0.0)
        self.assertIsNotNone(result["excess_liquidity"])

    def test_an_unparseable_value_is_unknown_rather_than_zero(self):
        result = risk_capacity({"nav": 1_000_000, "leverage": "n/a"}, assignment_base=0)
        self.assertIsNone(result["gross_leverage"])


class CurrencyMixingTests(unittest.TestCase):
    """Different currencies must not be added together without rates.

    USD 10,000 plus EUR 10,000 was reported as 20,000, silently, as though
    both legs were the same unit. At EURUSD 1.10 the true figure is 21,000,
    so exposure was understated and the error scales with the non-base
    book.

    normalize_currency already existed in this module and already raised on
    a missing rate. The aggregators just were not using it.
    """

    def positions(self):
        return [
            {"underlying": "MSFT", "currency": "USD", "contracts": 1, "side": "SELL",
             "right": "P", "strike": 100.0, "expiry": "2026-12-18",
             "contract_multiplier": 100},
            {"underlying": "SAP", "currency": "EUR", "contracts": 1, "side": "SELL",
             "right": "P", "strike": 100.0, "expiry": "2026-12-18",
             "contract_multiplier": 100},
        ]

    def rows(self, positions=None):
        return assignment_ledger(positions or self.positions(), as_of="2026-09-16")

    def test_a_mixed_book_without_rates_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            assignment_by_expiry_bucket(self.rows())
        self.assertIn("mixed_currencies_without_fx", str(ctx.exception))

    def test_the_refusal_names_the_currencies_involved(self):
        with self.assertRaises(ValueError) as ctx:
            assignment_by_expiry_bucket(self.rows())
        self.assertIn("EUR", str(ctx.exception))
        self.assertIn("USD", str(ctx.exception))

    def test_supplying_rates_converts_correctly(self):
        buckets = assignment_by_expiry_bucket(self.rows(), {"EUR": 1.10})
        self.assertAlmostEqual(buckets["91-365d"], 21_000.0)

    def test_a_single_currency_book_needs_no_rates(self):
        usd_only = [p for p in self.positions() if p["currency"] == "USD"]
        buckets = assignment_by_expiry_bucket(self.rows(usd_only))
        self.assertAlmostEqual(buckets["91-365d"], 10_000.0)

    def test_bucket_totals_match_the_fx_normalized_total(self):
        rows = self.rows()
        fx = {"EUR": 1.10}
        self.assertAlmostEqual(sum(assignment_by_expiry_bucket(rows, fx).values()),
                               normalized_assignment_total(rows, fx))

    def test_a_missing_rate_is_refused_rather_than_assumed_to_be_one(self):
        with self.assertRaises(ValueError):
            assignment_by_expiry_bucket(self.rows(), {"GBP": 1.25})

    def test_the_same_underlying_in_two_currencies_is_refused(self):
        # Rare but real: a dual-listed name quoted in two currencies. The
        # per-underlying grouping normally sidesteps the mixing problem
        # because one underlying is one currency; when that assumption
        # breaks it must say so rather than add them.
        dual = self.positions()
        dual[1]["underlying"] = "MSFT"
        with self.assertRaises(ValueError) as ctx:
            assignment_by_underlying(self.rows(dual))
        self.assertIn("mixed_currencies_for_underlying", str(ctx.exception))

    def test_per_underlying_aggregation_carries_its_currency(self):
        # A bare number with no unit attached is how the mixing happened.
        aggregated = assignment_by_underlying(self.rows())
        self.assertEqual(aggregated["SAP"]["currency"], "EUR")
        self.assertEqual(aggregated["MSFT"]["currency"], "USD")


class ShortOptionSignTests(unittest.TestCase):
    """A short option must lose when the move goes against it.

    Two position schemas exist. A signed `quantity` carries direction in its
    sign; the assignment/opportunity schema uses a positive `contracts` plus a
    separate `side`. Reading size without reading side turned every short
    option into a long one and inverted the sign of the stress result -- a
    short put reported +$500 on a -10% move when it loses $500. Understating
    magnitude is bad; pointing the wrong way is worse.
    """

    def short_put(self, **over):
        pos = {"asset_class": "OPT", "delta": -0.5, "market_value": 500.0,
               "side": "SELL", "contracts": 1, "contract_multiplier": 100,
               "underlying_price": 100.0}
        pos.update(over)
        return pos

    def pnl(self, pos, shock=-0.10):
        return scenario_stress(positions=[pos], nav=1_000_000,
                               scenarios=[{"name": "s", "equity_shock": shock}])[0]["pnl"]

    def test_short_put_loses_on_downward_move(self):
        self.assertLess(self.pnl(self.short_put()), 0.0)

    def test_long_put_gains_on_downward_move(self):
        self.assertGreater(self.pnl(self.short_put(side="BUY")), 0.0)

    def test_side_sell_matches_negative_signed_quantity(self):
        signed = {"asset_class": "OPT", "delta": -0.5, "market_value": 500.0,
                  "quantity": -1, "multiplier": 100, "underlying_price": 100.0}
        self.assertEqual(option_delta_exposure(self.short_put()),
                         option_delta_exposure(signed))

    def test_short_and_long_exposures_are_opposite(self):
        self.assertEqual(option_delta_exposure(self.short_put()),
                         -option_delta_exposure(self.short_put(side="BUY")))

    def test_contract_multiplier_is_not_defaulted_past(self):
        pos = self.short_put(side="BUY", delta=0.5, contract_multiplier=10)
        self.assertEqual(option_delta_exposure(pos), 0.5 * 1 * 10 * 100.0)

    def test_unknown_side_is_refused_rather_than_assumed_long(self):
        self.assertIsNone(option_delta_exposure(self.short_put(side="MAYBE")))

    def test_missing_size_is_refused(self):
        pos = self.short_put()
        pos.pop("contracts")
        self.assertIsNone(option_delta_exposure(pos))


class InfiniteNavIsNotSafetyTests(unittest.TestCase):
    """An unusable NAV must not read as the most reassuring possible answer.

    `nav > 0` is True for infinity, and infinity divides to exactly 0.0 --
    "no assignment exposure relative to capital". NaN and negative NAV
    already returned None; infinity slipped through because it is the one
    unusable value that is also positive.
    """

    def ratio(self, nav):
        return risk_capacity({"nav": nav, "cash": 1000.0}, 50_000.0)["assignment_to_nav"]

    def test_infinite_nav_reports_unknown_not_zero(self):
        self.assertIsNone(self.ratio(float("inf")))

    def test_nan_and_negative_nav_still_report_unknown(self):
        self.assertIsNone(self.ratio(float("nan")))
        self.assertIsNone(self.ratio(-100.0))

    def test_a_real_nav_still_computes_the_ratio(self):
        self.assertAlmostEqual(self.ratio(1_000_000.0), 0.05)
