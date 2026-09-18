import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .market_scout import (
    derived_market_scout_usage,
    market_scout_report,
    market_scout_summary,
    validate_market_scout,
)
from .opportunity_ledger import (
    identity_fingerprint,
    normalize_identity,
    soft_identity_fingerprint,
)
from .run_host_cycle import run_one, validate_input
from .test_run_host_cycle import add_market_scout, post_effective_full_cycle
from .test_tool_provenance import upgrade_tool_calls_to_v4


def _dispositions():
    return [
        {
            "stage_id": stage_id,
            "disposition": "no_change",
            "rationale": f"No durable change supported for {stage_id}.",
            "evidence": [f"stage:{stage_id}"],
        }
        for stage_id in (
            "learning_audit",
            "meta_research",
            "self_improvement",
        )
    ]


def _valid_input():
    return upgrade_tool_calls_to_v4(add_market_scout(post_effective_full_cycle(
        host_input_schema_version=3,
        learning_stage_dispositions=_dispositions(),
    )))


class MarketScoutValidationTests(unittest.TestCase):
    def test_new_staged_v4_requires_market_scout(self):
        data = upgrade_tool_calls_to_v4(post_effective_full_cycle(
            host_input_schema_version=3,
            learning_stage_dispositions=_dispositions(),
        ))
        self.assertIn(
            "market_scout_required",
            validate_input(
                data,
                "new-v4.json",
                require_full_schema=True,
            ),
        )

    def test_historical_v3_without_market_scout_remains_replayable(self):
        data = post_effective_full_cycle(
            host_input_schema_version=3,
            learning_stage_dispositions=_dispositions(),
        )
        self.assertNotIn(
            "market_scout_required",
            validate_input(data, "historical-v3.json"),
        )

    def test_valid_market_scout_is_a_core_stage(self):
        data = _valid_input()
        errors = validate_input(
            data,
            "new-v4.json",
            require_full_schema=True,
        )
        self.assertEqual(errors, [])
        self.assertFalse(any(
            error.startswith("full_cycle_specialist_not_isolated:market_scout")
            for error in errors
        ))

    def test_zero_candidate_scan_is_valid(self):
        data = _valid_input()
        report = market_scout_report(data)
        report["candidates"] = []
        director = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        director["output"]["research_agenda"]["candidates"][0].pop(
            "scout_candidate_id"
        )
        self.assertEqual(validate_market_scout(data, required=True), [])

    def test_candidate_evidence_must_resolve_to_scout_tool_call(self):
        data = _valid_input()
        market_scout_report(data)["candidates"][0][
            "evidence_tool_call_ids"
        ] = ["missing-call"]
        self.assertIn(
            "market_scout_candidate_evidence_invalid:0:dangling:missing-call",
            validate_market_scout(data, required=True),
        )

    def test_nested_cycle_time_controls_scout_provenance_window(self):
        data = _valid_input()
        data["as_of"] = "2026-09-17T16:10:00Z"
        data["snapshot"]["as_of"] = "2026-09-10T16:10:00Z"
        market_scout_report(data)["tool_calls"][0]["provenance"][
            "observed_at"
        ] = "2026-09-17T16:10:00Z"
        self.assertTrue(any(
            error.startswith(
                "market_scout_tool_provenance_invalid:"
                "0:observed_at_outside_cycle_window"
            )
            for error in validate_market_scout(data, required=True)
        ))

    def test_scout_reuses_strong_tool_provenance_contract(self):
        data = _valid_input()
        market_scout_report(data)["tool_calls"][0]["provenance"][
            "observed_at"
        ] = "2026-08-01T00:00:00Z"
        self.assertTrue(any(
            error.startswith(
                "market_scout_tool_provenance_invalid:"
                "0:observed_at_outside_cycle_window"
            )
            for error in validate_market_scout(data, required=True)
        ))

    def test_duplicate_candidate_identity_is_refused(self):
        data = _valid_input()
        report = market_scout_report(data)
        duplicate = copy.deepcopy(report["candidates"][0])
        duplicate["candidate_id"] = "scout-candidate-two"
        report["candidates"].append(duplicate)
        self.assertIn(
            "market_scout_candidate_invalid:"
            "1:duplicate_identity:scout-candidate-one",
            validate_market_scout(data, required=True),
        )

    def test_malformed_candidate_identity_is_a_refusal_not_a_crash(self):
        data = _valid_input()
        del market_scout_report(data)["candidates"][0]["identity"][
            "thesis_key"
        ]
        self.assertIn(
            "market_scout_candidate_invalid:"
            "0:identity:missing_fields:thesis_key",
            validate_market_scout(data, required=True),
        )

    def test_budget_usage_is_derived_and_variance_is_explicit(self):
        data = _valid_input()
        self.assertEqual(derived_market_scout_usage(data), {
            "specialist_investigations": 1,
            "external_searches": 1,
            "deep_dives": 0,
            "opportunity_updates": 0,
        })
        report = market_scout_report(data)
        report["budget"]["external_searches"] = 0
        self.assertIn(
            "market_scout_budget_variance_invalid:required",
            validate_market_scout(data, required=True),
        )
        report["budget_variance"] = {
            "exceeded": ["external_searches"],
            "rationale": "Fresh evidence required one unplanned search.",
        }
        self.assertEqual(validate_market_scout(data, required=True), [])

    def test_agenda_link_uses_scout_namespace(self):
        data = _valid_input()
        director = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        director["output"]["research_agenda"]["candidates"][0][
            "scout_candidate_id"
        ] = "unknown-scout-candidate"
        self.assertIn(
            "market_scout_agenda_link_invalid:0:unknown",
            validate_market_scout(data, required=True),
        )

    def test_agenda_link_must_match_scout_identity(self):
        data = _valid_input()
        director = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        director["output"]["research_agenda"]["candidates"][0][
            "instrument"
        ] = "different-instrument"
        self.assertIn(
            "market_scout_agenda_link_invalid:0:instrument",
            validate_market_scout(data, required=True),
        )


class MarketScoutFeedbackTests(unittest.TestCase):
    def test_summary_derives_usage_and_matches_existing_opportunity(self):
        data = _valid_input()
        report = market_scout_report(data)
        identity = normalize_identity(report["candidates"][0]["identity"])
        records = [
            {
                "record_id": "cycle-stage:cycle-one:market_scout",
                "record_type": "cycle_stage",
                "payload": {
                    "cycle_id": "cycle-one",
                    "agent_id": "market_scout",
                    "phase": "discovery",
                    "output": {"market_scout_report": report},
                },
            },
            {
                "record_id": "cycle-stage:cycle-one:research_director",
                "record_type": "cycle_stage",
                "payload": {
                    "cycle_id": "cycle-one",
                    "agent_id": "research_director",
                    "output": {
                        "research_agenda": {
                            "candidates": [{"selected": True}],
                        },
                    },
                },
            },
            {
                "record_id": "cycle-stage:cycle-one:macro_specialist",
                "record_type": "cycle_stage",
                "payload": {
                    "cycle_id": "cycle-one",
                    "agent_id": "macro_specialist",
                    "phase": "specialist",
                    "output": {},
                },
            },
            {
                "record_id": "opportunity-event:event-one",
                "record_type": "opportunity_event",
                "payload": {
                    "cycle_id": "cycle-one",
                    "event_id": "event-one",
                    "opportunity_id": "opportunity-one",
                    "identity": identity,
                    "identity_fingerprint": identity_fingerprint(identity),
                    "soft_identity_fingerprint":
                        soft_identity_fingerprint(identity),
                    "from_state": None,
                    "to_state": "new",
                    "thesis": "Fresh macro conditions changed portfolio risk.",
                    "rationale": "Track the opportunity longitudinally.",
                    "evidence": ["stage:market_scout"],
                    "event_count": 1,
                },
            },
        ]
        summary = market_scout_summary(records)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["usage"], {
            "specialist_investigations": 1,
            "external_searches": 1,
            "deep_dives": 0,
            "opportunity_updates": 1,
        })
        self.assertEqual(
            summary["candidates"][0]["matching_opportunity_ids"],
            ["opportunity-one"],
        )


class MarketScoutPersistenceTests(unittest.TestCase):
    def test_run_one_persists_stage_and_scout_provenance(self):
        root = Path(tempfile.mkdtemp(prefix="market-scout-e2e-"))
        path = root / "cycle.json"
        data = _valid_input()
        data["cycle_id"] = "cycle-market-scout-e2e"
        path.write_text(json.dumps(data), encoding="utf-8")
        journal = AuditJournal(root / "journal.jsonl")

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            receipt = run_one(path, journal)

        self.assertEqual(receipt["cycle_id"], "cycle-market-scout-e2e")
        records = journal.read()
        scout_stage = next(
            record for record in records
            if record.get("record_id")
            == "cycle-stage:cycle-market-scout-e2e:market_scout"
        )
        self.assertEqual(
            scout_stage["payload"]["output"]["market_scout_report"][
                "candidates"
            ][0]["candidate_id"],
            "scout-candidate-one",
        )
        provenance = next(
            record for record in records
            if record.get("record_id")
            == "tool-provenance:cycle-market-scout-e2e"
        )
        self.assertEqual(
            provenance["payload"]["calls"][0]["scope"],
            "market_scout",
        )
        self.assertEqual(
            provenance["payload"]["calls"][0]["tool_call_id"],
            "scout-search-one",
        )
        self.assertTrue(market_scout_summary(records)["available"])


if __name__ == "__main__":
    unittest.main()
