import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .audit_store import AuditJournal
from .opportunity_ledger import (
    backfill_opportunity_events,
    identity_fingerprint,
    opportunity_ledger_summary,
    validate_opportunity_updates,
)
from .run_host_cycle import main as run_host_cycle_main, run_one
from .test_learning_dispositions import v3_input


def identity(**overrides):
    value = {
        "instrument": "VRT",
        "instrument_type": "equity",
        "strategy_family": "special_situations",
        "direction": "long",
        "thesis_key": "data-center power acquisition",
    }
    value.update(overrides)
    return value


def event(**overrides):
    value = {
        "event_id": "vrt-new",
        "opportunity_id": "vrt-special-situation",
        "from_state": None,
        "to_state": "new",
        "identity": identity(),
        "thesis": "A corporate action may change normalized earnings power.",
        "rationale": "Fresh transaction evidence makes the idea worth retaining.",
        "evidence": ["finding:vrt-event"],
    }
    value.update(overrides)
    return value


def research_state(
    *,
    question_id="valuation-bridge",
    question_status="open",
    next_question_id="valuation-bridge",
    extra_missing=(),
    extra_triggers=(),
):
    return {
        "missing_information": [{
            "id": question_id,
            "question": "What cash-flow value is attributable to the event?",
            "why_it_matters": "The answer determines whether repricing is supported.",
            "status": question_status,
        }, *extra_missing],
        "uncertainties": [{
            "id": "economics-uncertain",
            "description": "Incremental economics are not yet measured.",
            "status": "open",
        }],
        "review_triggers": [{
            "id": "new-company-economics",
            "condition": "Fresh company evidence changes the economic bridge.",
            "status": "active",
        }, *extra_triggers],
        "next_question_id": next_question_id,
    }


def revisit(**overrides):
    value = {
        "trigger_id": "new-company-economics",
        "target_missing_information_id": "valuation-bridge",
        "expected_information_gain": (
            "Quantify whether the event adds enough cash flow to support "
            "the current valuation."
        ),
        "result": "partially_resolved",
        "result_summary": (
            "Fresh evidence narrowed the range but did not close the bridge."
        ),
        "evidence": ["finding:vrt-event"],
        "legacy_state_initialization": False,
        "retarget_reason": None,
    }
    value.update(overrides)
    return value


def input_with(*updates, cycle_id="cycle-opportunity", **overrides):
    data = v3_input(
        cycle_id=cycle_id,
        findings=[{
            "id": "vrt-event",
            "claim": "A current corporate action was observed.",
        }],
        opportunity_updates=list(updates),
    )
    data.update(overrides)
    return data


class OpportunityValidationTests(unittest.TestCase):
    def test_initial_event_has_stable_normalized_identity(self):
        data = input_with(event())
        self.assertEqual(
            validate_opportunity_updates(
                data["opportunity_updates"], data=data, records=[]),
            [],
        )
        self.assertEqual(
            identity_fingerprint(identity(instrument="  vrt  ")),
            identity_fingerprint(identity(instrument="VRT")),
        )

    def test_exact_identity_cannot_hide_behind_another_id(self):
        data = input_with(
            event(),
            event(
                event_id="vrt-duplicate",
                opportunity_id="another-vrt-id",
            ),
        )
        self.assertIn(
            "opportunity_duplicate_identity:1:"
            "another-vrt-id:vrt-special-situation",
            validate_opportunity_updates(
                data["opportunity_updates"], data=data, records=[]),
        )

    def test_multiple_ordered_events_for_one_opportunity_are_valid(self):
        data = input_with(
            event(),
            event(
                event_id="vrt-watch",
                from_state="new",
                to_state="watch",
                rationale="The idea is retained while valuation is reviewed.",
            ),
        )
        self.assertEqual(
            validate_opportunity_updates(
                data["opportunity_updates"], data=data, records=[]),
            [],
        )

    def test_evidence_must_resolve_inside_the_current_cycle(self):
        data = input_with(event(evidence=["finding:missing"]))
        self.assertIn(
            "opportunity_evidence_invalid:0:dangling_ref:finding:missing",
            validate_opportunity_updates(
                data["opportunity_updates"], data=data, records=[]),
        )

    def test_terminal_reopen_requires_latest_event_record(self):
        terminal = input_with(
            event(to_state="new"),
            event(
                event_id="vrt-rejected",
                from_state="new",
                to_state="rejected",
            ),
        )
        root = Path(tempfile.mkdtemp(prefix="opportunity-terminal-"))
        path = root / "cycle.json"
        path.write_text(json.dumps(terminal), encoding="utf-8")
        journal = AuditJournal(root / "journal.jsonl")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(path, journal)

        reopened = input_with(
            event(
                event_id="vrt-reopened",
                from_state="rejected",
                to_state="new",
                reopens_event_id="opportunity-event:vrt-rejected",
            ),
            cycle_id="cycle-opportunity-reopen",
        )
        self.assertEqual(
            validate_opportunity_updates(
                reopened["opportunity_updates"],
                data=reopened,
                records=journal.read(),
            ),
            [],
        )
        reopened["opportunity_updates"][0][
            "reopens_event_id"
        ] = "opportunity-event:not-latest"
        self.assertIn(
            "opportunity_reopen_invalid:0:vrt-special-situation",
            validate_opportunity_updates(
                reopened["opportunity_updates"],
                data=reopened,
                records=journal.read(),
            ),
        )

    def test_new_staged_events_require_explicit_research_state(self):
        data = input_with(event())
        self.assertIn(
            "opportunity_research_state_invalid:0:not_object",
            validate_opportunity_updates(
                data["opportunity_updates"],
                data=data,
                records=[],
                require_research_state=True,
            ),
        )

        data["opportunity_updates"][0]["research_state"] = research_state()
        self.assertEqual(
            validate_opportunity_updates(
                data["opportunity_updates"],
                data=data,
                records=[],
                require_research_state=True,
            ),
            [],
        )

    def test_research_state_ratchet_applies_after_effective_time(self):
        data = input_with(
            event(),
            as_of="2026-09-17T20:24:26Z",
        )

        self.assertIn(
            "opportunity_research_state_invalid:0:not_object",
            validate_opportunity_updates(
                data["opportunity_updates"],
                data=data,
                records=[],
            ),
        )

    def test_research_state_ratchet_preserves_historical_replay(self):
        data = input_with(
            event(),
            as_of="2026-09-17T20:24:24Z",
        )

        self.assertEqual(
            validate_opportunity_updates(
                data["opportunity_updates"],
                data=data,
                records=[],
            ),
            [],
        )

    def test_selected_existing_opportunity_requires_explicit_agenda_link(self):
        root = Path(tempfile.mkdtemp(prefix="opportunity-agenda-link-"))
        path = root / "first.json"
        first = input_with(event(research_state=research_state()))
        path.write_text(json.dumps(first), encoding="utf-8")
        journal = AuditJournal(root / "journal.jsonl")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(path, journal)

        second = input_with(cycle_id="cycle-second")
        scout = next(
            row for row in second["cognitive_stages"]
            if row["stage_id"] == "market_scout"
        )
        scout_candidate = scout["output"]["market_scout_report"][
            "candidates"
        ][0]
        scout_candidate["identity"] = identity()
        director = next(
            row for row in second["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        selected = director["output"]["research_agenda"]["candidates"][0]
        selected["instrument"] = "VRT"
        selected["strategy_family"] = "special_situations"

        self.assertIn(
            "opportunity_agenda_link_invalid:"
            "0:existing_id_required:vrt-special-situation",
            validate_opportunity_updates(
                [],
                data=second,
                records=journal.read(),
                require_research_state=True,
            ),
        )

    def test_same_state_revisit_uses_prior_committed_question(self):
        root = Path(tempfile.mkdtemp(prefix="opportunity-revisit-"))
        path = root / "first.json"
        first = input_with(event(research_state=research_state()))
        path.write_text(json.dumps(first), encoding="utf-8")
        journal = AuditJournal(root / "journal.jsonl")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(path, journal)

        extra = {
            "id": "unit-economics",
            "question": "What unit economics remain after the transaction?",
            "why_it_matters": "This becomes the next valuation input.",
            "status": "open",
        }
        updated_state = research_state(
            question_status="resolved",
            next_question_id="unit-economics",
            extra_missing=(extra,),
        )
        update = event(
            event_id="vrt-researched",
            from_state="new",
            to_state="new",
            research_state=updated_state,
            revisit=revisit(result="resolved"),
        )
        second = input_with(update, cycle_id="cycle-second")
        scout = next(
            row for row in second["cognitive_stages"]
            if row["stage_id"] == "market_scout"
        )
        scout["output"]["market_scout_report"]["candidates"][0][
            "identity"
        ] = identity()
        director = next(
            row for row in second["cognitive_stages"]
            if row["stage_id"] == "research_director"
        )
        selected = director["output"]["research_agenda"]["candidates"][0]
        selected.update({
            "instrument": "VRT",
            "strategy_family": "special_situations",
            "opportunity_id": "vrt-special-situation",
        })

        self.assertEqual(
            validate_opportunity_updates(
                second["opportunity_updates"],
                data=second,
                records=journal.read(),
                require_research_state=True,
            ),
            [],
        )

    def test_legacy_event_has_one_explicit_initialization_path(self):
        root = Path(tempfile.mkdtemp(prefix="opportunity-legacy-state-"))
        path = root / "legacy.json"
        legacy = input_with(event())
        path.write_text(json.dumps(legacy), encoding="utf-8")
        journal = AuditJournal(root / "journal.jsonl")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(path, journal)

        update = event(
            event_id="vrt-state-initialized",
            from_state="new",
            to_state="new",
            research_state=research_state(),
            revisit=revisit(
                result="no_new_information",
                legacy_state_initialization=True,
            ),
        )
        current = input_with(update, cycle_id="cycle-current")
        self.assertEqual(
            validate_opportunity_updates(
                current["opportunity_updates"],
                data=current,
                records=journal.read(),
                require_research_state=True,
            ),
            [],
        )


class OpportunityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="opportunity-ledger-"))
        self.input_path = self.root / "cycle.json"
        self.journal = AuditJournal(self.root / "journal.jsonl")

    def test_event_persists_with_receipt_and_evidence_anchors(self):
        data = input_with(event())
        self.input_path.write_text(json.dumps(data), encoding="utf-8")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(self.input_path, self.journal)

        record = next(
            row for row in self.journal.read()
            if row.get("record_type") == "opportunity_event"
        )
        self.assertEqual(
            record["record_id"], "opportunity-event:vrt-new")
        self.assertEqual(
            record["caused_by"],
            [
                "cycle-receipt:cycle-opportunity",
                "cycle-stage:cycle-opportunity:decision",
            ],
        )
        self.assertEqual(
            record["payload"]["evidence_record_ids"],
            ["cycle-stage:cycle-opportunity:decision"],
        )
        summary = opportunity_ledger_summary(self.journal.read())
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["counts_by_state"], {"new": 1})
        self.assertEqual(
            summary["items"][0]["opportunity_id"],
            "vrt-special-situation",
        )
        feedback_dir = self.root / "host_input"
        feedback_dir.mkdir()
        before = self.journal.read()
        self.assertEqual(
            run_host_cycle_main([
                "--input-dir", str(feedback_dir),
                "--journal", str(self.journal.path),
                "--refresh-feedback-only",
            ]),
            0,
        )
        after = self.journal.read()
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(
            [row["record_type"] for row in after[len(before):]],
            ["worker_research_projection"],
        )
        self.assertEqual(after[-1]["payload"]["items"], [])
        feedback = json.loads(
            (feedback_dir / "FEEDBACK.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            feedback["opportunity_ledger"]["items"][0]["recent_history"][
                0
            ]["record_id"],
            record["record_id"],
        )

    def test_research_state_and_revisit_metrics_persist(self):
        second_trigger = {
            "id": "second-company-update",
            "condition": "Another source updates the economic bridge.",
            "status": "active",
        }
        stable_state = research_state(extra_triggers=(second_trigger,))
        first = input_with(event(research_state=research_state()))
        self.input_path.write_text(json.dumps(first), encoding="utf-8")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(self.input_path, self.journal)

        second_path = self.root / "second.json"
        second = input_with(
            event(
                event_id="vrt-no-new-information",
                from_state="new",
                to_state="new",
                research_state=stable_state,
                revisit=revisit(result="no_new_information"),
            ),
            cycle_id="cycle-second",
        )
        second_path.write_text(json.dumps(second), encoding="utf-8")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(second_path, self.journal)

        summary = opportunity_ledger_summary(self.journal.read())
        item = summary["items"][0]
        self.assertEqual(
            item["research_state"]["next_question_id"],
            "valuation-bridge",
        )
        self.assertEqual(item["revisit_metrics"]["attempts"], 1)
        self.assertEqual(
            item["revisit_metrics"]["no_new_information"], 1,
        )
        self.assertEqual(
            item["revisit_metrics"]["attempts_by_trigger_and_target"][0],
            {
                "trigger_id": "new-company-economics",
                "target_missing_information_id": "valuation-bridge",
                "attempts": 1,
            },
        )

        third = input_with(
            event(
                event_id="vrt-repeat-no-information",
                from_state="new",
                to_state="new",
                research_state=stable_state,
                revisit=revisit(
                    result="no_new_information",
                    trigger_id="second-company-update",
                ),
            ),
            cycle_id="cycle-third",
        )
        self.assertIn(
            "opportunity_revisit_repeated_no_information:0",
            validate_opportunity_updates(
                third["opportunity_updates"],
                data=third,
                records=self.journal.read(),
            ),
        )

        resolved_next = {
            "id": "cash-flow-sensitivity",
            "question": "Which assumptions drive the remaining valuation range?",
            "why_it_matters": "The resolved bridge should advance to its sensitivity.",
            "status": "open",
        }
        progress_cases = {
            "resolved": research_state(
                question_status="resolved",
                next_question_id="cash-flow-sensitivity",
                extra_missing=(resolved_next,),
                extra_triggers=(second_trigger,),
            ),
            "partially_resolved": research_state(
                extra_triggers=(
                    second_trigger,
                    {
                        "id": "new-price-evidence",
                        "condition": "Fresh price evidence narrows the bridge.",
                        "status": "active",
                    },
                ),
            ),
        }
        for result, progressed_state in progress_cases.items():
            with self.subTest(result=result):
                progressed = input_with(
                    event(
                        event_id=f"vrt-{result}-after-no-information",
                        from_state="new",
                        to_state="new",
                        research_state=progressed_state,
                        revisit=revisit(
                            result=result,
                            result_summary=(
                                "Fresh evidence changed the committed research "
                                "state after the prior empty pass."
                            ),
                        ),
                    ),
                    cycle_id=f"cycle-{result}",
                )
                errors = validate_opportunity_updates(
                    progressed["opportunity_updates"],
                    data=progressed,
                    records=self.journal.read(),
                )
                self.assertNotIn(
                    "opportunity_revisit_repeated_no_information:0",
                    errors,
                )
                self.assertEqual(errors, [])

    def test_next_question_metrics_surface_age_and_deferrals(self):
        state = research_state()
        first_payload = event(
            to_state="researching",
            observed_at="2026-09-20T10:00:00Z",
            research_state=state,
        )
        first_payload["identity_fingerprint"] = identity_fingerprint(
            identity()
        )
        second_payload = event(
            event_id="vrt-revisit",
            from_state="researching",
            to_state="researching",
            cycle_id="cycle-second",
            observed_at="2026-09-20T12:00:00Z",
            research_state=state,
            revisit=revisit(result="no_new_information"),
        )
        second_payload["identity_fingerprint"] = identity_fingerprint(
            identity()
        )
        records = [
            {
                "record_id": "opportunity-event:vrt-new",
                "record_type": "opportunity_event",
                "created_at": "2026-09-20T10:00:00Z",
                "payload": first_payload,
            },
            {
                "record_id": "cycle-stage:cycle-agenda:research_director",
                "record_type": "cycle_stage",
                "created_at": "2026-09-20T11:00:00Z",
                "payload": {
                    "cycle_id": "cycle-agenda",
                    "stage_id": "research_director",
                    "completed_at": "2026-09-20T11:00:00Z",
                    "output": {
                        "research_agenda": {
                            "candidates": [{
                                "opportunity_id":
                                    "vrt-special-situation",
                                "target_missing_information_id":
                                    "valuation-bridge",
                                "selected": False,
                                "rejection_reason":
                                    "Fresh evidence is not available yet.",
                                "allocation_factors": {
                                    "expected_information_gain":
                                        "High after the next filing.",
                                },
                            }],
                        },
                    },
                },
            },
            {
                "record_id": "opportunity-event:vrt-revisit",
                "record_type": "opportunity_event",
                "created_at": "2026-09-20T12:00:00Z",
                "payload": second_payload,
            },
        ]

        metrics = opportunity_ledger_summary(records)["items"][0][
            "next_question_metrics"
        ]

        self.assertEqual(metrics["missing_information_id"], "valuation-bridge")
        self.assertEqual(metrics["first_observed_at"], "2026-09-20T10:00:00+00:00")
        self.assertEqual(metrics["age_seconds"], 7200)
        self.assertEqual(metrics["selected_count"], 0)
        self.assertEqual(metrics["deferred_count"], 1)
        self.assertEqual(metrics["last_disposition"], "deferred")
        self.assertEqual(
            metrics["last_expected_information_gain"],
            "High after the next filing.",
        )
        self.assertEqual(
            metrics["last_deferral_reason"],
            "Fresh evidence is not available yet.",
        )
        self.assertEqual(metrics["revisit_attempts"], 1)
        self.assertEqual(metrics["no_new_information"], 1)

    def test_same_cycle_transition_is_caused_by_prior_opportunity_event(self):
        data = input_with(
            event(),
            event(
                event_id="vrt-watch",
                from_state="new",
                to_state="watch",
            ),
        )
        self.input_path.write_text(json.dumps(data), encoding="utf-8")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(self.input_path, self.journal)

        record = next(
            row for row in self.journal.read()
            if row.get("record_id") == "opportunity-event:vrt-watch"
        )
        self.assertEqual(
            record["caused_by"],
            [
                "cycle-receipt:cycle-opportunity",
                "cycle-stage:cycle-opportunity:decision",
                "opportunity-event:vrt-new",
            ],
        )

    def test_missing_events_are_backfilled_idempotently(self):
        data = input_with(event())
        self.input_path.write_text(json.dumps(data), encoding="utf-8")
        with (
            patch(
                "runtime.run_host_cycle.load_journal_records",
                return_value=[],
            ),
            patch(
                "runtime.run_host_cycle.persist_opportunity_updates",
                return_value=0,
            ),
        ):
            run_one(self.input_path, self.journal)

        self.assertEqual(
            opportunity_ledger_summary(self.journal.read())["total"], 0)
        self.assertEqual(
            backfill_opportunity_events([self.input_path], self.journal), 1)
        self.assertEqual(
            backfill_opportunity_events([self.input_path], self.journal), 0)
        self.assertEqual(
            opportunity_ledger_summary(self.journal.read())["total"], 1)

    def test_backfill_skips_complete_older_cycles_after_later_transition(self):
        first = self.root / "first.json"
        second = self.root / "second.json"
        first.write_text(
            json.dumps(input_with(event(), cycle_id="cycle-first")),
            encoding="utf-8",
        )
        second.write_text(
            json.dumps(input_with(
                event(
                    event_id="vrt-researching",
                    from_state="new",
                    to_state="researching",
                ),
                cycle_id="cycle-second",
            )),
            encoding="utf-8",
        )
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(first, self.journal)
            run_one(second, self.journal)

        self.assertEqual(
            backfill_opportunity_events([second, first], self.journal), 0)
        summary = opportunity_ledger_summary(self.journal.read())
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["counts_by_state"], {"researching": 1})
        self.assertEqual(summary["items"][0]["event_count"], 2)

    def test_soft_identity_collisions_are_visible_not_auto_merged(self):
        data = input_with(
            event(),
            event(
                event_id="vrt-second-thesis",
                opportunity_id="vrt-second-thesis",
                identity=identity(thesis_key="margin expansion"),
            ),
        )
        self.input_path.write_text(json.dumps(data), encoding="utf-8")
        with patch(
            "runtime.run_host_cycle.load_journal_records",
            return_value=[],
        ):
            run_one(self.input_path, self.journal)

        summary = opportunity_ledger_summary(self.journal.read())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(
            summary["soft_identity_collisions"][0]["opportunity_ids"],
            ["vrt-second-thesis", "vrt-special-situation"],
        )

    def test_bounded_per_thesis_history_shows_reused_citations_without_merging(
        self,
    ):
        def append_event(
            event_id, opportunity_id, thesis_identity, refs, hour,
        ):
            self.journal.append(
                record_id=f"opportunity-event:{event_id}",
                record_type="opportunity_event",
                agent="sovereign-host",
                payload={
                    "event_id": event_id,
                    "cycle_id": f"cycle-{event_id}",
                    "opportunity_id": opportunity_id,
                    "identity_fingerprint": identity_fingerprint(
                        thesis_identity
                    ),
                    "identity": thesis_identity,
                    "observed_at": f"2026-09-20T{hour:02}:00:00Z",
                    "to_state": "watch",
                    "rationale": f"Research update {event_id}.",
                    "evidence": refs,
                    "research_state": research_state(),
                    "revisit": None,
                },
            )

        primary = "vrt-special-situation"
        second = "vrt-second-thesis"
        append_event("a1", primary, identity(), ["finding:shared"], 10)
        append_event("a2", primary, identity(), ["finding:shared"], 11)
        append_event(
            "b1", second, identity(thesis_key="independent economics"),
            ["finding:shared"], 12,
        )
        append_event(
            "a3", primary, identity(),
            ["finding:shared", "finding:new-source"], 13,
        )
        append_event(
            "a4", primary, identity(),
            ["finding:shared", "finding:new-source"], 14,
        )

        summary = opportunity_ledger_summary(self.journal.read(), limit=1)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["not_shown"], 1)
        item = summary["items"][0]
        self.assertEqual(item["opportunity_id"], primary)
        self.assertEqual(item["event_count"], 4)
        self.assertEqual(item["history_not_shown"], 1)
        self.assertEqual(
            [row["event_id"] for row in item["recent_history"]],
            ["a2", "a3", "a4"],
        )
        self.assertTrue(item["recent_history"][0]["same_refs_as_previous"])
        self.assertEqual(item["recent_history"][0]["first_seen_refs"], [])
        self.assertEqual(
            item["recent_history"][1]["first_seen_refs"],
            ["finding:new-source"],
        )
        self.assertEqual(
            item["recent_history"][1]["reused_refs"],
            ["finding:shared"],
        )
        self.assertEqual(item["recent_history"][2]["first_seen_refs"], [])
        self.assertTrue(item["recent_history"][2]["same_refs_as_previous"])
        self.assertEqual(
            item["history_retrieval"],
            {
                "repository_directory": "audit/",
                "record_type": "opportunity_event",
                "opportunity_id": primary,
            },
        )
        both = {
            row["opportunity_id"]: row
            for row in opportunity_ledger_summary(self.journal.read())[
                "items"
            ]
        }
        self.assertEqual(
            both[second]["recent_history"][0]["first_seen_refs"],
            ["finding:shared"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
