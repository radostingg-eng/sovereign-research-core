"""Tests for the mechanically pre-filled 'next candidate' skeleton.

The template exists so a weak host LLM stops copying stale values (task_id,
expected_slot, opportunity from_state, worker record ids, prior finalized
cycle id) forward from its own previous candidate. Each test below proves
one covered mechanical field is both present and, once its placeholders are
filled in, accepted by the real validator that would otherwise refuse it.

A single end-to-end ``build_semantic_candidate`` round trip is not attempted:
most of a semantic candidate (``source``, ``as_of``, ``snapshot``,
``findings``, ``decision``, ``research``, ``market_scout_report``,
``stage_outputs``) is intentionally left out of the template because it
requires the host's own market/risk reasoning, not journal-derived
mechanics. Synthesizing realistic content for all of those fields just to
exercise one validator per test would make the tests couple to (and
therefore start failing on unrelated changes to) code this module doesn't
touch. Instead each covered code is exercised against the exact validator
function that raises it, with a minimal ``data``/``records`` fixture built
only from documented, journal-derived shapes (matching the patterns already
used by ``test_decision_repetition.py`` and
``test_worker_research_dispositions.py``).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .decision_repetition import validate_decision_repetition_review
from .next_candidate_template import build_next_candidate_template
from .opportunity_ledger import validate_opportunity_updates
from .schedule_ledger import validate_schedule_context
from .worker_projection import persist_worker_projection, worker_projection_id
from .worker_research_dispositions import validate_worker_research_dispositions
from .audit_store import AuditJournal


def _opportunity_event(
    *,
    record_id: str,
    cycle_id: str,
    opportunity_id: str,
    to_state: str,
    observed_at: str,
    missing_id: str = "q1",
    missing_status: str = "open",
    trigger_id: str = "t1",
    trigger_status: str = "active",
) -> dict:
    return {
        "record_id": record_id,
        "record_type": "opportunity_event",
        "payload": {
            "schema_version": 2,
            "cycle_id": cycle_id,
            "event_id": f"{record_id}-event",
            "opportunity_id": opportunity_id,
            "identity": {
                "instrument": "AAPL",
                "instrument_type": "equity",
                "strategy_family": "factor_macro",
                "direction": "long",
                "thesis_key": "aapl-long-thesis",
            },
            "identity_fingerprint": f"fingerprint-{opportunity_id}",
            "to_state": to_state,
            "research_state": {
                "missing_information": [{
                    "id": missing_id,
                    "question": "What would confirm the thesis?",
                    "why_it_matters": "It changes the decision.",
                    "status": missing_status,
                }],
                "uncertainties": [{
                    "id": "u1",
                    "description": "Demand durability is unclear.",
                    "status": "open",
                }],
                "review_triggers": [{
                    "id": trigger_id,
                    "condition": "Guidance is cut next earnings call.",
                    "status": trigger_status,
                }],
                "next_question_id": missing_id if missing_status == "open"
                else None,
            },
            "observed_at": observed_at,
        },
    }


def _finalized_cycle_records(
    cycle_id: str,
    *,
    status: str = "wait",
    completed_at: str = "2026-09-21T01:00:00Z",
) -> list[dict]:
    return [
        {
            "record_id": f"cycle-receipt:{cycle_id}",
            "record_type": "cycle_receipt",
            "payload": {
                "cycle_id": cycle_id,
                "decision_status": status,
                "completed_at": completed_at,
            },
        },
        {
            "record_id": f"cycle-finalization:{cycle_id}",
            "record_type": "cycle_finalization",
            "caused_by": [f"cycle-receipt:{cycle_id}"],
            "payload": {
                "schema_version": 1,
                "cycle_id": cycle_id,
                "input": {"canonical_sha256": "a" * 64},
                "receipt": {"record_id": f"cycle-receipt:{cycle_id}"},
            },
        },
    ]


class NextCandidateTemplateTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile_root = Path(self._tmp.name)
        (self.profile_root / "runs").mkdir(parents=True)
        (self.profile_root / "runs" / "SCHEDULE.json").write_text(
            json.dumps({
                "schema_version": 1,
                "enabled": True,
                "task_id": "Sovereign Research IBKR hourly v2",
                "task_name": "Sovereign Research IBKR hourly v2",
                "timezone": "Europe/Prague",
                "cadence_minutes": 60,
                "anchor_at": "2026-09-20T12:57:00+00:00",
                "grace_minutes": 15,
                "source_max_age_minutes": 30,
                "accounting_window_hours": 48,
                "min_workflow_version": 2,
                "effective_core_commit": "a" * 40,
                "effective_host_input_schema_version": 1,
                "effective_prompt_sha256": "b" * 64,
            }),
            encoding="utf-8",
        )

    # -- schedule_context -------------------------------------------------

    def test_schedule_context_has_exact_task_id_from_schedule_json(self):
        template = build_next_candidate_template(
            [], research_inbox=None, profile_root=self.profile_root,
        )
        context = template["schedule_context"]
        self.assertEqual(
            context["task_id"], "Sovereign Research IBKR hourly v2",
        )
        self.assertTrue(context["expected_slot"].startswith("<FILL:"))
        self.assertIn("started_at", context["expected_slot"])

    def test_filled_schedule_context_passes_task_id_and_slot_checks(self):
        template = build_next_candidate_template(
            [], research_inbox=None, profile_root=self.profile_root,
        )
        context = dict(template["schedule_context"])
        # Fill placeholders the way the instructions ask the host to: pick
        # an actual started_at, derive expected_slot per the anchor/cadence/
        # grace rule the template states, in whole cadence-minute steps
        # from anchor (13:57Z is anchor + 1 cadence).
        context.update({
            "started_at": "2026-09-20T13:57:00Z",
            "source_observed_at": "2026-09-20T13:57:00Z",
            "expected_slot": "2026-09-20T13:57:00Z",
            "trigger": "scheduled",
            "intervention": "none",
        })
        contract = json.loads(
            (self.profile_root / "runs" / "SCHEDULE.json").read_text()
        )
        errors = validate_schedule_context(
            context,
            contract=contract,
            candidate_as_of="2026-09-20T13:57:00Z",
            candidate_cycle_id="cycle-20260920T135700Z-test",
            candidate_committed_at="2026-09-20T13:57:30Z",
        )
        self.assertNotIn("schedule_context_task_id", errors)
        self.assertEqual(errors, [])

    # -- opportunity revisit rows ------------------------------------------

    def test_opportunity_row_carries_from_state_and_stable_text(self):
        records = [
            _opportunity_event(
                record_id="opportunity-event:1",
                cycle_id="cycle-a",
                opportunity_id="opportunity-one",
                to_state="watch",
                observed_at="2026-09-21T10:00:00Z",
            ),
        ]
        template = build_next_candidate_template(
            records, research_inbox=None, profile_root=None,
        )
        rows = template["opportunity_updates"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["opportunity_id"], "opportunity-one")
        self.assertEqual(row["from_state"], "watch")
        missing = row["research_state"]["missing_information"]
        self.assertEqual(missing[0]["id"], "q1")
        self.assertEqual(
            missing[0]["question"], "What would confirm the thesis?",
        )
        self.assertEqual(missing[0]["status"], "open")
        self.assertEqual(row["research_state"]["next_question_id"], "q1")
        self.assertEqual(row["revisit"]["target_missing_information_id"], "q1")
        self.assertIn("t1", row["revisit"]["trigger_id"])

    def test_filled_revisit_row_passes_state_mismatch_and_stable_text_checks(
        self,
    ):
        records = [
            _opportunity_event(
                record_id="opportunity-event:1",
                cycle_id="cycle-a",
                opportunity_id="opportunity-one",
                to_state="watch",
                observed_at="2026-09-21T10:00:00Z",
            ),
        ]
        template = build_next_candidate_template(
            records, research_inbox=None, profile_root=None,
        )
        row = dict(template["opportunity_updates"][0])
        row.update({
            "event_id": "opportunity-one-revisit-1",
            "to_state": row["from_state"],  # unchanged: a pure revisit
            "thesis": "AAPL services growth supports re-rating.",
            "rationale": "Revisited the open question with new evidence.",
            "evidence": ["stage:research"],
        })
        row["research_state"] = dict(row["research_state"])
        row["research_state"].pop("_note", None)
        row["revisit"] = {
            "trigger_id": "t1",
            "target_missing_information_id": "q1",
            "expected_information_gain": "Confirms or rejects the thesis.",
            "result": "no_new_information",
            "result_summary": "No new evidence changed the picture.",
            "evidence": ["stage:research"],
            "legacy_state_initialization": False,
            "retarget_reason": None,
        }
        data = {
            "cycle_id": "cycle-b",
            "cognitive_stages": [{"stage_id": "research"}],
            "findings": [],
        }
        errors = validate_opportunity_updates(
            [row],
            data=data,
            records=records,
        )
        self.assertNotIn(
            next(
                (e for e in errors if e.startswith("opportunity_state_mismatch")),
                None,
            ),
            errors,
        )
        changed_codes = [
            e for e in errors
            if "opportunity_research_state_invalid" in e and ":changed:" in e
        ]
        self.assertEqual(changed_codes, [])

    # -- worker research dispositions ---------------------------------------

    def test_worker_dispositions_cover_every_required_record_id(self):
        journal_path = self.profile_root / "audit.jsonl"
        journal = AuditJournal(journal_path)
        summary = {
            "items": [{
                "record_id": "worker-rec-1",
                "worker_id": "worker-1",
                "status": "completed",
                "observed_at": "2026-09-21T09:00:00Z",
                "target": {"question_id": "q1", "question": "Q?"},
                "result": {"summary": "A bounded lead."},
            }],
            "adoption_required_record_ids": ["worker-rec-1"],
        }
        summary["projection_id"] = worker_projection_id(summary)
        persist_worker_projection(summary, journal)
        records = journal.read()

        template = build_next_candidate_template(
            records, research_inbox=summary, profile_root=None,
        )
        self.assertEqual(
            template["worker_research_projection_id"],
            summary["projection_id"],
        )
        rows = template["worker_research_dispositions"]
        self.assertEqual(
            [row["worker_record_id"] for row in rows], ["worker-rec-1"],
        )

        filled = dict(rows[0])
        filled.update({
            "disposition": "rejected",
            "evidence": [],
            "rationale": "Not independently confirmed.",
            "revisit_condition": None,
        })
        errors = validate_worker_research_dispositions(
            [filled],
            data={"cycle_id": "cycle-b", "schedule_context": {}},
            profile_root=None,
            records=records,
            projection_id=summary["projection_id"],
        )
        self.assertNotIn(
            "worker_research_disposition_missing:worker-rec-1", errors,
        )

    # -- decision repetition --------------------------------------------

    def test_prior_finalized_cycle_id_is_the_last_finalized_not_refused(self):
        records = (
            _finalized_cycle_records(
                "cycle-old-finalized", completed_at="2026-09-20T01:00:00Z",
            )
            + _finalized_cycle_records(
                "cycle-latest-finalized",
                completed_at="2026-09-21T01:00:00Z",
            )
        )
        template = build_next_candidate_template(
            records, research_inbox=None, profile_root=None,
        )
        hint = template["decision_repetition"]
        self.assertEqual(
            hint["prior_finalized_cycle_id"], "cycle-latest-finalized",
        )

    def test_filled_repetition_review_prior_cycle_id_matches_validator(self):
        records = (
            _finalized_cycle_records(
                "cycle-old-finalized", completed_at="2026-09-20T01:00:00Z",
            )
            + _finalized_cycle_records(
                "cycle-latest-finalized",
                completed_at="2026-09-21T01:00:00Z",
                status="wait",
            )
        )
        template = build_next_candidate_template(
            records, research_inbox=None, profile_root=None,
        )
        prior_cycle_id = template["decision_repetition"][
            "prior_finalized_cycle_id"
        ]

        def review_errors(cycle_id: str) -> list[str]:
            data = {
                "cycle_id": "cycle-current",
                "decision": {
                    "status": "wait",
                    "repetition_review": {
                        "prior_cycle_id": cycle_id,
                        "disposition": "deliberate_wait",
                        "evidence_delta": [],
                        "unresolved_question_ids": [],
                        "rationale": "Nothing material has changed yet.",
                    },
                },
            }
            return validate_decision_repetition_review(
                data, records=records, required=True,
            )

        mismatch_errors = review_errors("cycle-old-finalized")
        self.assertTrue(
            any(
                code.startswith("decision_repetition_prior_cycle_mismatch")
                for code in mismatch_errors
            ),
            mismatch_errors,
        )
        matching_errors = review_errors(prior_cycle_id)
        self.assertFalse(
            any(
                code.startswith("decision_repetition_prior_cycle_mismatch")
                for code in matching_errors
            ),
            matching_errors,
        )

    def test_committed_question_ids_surfaced_for_next_cycle(self):
        records = [
            _opportunity_event(
                record_id="opportunity-event:1",
                cycle_id="cycle-a",
                opportunity_id="opportunity-one",
                to_state="watch",
                observed_at="2026-09-21T10:00:00Z",
            ),
        ]
        template = build_next_candidate_template(
            records, research_inbox=None, profile_root=None,
        )
        committed = template["decision_repetition"]["committed_questions"]
        self.assertIn(
            {"opportunity_id": "opportunity-one", "question_id": "q1"},
            committed,
        )

    # -- determinism / size -------------------------------------------------

    def test_build_is_deterministic_across_calls(self):
        records = [
            _opportunity_event(
                record_id="opportunity-event:1",
                cycle_id="cycle-a",
                opportunity_id="opportunity-one",
                to_state="watch",
                observed_at="2026-09-21T10:00:00Z",
            ),
        ] + _finalized_cycle_records("cycle-old-finalized")
        research_inbox = {
            "projection_id": "worker-research-projection:v1:" + "0" * 64,
            "adoption_required_record_ids": ["worker-rec-1"],
        }
        first = build_next_candidate_template(
            records,
            research_inbox=research_inbox,
            profile_root=self.profile_root,
        )
        second = build_next_candidate_template(
            records,
            research_inbox=research_inbox,
            profile_root=self.profile_root,
        )
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )

    def test_template_stays_within_size_budget(self):
        records = [
            _opportunity_event(
                record_id=f"opportunity-event:{index}",
                cycle_id="cycle-a",
                opportunity_id=f"opportunity-{index}",
                to_state="watch",
                observed_at="2026-09-21T10:00:00Z",
            )
            for index in range(20)
        ] + _finalized_cycle_records("cycle-old-finalized")
        research_inbox = {
            "projection_id": "worker-research-projection:v1:" + "0" * 64,
            "adoption_required_record_ids": [
                f"worker-rec-{index}" for index in range(30)
            ],
        }
        template = build_next_candidate_template(
            records,
            research_inbox=research_inbox,
            profile_root=self.profile_root,
        )
        serialized = json.dumps(template)
        self.assertLess(
            len(serialized.encode("utf-8")), 15_000, len(serialized),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
