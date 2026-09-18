import unittest
from .instruments import (
    evaluate_expressions, expression_ready, normalize_expression,
    required_fields_for,
)

class InstrumentTests(unittest.TestCase):
    def test_normalizes_equity(self):
        row = normalize_expression({"candidate_id":"c1","asset_class":"equity","symbol":"MSFT","expression_type":"spot"})
        self.assertEqual(row["asset_class"], "equity")

    def test_option_contract_requires_contract_data(self):
        required = required_fields_for("option", "put")
        ok, missing = expression_ready({"required_fields": required}, {"price":100,"liquidity":1000,"expiry":"2027-01-01","strike":80,"implied_volatility":.3,"open_interest":100,"margin":5000})
        self.assertTrue(ok); self.assertEqual(missing, [])
        ok, missing = expression_ready({"required_fields": required}, {"price":100})
        self.assertFalse(ok); self.assertIn("margin", missing)

    def test_invalid_asset_fails(self):
        with self.assertRaises(ValueError):
            normalize_expression({"candidate_id":"c1","asset_class":"crypto","symbol":"X","expression_type":"spot"})

if __name__ == "__main__": unittest.main()


class ReadinessFollowsTheInstrumentTests(unittest.TestCase):
    """Readiness must follow what the instrument requires, not what it declared.

    required_fields_for already knew an option needs expiry, strike,
    implied_volatility, open_interest and margin. expression_ready read
    required_fields off the expression itself, defaulting to nothing, so an
    option became "ready" on a price alone. It also only tested for None, so
    a price of -5 or the string "REJECT" counted as an observation.
    """

    OPTION = {"candidate_id": "c1", "asset_class": "option",
              "expression_type": "put", "symbol": "AAPL"}
    OBSERVED = {"price": 100.0, "liquidity": 1.0, "expiry": "2026-12-31",
                "strike": 100.0, "implied_volatility": 0.2,
                "open_interest": 10.0, "margin": 1000.0}

    def test_option_without_its_fields_is_not_ready(self):
        ready, missing = expression_ready(self.OPTION, {})
        self.assertFalse(ready)
        for field in ("expiry", "strike", "implied_volatility", "open_interest", "margin"):
            self.assertIn(field, missing)

    def test_a_truncated_declaration_cannot_shrink_the_gate(self):
        expression = {**self.OPTION, "required_fields": ("price",)}
        ready, missing = expression_ready(expression, {"price": 100.0})
        self.assertFalse(ready)
        self.assertIn("strike", missing)

    def test_declared_extras_are_still_honoured(self):
        expression = {**self.OPTION, "required_fields": ("borrow_rate",)}
        ready, missing = expression_ready(expression, self.OBSERVED)
        self.assertFalse(ready)
        self.assertIn("borrow_rate", missing)

    def test_negative_price_is_not_an_observation(self):
        ready, problems = expression_ready(self.OPTION, {**self.OBSERVED, "price": -5.0})
        self.assertFalse(ready)
        self.assertIn("price:negative", problems)

    def test_non_numeric_quote_is_refused(self):
        ready, problems = expression_ready(self.OPTION, {**self.OBSERVED, "price": "REJECT"})
        self.assertFalse(ready)
        self.assertIn("price:not_numeric", problems)

    def test_nan_observation_is_refused(self):
        ready, problems = expression_ready(
            self.OPTION, {**self.OBSERVED, "implied_volatility": float("nan")})
        self.assertFalse(ready)
        self.assertIn("implied_volatility:not_finite", problems)

    def test_a_fully_observed_option_is_ready(self):
        self.assertEqual(expression_ready(self.OPTION, self.OBSERVED), (True, []))

    def test_equity_still_needs_only_the_base_fields(self):
        equity = {**self.OPTION, "asset_class": "equity", "expression_type": "spot"}
        self.assertEqual(expression_ready(equity, {"price": 10.0, "liquidity": 1.0}), (True, []))

    def test_normalize_defaults_to_the_instrument_requirement(self):
        row = normalize_expression({**self.OPTION, "direction": "long", "strike": 100.0,
                                    "expiry": "2026-12-31"})
        self.assertIn("implied_volatility", row["required_fields"])


class HostSelectedExpressionTests(unittest.TestCase):
    def option(self):
        return {
            "expression": {
                "candidate_id": "c1",
                "asset_class": "option",
                "symbol": "MSFT",
                "expression_type": "call",
                "direction": "short",
                "expiry": "2027-01-15",
                "strike": 600.0,
            },
            "observations": {
                "price": 28.0,
                "liquidity": 100.0,
                "expiry": "2027-01-15",
                "strike": 600.0,
                "implied_volatility": 0.3,
                "open_interest": 500.0,
                "margin": 10000.0,
            },
        }

    def test_a_fully_observed_expression_is_ready(self):
        result = evaluate_expressions([self.option()])
        self.assertTrue(result[0]["ready"])

    def test_a_missing_field_is_reported_not_filled(self):
        value = self.option()
        value["observations"].pop("margin")
        result = evaluate_expressions([value])
        self.assertFalse(result[0]["ready"])
        self.assertIn("margin", result[0]["problems"])

    def test_the_runtime_does_not_rank_expressions(self):
        result = evaluate_expressions([self.option(), self.option()])
        self.assertEqual([row["index"] for row in result], [0, 1])
        for row in result:
            self.assertNotIn("score", row)
            self.assertNotIn("winner", row)
