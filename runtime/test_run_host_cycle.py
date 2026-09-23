import json
import pathlib
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from .audit_store import AuditJournal
from .host_publication import content_sha256, marker_path
from .integrity import load_journal_records
from .learning_dispositions import LEARNING_STAGES
from .market_scout import market_scout_report
from .run_host_cycle import (EXECUTION_FAILED, EXECUTION_VERIFIED,
                             HOST_INPUT_COMMITTED, PUBLICATION_UNVALIDATED,
                             already_persisted, backfill_research_memory,
                             effective_snapshot,
                             execution_status, host_input_paths,
                             input_fingerprint, main, persisted_snapshot_id,
                             partition_validation_errors,
                             record_refusal,
                             run_one,
                             persist_order_instruction_activity,
                             persist_staged_order_instruction_recovery,
                             persist_tool_provenance,
                             validate_input,
                             validate_known_instruction_recovery)
from .test_tool_provenance import upgrade_tool_calls_to_v4
from .tool_artifacts import iter_tool_calls


def sample_input(**over):
    data = {
        "source": "ibkr", "as_of": "2026-09-16T12:00:00Z",
        "order_submission_used": False,
        # Fetched via "get order instructions" every cycle. [] is a real
        # answer; an absent field is refused, because not looking and
        # finding nothing are different facts.
        "order_instructions": [],
        "snapshot": {"net_liquidation_value": 1000.0, "cash": 10.0},
        "research": [{"question": "q", "finding": "f",
                      "strategy_family": "factor_macro",
                      "specialist_stage_id": "macro_specialist",
                      "tool_calls": [{"tool": "ibkr.positions", "result": "ok"}]}],
        "findings": [{"id": "x", "statement": "s"}],
        "decision": {"status": "wait", "rationale": "r", "rests_on": ["a"]},
    }
    data.update(over)
    return data


def v4_post_effective_full_cycle(**overrides):
    data = post_effective_full_cycle(
        learning_stage_dispositions=[
            {
                "stage_id": stage_id,
                "disposition": "no_change",
                "rationale": f"No durable change supported for {stage_id}.",
                "evidence": [f"stage:{stage_id}"],
            }
            for stage_id in LEARNING_STAGES
        ],
    )
    data.update(overrides)
    return upgrade_tool_calls_to_v4(data)


def market_sessions_input(eu_open=False, us_open=False):
    return {
        "observed_at": "2026-09-16T14:00:00Z",
        "markets": [
            {
                "region": "EU",
                "venue": "XETRA",
                "timezone": "Europe/Berlin",
                "local_time": "2026-09-16T16:00:00+02:00",
                "status": "open" if eu_open else "closed",
                "is_open": eu_open,
                "next_open": "2026-09-17T09:00:00+02:00",
                "next_close": (
                    "2026-09-16T17:30:00+02:00"
                    if eu_open
                    else "2026-09-17T17:30:00+02:00"
                ),
                "evidence": [{
                    "tool": "official Xetra calendar",
                    "result": {"is_open": eu_open},
                }],
            },
            {
                "region": "US",
                "venue": "NYSE",
                "timezone": "America/New_York",
                "local_time": "2026-09-16T10:00:00-04:00",
                "status": "open" if us_open else "closed",
                "is_open": us_open,
                "next_open": "2026-09-17T09:30:00-04:00",
                "next_close": (
                    "2026-09-16T16:00:00-04:00"
                    if us_open
                    else "2026-09-17T16:00:00-04:00"
                ),
                "evidence": [{
                    "tool": "Alpaca get clock",
                    "result": {"is_open": us_open},
                }],
            },
        ],
        "overlap": (
            "both_open" if eu_open and us_open
            else "eu_only" if eu_open
            else "us_only" if us_open
            else "none_open"
        ),
    }


def full_cycle_input(**over):
    stages = [
        ("portfolio", "observe", []),
        ("research_director", "direct", ["portfolio"]),
        ("memory_retrieval", "memory", ["research_director"]),
        ("macro_specialist", "specialist", ["memory_retrieval"]),
        ("evidence_arbitration", "gate", ["macro_specialist"]),
        ("portfolio_fit", "gate", ["evidence_arbitration"]),
        ("counterfactual", "gate", ["portfolio_fit"]),
        ("adversarial", "gate", ["counterfactual"]),
        ("governance_review", "governance", ["adversarial"]),
        ("decision", "decision", ["governance_review"]),
        ("learning_audit", "learning", ["decision"]),
        ("meta_research", "meta_research", ["learning_audit"]),
        ("self_improvement", "self_improvement", ["meta_research"]),
    ]
    data = sample_input(
        as_of="2026-09-16T14:00:00Z",
        host_input_schema_version=2,
        market_sessions=market_sessions_input(),
        cognitive_stages=[
            {
                "stage_id": stage_id,
                "phase": phase,
                "depends_on": depends_on,
                "required": True,
                "status": "completed",
                "tools_used": (
                    ["Interactive Brokers (IBKR)"]
                    if stage_id == "portfolio"
                    else ["Web"]
                    if stage_id == "macro_specialist"
                    else []
                ),
                "output": (
                    {
                        "observations": [],
                        "evidence_status": "verified",
                        "blockers": [],
                        "confidence": 0.8,
                        "next_actions": [],
                        **({
                            "decision_status": "wait",
                            "rationale": "r",
                        } if stage_id == "decision" else {
                            "summary": f"{stage_id} completed",
                        }),
                    }
                ),
            }
            for stage_id, phase, depends_on in stages
        ],
    )
    director = next(
        row for row in data["cognitive_stages"]
        if row["stage_id"] == "research_director")
    director["output"]["research_agenda"] = {
        "drivers": [{
            "observation": "Rates changed.",
            "source": "Web",
            "portfolio_relevance": "Current holdings are rate-sensitive.",
        }],
        "candidates": [
            {
                "candidate_id": "macro_specialist",
                "instrument": "portfolio",
                "strategy_family": "factor_macro",
                "trigger": "Fresh rate evidence.",
                "selected": True,
                "selection_reason": "Largest current portfolio driver.",
            },
            {
                "candidate_id": "value_alternative",
                "instrument": "ADBE",
                "strategy_family": "quality_at_discount",
                "trigger": "Recent earnings.",
                "selected": False,
                "selection_reason": "Considered against macro work.",
                "rejection_reason": "No fresh valuation evidence.",
            },
        ],
        "selection_rationale": (
            "Fresh macro evidence has greater current portfolio relevance "
            "than the alternative."
        ),
    }
    data.update(over)
    return data


def add_market_scout(data):
    report = {
        "scope": {
            "description": "Scan current market triggers for bounded research.",
            "limitations": [],
        },
        "budget": {
            "specialist_investigations": 1,
            "external_searches": 1,
            "deep_dives": 0,
            "opportunity_updates": len({
                row["opportunity_id"]
                for row in data.get("opportunity_updates", [])
                if isinstance(row, dict) and row.get("opportunity_id")
            }),
            "rationale": "Use one evidenced scan and one specialist.",
        },
        "tool_calls": [{
            "tool_call_id": "scout-search-one",
            "kind": "external_search",
            "tool": "web.search",
            "call": {"query": "fresh market trigger"},
            "result": "Fresh trigger found.",
            "provenance": {
                "result_origin": "host_summary",
                "observed_at": data["as_of"],
                "source_refs": [{
                    "kind": "url",
                    "value": "https://example.com/fresh-trigger",
                }],
            },
        }],
        "candidates": [{
            "candidate_id": "scout-candidate-one",
            "identity": {
                "instrument": "portfolio",
                "instrument_type": "portfolio",
                "strategy_family": "factor_macro",
                "direction": "hedge",
                "thesis_key": "fresh-macro-trigger",
            },
            "trigger": "Fresh rate evidence changed portfolio risk.",
            "rationale": "The selected specialist can reduce this uncertainty.",
            "evidence_tool_call_ids": ["scout-search-one"],
        }],
        "budget_variance": None,
    }
    stages = data["cognitive_stages"]
    portfolio_index = next(
        index for index, row in enumerate(stages)
        if row["stage_id"] == "portfolio"
    )
    stages.insert(portfolio_index + 1, {
        "stage_id": "market_scout",
        "phase": "discovery",
        "depends_on": ["portfolio"],
        "required": True,
        "status": "completed",
        "tools_used": ["Web"],
        "output": {
            "observations": ["One current trigger was found."],
            "market_scout_report": report,
            "evidence_status": "verified",
            "blockers": [],
            "confidence": 0.7,
            "next_actions": [],
        },
    })
    director = next(
        row for row in stages
        if row["stage_id"] == "research_director"
    )
    director["depends_on"] = ["market_scout"]
    agenda = director["output"]["research_agenda"]
    agenda["allocation_plan"] = {
        "new_opportunity": 0,
        "existing_opportunity": 0,
        "portfolio_risk": 1,
        "follow_up": 0,
        "market_session_context": data["market_sessions"]["overlap"],
        "rationale": (
            "Use the current session state and portfolio exposure to reserve "
            "one specialist pass for the selected portfolio risk."
        ),
    }
    agenda["allocation_variance"] = None
    selected = agenda["candidates"][0]
    selected["scout_candidate_id"] = "scout-candidate-one"
    selected["portfolio_risk_ref"] = "portfolio:account"
    selected["follow_up_ref"] = None
    selected["allocation_factors"] = {
        "novelty": "Fresh rate evidence changed the risk context.",
        "portfolio_impact": "Current holdings are rate-sensitive.",
        "missing_information": "The changed exposure effect is unresolved.",
        "expected_information_gain": (
            "The specialist pass can clarify the current portfolio impact."
        ),
    }
    rejected = agenda["candidates"][1]
    rejected["portfolio_risk_ref"] = None
    rejected["follow_up_ref"] = None
    rejected["allocation_factors"] = {
        "novelty": "Recent earnings made the alternative worth comparing.",
        "portfolio_impact": "The alternative has less current exposure impact.",
        "missing_information": "Fresh valuation support is still absent.",
        "expected_information_gain": (
            "A pass would add less decision value than the selected work."
        ),
    }
    return data


def post_effective_full_cycle(**over):
    market_sessions = market_sessions_input(us_open=True)
    market_sessions.update({
        "observed_at": "2026-09-17T16:00:00Z",
        "overlap": "us_only",
    })
    market_sessions["markets"][0].update({
        "local_time": "2026-09-17T18:00:00+02:00",
        "next_open": "2026-09-18T09:00:00+02:00",
        "next_close": "2026-09-18T17:30:00+02:00",
    })
    market_sessions["markets"][1].update({
        "local_time": "2026-09-17T12:00:00-04:00",
        "next_open": "2026-09-18T09:30:00-04:00",
        "next_close": "2026-09-17T16:00:00-04:00",
    })
    data = full_cycle_input(
        as_of="2026-09-17T16:00:00Z",
        market_sessions=market_sessions,
    )
    data["research"][0]["tool_calls"][0]["provenance"] = {
        "result_origin": "connector_response",
        "observed_at": "2026-09-17T16:01:00Z",
        "source_refs": [{
            "kind": "response_id",
            "value": "ibkr-response-1",
        }],
    }
    data.update(over)
    return data


def goal_creation(**goal_overrides):
    goal = {
        "goal_id": "goal-one",
        "created_at": "2026-09-16T14:00:00Z",
        "category": "research_quality",
        "statement": "Close one selected evidence gap.",
        "deadline": "2026-09-17T14:00:00Z",
        "success_metric": "unresolved_evidence_gaps",
        "success_target": 0,
        "partial_target": 0.5,
        "evaluation_rubric": "Met when the selected gap reaches zero.",
        "metric_type": "controllable",
        "baseline": 1,
        "direction": "lower_is_better",
        "caused_by": ["macro_specialist"],
    }
    goal.update(goal_overrides)
    return {"mode": "create", "goal": goal}


def goal_progress(**overrides):
    row = {
        "mode": "progress",
        "goal_id": "goal-one",
        "observed_at": "2026-09-16T15:00:00Z",
        "observed_value": 0.5,
        "assessment": "The selected evidence gap is half resolved.",
        "evidence": [{
            "evidence_id": "macro_specialist",
            "source": "specialist output",
            "finding": "One bounded part of the gap remains.",
        }],
        "caused_by": ["macro_specialist"],
    }
    row.update(overrides)
    return row


def open_goal_record():
    return {
        "record_type": "goal_event",
        "payload": {
            "goal_id": "goal-one",
            "event": "created",
            "status": "open",
            "opened_at": "2026-09-16T14:00:00Z",
            "source_cycle_id": "cycle-goal-one",
            "goal": goal_creation()["goal"],
        },
    }


def goal_close(**overrides):
    row = {
        "mode": "close",
        "goal_id": "goal-one",
        "observed_at": "2026-09-17T15:00:00Z",
        "closure_basis": "measurement",
        "observed_value": 0,
        "evidence": [{
            "evidence_id": "macro_specialist",
            "source": "specialist output",
            "finding": "No unresolved evidence gaps remain.",
        }],
        "caused_by": ["macro_specialist"],
        "analysis": {
            "causal_summary": "The selected specialist closed the gap.",
            "worked": ["The evidence path stayed focused."],
            "failed": [],
            "counterfactual": "A stale source would have kept the gap open.",
            "next_change": "Reuse the bounded comparison on later goals.",
        },
    }
    row.update(overrides)
    return row


def _valid_market_scout_input(*, schema_version=4):
    data = add_market_scout(post_effective_full_cycle(
        host_input_schema_version=3,
        learning_stage_dispositions=[
            {
                "stage_id": stage_id,
                "disposition": "no_change",
                "rationale": f"No durable change supported for {stage_id}.",
                "evidence": [f"stage:{stage_id}"],
            }
            for stage_id in (
                "learning_audit", "meta_research", "self_improvement",
            )
        ],
    ))
    return (
        upgrade_tool_calls_to_v4(data)
        if schema_version == 4
        else data
    )


class InputValidationTests(unittest.TestCase):
    def test_declared_evidence_coverage_reaches_production_validator(self):
        data = upgrade_tool_calls_to_v4(
            add_market_scout(v4_post_effective_full_cycle())
        )
        data["evidence_coverage_schema_version"] = 1
        data["evidence_calls"] = []

        errors = validate_input(data, "cycle.json")

        self.assertIn("evidence_producer_missing:portfolio", errors)
        self.assertIn(
            "evidence_producer_missing:saved_instructions",
            errors,
        )
        self.assertIn("evidence_producer_missing:market_sessions", errors)

    def test_divergent_cross_scope_tool_call_id_is_refused(self):
        data = upgrade_tool_calls_to_v4(
            add_market_scout(v4_post_effective_full_cycle())
        )
        scout = next(
            stage["output"]["market_scout_report"]["tool_calls"][0]
            for stage in data["cognitive_stages"]
            if stage["stage_id"] == "market_scout"
        )
        research = data["research"][0]["tool_calls"][0]
        scout["tool_call_id"] = research["tool_call_id"]
        scout["result"] = {"different": True}

        self.assertIn(
            f"tool_call_id_conflict:{research['tool_call_id']}",
            validate_input(data, "cycle.json"),
        )
    """An append-only journal cannot take back a half-written cycle.

    So a bad input is refused before anything is appended, rather than
    discovered partway through.
    """

    def test_v4_provenance_cannot_be_disabled_by_old_as_of(self):
        data = v4_post_effective_full_cycle()
        old = "2026-01-02T16:00:00Z"
        data["as_of"] = old
        data["snapshot"]["as_of"] = old
        for descriptor in iter_tool_calls(data):
            descriptor["call"]["provenance"]["observed_at"] = old
        del data["research"][0]["tool_calls"][0]["provenance"]
        self.assertTrue(any(
            error.endswith(":provenance_missing")
            for error in validate_input(
                data,
                "v4-old-as-of.json",
                validation_now=datetime(
                    2026, 1, 2, 16, 5, tzinfo=timezone.utc,
                ),
            )
        ))

    def test_a_complete_input_passes(self):
        self.assertEqual(validate_input(sample_input(), "t.json"), [])

    def test_a_missing_order_declaration_is_refused(self):
        data = sample_input()
        del data["order_submission_used"]
        self.assertIn("order_submission_declaration_required", validate_input(data, "t.json"))

    def test_a_declared_order_submission_is_refused(self):
        self.assertIn("live_order_submission_forbidden",
                      validate_input(sample_input(order_submission_used=True), "t.json"))

    def test_enabled_schedule_requires_context_only_after_anchor(self):
        with tempfile.TemporaryDirectory(prefix="schedule-context-") as tmp:
            root = pathlib.Path(tmp)
            input_dir = root / "host_input"
            input_dir.mkdir()
            runs = root / "runs"
            runs.mkdir()
            (runs / "SCHEDULE.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "enabled": True,
                    "task_id": "task-hourly-1",
                    "task_name": "Sovereign Research hourly cycle",
                    "timezone": "Europe/Sofia",
                    "cadence_minutes": 60,
                    "anchor_at": "2026-09-19T10:00:00+00:00",
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
            data = sample_input()
            data["as_of"] = "2026-09-19T10:05:00+00:00"
            data["snapshot"]["as_of"] = data["as_of"]

            self.assertIn(
                "schedule_context_required",
                validate_input(
                    data,
                    "candidate.json",
                    input_dir=input_dir,
                ),
            )

            data["schedule_context"] = {
                "schema_version": 1,
                "task_id": "task-hourly-1",
                "platform_run_id": "host-run-123",
                "expected_slot": "2026-09-19T10:00:00+00:00",
                "started_at": "2026-09-19T10:01:00+00:00",
                "source_observed_at": "2026-09-19T10:03:00+00:00",
                "trigger": "scheduled",
                "intervention": "none",
            }
            self.assertNotIn(
                "schedule_context_required",
                validate_input(
                    data,
                    "candidate.json",
                    input_dir=input_dir,
                ),
            )

            del data["schedule_context"]
            data["as_of"] = "2026-09-19T09:59:00+00:00"
            data["snapshot"]["as_of"] = data["as_of"]
            self.assertNotIn(
                "schedule_context_required",
                validate_input(
                    data,
                    "historical.json",
                    input_dir=input_dir,
                ),
            )

    def test_run_one_passes_runtime_validation_context(self):
        data = full_cycle_input(host_input_schema_version=3)
        captured = {}

        def refuse(_data, _name, **kwargs):
            captured.update(kwargs)
            return ["stop_before_execution"]

        class Journal:
            def read(self):
                return []

        with tempfile.TemporaryDirectory(prefix="validation-context-") as tmp:
            path = pathlib.Path(tmp) / "candidate.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with patch(
                "runtime.run_host_cycle.validate_input",
                side_effect=refuse,
            ):
                with self.assertRaisesRegex(
                    ValueError, "stop_before_execution",
                ):
                    run_one(path, Journal())

        self.assertEqual(captured["input_dir"], path.parent)
        self.assertTrue(captured["enforce_runtime_time_bounds"])
        self.assertNotIn("require_full_schema", captured)
        self.assertIsInstance(captured["validation_now"], datetime)
        self.assertIsNotNone(captured["validation_now"].tzinfo)

    def test_runtime_time_bounds_do_not_require_new_schema_fields(self):
        data = add_market_scout(post_effective_full_cycle())
        data["as_of"] = "2026-09-18T18:45:00Z"
        data["snapshot"]["as_of"] = "2026-09-18T18:45:00Z"
        errors = validate_input(
            data,
            "runtime.json",
            enforce_runtime_time_bounds=True,
            validation_now=datetime(
                2026, 9, 17, 22, 27, tzinfo=timezone.utc,
            ),
        )
        self.assertIn(
            "as_of_in_future:2026-09-18T18:45:00Z",
            errors,
        )
        self.assertNotIn(
            "staged_host_input_schema_version_required:2",
            errors,
        )

    def test_new_staged_input_cannot_claim_a_future_observation(self):
        data = add_market_scout(post_effective_full_cycle())
        data["as_of"] = "2026-09-18T18:45:00Z"
        data["snapshot"]["as_of"] = "2026-09-18T18:45:00Z"
        self.assertIn(
            "as_of_in_future:2026-09-18T18:45:00Z",
            validate_input(
                data,
                "future.json",
                require_full_schema=True,
                validation_now=datetime(
                    2026, 9, 17, 22, 27, tzinfo=timezone.utc,
                ),
            ),
        )

    def test_historical_replay_does_not_apply_wall_clock_bound(self):
        data = add_market_scout(post_effective_full_cycle())
        data["as_of"] = "2026-09-18T18:45:00Z"
        data["snapshot"]["as_of"] = "2026-09-18T18:45:00Z"
        self.assertNotIn(
            "as_of_in_future:2026-09-18T18:45:00Z",
            validate_input(
                data,
                "historical.json",
                validation_now=datetime(
                    2026, 9, 17, 22, 27, tzinfo=timezone.utc,
                ),
            ),
        )

    def test_small_staging_clock_skew_is_allowed(self):
        data = add_market_scout(post_effective_full_cycle())
        errors = validate_input(
            data,
            "near-future.json",
            require_full_schema=True,
            validation_now=datetime(
                2026, 9, 17, 15, 50, tzinfo=timezone.utc,
            ),
        )
        self.assertFalse(
            any(error.startswith("as_of_in_future:") for error in errors),
            errors,
        )

    def test_rediscovered_candidate_without_reference_is_refused(self):
        # A prior receipted cycle already proposed this exact identity
        # (scout-candidate-one / fresh-macro-trigger) and it was never
        # promoted into the opportunity ledger.
        prior_report = {"candidates": [{
            "candidate_id": "scout-candidate-one",
            "identity": {
                "instrument": "portfolio",
                "instrument_type": "portfolio",
                "strategy_family": "factor_macro",
                "direction": "hedge",
                "thesis_key": "fresh-macro-trigger",
            },
            "trigger": "t", "rationale": "r",
            "evidence_tool_call_ids": ["prior-search"],
        }]}
        records = [
            {"record_id": "cycle-receipt:cycle-prior-one",
             "record_type": "cycle_receipt",
             "payload": {"cycle_id": "cycle-prior-one"}},
            {"record_id": "cycle-stage:cycle-prior-one:market_scout",
             "record_type": "cycle_stage",
             "payload": {"cycle_id": "cycle-prior-one",
                         "agent_id": "market_scout",
                         "output": {"market_scout_report": prior_report}}},
        ]
        data = _valid_market_scout_input()
        errors = validate_input(
            data, "new-rediscovery.json",
            records=records, require_full_schema=True,
        )
        self.assertIn(
            "market_scout_candidate_rediscovery_invalid:"
            "0:required:cycle-prior-one:scout-candidate-one",
            errors,
        )

    def test_rediscovered_candidate_with_valid_reference_is_accepted(self):
        prior_report = {"candidates": [{
            "candidate_id": "scout-candidate-one",
            "identity": {
                "instrument": "portfolio",
                "instrument_type": "portfolio",
                "strategy_family": "factor_macro",
                "direction": "hedge",
                "thesis_key": "fresh-macro-trigger",
            },
            "trigger": "t", "rationale": "r",
            "evidence_tool_call_ids": ["prior-search"],
        }]}
        records = [
            {"record_id": "cycle-receipt:cycle-prior-one",
             "record_type": "cycle_receipt",
             "payload": {"cycle_id": "cycle-prior-one"}},
            {"record_id": "cycle-stage:cycle-prior-one:market_scout",
             "record_type": "cycle_stage",
             "payload": {"cycle_id": "cycle-prior-one",
                         "agent_id": "market_scout",
                         "output": {"market_scout_report": prior_report}}},
        ]
        data = _valid_market_scout_input()
        market_scout_report(data)["candidates"][0]["rediscovery_of"] = {
            "cycle_id": "cycle-prior-one",
            "candidate_id": "scout-candidate-one",
        }
        errors = validate_input(
            data, "new-rediscovery.json",
            records=records, require_full_schema=True,
        )
        self.assertFalse(
            any(error.startswith("market_scout_candidate_rediscovery_invalid")
                for error in errors),
            errors,
        )

    def test_historical_replay_does_not_enforce_rediscovery(self):
        prior_report = {"candidates": [{
            "candidate_id": "scout-candidate-one",
            "identity": {
                "instrument": "portfolio",
                "instrument_type": "portfolio",
                "strategy_family": "factor_macro",
                "direction": "hedge",
                "thesis_key": "fresh-macro-trigger",
            },
            "trigger": "t", "rationale": "r",
            "evidence_tool_call_ids": ["prior-search"],
        }]}
        records = [
            {"record_id": "cycle-receipt:cycle-prior-one",
             "record_type": "cycle_receipt",
             "payload": {"cycle_id": "cycle-prior-one"}},
            {"record_id": "cycle-stage:cycle-prior-one:market_scout",
             "record_type": "cycle_stage",
             "payload": {"cycle_id": "cycle-prior-one",
                         "agent_id": "market_scout",
                         "output": {"market_scout_report": prior_report}}},
        ]
        data = _valid_market_scout_input(schema_version=3)
        errors = validate_input(
            data, "historical-rediscovery.json", records=records,
        )
        self.assertFalse(
            any(error.startswith("market_scout_candidate_rediscovery_invalid")
                for error in errors),
            errors,
        )

    def test_experiment_requires_a_complete_contract(self):
        data = sample_input(decision={
            "status": "experiment",
            "rationale": "Resolve uncertainty.",
            "rests_on": ["a"],
        })
        self.assertIn(
            "experiment_contract_required",
            validate_input(data, "t.json"),
        )
        data["decision"]["experiment"] = {
            "hypothesis": "h",
            "mechanism": "m",
            "measurement": "measure",
            "counter_metric": "counter",
            "evaluation_window": "two accepted cycles",
            "rollback_condition": "rollback",
        }
        self.assertEqual(validate_input(data, "t.json"), [])

    def test_a_non_ibkr_source_is_refused(self):
        self.assertIn("source_must_be_ibkr",
                      validate_input(sample_input(source="guess"), "t.json"))

    def test_missing_research_or_decision_is_refused(self):
        self.assertIn("missing_research", validate_input(sample_input(research=[]), "t.json"))
        self.assertIn("missing_decision", validate_input(sample_input(decision={}), "t.json"))


class FullCycleEnvelopeValidationTests(unittest.TestCase):
    def test_a_complete_v2_input_passes(self):
        self.assertEqual(validate_input(full_cycle_input(), "v2.json"), [])

    def test_staged_inputs_require_the_v2_contract(self):
        self.assertIn(
            "host_input_schema_version_required",
            validate_input(
                sample_input(),
                "candidate.json",
                require_full_schema=True,
            ),
        )

    def test_post_effective_v2_tool_calls_require_provenance(self):
        data = post_effective_full_cycle()
        del data["research"][0]["tool_calls"][0]["provenance"]
        errors = validate_input(data, "candidate.json")
        self.assertIn(
            "tool_provenance_invalid:0:0:provenance_missing",
            errors,
        )

    def test_valid_post_effective_connector_provenance_is_accepted(self):
        errors = validate_input(
            post_effective_full_cycle(),
            "candidate.json",
        )
        self.assertFalse(
            any(error.startswith("tool_provenance_invalid:") for error in errors),
            errors,
        )

    def test_pre_effective_v2_tool_calls_remain_replayable(self):
        errors = validate_input(full_cycle_input(), "candidate.json")
        self.assertFalse(
            any(error.startswith("tool_provenance_invalid:") for error in errors),
            errors,
        )

    def test_research_agenda_is_required(self):
        data = full_cycle_input()
        director = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "research_director")
        del director["output"]["research_agenda"]
        self.assertIn(
            "research_agenda_invalid:missing",
            validate_input(data, "v2.json"),
        )

    def test_research_agenda_requires_a_rejected_alternative(self):
        data = full_cycle_input()
        director = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "research_director")
        director["output"]["research_agenda"]["candidates"] = [
            director["output"]["research_agenda"]["candidates"][0]
        ]
        self.assertIn(
            "research_agenda_invalid:rejected_alternative",
            validate_input(data, "v2.json"),
        )

    def test_research_must_bind_to_a_selected_specialist(self):
        data = full_cycle_input()
        data["research"][0]["specialist_stage_id"] = "value_alternative"
        self.assertIn(
            "research_binding_invalid:0:not_selected:value_alternative",
            validate_input(data, "v2.json"),
        )

    def test_open_goal_creation_is_validated_before_execution(self):
        data = full_cycle_input(
            goal_observations=[goal_creation(baseline="unknown")])
        self.assertIn(
            "goal_creation_invalid:0:baseline_not_numeric",
            validate_input(data, "v2.json"),
        )

    def test_goal_progress_requires_an_existing_open_goal(self):
        data = full_cycle_input(
            as_of="2026-09-16T15:00:00Z",
            goal_observations=[goal_progress()],
        )
        self.assertIn(
            "goal_progress_invalid:0:goal_not_open",
            validate_input(data, "v2.json", records=[]),
        )

    def test_goal_progress_rejects_terminal_fields_and_duplicate_rows(self):
        data = full_cycle_input(
            as_of="2026-09-16T15:00:00Z",
            goal_observations=[
                goal_progress(status="met"),
                goal_progress(assessment="Duplicate observation."),
            ],
        )
        errors = validate_input(
            data, "v2.json", records=[open_goal_record()])
        self.assertIn(
            "goal_progress_invalid:0:unexpected_field_status", errors)
        self.assertIn(
            "goal_progress_invalid:multiple_progress:goal-one", errors)

    def test_goal_progress_must_advance_the_observation_time(self):
        data = full_cycle_input(
            as_of="2026-09-16T14:00:00Z",
            goal_observations=[goal_progress(
                observed_at="2026-09-16T14:00:00Z")],
        )
        self.assertIn(
            "goal_progress_invalid:0:observed_at_not_after_previous",
            validate_input(
                data, "v2.json", records=[open_goal_record()]),
        )

    def test_goal_close_refuses_early_measurement_and_hidden_grade(self):
        data = full_cycle_input(
            as_of="2026-09-16T15:00:00Z",
            goal_observations=[goal_close(
                observed_at="2026-09-16T15:00:00Z",
                status="met",
            )],
        )
        errors = validate_input(
            data, "v2.json", records=[open_goal_record()])
        self.assertIn(
            "goal_close_invalid:0:measurement_before_deadline", errors)
        self.assertIn(
            "goal_close_invalid:0:unexpected_field_status", errors)

    def test_goal_close_requires_an_existing_open_goal(self):
        data = full_cycle_input(
            as_of="2026-09-17T15:00:00Z",
            goal_observations=[goal_close()],
        )
        self.assertIn(
            "goal_close_invalid:0:goal_not_open",
            validate_input(data, "v2.json", records=[]),
        )

    def test_goal_cannot_progress_and_close_in_one_cycle(self):
        data = full_cycle_input(
            as_of="2026-09-17T15:00:00Z",
            goal_observations=[
                goal_progress(
                    observed_at="2026-09-17T15:00:00Z"),
                goal_close(),
            ],
        )
        self.assertIn(
            "goal_close_invalid:multiple_updates:goal-one",
            validate_input(
                data, "v2.json", records=[open_goal_record()]),
        )

    def test_v2_cycle_cannot_use_legacy_self_grading(self):
        data = full_cycle_input(goal_observations=[{
            "mode": "grade",
            "goal": goal_creation()["goal"],
            "observed_value": 0,
            "now": "2026-09-17T15:00:00Z",
            "invalidated": True,
        }])
        self.assertIn(
            "goal_mode_invalid:0:legacy_grade",
            validate_input(data, "v2.json", records=[open_goal_record()]),
        )

    def test_only_exact_legacy_input_can_replay_without_goal_mode(self):
        legacy_path = pathlib.Path(
            "host_input/cycle-20260917T145856Z-r41d7.json")
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        self.assertEqual(input_fingerprint(legacy), "74c9e9974fa2248e")
        errors = validate_input(legacy, legacy_path.name, records=[])
        self.assertFalse(any(
            error.startswith("goal_mode_invalid:") for error in errors))

        changed = json.loads(json.dumps(legacy))
        changed["cycle_id"] = "cycle-20260917T145856Z-r41d7-copy"
        self.assertIn(
            "goal_mode_invalid:0:missing",
            validate_input(changed, "copy.json", records=[]),
        )

    def test_v2_goal_modes_are_explicit_and_supported(self):
        cases = [
            ({}, "missing"),
            ({"mode": "   "}, "missing"),
            ({"mode": "grade"}, "legacy_grade"),
            ({"mode": "replace"}, "unknown"),
            ("grade", "not_an_object"),
        ]
        for row, problem in cases:
            with self.subTest(row=row):
                data = full_cycle_input(goal_observations=[row])
                self.assertIn(
                    f"goal_mode_invalid:0:{problem}",
                    validate_input(data, "v2.json", records=[]),
                )

    def test_supported_goal_modes_do_not_emit_goal_mode_errors(self):
        cases = [
            (goal_creation(), []),
            (goal_progress(), [open_goal_record()]),
            (goal_close(), [open_goal_record()]),
        ]
        for row, records in cases:
            with self.subTest(mode=row["mode"]):
                data = full_cycle_input(
                    as_of=row.get(
                        "observed_at",
                        row.get("goal", {}).get(
                            "created_at", "2026-09-16T14:00:00Z")),
                    goal_observations=[row],
                )
                self.assertFalse(any(
                    error.startswith("goal_mode_invalid:")
                    for error in validate_input(
                        data, "v2.json", records=records)
                ))

    def test_a_missing_core_stage_is_refused(self):
        data = full_cycle_input()
        data["cognitive_stages"] = [
            row for row in data["cognitive_stages"]
            if row["stage_id"] != "adversarial"
        ]
        self.assertIn(
            "full_cycle_stage_missing:adversarial",
            validate_input(data, "v2.json"),
        )

    def test_a_core_stage_cannot_be_optional(self):
        data = full_cycle_input()
        next(row for row in data["cognitive_stages"]
             if row["stage_id"] == "learning_audit")["required"] = False
        self.assertIn(
            "full_cycle_stage_not_required:learning_audit",
            validate_input(data, "v2.json"),
        )

    def test_a_cyclic_plan_is_refused(self):
        data = full_cycle_input()
        next(row for row in data["cognitive_stages"]
             if row["stage_id"] == "portfolio")["depends_on"] = ["decision"]
        self.assertIn(
            "cognitive_plan:cyclic_dependency",
            validate_input(data, "v2.json"),
        )

    def test_the_decision_stage_must_match_the_top_level(self):
        data = full_cycle_input()
        next(row for row in data["cognitive_stages"]
             if row["stage_id"] == "decision")["output"][
                 "decision_status"] = "recommended"
        self.assertIn(
            "decision_stage_disagrees_with_decision",
            validate_input(data, "v2.json"),
        )

    def test_a_placeholder_stage_output_is_refused(self):
        data = full_cycle_input()
        next(row for row in data["cognitive_stages"]
             if row["stage_id"] == "adversarial")["output"] = {
                 "summary": "done"}
        errors = validate_input(data, "v2.json")
        self.assertIn(
            "cognitive_stage_output_missing:adversarial:"
            "blockers|confidence|evidence_status|next_actions|observations",
            errors,
        )
        self.assertFalse(
            any(error.startswith(
                "cognitive_stage_evidence_status_invalid:adversarial")
                for error in errors),
            errors,
        )

    def test_a_blocked_stage_must_name_its_blocker(self):
        data = full_cycle_input()
        stage = next(row for row in data["cognitive_stages"]
                     if row["stage_id"] == "meta_research")
        stage["status"] = "blocked"
        stage["output"]["blockers"] = []
        self.assertIn(
            "cognitive_stage_blockers_empty:meta_research",
            validate_input(data, "v2.json"),
        )

    def test_specialists_must_be_isolated_siblings(self):
        data = full_cycle_input()
        specialist = {
            "stage_id": "volatility_specialist",
            "phase": "specialist",
            "depends_on": ["macro_specialist"],
            "required": True,
            "status": "completed",
            "tools_used": ["Web"],
            "output": {
                "observations": ["Option evidence reviewed."],
                "evidence_status": "partial",
                "blockers": ["No complete option chain was available."],
                "confidence": 0.4,
                "next_actions": ["Refresh the option chain next cycle."],
            },
        }
        data["cognitive_stages"].insert(4, specialist)
        arbitration = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "evidence_arbitration")
        arbitration["depends_on"].append("volatility_specialist")

        self.assertIn(
            "full_cycle_specialist_not_isolated:volatility_specialist:"
            "depends_on_must_be_memory_retrieval",
            validate_input(data, "v2.json"),
        )

    def test_arbitration_must_receive_every_specialist_output(self):
        data = full_cycle_input()
        data["cognitive_stages"].insert(4, {
            "stage_id": "volatility_specialist",
            "phase": "specialist",
            "depends_on": ["memory_retrieval"],
            "required": True,
            "status": "completed",
            "tools_used": ["Web"],
            "output": {
                "observations": ["Option evidence reviewed."],
                "evidence_status": "partial",
                "blockers": ["No complete option chain was available."],
                "confidence": 0.4,
                "next_actions": ["Refresh the option chain next cycle."],
            },
        })

        errors = validate_input(data, "v2.json")
        self.assertTrue(
            any(error.startswith(
                "evidence_arbitration_dependencies_mismatch:")
                for error in errors),
            errors,
        )

    def test_completed_stage_cannot_follow_a_blocked_dependency(self):
        data = full_cycle_input()
        specialist = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "macro_specialist")
        specialist["status"] = "blocked"
        specialist["output"]["blockers"] = ["The research tool was unavailable."]

        self.assertIn(
            "cognitive_stage_completed_after_noncompleted_dependency:"
            "evidence_arbitration:macro_specialist",
            validate_input(data, "v2.json"),
        )

    def test_partial_evidence_can_still_be_a_completed_pass(self):
        data = full_cycle_input()
        specialist = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "macro_specialist")
        specialist["output"].update({
            "evidence_status": "partial",
            "blockers": ["One primary source was unavailable."],
        })

        self.assertEqual(validate_input(data, "v2.json"), [])

    def test_evidence_status_uses_the_documented_vocabulary(self):
        data = full_cycle_input()
        stage = next(
            row for row in data["cognitive_stages"]
            if row["stage_id"] == "macro_specialist")
        stage["output"]["evidence_status"] = "mostly_good"

        self.assertIn(
            "cognitive_stage_evidence_status_invalid:"
            "macro_specialist:mostly_good",
            validate_input(data, "v2.json"),
        )


class FullCycleEnvelopeRunsEveryCommittedStageTests(unittest.TestCase):
    def test_v2_persists_the_full_plan(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="full-host-cycle-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        (inputs / "cycle.json").write_text(
            json.dumps(full_cycle_input()), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0)
        records = AuditJournal(journal_path).read()
        stages = [
            record["payload"]["stage_id"]
            for record in records
            if record["record_type"] == "cycle_stage"
        ]
        receipt = next(
            record["payload"] for record in records
            if record["record_type"] == "cycle_receipt"
        )
        self.assertEqual(len(stages), 13)
        self.assertEqual(
            receipt["mode"], "production-host-full-cycle")
        self.assertEqual(
            set(receipt["required_stages"]), set(stages))
        self.assertIn("meta_research", stages)
        self.assertIn("self_improvement", stages)
        director = next(
            record["payload"]["output"]
            for record in records
            if record["record_type"] == "cycle_stage"
            and record["payload"]["stage_id"] == "research_director"
        )
        self.assertEqual(
            director["research_agenda"]["candidates"][0]["candidate_id"],
            "macro_specialist",
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(
            feedback["reliability"]["candidate_attempts"][
                "accepted_receipts"],
            1,
        )
        self.assertEqual(
            feedback["reliability"]["accepted_candidate_streak"]["current"],
            1,
        )

    def test_legacy_input_remains_an_honest_three_stage_replay(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="legacy-host-cycle-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        (inputs / "cycle.json").write_text(
            json.dumps(sample_input()), encoding="utf-8")
        main(["--input-dir", str(inputs), "--journal", str(journal_path)])
        receipt = next(
            record["payload"] for record in AuditJournal(journal_path).read()
            if record["record_type"] == "cycle_receipt"
        )
        self.assertEqual(receipt["mode"], "host_input_replay")
        self.assertEqual(len(receipt["stages"]), 3)

    def test_retry_lineage_reaches_receipt_and_finalization(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="retry-lineage-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        corrects = "prior.semantic.json@sha256:" + "a" * 64
        AuditJournal(journal_path).append(
            record_id="host-input-refusal:prior",
            record_type="host_input_refusal",
            agent="test",
            payload={
                "input": "cycle-prior.semantic.json",
                "candidate_id": corrects,
                "corrects_candidate_id": None,
                "at": "2026-09-16T13:00:00Z",
                "codes": ["missing_research"],
                "reason": "ValueError: missing_research",
            },
        )
        data = full_cycle_input(
            cycle_id="cycle-retry-lineage",
            corrects_candidate_id=corrects,
        )
        (inputs / "cycle.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )

        self.assertEqual(
            main([
                "--input-dir",
                str(inputs),
                "--journal",
                str(journal_path),
            ]),
            0,
        )
        records = AuditJournal(journal_path).read()
        receipt = next(
            record["payload"] for record in records
            if record["record_type"] == "cycle_receipt"
        )
        finalization = next(
            record["payload"] for record in records
            if record["record_type"] == "cycle_finalization"
        )
        self.assertEqual(receipt["corrects_candidate_id"], corrects)
        self.assertEqual(
            finalization["corrects_candidate_id"],
            corrects,
        )

    def test_unknown_retry_lineage_is_refused_before_execution(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="bad-lineage-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = full_cycle_input(
            cycle_id="cycle-bad-lineage",
            corrects_candidate_id=(
                "missing.semantic.json@sha256:" + "b" * 64
            ),
        )
        (inputs / "cycle.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )

        self.assertEqual(
            main([
                "--input-dir",
                str(inputs),
                "--journal",
                str(journal_path),
            ]),
            1,
        )
        records = AuditJournal(journal_path).read()
        self.assertFalse(any(
            record["record_type"] == "cycle_receipt"
            for record in records
        ))
        refusal = next(
            record["payload"] for record in records
            if record["record_type"] == "host_input_refusal"
        )
        self.assertIn(
            "retry_lineage_reference_missing:",
            refusal["reason"],
        )

    def test_open_goal_is_persisted_and_exposed_in_feedback(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="open-goal-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = full_cycle_input(
            cycle_id="cycle-goal-one",
            goal_observations=[goal_creation()],
        )
        (inputs / "cycle.json").write_text(
            json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )

        records = AuditJournal(journal_path).read()
        goal_record = next(
            record for record in records
            if record["record_type"] == "goal_event")
        self.assertEqual(goal_record["record_id"], "goal:goal-one")
        self.assertEqual(goal_record["payload"]["event"], "created")
        self.assertEqual(goal_record["payload"]["status"], "open")
        self.assertEqual(
            goal_record["caused_by"],
            ["cycle-receipt:cycle-goal-one"],
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback["goals"]["open_count"], 1)
        self.assertEqual(
            feedback["goals"]["open"][0]["goal_id"],
            "goal-one",
        )

    def test_conflicting_goal_is_refused_before_second_receipt(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="goal-conflict-"))
        first_inputs = directory / "first"
        second_inputs = directory / "second"
        first_inputs.mkdir()
        second_inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        first = full_cycle_input(
            cycle_id="cycle-goal-one",
            goal_observations=[goal_creation()],
        )
        second = full_cycle_input(
            cycle_id="cycle-goal-two",
            as_of="2026-09-16T15:00:00Z",
            goal_observations=[goal_creation(
                created_at="2026-09-16T15:00:00Z",
                statement="Replace the open goal.",
            )],
        )
        for path, value in (
            (first_inputs / "first.json", first),
            (second_inputs / "second.json", second),
        ):
            path.write_text(json.dumps(value), encoding="utf-8")

        self.assertEqual(
            main([
                "--input-dir", str(first_inputs),
                "--journal", str(journal_path),
            ]),
            0,
        )
        self.assertEqual(
            main([
                "--input-dir", str(second_inputs),
                "--journal", str(journal_path),
            ]),
            1,
        )

        receipts = [
            record for record in AuditJournal(journal_path).read()
            if record["record_type"] == "cycle_receipt"
        ]
        self.assertEqual(len(receipts), 1)

    def test_goal_progress_is_persisted_and_exposed_without_closing(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="goal-progress-"))
        first_inputs = directory / "first"
        second_inputs = directory / "second"
        first_inputs.mkdir()
        second_inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        first = full_cycle_input(
            cycle_id="cycle-goal-one",
            goal_observations=[goal_creation()],
        )
        second = full_cycle_input(
            cycle_id="cycle-goal-two",
            as_of="2026-09-16T15:00:00Z",
            goal_observations=[goal_progress(
                observed_value=-0.5,
                assessment=(
                    "The measured gap passed the target, pending grading."
                ),
            )],
        )
        second["market_sessions"]["observed_at"] = (
            "2026-09-16T15:00:00Z")
        second["market_sessions"]["markets"][0]["local_time"] = (
            "2026-09-16T17:00:00+02:00")
        second["market_sessions"]["markets"][1]["local_time"] = (
            "2026-09-16T11:00:00-04:00")
        (first_inputs / "first.json").write_text(
            json.dumps(first), encoding="utf-8")
        (second_inputs / "second.json").write_text(
            json.dumps(second), encoding="utf-8")

        self.assertEqual(
            main([
                "--input-dir", str(first_inputs),
                "--journal", str(journal_path),
            ]),
            0,
        )
        self.assertEqual(
            main([
                "--input-dir", str(second_inputs),
                "--journal", str(journal_path),
            ]),
            0,
        )

        records = AuditJournal(journal_path).read()
        progress = next(
            record for record in records
            if record["record_id"]
            == "goal:goal-one:progress:cycle-goal-two"
        )
        payload = progress["payload"]
        self.assertEqual(payload["event"], "progress")
        self.assertEqual(payload["status"], "open")
        self.assertEqual(payload["previous_value"], 1)
        self.assertEqual(payload["observed_value"], -0.5)
        self.assertEqual(payload["delta"], -1.5)
        self.assertEqual(payload["remaining_to_target"], 0)
        self.assertEqual(
            payload["goal"]["statement"],
            goal_creation()["goal"]["statement"],
        )
        feedback = json.loads(
            (second_inputs / "FEEDBACK.json").read_text(
                encoding="utf-8"))
        row = feedback["goals"]["open"][0]
        self.assertEqual(row["source_cycle_id"], "cycle-goal-one")
        self.assertEqual(
            row["last_progress_cycle_id"], "cycle-goal-two")
        self.assertEqual(row["latest_observed_value"], -0.5)

    def test_goal_closure_is_computed_persisted_and_allows_next_goal(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="goal-close-"))
        journal_path = directory / "journal.jsonl"
        cycles = [
            full_cycle_input(
                cycle_id="cycle-goal-one",
                goal_observations=[goal_creation()],
            ),
            full_cycle_input(
                cycle_id="cycle-goal-progress",
                as_of="2026-09-16T15:00:00Z",
                goal_observations=[goal_progress()],
            ),
            full_cycle_input(
                cycle_id="cycle-goal-close",
                as_of="2026-09-17T15:00:00Z",
                goal_observations=[goal_close()],
            ),
        ]
        cycles[1]["market_sessions"]["observed_at"] = (
            "2026-09-16T15:00:00Z")
        cycles[1]["market_sessions"]["markets"][0]["local_time"] = (
            "2026-09-16T17:00:00+02:00")
        cycles[1]["market_sessions"]["markets"][1]["local_time"] = (
            "2026-09-16T11:00:00-04:00")
        close_sessions = cycles[2]["market_sessions"]
        close_sessions["observed_at"] = "2026-09-17T15:00:00Z"
        close_sessions["overlap"] = "both_open"
        for market, local_time, next_open, next_close in (
            (
                close_sessions["markets"][0],
                "2026-09-17T17:00:00+02:00",
                "2026-09-18T09:00:00+02:00",
                "2026-09-17T17:30:00+02:00",
            ),
            (
                close_sessions["markets"][1],
                "2026-09-17T11:00:00-04:00",
                "2026-09-18T09:30:00-04:00",
                "2026-09-17T16:00:00-04:00",
            ),
        ):
            market["local_time"] = local_time
            market["status"] = "open"
            market["is_open"] = True
            market["next_open"] = next_open
            market["next_close"] = next_close

        input_dirs = []
        for index, cycle in enumerate(cycles):
            inputs = directory / f"cycle-{index}"
            inputs.mkdir()
            (inputs / "cycle.json").write_text(
                json.dumps(cycle), encoding="utf-8")
            input_dirs.append(inputs)
            self.assertEqual(
                main([
                    "--input-dir", str(inputs),
                    "--journal", str(journal_path),
                ]),
                0,
            )

        records = AuditJournal(journal_path).read()
        closed = next(
            record
            for record in records
            if record["record_id"]
            == "goal:goal-one:closed:cycle-goal-close"
        )
        payload = closed["payload"]
        self.assertEqual(payload["status"], "closed")
        self.assertEqual(payload["terminal_status"], "met")
        self.assertEqual(payload["created_cycle_id"], "cycle-goal-one")
        self.assertEqual(payload["source_cycle_id"], "cycle-goal-close")
        self.assertEqual(payload["progress_count"], 1)
        feedback = json.loads(
            (input_dirs[-1] / "FEEDBACK.json").read_text(
                encoding="utf-8"))
        self.assertEqual(feedback["goals"]["open_count"], 0)
        row = feedback["goals"]["recent_closed"][0]
        self.assertEqual(row["terminal_status"], "met")
        self.assertEqual(
            row["analysis"]["next_change"],
            goal_close()["analysis"]["next_change"],
        )
        attribution = feedback["goal_attribution"]
        self.assertEqual(attribution["sample_count"], 1)
        self.assertEqual(attribution["outcome_counts"]["met"], 1)
        self.assertEqual(
            attribution["coverage"]["closures_with_goal_pattern"], 1)
        self.assertEqual(
            attribution["closure_evidence_sources"]["rows"][0]["source"],
            "specialist output",
        )

        next_goal = full_cycle_input(
            cycle_id="cycle-goal-next",
            as_of="2026-09-17T16:00:00Z",
            goal_observations=[goal_creation(
                goal_id="goal-two",
                created_at="2026-09-17T16:00:00Z",
                deadline="2026-09-18T16:00:00Z",
                statement="Close another selected evidence gap.",
                success_metric="second_unresolved_gap",
            )],
        )
        next_goal["market_sessions"]["observed_at"] = (
            "2026-09-17T16:00:00Z")
        next_goal["market_sessions"]["markets"][0]["local_time"] = (
            "2026-09-17T18:00:00+02:00")
        next_goal["market_sessions"]["markets"][1]["local_time"] = (
            "2026-09-17T12:00:00-04:00")
        errors = validate_input(
            next_goal, "next.json", records=records)
        self.assertFalse([
            error for error in errors
            if error.startswith("goal_")
        ])

        reused = dict(next_goal)
        reused["goal_observations"] = [goal_creation(
            created_at="2026-09-17T16:00:00Z",
            deadline="2026-09-18T16:00:00Z",
        )]
        self.assertIn(
            "goal_creation_invalid:0:goal_id_conflict",
            validate_input(reused, "reused.json", records=records),
        )

    def test_invalidated_goal_persists_without_a_fake_measurement(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="goal-invalidated-"))
        first_inputs = directory / "first"
        second_inputs = directory / "second"
        first_inputs.mkdir()
        second_inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        first = full_cycle_input(
            cycle_id="cycle-goal-one",
            goal_observations=[goal_creation()],
        )
        close = goal_close(
            observed_at="2026-09-16T15:00:00Z",
            closure_basis="invalidated",
            invalidation_reason=(
                "The source stopped publishing the metric."
            ),
        )
        close.pop("observed_value")
        second = full_cycle_input(
            cycle_id="cycle-goal-invalidated",
            as_of="2026-09-16T15:00:00Z",
            goal_observations=[close],
        )
        second["market_sessions"]["observed_at"] = (
            "2026-09-16T15:00:00Z")
        second["market_sessions"]["markets"][0]["local_time"] = (
            "2026-09-16T17:00:00+02:00")
        second["market_sessions"]["markets"][1]["local_time"] = (
            "2026-09-16T11:00:00-04:00")
        (first_inputs / "first.json").write_text(
            json.dumps(first), encoding="utf-8")
        (second_inputs / "second.json").write_text(
            json.dumps(second), encoding="utf-8")

        self.assertEqual(
            main([
                "--input-dir", str(first_inputs),
                "--journal", str(journal_path),
            ]),
            0,
        )
        self.assertEqual(
            main([
                "--input-dir", str(second_inputs),
                "--journal", str(journal_path),
            ]),
            0,
        )
        payload = next(
            record["payload"]
            for record in AuditJournal(journal_path).read()
            if record["record_id"]
            == "goal:goal-one:closed:cycle-goal-invalidated"
        )
        self.assertEqual(payload["terminal_status"], "invalidated")
        self.assertNotIn("observed_value", payload)
        feedback = json.loads(
            (second_inputs / "FEEDBACK.json").read_text(
                encoding="utf-8"))
        self.assertEqual(feedback["goals"]["invalidated_count"], 1)


class EffectiveSnapshotPersistenceTests(unittest.TestCase):
    def test_order_instruction_event_uses_nested_snapshot_time(self):
        directory = pathlib.Path(tempfile.mkdtemp(
            prefix="effective-event-time-",
        ))
        journal = AuditJournal(directory / "journal.jsonl")
        data = {
            "as_of": "top-level",
            "snapshot": {
                "as_of": "nested",
                "order_instructions": [],
            },
            "decision": {"status": "wait"},
            "order_instruction_activity": [{
                "operation": "get",
                "tool": "get order instructions",
                "result": {"order_instructions": []},
            }],
        }
        receipt = {"cycle_id": "cycle-nested-time"}
        journal.append(
            record_id="cycle-receipt:cycle-nested-time",
            record_type="cycle_receipt",
            agent="test",
            payload={"cycle_id": "cycle-nested-time"},
        )

        persist_order_instruction_activity(data, journal, receipt)

        payload = next(
            record["payload"]
            for record in journal.read()
            if record["record_type"] == "order_instruction_event"
        )
        self.assertEqual(payload["at"], "nested")


class HistoricalRefusalFeedbackTests(unittest.TestCase):
    def test_mixed_then_all_known_passes_report_only_current_failures(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="known-refusal-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        journal = AuditJournal(journal_path)
        old_path = inputs / "old.json"
        new_path = inputs / "new.json"
        old_path.write_text(
            json.dumps(sample_input(research=[])), encoding="utf-8")
        new_path.write_text(
            json.dumps(sample_input(research=[])), encoding="utf-8")
        old_reason = (
            "ValueError: invalid_host_input:old.json:missing_research"
        )
        self.assertTrue(record_refusal(journal, old_path, old_reason))

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            1,
        )
        first = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(first["last_pass"]["refused"], 1)
        self.assertEqual(
            [row["input"] for row in first["refused"]],
            ["new.json"],
        )
        self.assertEqual(first["refusal_patterns"]["cycles_analyzed"], 2)
        self.assertEqual(first["refusal_patterns"]["current_cycles"], 1)
        self.assertEqual(first["refusal_patterns"]["historical_cycles"], 1)
        new_refusal = next(
            record for record in AuditJournal(journal_path).read()
            if record["record_type"] == "host_input_refusal"
            and record["payload"]["input"] == "new.json"
        )
        self.assertTrue(
            new_refusal["payload"]["pass_id"].startswith("executor-pass:"))

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        second = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(second["last_pass"]["refused"], 0)
        self.assertEqual(second["refused"], [])
        self.assertIsNone(second["expected_input_shape"])
        self.assertEqual(second["refusal_patterns"]["current_cycles"], 0)
        self.assertEqual(second["refusal_patterns"]["historical_cycles"], 2)


class RewrittenInputIsRefusedTests(unittest.TestCase):
    """A daily cron that runs twice overwrites its own file.

    The filename is unchanged, so the derived cycle_id is unchanged, and the
    second cycle was silently skipped as "already persisted" -- discarding a
    real cycle while printing a line that reads like success.
    """

    def setUp(self):
        """Builds its own legacy receipt instead of borrowing the live journal.

        It used to copy the real audit file and rely on a pre-fingerprint
        receipt happening to be in there. That made the test depend on
        production state, and it broke the moment the chain was archived --
        which is the right outcome for the wrong reason.
        """
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="host-cycle-legacy-"))
        self.inputs = self.dir / "host_input"
        self.inputs.mkdir()
        self.journal_path = self.dir / "journal.jsonl"
        data = sample_input()
        (self.inputs / "legacy-cycle.json").write_text(json.dumps(data), encoding="utf-8")

        journal = AuditJournal(self.journal_path)
        journal.append(record_id="genesis", record_type="system_change", agent="t",
                       payload={"change": "test root"}, caused_by=())
        # snapshot_id with no ":" is the pre-fingerprint shape.
        journal.append(
            record_id="cycle-receipt:cycle-legacy-cycle", record_type="cycle_receipt",
            agent="t", payload={"cycle_id": "cycle-legacy-cycle",
                                "snapshot_id": "legacy-cycle"}, caused_by=())

    def test_an_unfingerprinted_predecessor_is_refused_not_skipped(self):
        """Still refused, but recorded rather than raised.

        Raising out of the loop meant one unrunnable input took every later
        valid cycle down with it. The input is still not executed; the
        difference is that the refusal is now durable and the pass continues.
        """
        code = main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path)])
        self.assertEqual(code, 1)
        journal = AuditJournal(self.journal_path)
        refusals = [r for r in journal.read()
                    if r["record_type"] == "host_input_refusal"]
        self.assertEqual(len(refusals), 1)
        self.assertIn("cannot_verify_input_unchanged", refusals[0]["payload"]["reason"])
        self.assertFalse([r for r in journal.read()
                          if r["record_id"] == "cycle-receipt:cycle-legacy-cycle"
                          and r["payload"].get("snapshot_id", "").count(":")])

    def test_a_refused_input_does_not_block_a_later_valid_one(self):
        """The poison pill: the host drifted once, corrected itself, and the
        corrected cycle was lost because the runner never got past the bad
        file."""
        import json as _json
        good = dict(_json.loads((self.inputs / "legacy-cycle.json").read_text()))
        good["cycle_id"] = "cycle-later-good"
        (self.inputs / "zz-later-good.json").write_text(_json.dumps(good))
        code = main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path)])
        self.assertEqual(code, 1)
        self.assertTrue(already_persisted(AuditJournal(self.journal_path),
                                          "cycle-later-good"))

    def test_an_explicit_cycle_id_lets_the_run_proceed(self):
        """The escape is naming the cycle, not assuming it is the old one."""
        code = main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path),
                     "--cycle-id", "cycle-named-by-hand"])
        self.assertEqual(code, 0)
        self.assertTrue(already_persisted(AuditJournal(self.journal_path),
                                          "cycle-named-by-hand"))

    def test_the_host_file_is_never_edited_to_give_it_an_identity(self):
        """The filename is addressing; the content is evidence."""
        before = (self.inputs / "legacy-cycle.json").read_text(encoding="utf-8")
        main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path),
              "--cycle-id", "cycle-untouched-check"])
        self.assertEqual((self.inputs / "legacy-cycle.json").read_text(encoding="utf-8"),
                         before)

    def test_feedback_only_does_not_execute_or_refuse_inputs(self):
        before = AuditJournal(self.journal_path).read()

        code = main([
            "--input-dir",
            str(self.inputs),
            "--journal",
            str(self.journal_path),
            "--refresh-feedback-only",
        ])

        after = AuditJournal(self.journal_path).read()
        self.assertEqual(code, 0)
        self.assertEqual(after, before)
        self.assertTrue((self.inputs / "FEEDBACK.json").is_file())


class ExecutionStateIsDerivedTests(unittest.TestCase):
    """"The host succeeded" and "the cycle executed" are different facts.

    Collapsing them is how a system reports E2E PASS when only the front half
    ran. Derived from the journal rather than declared, because a status
    field somebody writes by hand drifts from what it describes and then
    outranks it -- which is what STATE.json did while claiming the first
    receipt was still pending.
    """

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="host-state-test-"))
        self.inputs = self.dir / "host_input"
        self.inputs.mkdir()
        self.journal_path = self.dir / "journal.jsonl"
        shutil.copy(sorted(pathlib.Path("audit").glob("*.jsonl"))[-1], self.journal_path)

    def write(self, name, data):
        (self.inputs / name).write_text(json.dumps(data), encoding="utf-8")

    def states(self):
        return {r["input"]: r["state"]
                for r in execution_status(self.inputs, AuditJournal(self.journal_path).read())}

    def test_an_unexecuted_input_is_not_a_failure(self):
        """It is a cycle that has not run, not the host's fault and not a pass."""
        self.write("pending-cycle.json", sample_input())
        self.assertEqual(self.states()["pending-cycle.json"], HOST_INPUT_COMMITTED)

    def test_an_executed_input_is_verified(self):
        self.write("done-cycle.json", sample_input())
        main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path)])
        self.assertEqual(self.states()["done-cycle.json"], EXECUTION_VERIFIED)

    def test_the_two_states_are_distinguishable_in_one_run(self):
        """The case the model exists for: front half ran, back half did not."""
        self.write("done-cycle.json", sample_input())
        main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path)])
        self.write("pending-cycle.json", sample_input(as_of="2026-09-17T12:00:00Z"))
        states = self.states()
        self.assertEqual(states["done-cycle.json"], EXECUTION_VERIFIED)
        self.assertEqual(states["pending-cycle.json"], HOST_INPUT_COMMITTED)

    def test_a_rewritten_input_stops_matching_its_receipt(self):
        """The fingerprint is what ties a receipt to an input.

        Rewriting the file means the old receipt no longer evidences it, so
        the state falls back to committed-but-unexecuted rather than
        continuing to claim the old run.
        """
        self.write("c.json", sample_input())
        main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path)])
        self.assertEqual(self.states()["c.json"], EXECUTION_VERIFIED)
        self.write("c.json", sample_input(as_of="2026-09-18T00:00:00Z"))
        self.assertEqual(self.states()["c.json"], HOST_INPUT_COMMITTED)

    def test_a_malformed_input_does_not_hide_later_status_rows(self):
        (self.inputs / "a-malformed.json").write_text(
            '{"cycle_id":', encoding="utf-8")
        self.write("z-pending.json", sample_input())
        states = self.states()
        self.assertEqual(states["a-malformed.json"], EXECUTION_FAILED)
        self.assertEqual(states["z-pending.json"], HOST_INPUT_COMMITTED)

    def test_runtime_owned_json_is_not_a_host_status_row(self):
        (self.inputs / "FEEDBACK.json").write_text(
            json.dumps({"last_pass": {}}), encoding="utf-8")
        self.assertNotIn("FEEDBACK.json", self.states())

    def test_unpromoted_input_is_visible_but_inert_when_policy_exists(self):
        value = sample_input(cycle_id="cycle-direct")
        self.write("direct.json", value)
        (self.inputs / ".promotion_policy.json").write_text(
            json.dumps({"schema_version": 1, "legacy_files": []}),
            encoding="utf-8",
        )
        self.assertEqual(
            self.states()["direct.json"],
            PUBLICATION_UNVALIDATED,
        )
        self.assertEqual(host_input_paths(self.inputs), [])


class ResearchNeedsItsEvidenceTests(unittest.TestCase):
    """A conclusion without the calls that produced it cannot be audited.

    That is the gap that made every earlier cycle unverifiable.
    """

    def test_empty_tool_calls_is_refused(self):
        data = sample_input(research=[{"question": "q", "tool_calls": []}])
        self.assertTrue(any(e.startswith("research_without_tool_calls")
                            for e in validate_input(data, "t.json")))

    def test_absent_tool_calls_is_refused(self):
        data = sample_input(research=[{"question": "q"}])
        self.assertTrue(any(e.startswith("research_without_tool_calls")
                            for e in validate_input(data, "t.json")))

    def test_a_research_entry_that_is_not_an_object_is_refused(self):
        data = sample_input(research=["oops"])
        self.assertIn("research_entry_not_an_object:0", validate_input(data, "t.json"))

    def test_real_tool_calls_pass(self):
        self.assertEqual(validate_input(sample_input(), "t.json"), [])


class ValidationReadsWhatExecutionUsesTests(unittest.TestCase):
    """Validation checked the top level while handlers read the nested snapshot.

    An input whose top level said "ibkr" and whose snapshot said "guesswork",
    carried an invalid timestamp and declared order_submission_used=True was
    accepted and executed. The live-order denial was bypassed by the input
    disagreeing with itself.
    """

    def contradictory(self):
        data = sample_input()
        data["snapshot"] = {"source": "guesswork", "as_of": "not-a-date",
                            "order_submission_used": True, "nlv": 1.0}
        return data

    def test_a_contradictory_input_is_refused(self):
        errors = validate_input(self.contradictory(), "t.json")
        self.assertIn("contradictory_source", errors)
        self.assertIn("live_order_submission_forbidden", errors)

    def test_the_handler_and_the_validator_see_the_same_snapshot(self):
        data = self.contradictory()
        snapshot = effective_snapshot(data)
        self.assertEqual(snapshot["source"], "guesswork")
        self.assertIs(snapshot["order_submission_used"], True)

    def test_an_unparseable_timestamp_is_refused(self):
        data = sample_input()
        data["as_of"] = "not-a-date"
        self.assertTrue(any(e.startswith("unparseable_as_of")
                            for e in validate_input(data, "t.json")))

    def test_nanosecond_timestamp_is_portable(self):
        data = sample_input()
        data["as_of"] = "2026-09-17T10:31:54.69185424Z"

        self.assertEqual(validate_input(data, "t.json"), [])

    def test_a_clean_input_still_passes(self):
        self.assertEqual(validate_input(sample_input(), "t.json"), [])


class TheReceiptDoesNotClaimAFullCycleTests(unittest.TestCase):
    """Three replay stages are not a completed research cycle.

    portfolio, research and decision run here. Governance, adversarial
    re-derivation, counterfactual and learning do not, and a receipt saying
    mode=production invites the reader to assume they did.
    """

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="mode-test-"))
        self.inputs = self.dir / "host_input"
        self.inputs.mkdir()
        self.journal_path = self.dir / "journal.jsonl"
        journal = AuditJournal(self.journal_path)
        journal.append(record_id="genesis", record_type="system_change", agent="t",
                       payload={"change": "test root"}, caused_by=())
        (self.inputs / "m-cycle.json").write_text(json.dumps(sample_input()), encoding="utf-8")
        main(["--input-dir", str(self.inputs), "--journal", str(self.journal_path)])
        self.receipt = next(
            r["payload"] for r in AuditJournal(self.journal_path).read()
            if r.get("record_type") == "cycle_receipt")

    def test_the_mode_names_it_a_replay(self):
        self.assertEqual(self.receipt["mode"], "host_input_replay")

    def test_the_host_claim_names_the_stages_that_did_not_run(self):
        claim = self.receipt["host"]["cognitive_execution_claim"]
        self.assertIn("did NOT run", claim)
        self.assertIn("governance", claim.lower())


class EveryDecisionCarriesItsFrozenExAnteStateTests(unittest.TestCase):
    """effectiveness.py existed and no production path called it.

    Every real cycle already has the portfolio, evidence, and research trace
    needed to freeze the state the decision saw. Wiring it here makes the
    anti-hindsight boundary an executed artifact rather than a tested helper.
    """

    def test_the_persisted_decision_stage_carries_a_valid_snapshot(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="effectiveness-adoption-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        (inputs / "cycle.json").write_text(
            json.dumps(sample_input()), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0)
        decision = next(
            record["payload"]["output"]
            for record in AuditJournal(journal_path).read()
            if record.get("record_type") == "cycle_stage"
            and record.get("payload", {}).get("stage_id") == "decision"
        )

        from .effectiveness import verify_snapshot_integrity

        frozen = decision["ex_ante_snapshot"]
        self.assertEqual(verify_snapshot_integrity(frozen), [])
        self.assertEqual(decision["snapshot_hash"], frozen["snapshot_hash"])
        self.assertEqual(
            frozen["portfolio"]["net_liquidation_value"], 1000.0)
        self.assertEqual(frozen["evidence"][0]["finding"], "f")


class OrderInstructionStateMustBeObservedTests(unittest.TestCase):
    """IBKR is the authority on what instructions exist, not our journal.

    Three consecutive cycles sent no order data at all, so the host could not
    know whether something it staged days earlier was still sitting there,
    transmittable, on a thesis that had since broken. Not looking and finding
    nothing are different facts, and only one of them is evidence.
    """

    def test_an_absent_field_is_refused(self):
        data = sample_input()
        del data["order_instructions"]
        self.assertIn("order_instructions_required",
                      validate_input(data, "t.json"))

    def test_an_empty_list_is_a_real_answer(self):
        self.assertEqual(validate_input(sample_input(order_instructions=[]),
                                        "t.json"), [])

    def test_standing_instructions_are_accepted(self):
        data = sample_input(order_instructions=[
            {"id": "ibkr-1", "symbol": "MSFT", "action": "SELL",
             "quantity": 10, "limit_price": 100.0}])
        self.assertEqual(validate_input(data, "t.json"), [])

    def test_a_non_list_is_refused(self):
        self.assertIn("order_instructions_must_be_a_list",
                      validate_input(sample_input(order_instructions="none"),
                                     "t.json"))

    def test_the_worked_example_satisfies_the_rule(self):
        """An example that would be refused teaches the mistake it corrects."""
        from .host_feedback import CANONICAL_EXAMPLE
        self.assertIn("order_instructions", CANONICAL_EXAMPLE)
        self.assertEqual(
            validate_input(
                CANONICAL_EXAMPLE,
                "example",
                require_full_schema=True,
            ),
            [],
        )


class RecommendedInstructionMustBeStagedTests(unittest.TestCase):
    def recommended_input(self, *, full_cycle=False):
        data = full_cycle_input() if full_cycle else sample_input()
        instruction = {
            "action": "SELL",
            "symbol": "MSFT",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 500.0,
            "time_in_force": "DAY",
            "expected_effect": "Reduce exposure if the operator transmits it.",
            "invalidation": "Delete if the thesis changes before review.",
            "rationale_one_line": "Reduce a bounded, evidenced exposure.",
            "review_condition": "Review current evidence before transmission.",
            "rollback_condition": "Do not transmit if the evidence changes.",
        }
        data["decision"] = {
            "status": "recommended",
            "rationale": "Evidence supports a bounded reduction.",
            "rests_on": ["verified evidence"],
            "supersedes": [],
            "instruction": instruction,
            "instruction_staged": True,
            "ibkr_instruction_id": "ibkr-1",
        }
        data["order_instructions"] = [{
            "id": "ibkr-1",
            "symbol": "MSFT",
            "action": "SELL",
            "quantity": 1,
        }]
        data["order_instruction_activity"] = [
            {
                "operation": "create",
                "tool": "IBKR create order instruction",
                "request": instruction,
                "result": {"instruction_id": "ibkr-1"},
                "instruction_id": "ibkr-1",
            },
            {
                "operation": "get",
                "tool": "IBKR get order instructions",
                "result": {"order_instructions": data["order_instructions"]},
            },
        ]
        if full_cycle:
            decision_stage = next(
                row for row in data["cognitive_stages"]
                if row["stage_id"] == "decision")
            decision_stage["output"]["decision_status"] = "recommended"
            decision_stage["output"]["rationale"] = data["decision"]["rationale"]
        return data

    def test_a_recommendation_with_create_and_post_get_passes(self):
        self.assertEqual(
            validate_input(self.recommended_input(), "recommended.json"),
            [],
        )

    def test_a_json_only_recommendation_is_refused(self):
        data = self.recommended_input()
        data["decision"]["instruction_staged"] = False
        data["decision"].pop("ibkr_instruction_id")
        data.pop("order_instruction_activity")
        errors = validate_input(data, "recommended.json")
        self.assertIn("recommended_instruction_not_staged", errors)
        self.assertIn("recommended_instruction_id_required", errors)
        self.assertIn(
            "recommended_instruction_create_evidence_required", errors)
        self.assertIn("recommended_instruction_post_get_required", errors)

    def test_the_post_create_state_must_contain_the_exact_id(self):
        data = self.recommended_input()
        data["order_instructions"] = [{"id": "different"}]
        self.assertIn(
            "recommended_instruction_missing_from_post_state:ibkr-1",
            validate_input(data, "recommended.json"),
        )

    def test_a_staged_instruction_requires_operator_justification(self):
        data = self.recommended_input()
        del data["decision"]["instruction"]["rationale_one_line"]
        del data["decision"]["instruction"]["review_condition"]
        del data["decision"]["instruction"]["rollback_condition"]
        errors = validate_input(data, "recommended.json")
        self.assertIn(
            "recommended_instruction_field_required:rationale_one_line",
            errors,
        )
        self.assertIn(
            "recommended_instruction_field_required:review_condition",
            errors,
        )
        self.assertIn(
            "recommended_instruction_field_required:rollback_condition",
            errors,
        )


class KnownInstructionRecoveryTests(unittest.TestCase):
    def recommended_input(self, *, full_cycle=False):
        return RecommendedInstructionMustBeStagedTests.recommended_input(
            self,
            full_cycle=full_cycle,
        )

    def setUp(self):
        self.input_dir = pathlib.Path(
            tempfile.mkdtemp(prefix="instruction-recovery-"))
        source_name = "cycle-20260917T000356Z-r27s3.json"
        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "host_input"
            / source_name
        )
        shutil.copy2(source, self.input_dir / source_name)
        (self.input_dir / ".promotion_policy.json").write_text(
            json.dumps({
                "schema_version": 1,
                "legacy_files": [source_name],
                "required_recovery_sources": [{
                    "file": source_name,
                    "sha256": (
                        "8b61783cea4942a865df80face57f4270"
                        "e4df4e78d7de11e0daf6b03d62b5bff"
                    ),
                }],
            }),
            encoding="utf-8",
        )
        self.source = json.loads(source.read_text(encoding="utf-8"))

    def recovery_input(self, *, status="present"):
        data = full_cycle_input()
        data["as_of"] = "2026-09-17T08:15:00Z"
        instructions = (
            [{
                "id": "102",
                "symbol": "WHR",
                "contract_id_ex": "776900613@SMART",
                "side": "BUY",
                "quantity": 6.0,
                "order_type": "LIMIT",
                "limit_price": 10.0,
                "time_in_force": "DAY",
            }]
            if status == "present"
            else []
        )
        data["order_instructions"] = instructions
        create = self.source["order_instruction_activity"][0]
        source_instruction = self.source["decision"]["instruction"]
        data["staged_order_instruction_recovery"] = {
            "source_cycle_id": self.source["cycle_id"],
            "source_sha256": (
                "8b61783cea4942a865df80face57f4270"
                "e4df4e78d7de11e0daf6b03d62b5bff"
            ),
            "source_decision_status": self.source["decision"]["status"],
            "create_activity": create,
            "fresh_get": {
                "operation": "get",
                "tool": "Interactive Brokers (IBKR).get_order_instructions",
                "request": {},
                "result": {"order_instructions": instructions},
                "observed_at": data["as_of"],
            },
            "status": status,
            "instruction": (
                {
                    "action": source_instruction["action"],
                    "contract_id_ex": "776900613@SMART",
                    "quantity": source_instruction["quantity"],
                    "order_type": source_instruction["order_type"],
                    "limit_price": source_instruction["limit_price"],
                    "time_in_force": "DAY",
                    "rationale_one_line": (
                        "Reduce the bounded WHR assignment exposure."
                    ),
                    "review_condition": (
                        "Review current WHR evidence before transmission."
                    ),
                    "rollback_condition": (
                        "Do not transmit if the WHR thesis improves."
                    ),
                }
                if status == "present"
                else None
            ),
        }
        return data

    def validate(self, data):
        return validate_known_instruction_recovery(
            data,
            records=[],
            input_dir=self.input_dir,
        )

    def test_r33_style_omission_is_refused(self):
        data = self.recovery_input()
        data.pop("staged_order_instruction_recovery")
        self.assertEqual(
            self.validate(data),
            ["known_instruction_recovery_required:102"],
        )

    def test_exact_create_and_current_get_pass_without_recommendation(self):
        data = self.recovery_input()
        self.assertEqual(data["decision"]["status"], "wait")
        self.assertEqual(self.validate(data), [])

    def test_changed_historical_create_is_refused(self):
        data = self.recovery_input()
        data["staged_order_instruction_recovery"][
            "create_activity"
        ]["request"]["quantity"] = 7
        self.assertIn(
            "known_instruction_recovery_create_mismatch:102",
            self.validate(data),
        )

    def test_stale_get_cannot_be_relabelled_as_current(self):
        data = self.recovery_input()
        data["staged_order_instruction_recovery"][
            "fresh_get"
        ]["observed_at"] = self.source["as_of"]
        self.assertIn(
            "known_instruction_recovery_fresh_get_not_current_cycle:102",
            self.validate(data),
        )

    def test_absent_instruction_is_accepted_without_closing_proof(self):
        self.assertEqual(self.validate(self.recovery_input(status="absent")), [])

    def test_recovered_present_event_closes_live_proof(self):
        from .delivery_acceptance import FULL_CYCLE_STAGES, live_proofs

        data = self.recovery_input()
        journal = AuditJournal(
            pathlib.Path(tempfile.mkdtemp(prefix="recovery-journal-"))
            / "audit.ndjson"
        )
        cycle_id = "cycle-recovery-proof"
        persist_staged_order_instruction_recovery(
            data,
            journal,
            {"cycle_id": cycle_id},
        )
        receipt_record = {
            "record_id": f"cycle-receipt:{cycle_id}",
            "record_type": "cycle_receipt",
            "agent": "sovereign-host",
            "payload": {
                "mode": "production-host-full-cycle",
                "status": "completed",
                "stages": [
                    {"stage_id": stage, "status": "completed"}
                    for stage in FULL_CYCLE_STAGES
                ],
            },
        }
        records = [receipt_record, *journal.read()]
        self.assertTrue(live_proofs(records)["staged_order_instruction"])
        event = journal.read()[0]
        self.assertEqual(
            event["payload"]["operation"],
            "recovered_create",
        )
        self.assertEqual(
            event["payload"]["current_decision_status"],
            "wait",
        )

    def test_a_wait_cannot_create_a_speculative_instruction(self):
        data = self.recommended_input()
        data["decision"]["status"] = "wait"
        data["decision"]["instruction_staged"] = False
        data["decision"].pop("ibkr_instruction_id")
        self.assertIn(
            "instruction_created_without_recommendation:wait",
            validate_input(data, "wait.json"),
        )

    def test_verified_create_is_persisted_with_lifecycle_event(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="instruction-stage-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        (inputs / "cycle.json").write_text(
            json.dumps(self.recommended_input(full_cycle=True)),
            encoding="utf-8",
        )

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        records = AuditJournal(journal_path).read()
        event = next(
            record for record in records
            if record["record_type"] == "order_instruction_event")
        self.assertTrue(event["payload"]["verified_present"])
        self.assertEqual(event["payload"]["instruction_id"], "ibkr-1")
        lifecycle = next(
            record for record in records
            if record["record_type"] == "lifecycle_event")
        self.assertEqual(
            lifecycle["payload"]["to_state"], "instruction_created")

    def test_explicit_operator_approval_is_persisted(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="instruction-approval-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = sample_input(instruction_lifecycle_updates=[{
            "recommendation_id": "earlier-cycle",
            "event_id": "approval-1",
            "from_state": "instruction_created",
            "to_state": "approved",
            "evidence_ids": ["open-order-1"],
            "evidence": [{
                "tool": "get account orders",
                "result": {"order_id": "open-order-1"},
            }],
            "metadata": {"ibkr_instruction_id": "ibkr-1"},
        }])
        (inputs / "cycle.json").write_text(
            json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        lifecycle = next(
            record for record in AuditJournal(journal_path).read()
            if record["record_type"] == "lifecycle_event")
        self.assertEqual(lifecycle["payload"]["to_state"], "approved")
        self.assertEqual(
            lifecycle["payload"]["evidence"][0]["tool"],
            "get account orders",
        )

    def test_execution_requires_the_account_trades_tool(self):
        data = sample_input(instruction_lifecycle_updates=[{
            "recommendation_id": "earlier-cycle",
            "event_id": "execution-1",
            "from_state": "approved",
            "to_state": "executed",
            "evidence_ids": ["trade-1"],
            "evidence": [{
                "tool": "get account orders",
                "result": {"order_id": "open-order-1"},
            }],
        }])
        self.assertIn(
            "instruction_execution_requires_account_trades:0",
            validate_input(data, "execution.json"),
        )

        data["instruction_lifecycle_updates"][0]["evidence"][0] = {
            "tool": "get account trades",
            "result": {"trade_id": "trade-1"},
        }
        self.assertEqual(validate_input(data, "execution.json"), [])

    def test_submission_requires_account_orders(self):
        data = sample_input(instruction_lifecycle_updates=[{
            "recommendation_id": "earlier-cycle",
            "event_id": "submitted-1",
            "from_state": "instruction_created",
            "to_state": "submitted",
            "evidence_ids": ["order-1"],
            "evidence": [{
                "tool": "get order instructions",
                "result": {"instruction_id": "ibkr-1"},
            }],
        }])
        self.assertIn(
            "instruction_submission_requires_account_orders:0",
            validate_input(data, "submitted.json"),
        )

    def test_deletion_requires_action_or_operator_confirmation(self):
        data = sample_input(instruction_lifecycle_updates=[{
            "recommendation_id": "earlier-cycle",
            "event_id": "deleted-1",
            "from_state": "instruction_created",
            "to_state": "deleted",
            "evidence_ids": ["ibkr-1"],
            "evidence": [{
                "tool": "get order instructions",
                "result": {"order_instructions": []},
            }],
        }])
        self.assertIn(
            "instruction_deletion_requires_explicit_evidence:0",
            validate_input(data, "deleted.json"),
        )
        data["instruction_lifecycle_updates"][0]["evidence"][0] = {
            "tool": "operator confirmation",
            "result": {"instruction_id": "ibkr-1", "action": "deleted"},
        }
        self.assertEqual(validate_input(data, "deleted.json"), [])

    def test_deleted_cannot_conflict_with_fresh_connector_state(self):
        data = sample_input(
            order_instructions=[{"id": "ibkr-1"}],
            instruction_lifecycle_updates=[{
                "recommendation_id": "earlier-cycle",
                "event_id": "deleted-1",
                "from_state": "instruction_created",
                "to_state": "deleted",
                "evidence_ids": ["ibkr-1"],
                "evidence": [{
                    "tool": "operator confirmation",
                    "result": {
                        "instruction_id": "ibkr-1",
                        "action": "deleted in app",
                    },
                }],
                "metadata": {"ibkr_instruction_id": "ibkr-1"},
            }],
        )
        self.assertIn(
            "instruction_deletion_conflicts_with_fresh_connector_state:"
            "0:ibkr-1",
            validate_input(data, "deleted.json"),
        )

        data["order_instructions"] = []
        self.assertEqual(validate_input(data, "deleted.json"), [])


class ToolInventoryRunsThroughTheRealCycleTests(unittest.TestCase):
    def report(self):
        from .tool_inventory import (
            NONTRANSMITTING_WRITE_ACTIONS,
            REQUIRED_IBKR_ACTIONS,
            WRITE_ACTIONS,
        )

        return {
            "observed_at": "2026-09-16T22:34:48Z",
            "complete_for_current_session": True,
            "connectors": [{
                "name": "Interactive Brokers (IBKR)",
                "actions": [{
                    "name": name,
                    "inputs": [],
                    "returns": "result",
                    "mode": (
                        "write_nontransmitting"
                        if name in NONTRANSMITTING_WRITE_ACTIONS
                        else "write"
                        if name in WRITE_ACTIONS
                        else "read"
                    ),
                } for name in sorted(REQUIRED_IBKR_ACTIONS)],
            }],
            "manifest_discrepancies": [],
            "unreachable_manifest_connectors": [],
        }

    def test_action_level_inventory_is_persisted_and_surfaced(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="tool-inventory-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = full_cycle_input(tool_manifest_report=self.report())
        (inputs / "cycle.json").write_text(
            json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        inventory = next(
            record for record in AuditJournal(journal_path).read()
            if record["record_type"] == "tool_inventory")
        self.assertTrue(
            inventory["payload"]["complete_for_current_session"])
        self.assertEqual(
            len(inventory["payload"]["changes"]["added_actions"]),
            len(self.report()["connectors"][0]["actions"]),
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(
            feedback["tool_inventory"]["observed_at"],
            "2026-09-16T22:34:48Z",
        )
        self.assertEqual(
            feedback["tool_inventory"]["action_count"],
            len(self.report()["connectors"][0]["actions"]),
        )
        self.assertTrue(
            feedback["tool_inventory"]["changes"]["baseline_established"])
        self.assertEqual(
            feedback["tool_inventory"]["changes"]["added_actions"],
            [],
        )
        self.assertEqual(
            feedback["tool_inventory"]["changes"]["added_action_count"],
            len(self.report()["connectors"][0]["actions"]),
        )
        self.assertFalse(feedback["tool_inventory"]["stale"])
        self.assertEqual(
            feedback["tool_inventory"]["source_record_id"],
            f"tool-inventory:{feedback['tool_inventory']['source_cycle_id']}",
        )

    def test_lookalike_inventory_keys_are_refused(self):
        for key in (
            "tool_manifest",
            "tool_inventory",
            "tool_manifest_check",
        ):
            with self.subTest(key=key):
                data = full_cycle_input()
                data[key] = {"complete_for_current_session": True}
                self.assertIn(
                    f"tool_manifest_lookalike_key_unsupported:{key}",
                    validate_input(data, "cycle.json"),
                )

    def test_null_report_does_not_hide_a_lookalike_inventory(self):
        data = full_cycle_input(
            tool_manifest_report=None,
            tool_manifest={"complete_for_current_session": True},
        )
        self.assertIn(
            "tool_manifest_lookalike_key_unsupported:tool_manifest",
            validate_input(data, "cycle.json"),
        )
        self.assertTrue(
            any(
                error.startswith(
                    "tool_manifest_lookalike_contract_preview:tool_manifest:")
                for error in validate_input(data, "cycle.json")
            )
        )

    def test_inventory_remains_optional_when_no_inventory_key_is_present(self):
        errors = validate_input(full_cycle_input(), "cycle.json")
        self.assertFalse(
            any(error.startswith("tool_manifest_") for error in errors),
            errors,
        )


class ToolProvenanceRunsThroughTheRealCycleTests(unittest.TestCase):
    def test_only_citation_envelope_defects_are_advisory(self):
        data = v4_post_effective_full_cycle()
        errors = [
            "evidence_call_invalid:0:provenance:capture_missing",
            "evidence_call_invalid:0:provenance:web_source_0_fields",
            "tool_provenance_invalid:0:0:web_source_0_reconstruction_status",
            "market_scout_tool_provenance_invalid:0:web_source_0_fields",
            "evidence_call_invalid:0:binding:0:mismatch",
            "evidence_call_invalid:1:producer",
        ]

        blocking, advisory = partition_validation_errors(data, errors)

        self.assertEqual(blocking, [
            "evidence_call_invalid:0:binding:0:mismatch",
            "evidence_call_invalid:1:producer",
        ])
        self.assertEqual(advisory, [
            "evidence_call_invalid:0:provenance:capture_missing",
            "evidence_call_invalid:0:provenance:web_source_0_fields",
            "market_scout_tool_provenance_invalid:0:"
            "web_source_0_fields",
            "tool_provenance_invalid:0:0:"
            "web_source_0_reconstruction_status",
        ])

    def test_partial_cycle_cannot_settle_forecast(self):
        data = v4_post_effective_full_cycle(
            forecast_outcomes=[{"forecast_id": "forecast-1"}],
        )

        blocking, advisory = partition_validation_errors(data, [
            "evidence_call_invalid:0:provenance:capture_missing",
        ])

        self.assertEqual(advisory, [
            "evidence_call_invalid:0:provenance:capture_missing",
        ])
        self.assertIn(
            "partial_cycle_forecast_outcome_forbidden",
            blocking,
        )

    def test_advisory_only_cycle_finalizes_as_research_only(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="partial-cycle-"))
        path = directory / "cycle.json"
        journal = AuditJournal(directory / "audit" / "journal.jsonl")
        data = v4_post_effective_full_cycle()
        data["cycle_id"] = "cycle-partial-evidence"
        call = data["research"][0]["tool_calls"][0]
        call["provenance"]["source_refs"] = [{
            "kind": "url",
            "value": "https://example.test/source",
        }]
        call["provenance"]["web_sources"] = [{
            "url": "https://example.test/source",
            "title": "Source",
            "published_at": None,
            "retrieved_at": data["as_of"],
            "excerpt": None,
            "excerpt_sha256": None,
            "reconstruction_status": "invalid",
        }]
        path.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            side_effect=lambda: journal.read(),
        ):
            result = run_one(path, journal)

        self.assertEqual(result["evidence_completeness"], "partial")
        self.assertTrue(any(
            "web_source_0_fields" in error
            for error in result["evidence_advisories"]
        ))
        records = journal.read()
        self.assertFalse(any(
            record["record_type"] == "tool_provenance"
            for record in records
        ))
        finalization = next(
            record for record in records
            if record["record_type"] == "cycle_finalization"
        )
        self.assertEqual(finalization["payload"]["artifacts"], [])

    def test_hash_and_source_index_is_persisted_and_surfaced(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="tool-provenance-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = post_effective_full_cycle()
        data["research"][0]["tool_calls"][0]["provenance"][
            "source_refs"
        ] = [{
            "kind": "response_id",
            "value": "ibkr-response-1",
        }]
        (inputs / "cycle.json").write_text(
            json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        records = AuditJournal(journal_path).read()
        provenance = next(
            record for record in records
            if record["record_type"] == "tool_provenance")
        self.assertEqual(
            provenance["record_id"],
            f"tool-provenance:{provenance['payload']['cycle_id']}",
        )
        self.assertEqual(
            provenance["caused_by"],
            [f"cycle-receipt:{provenance['payload']['cycle_id']}"],
        )
        self.assertEqual(
            provenance["payload"]["calls"][0]["source_refs"],
            [{"kind": "response_id", "value": "ibkr-response-1"}],
        )
        self.assertNotIn("result", provenance["payload"]["calls"][0])
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback["tool_provenance"]["call_count"], 1)
        self.assertEqual(
            feedback["tool_provenance"]["connector_response_count"],
            1,
        )
        self.assertIn(
            "do not prove",
            feedback["tool_provenance"]["what_this_means"],
        )

    def test_identical_index_is_idempotent(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="tool-provenance-"))
        journal = AuditJournal(directory / "journal.jsonl")
        data = post_effective_full_cycle()
        receipt = {"cycle_id": "cycle-one"}

        persist_tool_provenance(data, journal, receipt)
        persist_tool_provenance(data, journal, receipt)

        self.assertEqual(
            sum(
                record["record_type"] == "tool_provenance"
                for record in journal.read()
            ),
            1,
        )

    def test_same_record_id_with_different_payload_is_refused(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="tool-provenance-"))
        journal = AuditJournal(directory / "journal.jsonl")
        journal.append(
            record_id="tool-provenance:cycle-one",
            record_type="tool_provenance",
            agent="sovereign-host",
            caused_by=(),
            payload={"cycle_id": "cycle-one", "calls": []},
        )

        with self.assertRaisesRegex(
            ValueError,
            "tool_provenance_payload_mismatch:"
            "tool-provenance:cycle-one",
        ):
            persist_tool_provenance(
                post_effective_full_cycle(),
                journal,
                {"cycle_id": "cycle-one"},
            )

    def test_post_effective_legacy_input_does_not_enter_v2_persistence(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="tool-provenance-"))
        journal = AuditJournal(directory / "journal.jsonl")
        data = sample_input(as_of="2026-09-17T16:00:00Z")

        persist_tool_provenance(data, journal, {"cycle_id": "legacy-cycle"})

        self.assertEqual(journal.read(), [])

    def test_v4_cycle_persists_private_response_artifact(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="tool-artifact-run-"))
        path = directory / "cycle.json"
        journal = AuditJournal(directory / "audit" / "journal.jsonl")
        data = v4_post_effective_full_cycle()
        data["cycle_id"] = "cycle-v4-artifact"
        path.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            side_effect=lambda: journal.read(),
        ):
            run_one(path, journal)

        provenance = next(
            record for record in journal.read()
            if record["record_type"] == "tool_provenance"
        )
        row = provenance["payload"]["calls"][0]
        self.assertTrue(row["artifact_ref"].startswith("profile://"))
        self.assertGreater(row["artifact_byte_length"], 0)
        self.assertEqual(row["capture_representation"], "canonical_response")
        self.assertEqual(
            len(list((directory / "tool_artifacts").rglob("*.json"))),
            1,
        )

    def test_v4_receipt_without_index_recovers_exactly_once(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="tool-artifact-recover-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        (inputs / ".promotion_policy.json").write_text(
            json.dumps({"schema_version": 1, "legacy_files": []}),
            encoding="utf-8",
        )
        path = inputs / "cycle.json"
        journal_path = directory / "audit" / "journal.jsonl"
        journal = AuditJournal(journal_path)
        data = v4_post_effective_full_cycle()
        data["cycle_id"] = "cycle-v4-recovery"
        path.write_text(json.dumps(data), encoding="utf-8")
        marker = marker_path(inputs, path.name)
        marker.parent.mkdir()
        marker.write_text(
            content_sha256(path) + "\n",
            encoding="utf-8",
        )

        with (
            patch(
                "runtime.run_host_cycle.load_journal_records",
                side_effect=lambda: journal.read(),
            ),
            patch(
                "runtime.run_host_cycle.materialize_artifacts",
                side_effect=OSError("simulated artifact write failure"),
            ),
            self.assertRaisesRegex(
                OSError,
                "simulated artifact write failure",
            ),
        ):
            run_one(path, journal)

        self.assertEqual(
            sum(
                record["record_type"] == "cycle_receipt"
                for record in journal.read()
            ),
            1,
        )
        self.assertFalse(any(
            record["record_type"] == "tool_provenance"
            for record in journal.read()
        ))

        with patch(
            "runtime.run_host_cycle.load_journal_records",
            side_effect=lambda: journal.read(),
        ):
            for _ in range(2):
                self.assertEqual(
                    main([
                        "--input-dir",
                        str(inputs),
                        "--journal",
                        str(journal_path),
                    ]),
                    0,
                )

        records = journal.read()
        self.assertEqual(
            sum(r["record_type"] == "cycle_receipt" for r in records),
            1,
        )
        self.assertEqual(
            sum(r["record_type"] == "tool_provenance" for r in records),
            1,
        )


class MarketSessionsRunThroughTheRealCycleTests(unittest.TestCase):
    def test_session_state_is_persisted_and_surfaced(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="market-sessions-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = full_cycle_input(
            as_of="2026-09-16T14:00:00Z",
            market_sessions=market_sessions_input(eu_open=True, us_open=False))
        (inputs / "cycle.json").write_text(
            json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        sessions = next(
            record for record in AuditJournal(journal_path).read()
            if record["record_type"] == "market_sessions")
        self.assertEqual(sessions["payload"]["overlap"], "eu_only")
        from .delivery_acceptance import live_proofs
        self.assertTrue(
            live_proofs(
                AuditJournal(journal_path).read())["market_session_awareness"])
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback["market_sessions"]["overlap"], "eu_only")

    def test_legacy_input_does_not_persist_unvalidated_session_state(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="legacy-sessions-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = sample_input(market_sessions={"unvalidated": True})
        (inputs / "cycle.json").write_text(
            json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        self.assertFalse(any(
            record["record_type"] == "market_sessions"
            for record in AuditJournal(journal_path).read()
        ))


class FeedbackCompoundsOnlyAcceptedCyclesTests(unittest.TestCase):
    def cycle(self, cycle_id, as_of, family, tool, question, *, nlv):
        sessions = market_sessions_input()
        sessions["observed_at"] = as_of
        for row in sessions["markets"]:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            observed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            row["local_time"] = observed.astimezone(
                ZoneInfo(row["timezone"])).isoformat()
        return full_cycle_input(
            cycle_id=cycle_id,
            as_of=as_of,
            snapshot={
                "net_liquidation_value": nlv,
                "cash": 10.0,
                "positions": [{
                    "symbol": "MSFT",
                    "market_value": 500.0,
                    "market_price": 500.0,
                }],
            },
            market_sessions=sessions,
            families_to_explore=["hedging"],
            research=[{
                "question": question,
                "finding": "Evidence-backed finding.",
                "strategy_family": family,
                "specialist_stage_id": "macro_specialist",
                "tool_calls": [{"tool": tool, "result": {"ok": True}}],
            }],
        )

    def test_refused_parseable_cycle_cannot_shape_feedback(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="accepted-feedback-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        accepted_one = self.cycle(
            "cycle-accepted-1",
            "2026-09-16T14:00:00Z",
            "hedging",
            "Longbridge",
            "Accepted first question?",
            nlv=1000.0,
        )
        accepted_two = self.cycle(
            "cycle-accepted-2",
            "2026-09-16T14:05:00Z",
            "relative_value",
            "Alpaca",
            "Accepted latest question?",
            nlv=1010.0,
        )
        refused = self.cycle(
            "cycle-refused",
            "2026-09-16T14:10:00Z",
            "refused_family",
            "Stocktwits",
            "Refused question must disappear?",
            nlv=9999.0,
        )
        refused["decision"]["status"] = "nonsense"
        for name, data in (
            ("a-accepted.json", accepted_one),
            ("b-accepted.json", accepted_two),
            ("z-refused.json", refused),
        ):
            (inputs / name).write_text(json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            1,
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertNotIn(
            "refused_family",
            feedback["strategy_coverage"]["families_used"],
        )
        self.assertNotIn(
            "stocktwits",
            feedback["source_coverage"]["sources_used"],
        )
        questions = [
            question
            for row in feedback["recent_reasoning"]["cycles"]
            for question in row["questions_asked"]
        ]
        self.assertIn("Accepted latest question?", questions)
        self.assertNotIn("Refused question must disappear?", questions)
        self.assertEqual(feedback["decision_outcomes"]["count"], 1)
        self.assertEqual(
            feedback["decision_outcomes"]["decisions"][0]["compared_against"],
            "2026-09-16T14:05:00Z",
        )
        self.assertEqual(
            feedback["research_candidates"]["requested_families"],
            ["hedging"],
        )
        self.assertEqual(
            feedback["research_agenda"]["cycles_examined"],
            2,
        )
        self.assertEqual(
            feedback["research_agenda"]["selection_counts"][
                "portfolio|factor_macro"
            ],
            2,
        )
        self.assertEqual(
            feedback["recent_input_selection"]["inputs_considered"],
            ["a-accepted.json", "b-accepted.json"],
        )
        self.assertIn(
            "z-refused.json",
            feedback["recent_input_selection"][
                "inputs_excluded_without_valid_receipt"],
        )

    def test_feedback_orders_cycles_by_observation_time_not_filename(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="feedback-order-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        older = self.cycle(
            "cycle-older",
            "2026-09-16T14:00:00Z",
            "hedging",
            "Longbridge",
            "Older question?",
            nlv=1000.0,
        )
        newer = self.cycle(
            "cycle-newer",
            "2026-09-16T14:05:00Z",
            "relative_value",
            "Alpaca",
            "Newer question?",
            nlv=1010.0,
        )
        (inputs / "z-older.json").write_text(
            json.dumps(older), encoding="utf-8")
        (inputs / "a-newer.json").write_text(
            json.dumps(newer), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )

        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(
            feedback["recent_reasoning"]["cycles"][-1]["cycle_id"],
            "cycle-newer",
        )
        self.assertEqual(
            feedback["recent_input_selection"]["inputs_considered"],
            ["z-older.json", "a-newer.json"],
        )


class RecoverableCycleFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = pathlib.Path(tempfile.mkdtemp(
            prefix="cycle-finalization-"
        ))
        self.inputs = self.directory / "host_input"
        self.inputs.mkdir()
        self.journal_path = self.directory / "journal.jsonl"
        self.cycle_id = "cycle-finalization-recovery"
        self.input_path = self.inputs / "cycle.json"
        self.data = v4_post_effective_full_cycle(cycle_id=self.cycle_id)
        self.input_path.write_text(
            json.dumps(self.data),
            encoding="utf-8",
        )
        (self.inputs / ".promotion_policy.json").write_text(
            json.dumps({"schema_version": 1, "legacy_files": []}),
            encoding="utf-8",
        )
        marker = marker_path(self.inputs, self.input_path.name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            content_sha256(self.input_path) + "\n",
            encoding="utf-8",
        )

    def records(self):
        return AuditJournal(self.journal_path).read()

    def test_receipt_and_provenance_without_dispositions_are_recovered(self):
        with patch(
            "runtime.run_host_cycle.persist_learning_dispositions",
            return_value=None,
        ):
            self.assertEqual(
                main([
                    "--input-dir",
                    str(self.inputs),
                    "--journal",
                    str(self.journal_path),
                ]),
                1,
            )

        partial = self.records()
        self.assertEqual(
            sum(r["record_type"] == "cycle_receipt" for r in partial),
            1,
        )
        self.assertEqual(
            sum(r["record_type"] == "tool_provenance" for r in partial),
            1,
        )
        self.assertEqual(
            sum(
                r["record_type"] == "learning_disposition"
                for r in partial
            ),
            0,
        )
        self.assertFalse(any(
            r["record_type"] == "cycle_finalization"
            for r in partial
        ))

        first = self.data["learning_stage_dispositions"][0]
        AuditJournal(self.journal_path).append(
            record_id=(
                f"learning-disposition:{self.cycle_id}:"
                f"{first['stage_id']}"
            ),
            record_type="learning_disposition",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{self.cycle_id}",),
            payload={
                "host_input_schema_version": 4,
                "stage_id": first["stage_id"],
                "disposition": first["disposition"],
                "rationale": first["rationale"],
                "evidence": list(first["evidence"]),
                "artifact_refs": [],
            },
        )

        self.assertEqual(
            main([
                "--input-dir",
                str(self.inputs),
                "--journal",
                str(self.journal_path),
            ]),
            0,
        )
        recovered = self.records()
        self.assertEqual(
            sum(
                r["record_type"] == "learning_disposition"
                for r in recovered
            ),
            3,
        )
        manifests = [
            r for r in recovered
            if r["record_type"] == "cycle_finalization"
        ]
        self.assertEqual(len(manifests), 1)
        self.assertEqual(
            {
                row["record_id"]
                for row in manifests[0]["payload"]["required_records"]
            },
            {
                f"learning-disposition:{self.cycle_id}:{stage_id}"
                for stage_id in LEARNING_STAGES
            }
            | {
                f"tool-provenance:{self.cycle_id}",
                f"forecast-assessment:{self.cycle_id}",
            },
        )

        record_count = len(recovered)
        self.assertEqual(
            main([
                "--input-dir",
                str(self.inputs),
                "--journal",
                str(self.journal_path),
            ]),
            0,
        )
        self.assertEqual(len(self.records()), record_count)

    def test_legacy_receipt_without_manifest_is_not_revalidated(self):
        data = v4_post_effective_full_cycle(
            cycle_id=self.cycle_id,
        )
        self.input_path.write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        run_one(self.input_path, AuditJournal(self.journal_path))
        records = self.records()
        receipt = next(
            row for row in records
            if row["record_type"] == "cycle_receipt"
        )
        receipt["payload"].pop("finalization_schema_version", None)
        from .cycle_receipt import receipt_hash
        receipt["payload"]["receipt_hash"] = receipt_hash(
            receipt["payload"]
        )
        legacy = [
            row for row in records
            if row["record_type"] != "cycle_finalization"
        ]
        previous = None
        from .engine import hash_record
        for row in legacy:
            row["prev_hash"] = previous
            row["record_hash"] = hash_record(row)
            previous = row["record_hash"]
        self.journal_path.write_text(
            "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":"))
                + "\n"
                for row in legacy
            ),
            encoding="utf-8",
        )
        with patch(
            "runtime.run_host_cycle.run_one",
            side_effect=AssertionError("legacy cycle was rerun"),
        ):
            self.assertEqual(
                main([
                    "--input-dir",
                    str(self.inputs),
                    "--journal",
                    str(self.journal_path),
                ]),
                0,
            )
        self.assertFalse(any(
            row["record_type"] == "cycle_finalization"
            for row in self.records()
        ))

    def test_existing_disposition_with_different_payload_fails_closed(self):
        with patch(
            "runtime.run_host_cycle.persist_learning_dispositions",
            return_value=None,
        ):
            self.assertEqual(
                main([
                    "--input-dir",
                    str(self.inputs),
                    "--journal",
                    str(self.journal_path),
                ]),
                1,
            )
        first = self.data["learning_stage_dispositions"][0]
        AuditJournal(self.journal_path).append(
            record_id=(
                f"learning-disposition:{self.cycle_id}:"
                f"{first['stage_id']}"
            ),
            record_type="learning_disposition",
            agent="sovereign-host",
            caused_by=(f"cycle-receipt:{self.cycle_id}",),
            payload={
                "host_input_schema_version": 4,
                "stage_id": first["stage_id"],
                "disposition": first["disposition"],
                "rationale": "different persisted meaning",
                "evidence": list(first["evidence"]),
                "artifact_refs": [],
            },
        )

        self.assertEqual(
            main([
                "--input-dir",
                str(self.inputs),
                "--journal",
                str(self.journal_path),
            ]),
            1,
        )
        self.assertFalse(any(
            r["record_type"] == "cycle_finalization"
            for r in self.records()
        ))

    def test_reformatting_same_json_keeps_finalization_identity(self):
        self.assertEqual(
            main([
                "--input-dir",
                str(self.inputs),
                "--journal",
                str(self.journal_path),
            ]),
            0,
        )
        self.input_path.write_text(
            json.dumps(self.data, indent=4) + "\n",
            encoding="utf-8",
        )
        before = len(self.records())
        self.assertEqual(
            main([
                "--input-dir",
                str(self.inputs),
                "--journal",
                str(self.journal_path),
            ]),
            0,
        )
        self.assertEqual(len(self.records()), before)

    def test_missing_private_artifact_is_rematerialized_before_success(self):
        self.assertEqual(
            main([
                "--input-dir",
                str(self.inputs),
                "--journal",
                str(self.journal_path),
            ]),
            0,
        )
        artifacts = [
            path
            for path in (self.directory / "tool_artifacts").rglob("*.json")
        ]
        self.assertEqual(len(artifacts), 1)
        artifacts[0].unlink()

        self.assertEqual(
            main([
                "--input-dir",
                str(self.inputs),
                "--journal",
                str(self.journal_path),
            ]),
            0,
        )
        self.assertTrue(artifacts[0].is_file())


class MemoryDistillationRunsThroughTheRealCycleTests(unittest.TestCase):
    def active_entry(self, **over):
        entry = {
            "memory_id": "m1",
            "layer": "active_brain",
            "status": "active",
            "as_of": "2026-09-16T20:00:00Z",
            "claim": "A durable, source-grounded process lesson.",
            "source_ids": ["source-1"],
            "evidence_status": "verified",
            "confidence": 0.8,
            "reconstruction_status": "passed",
            "claim_ids": ["c1"],
        }
        entry.update(over)
        return entry

    def research_entry(self, **over):
        entry = self.active_entry(
            memory_id="r1",
            layer="research_memory",
            status="validated",
        )
        entry.update(over)
        return entry

    def distillation(self, **over):
        value = {
            "distillation_id": "d1",
            "source_ids": ["source-1"],
            "source_time_bounds": {
                "from": "2026-09-16T00:00:00Z",
                "to": "2026-09-16T20:00:00Z",
            },
            "memory_objects": [self.research_entry()],
            "claim_ids": ["c1"],
            "contradiction_groups": {},
            "active_brain_proposals": [self.active_entry()],
            "retirements": [],
            "reconstruction_spec": {
                "required_claim_ids": ["c1"],
                "distilled_claim_ids": ["c1"],
                "source_claim_ids": ["c1"],
                "distilled_contradiction_groups": {},
            },
            "compression_metrics": {"raw_units": 100, "distilled_units": 10},
            "blockers": [],
            "ex_post_material": [],
            "brain_version": 1,
        }
        value.update(over)
        return value

    def test_an_admitted_memory_is_persisted_and_surfaced(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="memory-adoption-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        data = sample_input(memory_distillation=self.distillation())
        (inputs / "cycle.json").write_text(
            json.dumps(data), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0)
        records = AuditJournal(journal_path).read()
        self.assertIn("memory_distillation",
                      {record["record_type"] for record in records})
        self.assertIn("memory", {record["record_type"] for record in records})
        self.assertIn(
            "research_memory",
            {record["record_type"] for record in records},
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback["active_memory"]["count"], 1)
        self.assertEqual(feedback["research_memory"]["count"], 1)

    def test_partial_distillation_write_resumes_without_semantic_drift(self):
        directory = pathlib.Path(tempfile.mkdtemp(
            prefix="memory-finalization-recovery-"
        ))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        cycle_id = "cycle-memory-finalization-recovery"
        data = sample_input(
            cycle_id=cycle_id,
            memory_distillation=self.distillation(),
        )
        (inputs / "cycle.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        original = AuditJournal.append_idempotent
        failed = False

        def interrupt_after_distillation(journal, **kwargs):
            nonlocal failed
            if kwargs.get("record_type") == "research_memory" and not failed:
                failed = True
                raise ValueError("injected_after_distillation")
            return original(journal, **kwargs)

        with patch.object(
            AuditJournal,
            "append_idempotent",
            new=interrupt_after_distillation,
        ):
            self.assertEqual(
                main([
                    "--input-dir",
                    str(inputs),
                    "--journal",
                    str(journal_path),
                ]),
                1,
            )

        partial = AuditJournal(journal_path).read()
        distillation = next(
            record for record in partial
            if record["record_type"] == "memory_distillation"
        )
        self.assertTrue(
            distillation["payload"]["evaluation"]["admitted"]
        )
        self.assertFalse(any(
            record["record_type"] == "research_memory"
            for record in partial
        ))
        self.assertFalse(any(
            record["record_type"] == "memory"
            for record in partial
        ))
        self.assertFalse(any(
            record["record_type"] == "cycle_finalization"
            for record in partial
        ))

        self.assertEqual(
            main([
                "--input-dir",
                str(inputs),
                "--journal",
                str(journal_path),
            ]),
            0,
        )
        recovered = AuditJournal(journal_path).read()
        self.assertEqual(
            sum(
                record["record_type"] == "memory_distillation"
                for record in recovered
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["record_type"] == "research_memory"
                for record in recovered
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["record_type"] == "memory"
                for record in recovered
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["record_type"] == "cycle_finalization"
                for record in recovered
            ),
            1,
        )

    def test_failed_reconstruction_is_recorded_but_not_activated(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="memory-blocked-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        distillation = self.distillation()
        distillation["reconstruction_spec"]["distilled_claim_ids"] = []
        (inputs / "cycle.json").write_text(
            json.dumps(sample_input(memory_distillation=distillation)),
            encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0)
        records = AuditJournal(journal_path).read()
        distillation_record = next(
            record for record in records
            if record["record_type"] == "memory_distillation")
        self.assertFalse(
            distillation_record["payload"]["evaluation"]["admitted"])
        self.assertNotIn(
            "memory", {record["record_type"] for record in records})
        self.assertIn(
            "research_memory",
            {record["record_type"] for record in records},
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertFalse(feedback["memory_distillation"]["admitted"])
        self.assertFalse(
            feedback["memory_distillation"]["research_admitted"])
        self.assertIn(
            "reconstruction_missing_required_claims",
            feedback["memory_distillation"]["errors"],
        )
        self.assertEqual(feedback["research_memory"]["count"], 0)
        self.assertEqual(feedback["research_memory"]["inactive_count"], 1)

    def test_active_memory_accepts_a_new_version_of_the_same_id(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="memory-version-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"

        first = self.distillation()
        (inputs / "a.json").write_text(json.dumps(sample_input(
            cycle_id="cycle-memory-v1",
            memory_distillation=first,
        )), encoding="utf-8")
        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )

        second = self.distillation(
            distillation_id="d2",
            memory_objects=[self.research_entry(
                claim="Updated durable research claim.")],
            active_brain_proposals=[self.active_entry(
                claim="Updated active claim.")],
        )
        (inputs / "b.json").write_text(json.dumps(sample_input(
            cycle_id="cycle-memory-v2",
            memory_distillation=second,
        )), encoding="utf-8")
        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )

        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback["active_memory"]["count"], 1)
        self.assertEqual(
            feedback["active_memory"]["items"][0]["claim"],
            "Updated active claim.",
        )

    def test_stale_research_memory_can_be_revalidated_later(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="memory-retirement-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"

        (inputs / "a.json").write_text(json.dumps(sample_input(
            cycle_id="cycle-memory-create",
            memory_distillation=self.distillation(
                active_brain_proposals=[]),
        )), encoding="utf-8")
        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )

        retirement = self.distillation(
            distillation_id="d2",
            source_ids=["source-1", "source-retirement"],
            memory_objects=[],
            claim_ids=[],
            active_brain_proposals=[],
            retirements=[{
                "memory_id": "r1",
                "status": "stale",
                "reason": "New evidence invalidated the current version.",
                "source_ids": ["source-retirement"],
            }],
            reconstruction_spec={
                "required_claim_ids": [],
                "distilled_claim_ids": [],
                "source_claim_ids": [],
                "distilled_contradiction_groups": {},
            },
        )
        (inputs / "b.json").write_text(json.dumps(sample_input(
            cycle_id="cycle-memory-retire",
            memory_distillation=retirement,
        )), encoding="utf-8")
        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback["research_memory"]["count"], 0)
        self.assertEqual(
            feedback["research_memory"]["retired_ids"],
            ["r1"],
        )
        records = AuditJournal(journal_path).read()
        self.assertIn(
            "memory_retirement",
            {record["record_type"] for record in records},
        )
        latest_distillation = [
            record for record in records
            if record["record_type"] == "memory_distillation"
        ][-1]
        self.assertEqual(
            latest_distillation["payload"]["lifecycle"][
                "retirements_applied"],
            ["r1"],
        )

        restored = self.distillation(
            distillation_id="d3",
            memory_objects=[self.research_entry(
                claim="Revalidated after fresh evidence.")],
            active_brain_proposals=[],
        )
        (inputs / "c.json").write_text(json.dumps(sample_input(
            cycle_id="cycle-memory-restore",
            memory_distillation=restored,
        )), encoding="utf-8")
        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )
        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback["research_memory"]["count"], 1)
        self.assertEqual(
            feedback["research_memory"]["items"][0]["claim"],
            "Revalidated after fresh evidence.",
        )

    def test_existing_accepted_cycle_backfills_research_memory(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="memory-backfill-"))
        input_path = directory / "historical.json"
        journal = AuditJournal(directory / "journal.jsonl")
        data = sample_input(
            cycle_id="cycle-memory-historical",
            memory_distillation=self.distillation(
                active_brain_proposals=[]),
        )
        input_path.write_text(json.dumps(data), encoding="utf-8")
        receipt_id = "cycle-receipt:cycle-memory-historical"
        journal.append(
            record_id=receipt_id,
            record_type="cycle_receipt",
            agent="sovereign-host",
            payload={"cycle_id": "cycle-memory-historical"},
        )
        journal.append(
            record_id="memory-distillation:historical",
            record_type="memory_distillation",
            agent="sovereign-host",
            caused_by=(receipt_id,),
            payload={
                "distillation_id": "d1",
                "evaluation": {
                    "admitted": True,
                    "research_admitted": True,
                },
            },
        )

        self.assertEqual(
            backfill_research_memory([input_path], journal),
            1,
        )
        self.assertEqual(
            backfill_research_memory([input_path], journal),
            0,
        )
        record = next(
            row for row in journal.read()
            if row["record_type"] == "research_memory")
        self.assertTrue(record["payload"]["backfilled"])
        self.assertEqual(
            record["caused_by"],
            ["memory-distillation:historical"],
        )

    def test_reused_distillation_id_cannot_replace_memory(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="memory-id-reuse-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"

        (inputs / "a.json").write_text(json.dumps(sample_input(
            cycle_id="cycle-memory-original",
            memory_distillation=self.distillation(),
        )), encoding="utf-8")
        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )

        reused = self.distillation(
            memory_objects=[self.research_entry(
                claim="Replacement from reused distillation id.")],
            active_brain_proposals=[self.active_entry(
                claim="Replacement from reused distillation id.")],
        )
        (inputs / "b.json").write_text(json.dumps(sample_input(
            cycle_id="cycle-memory-reused",
            memory_distillation=reused,
        )), encoding="utf-8")
        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0,
        )

        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertIn(
            "distillation_id_reused:d1",
            feedback["memory_distillation"]["errors"],
        )
        self.assertEqual(
            feedback["active_memory"]["items"][0]["claim"],
            "A durable, source-grounded process lesson.",
        )
        self.assertEqual(
            feedback["research_memory"]["items"][0]["claim"],
            "A durable, source-grounded process lesson.",
        )


class MechanicalComponentsRunThroughTheRealCycleTests(unittest.TestCase):
    def mechanical_input(self):
        return sample_input(
            portfolio_mechanics={
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
                }],
            },
            expressions=[{
                "expression": {
                    "candidate_id": "expression-1",
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
            }],
            covered_call_candidates=[{
                "position": {
                    "underlying": "MSFT",
                    "shares": 100,
                    "spot": 490.0,
                },
                "call": {
                    "strike": 520.0,
                    "premium": 10.0,
                    "contracts": 1,
                    "target_price": 550.0,
                },
                "nav": 1_000_000.0,
            }],
            historical_backfill={
                "inception": "2026-09-15",
                "cutoff": "2026-09-16",
                "trade_windows": {
                    "1D": [{
                        "trade_id": "t1",
                        "trade_time": "2026-09-16T10:00:00Z",
                        "symbol": "MSFT",
                        "quantity": 1,
                        "price": 490.0,
                    }],
                },
                "performance": {
                    "dates": ["20260915", "20260916"],
                    "nav": [1000.0, 1005.0],
                    "cps": [0.0, 0.005],
                },
            },
            source_arbitrations=[{
                "as_of": "2026-09-16T20:00:00Z",
                "max_age_hours": 24,
                "numeric_conflict_tolerance": 0.01,
                "observations": [{
                    "source": "primary",
                    "observed_at": "2026-09-16T19:00:00Z",
                    "value": 100.0,
                    "confidence": 0.9,
                    "tier": 3,
                }],
            }],
            backtests=[{
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
            }],
            calibration_requests=[{
                "observations": [
                    {"probability": 0.8, "outcome": 1},
                    {"probability": 0.2, "outcome": 0},
                ],
                "min_samples": 10,
            }],
            experiment_evaluations=[{
                "baseline": [0.1] * 3,
                "variant": [0.2] * 3,
                "baseline_counter": [0.0] * 3,
                "variant_counter": [0.0] * 3,
                "min_samples": 3,
            }],
            goal_observations=[{
                "goal": {
                    "goal_id": "g1",
                    "category": "process",
                    "statement": "Close adoption debt.",
                    "deadline": "2026-09-20T00:00:00Z",
                    "success_metric": "unreachable_modules",
                    "success_target": 0,
                    "evaluation_rubric": "lower is better",
                    "metric_type": "controllable",
                    "baseline": 15,
                    "caused_by": ["finding-1"],
                },
                "observed_value": 0,
                "now": "2026-09-16T20:00:00Z",
            }],
        )

    def test_all_components_are_persisted_without_a_decision(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="mechanics-adoption-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        (inputs / "cycle.json").write_text(
            json.dumps(self.mechanical_input()), encoding="utf-8")

        self.assertEqual(
            main(["--input-dir", str(inputs), "--journal", str(journal_path)]),
            0)
        decision = next(
            record["payload"]["output"]
            for record in AuditJournal(journal_path).read()
            if record.get("record_type") == "cycle_stage"
            and record.get("payload", {}).get("stage_id") == "decision"
        )
        analysis = decision["mechanical_analysis"]
        self.assertTrue(analysis["portfolio"]["valid"])
        self.assertTrue(analysis["expressions"][0]["ready"])
        self.assertTrue(analysis["covered_calls"][0]["valid"])
        self.assertTrue(analysis["historical_backfill"]["valid"])
        self.assertTrue(analysis["source_arbitrations"][0]["valid"])
        self.assertTrue(analysis["backtests"][0]["valid"])
        self.assertTrue(analysis["calibration"][0]["valid"])
        self.assertEqual(
            analysis["calibration"][0]["source"],
            "host_supplied_unverified",
        )
        self.assertTrue(analysis["experiments"][0]["valid"])
        self.assertTrue(analysis["goals"][0]["valid"])
        for forbidden in ("winner", "rank", "recommended_action"):
            self.assertNotIn(forbidden, json.dumps(analysis))

    def test_the_analysis_is_surfaced_to_the_next_cycle(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="mechanics-feedback-"))
        inputs = directory / "host_input"
        inputs.mkdir()
        journal_path = directory / "journal.jsonl"
        (inputs / "cycle.json").write_text(
            json.dumps(self.mechanical_input()), encoding="utf-8")
        main(["--input-dir", str(inputs), "--journal", str(journal_path)])

        feedback = json.loads(
            (inputs / "FEEDBACK.json").read_text(encoding="utf-8"))
        self.assertTrue(
            feedback["mechanical_analysis"]["expressions"][0]["ready"])
