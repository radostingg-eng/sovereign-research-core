import unittest

from .portfolio import normalized_assignment_total
from .research import (
    candidate_gate, evaluate_candidate_readiness,
    evaluate_portfolio_mechanics, reconcile_portfolio,
)


class ResearchMechanicsTests(unittest.TestCase):
    def test_reconcile_requires_explicit_fx(self):
        snapshot = {
            "nav": 100000.0,
            "total_cash": 1000.0,
            "available_funds": 50000.0,
            "leverage": 1.1,
            "excess_liquidity": 40000.0,
        }
        positions = [{
            "underlying": "BMW",
            "currency": "EUR",
            "contracts": 2,
            "strike": 40,
            "expiry": "2027-01-01",
            "right": "P",
            "side": "SELL",
            "spot": 60,
        }]
        state = reconcile_portfolio(
            snapshot, positions, {"EUR": 1.15}, as_of="2026-09-15")
        self.assertAlmostEqual(
            state["capacity"]["assignment_notional"], 9200.0)

    def test_candidate_gate_fails_closed(self):
        ready, blockers = candidate_gate(
            {"candidate_id": "x", "expected_return": 0.1, "risk": 0.2},
            {"capacity": {"assignment_to_nav": 0.5}},
        )
        self.assertFalse(ready)
        self.assertIn("missing:capital_usage", blockers)

    def test_a_ready_candidate_is_reported_not_judged(self):
        candidate = {
            "candidate_id": "x",
            "expected_return": 0.20,
            "risk": 0.05,
            "capital_usage": 0.1,
            "portfolio_fit": 0.03,
            "evidence_status": "verified",
        }
        ready, blockers = candidate_gate(
            candidate, {"capacity": {"assignment_to_nav": 0.5}})
        self.assertTrue(ready)
        self.assertEqual(blockers, [])

    def test_readiness_preserves_the_hosts_order(self):
        candidates = [
            {
                "candidate_id": "first",
                "expected_return": 0.1,
                "risk": 0.1,
                "capital_usage": 0.1,
                "evidence_status": "unknown",
            },
            {
                "candidate_id": "second",
                "expected_return": 0.2,
                "risk": 0.1,
                "capital_usage": 0.1,
                "evidence_status": "verified",
            },
        ]
        rows = evaluate_candidate_readiness(
            candidates, {"capacity": {"assignment_to_nav": 0.5}})
        self.assertEqual(
            [row["candidate_id"] for row in rows], ["first", "second"])
        self.assertFalse(rows[0]["ready"])
        self.assertTrue(rows[1]["ready"])

    def test_readiness_adds_no_aggregate_score_or_winner(self):
        rows = evaluate_candidate_readiness(
            [{
                "candidate_id": "x",
                "expected_return": 0.2,
                "risk": 0.1,
                "capital_usage": 0.1,
                "evidence_status": "verified",
            }],
            {"capacity": {"assignment_to_nav": 0.5}},
        )
        for forbidden in (
            "score", "rank", "winner", "recommended", "beats_baseline",
        ):
            self.assertNotIn(forbidden, rows[0])

    def test_the_runtime_exposes_no_research_ranking_function(self):
        from . import research

        self.assertFalse(hasattr(research, "decide"))
        self.assertFalse(hasattr(research, "rank_candidates"))


class BreakdownUsesSameCurrencyAsTotalTests(unittest.TestCase):
    def positions(self):
        common = {
            "asset_class": "OPT",
            "right": "P",
            "side": "SELL",
            "contracts": 1,
            "strike": 100.0,
            "contract_multiplier": 100,
            "expiry": "2026-12-31",
        }
        return [
            {
                "symbol": "AAA",
                "underlying": "AAA",
                "currency": "USD",
                **common,
            },
            {
                "symbol": "BBB",
                "underlying": "BBB",
                "currency": "EUR",
                **common,
            },
        ]

    def reconciled(self):
        return reconcile_portfolio(
            {"nav": 1_000_000, "cash": 500_000},
            self.positions(),
            {"USD": 1.0, "EUR": 2.0},
            "USD",
        )

    def test_breakdown_sums_to_the_reported_total(self):
        result = self.reconciled()
        breakdown = sum(
            value["assignment_notional"]
            for value in result["assignment_by_underlying"].values()
        )
        self.assertEqual(
            breakdown, result["capacity"]["assignment_notional"])

    def test_non_base_currency_is_converted_in_the_breakdown(self):
        rows = self.reconciled()["assignment_by_underlying"]
        self.assertEqual(rows["BBB"]["assignment_notional"], 20000.0)
        self.assertEqual(rows["AAA"]["assignment_notional"], 10000.0)

    def test_breakdown_states_its_currency(self):
        rows = self.reconciled()["assignment_by_underlying"]
        self.assertTrue(all(row["currency"] == "USD" for row in rows.values()))

    def test_total_matches_the_normalized_helper(self):
        result = self.reconciled()
        total = normalized_assignment_total(
            result["assignment_rows"], {"USD": 1.0, "EUR": 2.0})
        self.assertEqual(total, result["capacity"]["assignment_notional"])


class HostPortfolioMechanicsTests(unittest.TestCase):
    def payload(self):
        return {
            "account": {
                "nav": 1_000_000.0,
                "total_cash": 10000.0,
                "available_funds": 500000.0,
                "leverage": 1.1,
                "excess_liquidity": 400000.0,
            },
            "positions": [{
                "underlying": "MSFT",
                "currency": "USD",
                "contracts": 1,
                "strike": 400.0,
                "expiry": "2027-01-15",
                "right": "P",
                "side": "SELL",
                "spot": 490.0,
                "asset_class": "OPT",
                "delta": -0.2,
                "underlying_price": 490.0,
                "market_value": -1000.0,
            }],
            "fx_to_base": {},
            "base_currency": "USD",
            "as_of": "2026-09-16T20:00:00Z",
            "candidates": [{
                "candidate_id": "c1",
                "expected_return": 0.1,
                "risk": 0.2,
                "capital_usage": 0.1,
                "evidence_status": "verified",
            }],
            "scenarios": [{
                "name": "host-supplied-down",
                "equity_shock": -0.1,
                "assignment_notional": 40000.0,
                "assignment_shock": -0.1,
            }],
        }

    def test_components_are_computed_without_a_winner(self):
        result = evaluate_portfolio_mechanics(self.payload())
        self.assertTrue(result["valid"])
        self.assertEqual(
            result["portfolio"]["capacity"]["assignment_notional"], 40000.0)
        self.assertTrue(result["candidate_readiness"][0]["ready"])
        self.assertEqual(
            result["stress"][0]["name"], "host-supplied-down")
        self.assertNotIn("winner", result)
        self.assertNotIn("recommended", result)

    def test_missing_mechanical_inputs_are_reported(self):
        result = evaluate_portfolio_mechanics({"account": {}})
        self.assertFalse(result["valid"])


if __name__ == "__main__":
    unittest.main()
