"""Durable candidate-registry index and the rediscovery acknowledgment gate.

Records are built directly, matching the pattern already used in
`MarketScoutFeedbackTests` in test_market_scout.py, since this module reads
only cycle_stage / cycle_receipt / opportunity_event record shapes and does
not need the full staged-input machinery.
"""

import unittest

from .candidate_registry import (
    candidate_registry,
    candidate_registry_summary,
    occurrences,
    unpromoted_candidate_registry,
    validate_rediscovery_candidates,
)
from .opportunity_ledger import identity_fingerprint, soft_identity_fingerprint

_IDENTITY = {
    "instrument": "AAPL",
    "instrument_type": "equity",
    "strategy_family": "quality_at_discount",
    "direction": "long",
    "thesis_key": "buyback-driven-eps-growth",
}
_OTHER_IDENTITY = {
    "instrument": "MSFT",
    "instrument_type": "equity",
    "strategy_family": "growth",
    "direction": "long",
    "thesis_key": "cloud-margin-expansion",
}


def _receipt(cycle_id):
    return {
        "record_id": f"cycle-receipt:{cycle_id}",
        "record_type": "cycle_receipt",
        "payload": {"cycle_id": cycle_id},
    }


def _scout_stage(cycle_id, candidates, *, carried=False):
    output = {"market_scout_report": {"candidates": candidates}}
    if carried:
        output["carry_forward"] = {
            "market_scout_report": {
                "source_cycle_id": "cycle-origin",
                "count": 1,
            },
        }
    return {
        "record_id": f"cycle-stage:{cycle_id}:market_scout",
        "record_type": "cycle_stage",
        "payload": {
            "cycle_id": cycle_id,
            "agent_id": "market_scout",
            "output": output,
        },
    }


def _director_stage(cycle_id, selected_scout_ids):
    return {
        "record_id": f"cycle-stage:{cycle_id}:research_director",
        "record_type": "cycle_stage",
        "payload": {
            "cycle_id": cycle_id,
            "agent_id": "research_director",
            "output": {
                "research_agenda": {
                    "candidates": [
                        {"selected": True, "scout_candidate_id": scout_id}
                        for scout_id in selected_scout_ids
                    ],
                },
            },
        },
    }


def _opportunity_event(cycle_id, opportunity_id, identity):
    return {
        "record_id": f"opportunity-event:{opportunity_id}-event",
        "record_type": "opportunity_event",
        "payload": {
            "cycle_id": cycle_id,
            "event_id": f"{opportunity_id}-event",
            "opportunity_id": opportunity_id,
            "identity": identity,
            "identity_fingerprint": identity_fingerprint(identity),
            "soft_identity_fingerprint": soft_identity_fingerprint(identity),
            "from_state": None,
            "to_state": "new",
            "thesis": "Evidence-backed thesis.",
            "rationale": "Track this longitudinally.",
            "evidence": ["stage:market_scout"],
            "event_count": 1,
        },
    }


def _candidate(candidate_id, identity=_IDENTITY, rediscovery_of=None):
    row = {
        "candidate_id": candidate_id,
        "identity": identity,
        "trigger": "Fresh evidence.",
        "rationale": "Worth a look.",
        "evidence_tool_call_ids": ["scout-search-one"],
    }
    if rediscovery_of is not None:
        row["rediscovery_of"] = rediscovery_of
    return row


class RegistryBuildTests(unittest.TestCase):
    def test_unreceipted_cycle_is_ignored(self):
        records = [
            _scout_stage("cycle-one", [_candidate("c1")]),
            # No _receipt("cycle-one") -- crashed/partial cycle.
        ]
        self.assertEqual(candidate_registry(records), {})

    def test_single_receipted_proposal_is_indexed(self):
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        registry = candidate_registry(records)
        fingerprint = identity_fingerprint(_IDENTITY)
        self.assertIn(fingerprint, registry)
        entry = registry[fingerprint]
        self.assertEqual(entry["times_proposed"], 1)
        self.assertEqual(entry["times_selected"], 0)
        self.assertEqual(entry["first_seen_cycle_id"], "cycle-one")
        self.assertEqual(entry["last_seen_cycle_id"], "cycle-one")
        self.assertEqual(entry["matching_opportunity_ids"], [])

    def test_repeated_proposal_across_cycles_accumulates(self):
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
            _receipt("cycle-two"),
            _scout_stage("cycle-two", [_candidate("c1")]),
        ]
        entry = candidate_registry(records)[identity_fingerprint(_IDENTITY)]
        self.assertEqual(entry["times_proposed"], 2)
        self.assertEqual(entry["first_seen_cycle_id"], "cycle-one")
        self.assertEqual(entry["last_seen_cycle_id"], "cycle-two")

    def test_carried_scout_report_does_not_inflate_proposal_count(self):
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
            _receipt("cycle-two"),
            _scout_stage(
                "cycle-two",
                [_candidate("c1")],
                carried=True,
            ),
        ]

        entry = candidate_registry(records)[identity_fingerprint(_IDENTITY)]

        self.assertEqual(entry["times_proposed"], 1)
        self.assertEqual(entry["last_seen_cycle_id"], "cycle-one")

    def test_selection_is_tracked_separately_from_promotion(self):
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
            _director_stage("cycle-one", ["c1"]),
        ]
        entry = candidate_registry(records)[identity_fingerprint(_IDENTITY)]
        self.assertEqual(entry["times_selected"], 1)
        # Selected, but never became an opportunity: still unpromoted.
        self.assertEqual(entry["matching_opportunity_ids"], [])
        self.assertIn(
            identity_fingerprint(_IDENTITY),
            unpromoted_candidate_registry(records),
        )

    def test_matching_opportunity_removes_it_from_unpromoted(self):
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
            _opportunity_event("cycle-one", "opportunity-one", _IDENTITY),
        ]
        entry = candidate_registry(records)[identity_fingerprint(_IDENTITY)]
        self.assertEqual(entry["matching_opportunity_ids"], ["opportunity-one"])
        self.assertNotIn(
            identity_fingerprint(_IDENTITY),
            unpromoted_candidate_registry(records),
        )

    def test_occurrences_map_resolves_exact_pairs(self):
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        table = occurrences(records)
        self.assertEqual(
            table[("cycle-one", "c1")], identity_fingerprint(_IDENTITY),
        )
        self.assertNotIn(("cycle-one", "unknown"), table)

    def test_summary_is_descriptive_and_bounded(self):
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        summary = candidate_registry_summary(records)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["unpromoted_total"], 1)
        self.assertEqual(len(summary["items"]), 1)
        self.assertEqual(summary["not_shown"], 0)
        self.assertNotIn("rank", summary)
        self.assertNotIn("score", summary)

    def test_empty_registry_is_unavailable(self):
        summary = candidate_registry_summary([])
        self.assertFalse(summary["available"])
        self.assertEqual(summary["unpromoted_total"], 0)


class RediscoveryEnforcementTests(unittest.TestCase):
    def _data(self, candidates):
        return {
            "cycle_id": "cycle-two",
            "cognitive_stages": [{
                "stage_id": "market_scout",
                "output": {
                    "market_scout_report": {"candidates": candidates},
                },
            }],
        }

    def test_disabled_when_not_enforcing(self):
        data = self._data([_candidate("c2")])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        self.assertEqual(
            validate_rediscovery_candidates(
                data, records=records, enforce=False,
            cycle_id=data["cycle_id"],
            ),
            [],
        )

    def test_fresh_identity_requires_no_reference(self):
        data = self._data([_candidate("c2", identity=_OTHER_IDENTITY)])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        self.assertEqual(
            validate_rediscovery_candidates(
                data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
            ),
            [],
        )

    def test_fresh_identity_with_reference_is_refused(self):
        data = self._data([_candidate(
            "c2", identity=_OTHER_IDENTITY,
            rediscovery_of={"cycle_id": "cycle-one", "candidate_id": "c1"},
        )])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        errors = validate_rediscovery_candidates(
            data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
        )
        self.assertEqual(
            errors,
            ["market_scout_candidate_rediscovery_invalid:0:not_applicable"],
        )

    def test_rediscovery_without_reference_is_refused(self):
        data = self._data([_candidate("c2")])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        errors = validate_rediscovery_candidates(
            data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
        )
        self.assertEqual(
            errors,
            [
                "market_scout_candidate_rediscovery_invalid:"
                "0:required:cycle-one:c1"
            ],
        )

    def test_rediscovery_with_valid_reference_is_accepted(self):
        data = self._data([_candidate(
            "c2",
            rediscovery_of={"cycle_id": "cycle-one", "candidate_id": "c1"},
        )])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        self.assertEqual(
            validate_rediscovery_candidates(
                data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
            ),
            [],
        )

    def test_reference_to_wrong_identity_is_unresolvable(self):
        data = self._data([_candidate(
            "c2",
            rediscovery_of={"cycle_id": "cycle-one", "candidate_id": "other"},
        )])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [
                _candidate("c1"),
                _candidate("other", identity=_OTHER_IDENTITY),
            ]),
        ]
        errors = validate_rediscovery_candidates(
            data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
        )
        self.assertEqual(
            errors,
            ["market_scout_candidate_rediscovery_invalid:0:unresolvable"],
        )

    def test_reference_to_nonexistent_occurrence_is_unresolvable(self):
        data = self._data([_candidate(
            "c2",
            rediscovery_of={
                "cycle_id": "cycle-nonexistent", "candidate_id": "ghost",
            },
        )])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        errors = validate_rediscovery_candidates(
            data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
        )
        self.assertEqual(
            errors,
            ["market_scout_candidate_rediscovery_invalid:0:unresolvable"],
        )

    def test_reference_to_current_cycle_is_unresolvable(self):
        # IDENTITY is already a genuine unpromoted registry entry from
        # cycle-one. The staged cycle-two candidate re-proposes it but
        # cites a candidate row from ITS OWN staged cycle rather than the
        # real prior occurrence -- a self-reference must not satisfy the
        # requirement, even though nothing else about the shape is wrong.
        data = {
            "cycle_id": "cycle-two",
            "cognitive_stages": [{
                "stage_id": "market_scout",
                "output": {
                    "market_scout_report": {"candidates": [
                        _candidate("c-same-cycle", identity=_OTHER_IDENTITY),
                        _candidate(
                            "c2",
                            rediscovery_of={
                                "cycle_id": "cycle-two",
                                "candidate_id": "c-same-cycle",
                            },
                        ),
                    ]},
                },
            }],
        }
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
        ]
        errors = validate_rediscovery_candidates(
            data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
        )
        self.assertEqual(
            errors,
            ["market_scout_candidate_rediscovery_invalid:1:unresolvable"],
        )

    def test_promoted_candidate_never_requires_acknowledgment(self):
        data = self._data([_candidate("c2")])
        records = [
            _receipt("cycle-one"),
            _scout_stage("cycle-one", [_candidate("c1")]),
            _opportunity_event("cycle-one", "opportunity-one", _IDENTITY),
        ]
        self.assertEqual(
            validate_rediscovery_candidates(
                data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
            ),
            [],
        )

    def test_unreceipted_prior_cycle_does_not_force_acknowledgment(self):
        data = self._data([_candidate("c2")])
        records = [
            _scout_stage("cycle-one", [_candidate("c1")]),
            # cycle-one has no receipt: a crashed cycle must not force
            # acknowledgment of a proposal that never durably executed.
        ]
        self.assertEqual(
            validate_rediscovery_candidates(
                data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
            ),
            [],
        )

    def test_last_seen_cycle_matching_current_cycle_is_not_rediscovery(self):
        # Two candidates with the same identity inside the SAME staged
        # cycle are a same-cycle duplicate (already refused elsewhere by
        # market_scout's own duplicate_identity check), not a rediscovery.
        data = self._data([_candidate("c1"), _candidate("c2")])
        records = []
        self.assertEqual(
            validate_rediscovery_candidates(
                data, records=records, enforce=True,
            cycle_id=data["cycle_id"],
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
