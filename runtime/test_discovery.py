import unittest

from .discovery import FAMILIES, discover_candidates


class DiscoveryTests(unittest.TestCase):
    def test_discovers_across_requested_families(self):
        rows = discover_candidates(universe=[{"symbol": "AAPL", "instruments": ["stock", "option"]}], families=["deep_value_fcf", "volatility_options"], max_candidates=10)
        self.assertEqual({r["family"] for r in rows}, {"deep_value_fcf", "volatility_options"})
        self.assertIn("option", rows[1]["expression_types"])

    def test_unknown_family_fails_closed(self):
        with self.assertRaises(ValueError):
            discover_candidates(universe=[{"symbol": "AAPL"}], families=["not_a_family"])

    def test_cap_is_enforced(self):
        rows = discover_candidates(universe=[{"symbol": "AAPL"}, {"symbol": "MSFT"}], families=FAMILIES, max_candidates=7)
        self.assertEqual(len(rows), 7)
        self.assertTrue(all("required_evidence" in r for r in rows))


if __name__ == "__main__":
    unittest.main()


class FamilySelectionTests(unittest.TestCase):
    """The Research Director selects families; code must not default to all.

    discover_candidates defaulted to the entire FAMILIES catalogue when the
    caller supplied none. That is a hardcoded strategy sequence, which
    PHILOSOPHY.md forbids and which build_plan had removed for exactly the
    same reason.
    """

    def test_omitting_the_family_selection_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            discover_candidates(universe=[{"symbol": "AAPL"}])
        self.assertIn("family_selection_required", str(ctx.exception))

    def test_the_refusal_names_who_selects(self):
        with self.assertRaises(ValueError) as ctx:
            discover_candidates(universe=[{"symbol": "AAPL"}])
        self.assertIn("Research Director", str(ctx.exception))

    def test_an_empty_selection_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            discover_candidates(universe=[{"symbol": "AAPL"}], families=[])
        self.assertIn("empty_family_selection", str(ctx.exception))

    def test_an_explicit_selection_works(self):
        rows = discover_candidates(universe=[{"symbol": "AAPL"}],
                                   families=["deep_value_fcf"])
        self.assertTrue(rows)

    def test_selecting_the_whole_catalogue_is_allowed_when_explicit(self):
        self.assertTrue(discover_candidates(universe=[{"symbol": "AAPL"}],
                                            families=FAMILIES))

    def test_an_unknown_family_is_still_refused_for_a_different_reason(self):
        # NOT a strategy ceiling. A family is a key into the required-evidence
        # map, so it decides what a candidate must prove before it can pass
        # the evidence gate. An off-catalogue family has no evidence contract,
        # and admitting one would mint candidates nothing requires evidence
        # for. Inventing a family means supplying its evidence requirements
        # too, which is a real change rather than a looser check.
        with self.assertRaises(ValueError) as ctx:
            discover_candidates(universe=[{"symbol": "AAPL"}], families=["invented"])
        self.assertIn("unknown_family", str(ctx.exception))
