"""Tests for the open-ended `discovery` Azure worker role.

Discovery differs from every other role: it does not select one of the
host's existing research candidates (via `select_target`); it gets a
bounded target built from the opportunity ledger's existing identities
(via `select_discovery_target`) and proposes up to `MAX_DISCOVERY_LEADS`
NEW leads that must not duplicate an existing identity.
"""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ops.azure_worker import (
    DISCOVERY_ROLE,
    MAX_DISCOVERY_LEADS,
    build_request,
    run_worker,
    select_discovery_target,
)
from runtime.audit_store import AuditJournal
from runtime.research_inbox import load_inbox_record, research_inbox_summary
from runtime.worker_projection import (
    load_worker_projection,
    persist_worker_projection,
    worker_projection_id,
)
from runtime.worker_research_dispositions import (
    projected_worker_records,
    validate_worker_research_dispositions,
)
from runtime.worker_role_contracts import (
    discovery_identity_key,
    role_result_validation_errors,
)


def discovery_feedback(*, not_shown=0):
    return {
        "opportunity_ledger": {
            "not_shown": not_shown,
            "items": [{
                "opportunity_id": "opportunity-copper",
                "identity_fingerprint": "fingerprint-copper",
                "state": "researching",
                "identity": {
                    "instrument": "copper futures",
                    "instrument_type": "future",
                    "strategy_family": "supply_shock_momentum",
                    "direction": "long",
                    "thesis_key": "strike disruption",
                },
                "research_state": {"missing_information": []},
            }],
        },
    }


def valid_lead(**overrides):
    lead = {
        "instrument_or_theme": "lithium miners",
        "strategy_family": "battery_demand_ramp",
        "mechanism_or_thesis": "EV battery demand outstrips new supply.",
        "why_now": "New gigafactory capacity announced this quarter.",
        "strongest_primary_evidence": "Offtake agreement filings.",
        "strongest_counterevidence": "Brine project ramp could add supply.",
        "cheap_test": "Check spot lithium carbonate price trend.",
        "novelty_vs_existing": (
            "No existing ledger identity shares this instrument and "
            "strategy_family."
        ),
    }
    lead.update(overrides)
    return lead


def discovery_result(leads):
    return {
        "summary": "Discovery scan for leads outside the current ledger.",
        "uncertainties": ["Demand ramp timing is uncertain."],
        "suggested_next_question": "How fast is gigafactory capacity coming online?",
        "falsification_conditions": [{
            "claim": "The lead is decision-relevant.",
            "condition": "Supply ramps faster than demand.",
            "evidence_needed": "Producer capacity disclosures.",
        }],
        "leads": leads,
    }


def discovery_worker_record(
    record_id: str,
    *,
    observed_at: str,
    question_id: str,
    leads,
) -> dict:
    return {
        "schema_version": 1,
        "record_id": record_id,
        "worker_id": "azure-a-discovery",
        "status": "completed",
        "origin": "worker_attested",
        "observed_at": observed_at,
        "expires_at": "2026-09-22T00:00:00Z",
        "deployment": {"deployment": "gpt-6-astra"},
        "selection": {"rule": "discovery_target_from_ledger_identities"},
        "target": {
            "question_id": question_id,
            "question": "Find up to 3 opportunities not already present.",
            "why_it_matters": "Discovery surfaces new leads.",
            "existing_identities": [
                {"instrument": "copper futures", "strategy_family": "supply_shock_momentum"},
            ],
        },
        "request": {
            "sha256": "a" * 64,
            "role": DISCOVERY_ROLE,
            "output_contract": {
                "schema_version": 3,
                "role": DISCOVERY_ROLE,
            },
        },
        "result": discovery_result(leads),
        "quality": {"result_schema_complete": True},
    }


class DiscoveryTargetTests(unittest.TestCase):
    def test_target_construction_contains_no_forbidden_keys(self):
        target = select_discovery_target(discovery_feedback())

        self.assertIsNotNone(target)
        encoded = json.dumps(target).casefold()
        for forbidden in (
            "\"account\"", "\"cash\"", "\"positions\"", "\"quantity\"",
            "net_liquidation_value",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(
            target["existing_identities"],
            [{"instrument": "copper futures", "strategy_family": "supply_shock_momentum"}],
        )

    def test_build_request_for_discovery_role_is_safe_and_bounded(self):
        target = select_discovery_target(discovery_feedback())
        request = build_request(target, role=DISCOVERY_ROLE)

        encoded = json.dumps(request).casefold()
        for forbidden in ("\"cash\"", "\"positions\"", "\"quantity\""):
            self.assertNotIn(forbidden, encoded)
        self.assertIn(str(MAX_DISCOVERY_LEADS), request["instructions"])
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertIn(
            "existing_identities", json.loads(request["input"]),
        )

    def test_truncated_ledger_yields_no_target(self):
        self.assertIsNone(
            select_discovery_target(discovery_feedback(not_shown=1))
        )

    def test_distinct_ledger_snapshots_get_distinct_question_ids(self):
        first = select_discovery_target(
            discovery_feedback(), target_offset=4, rotation_index=100,
        )
        second = select_discovery_target(
            discovery_feedback(), target_offset=4, rotation_index=101,
        )
        self.assertNotEqual(first["question_id"], second["question_id"])


class DiscoveryRoleValidationTests(unittest.TestCase):
    def test_valid_lead_passes(self):
        errors = role_result_validation_errors(
            discovery_result([valid_lead()]),
            role=DISCOVERY_ROLE,
        )
        self.assertEqual(errors, [])

    def test_duplicate_of_existing_identity_is_rejected(self):
        duplicate = valid_lead(
            instrument_or_theme="Copper Futures",
            strategy_family="Supply Shock Momentum",
        )
        errors = role_result_validation_errors(
            discovery_result([duplicate]),
            role=DISCOVERY_ROLE,
            existing_identities=[{
                "instrument": "copper futures",
                "strategy_family": "supply_shock_momentum",
            }],
        )
        self.assertIn("leads:0:duplicate_of_existing", errors)

    def test_forbidden_extra_key_in_lead_is_rejected(self):
        bad_lead = valid_lead()
        bad_lead["account"] = "12345"
        errors = role_result_validation_errors(
            discovery_result([bad_lead]),
            role=DISCOVERY_ROLE,
        )
        self.assertIn("leads:0", errors)

    def test_over_max_leads_is_rejected(self):
        errors = role_result_validation_errors(
            discovery_result([valid_lead() for _ in range(MAX_DISCOVERY_LEADS + 1)]),
            role=DISCOVERY_ROLE,
        )
        self.assertIn("leads", errors)

    def test_identity_key_normalizes_case_and_whitespace(self):
        self.assertEqual(
            discovery_identity_key("Copper  Futures", "Supply-Shock Momentum"),
            discovery_identity_key("copper futures", "supply_shock momentum"),
        )


class DiscoveryRunWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="discovery-worker-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.feedback = self.root / "FEEDBACK.json"
        self.feedback.write_text(
            json.dumps(discovery_feedback()), encoding="utf-8",
        )
        self.outbox = self.root / "outbox"

    def test_valid_discovery_lead_completes(self):
        def caller(**_kwargs):
            return (
                discovery_result([valid_lead()]),
                {
                    "id": "response-1",
                    "model": "gpt-6-astra",
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                },
            )

        path = run_worker(
            feedback_path=self.feedback,
            outbox_dir=self.outbox,
            worker_id="azure-a-discovery",
            endpoint="https://example.openai.azure.com",
            deployment="gpt-6-astra",
            subscription_id="sub-test",
            role=DISCOVERY_ROLE,
            now=datetime(2026, 9, 21, 0, 45, tzinfo=timezone.utc),
            caller=caller,
        )
        value = load_inbox_record(path)

        self.assertEqual(value["status"], "completed")
        self.assertEqual(
            value["target"]["existing_identities"],
            [{"instrument": "copper futures", "strategy_family": "supply_shock_momentum"}],
        )
        self.assertEqual(len(value["result"]["leads"]), 1)

    def test_duplicate_lead_is_never_recorded_as_completed(self):
        duplicate = valid_lead(
            instrument_or_theme="copper futures",
            strategy_family="supply_shock_momentum",
        )

        def caller(**_kwargs):
            return (
                discovery_result([duplicate]),
                {
                    "id": "response-1",
                    "model": "gpt-6-astra",
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                },
            )

        path = run_worker(
            feedback_path=self.feedback,
            outbox_dir=self.outbox,
            worker_id="azure-a-discovery",
            endpoint="https://example.openai.azure.com",
            deployment="gpt-6-astra",
            subscription_id="sub-test",
            role=DISCOVERY_ROLE,
            now=datetime(2026, 9, 21, 0, 45, tzinfo=timezone.utc),
            caller=caller,
        )
        value = load_inbox_record(path)

        self.assertNotEqual(value["status"], "completed")
        self.assertEqual(
            value["error"]["detail_code"], "invalid_structured_output",
        )


class DiscoveryProjectionAndDispositionTests(unittest.TestCase):
    """A completed discovery record must flow through the same projection
    and disposition machinery as every other role."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="discovery-projection-"))
        self.addCleanup(shutil.rmtree, self.root)

    def write_record(self, value: dict) -> None:
        path = (
            self.root
            / "research_inbox"
            / value["worker_id"]
            / f"{value['record_id'].replace(':', '-')}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_record_round_trips_through_projection_and_disposition(self):
        self.write_record(discovery_worker_record(
            "discovery-1",
            observed_at="2026-09-21T00:45:00Z",
            question_id="discovery-aaaa",
            leads=[valid_lead()],
        ))

        summary = research_inbox_summary(
            self.root, now=datetime(2026, 9, 21, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["record_count"], 1)
        self.assertEqual(summary["items"][0]["result"]["role"], DISCOVERY_ROLE)

        projected = projected_worker_records(
            self.root, source_observed_at="2026-09-21T01:00:00Z",
        )
        self.assertEqual([row["record_id"] for row in projected], ["discovery-1"])

        proj_summary = {
            "items": projected,
            "adoption_required_record_ids": [
                row["record_id"] for row in projected
            ],
        }
        projection_id = worker_projection_id(proj_summary)
        journal = AuditJournal(self.root / "audit" / "2026" / "09-21.jsonl")
        proj_summary["projection_id"] = projection_id
        self.assertEqual(
            persist_worker_projection(proj_summary, journal), projection_id,
        )

        loaded = load_worker_projection(projection_id, journal.read())
        self.assertEqual([row["record_id"] for row in loaded], ["discovery-1"])

        data = {
            "host_input_schema_version": 4,
            "cycle_id": "cycle-discovery",
            "schedule_context": {
                "source_observed_at": "2026-09-21T01:00:00Z",
            },
            "cognitive_stages": [{"stage_id": "research_director"}],
            "findings": [],
            "worker_research_projection_id": projection_id,
        }
        rows = [{
            "worker_record_id": "discovery-1",
            "disposition": "used_as_lead",
            "evidence": ["stage:research_director"],
            "rationale": "Adopted the lithium demand-ramp lead for scouting.",
            "revisit_condition": None,
        }]

        errors = validate_worker_research_dispositions(
            rows,
            data=data,
            profile_root=self.root,
            records=journal.read(),
        )
        self.assertEqual(errors, [])

    def test_reject_disposition_also_round_trips(self):
        self.write_record(discovery_worker_record(
            "discovery-2",
            observed_at="2026-09-21T00:45:00Z",
            question_id="discovery-bbbb",
            leads=[valid_lead()],
        ))
        data = {
            "host_input_schema_version": 4,
            "cycle_id": "cycle-discovery-2",
            "schedule_context": {
                "source_observed_at": "2026-09-21T01:00:00Z",
            },
            "cognitive_stages": [{"stage_id": "research_director"}],
            "findings": [],
        }
        rows = [{
            "worker_record_id": "discovery-2",
            "disposition": "rejected",
            "evidence": [],
            "rationale": "Lithium demand thesis is already covered internally.",
            "revisit_condition": None,
        }]
        self.assertEqual(
            validate_worker_research_dispositions(
                rows, data=data, profile_root=self.root, records=(),
            ),
            [],
        )

    def test_negative_two_discovery_records_are_not_deduplicated_away(self):
        """Regression guard: research_inbox_summary used to dedupe on
        `target.question_id`, which was always empty for every role that
        didn't set it. Discovery must give every distinct target a real
        question_id so two different discovery runs both survive."""
        self.write_record(discovery_worker_record(
            "discovery-first",
            observed_at="2026-09-21T00:45:00Z",
            question_id="discovery-aaaa",
            leads=[valid_lead()],
        ))
        self.write_record(discovery_worker_record(
            "discovery-second",
            observed_at="2026-09-21T03:45:00Z",
            question_id="discovery-bbbb",
            leads=[valid_lead(instrument_or_theme="uranium miners")],
        ))

        summary = research_inbox_summary(
            self.root, now=datetime(2026, 9, 21, 4, tzinfo=timezone.utc),
        )

        self.assertEqual(
            sorted(row["record_id"] for row in summary["items"]),
            ["discovery-first", "discovery-second"],
        )


if __name__ == "__main__":
    unittest.main()
