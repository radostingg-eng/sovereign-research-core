import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from .run_host_cycle import validate_input
from .decision_repetition import validate_decision_repetition_review
from .market_sessions import validate_market_sessions
from .opportunity_ledger import identity_fingerprint, validate_opportunity_updates
from .research_allocation import validate_research_allocation
from .semantic_candidate import (
    SemanticCandidateError,
    SemanticIssue,
    build_semantic_candidate,
    main,
    probe_semantic_candidate,
    translate_pointer,
)
from .semantic_candidate import _parse_market_timestamp
from .test_decision_repetition import finalized_decision_records
from .test_opportunity_ledger import identity
from .test_run_host_cycle import (
    add_market_scout,
    v4_post_effective_full_cycle,
)
from .test_tool_provenance import upgrade_tool_calls_to_v4
from .test_run_host_cycle import ToolInventoryRunsThroughTheRealCycleTests
from .tool_provenance import validate_tool_call_provenance


def compact_call(call):
    provenance = call["provenance"]
    capture = provenance["capture"]
    sources = []
    for source in provenance.get("web_sources") or ():
        row = {
            key: source.get(key)
            for key in (
                "url",
                "title",
                "published_at",
                "retrieved_at",
                "excerpt",
            )
            if key in source
        }
        sources.append(row)
    return {
        "tool_call_id": call["tool_call_id"],
        "kind": call["kind"],
        "tool": call["tool"],
        "action": call["call"]["action"],
        "arguments": call["call"]["arguments"],
        "result": copy.deepcopy(call["result"]),
        "capture_origin": (
            capture.get("capture_origin")
            or "host_summary"
            if provenance["result_origin"] == "host_summary"
            else "direct_connector_response"
        ),
        "observed_at": provenance["observed_at"],
        "source_refs": copy.deepcopy(provenance["source_refs"]),
        "web_sources": sources,
        "redactions": copy.deepcopy(capture.get("redactions") or []),
        "request_redactions": copy.deepcopy(
            capture.get("request_redactions") or []
        ),
    }


def semantic_candidate():
    canonical = upgrade_tool_calls_to_v4(
        add_market_scout(v4_post_effective_full_cycle(
            cycle_id="cycle-semantic",
        ))
    )
    snapshot = canonical["snapshot"]
    snapshot.update({
        "positions": [],
        "open_orders": [],
        "trades": [],
        "order_instructions": [],
    })
    canonical["order_instructions"] = []
    evidence = [
        {
            "producer": "portfolio",
            "call": {
                "tool_call_id": "semantic-portfolio",
                "kind": "connector_lookup",
                "tool": "IBKR",
                "action": "get_portfolio",
                "arguments": {},
                "result": {
                    "net_liquidation_value":
                        snapshot["net_liquidation_value"],
                    "cash": snapshot["cash"],
                    "positions": [],
                },
                "capture_origin": "direct_connector_response",
                "observed_at": canonical["as_of"],
                "source_refs": [],
            },
        },
        {
            "producer": "saved_instructions",
            "call": {
                "tool_call_id": "semantic-instructions",
                "kind": "connector_lookup",
                "tool": "IBKR",
                "action": "get_order_instructions",
                "arguments": {},
                "result": {"order_instructions": []},
                "capture_origin": "direct_connector_response",
                "observed_at": canonical["as_of"],
                "source_refs": [],
            },
        },
        {
            "producer": "account_orders",
            "call": {
                "tool_call_id": "semantic-orders",
                "kind": "connector_lookup",
                "tool": "IBKR",
                "action": "get_account_orders",
                "arguments": {},
                "result": {"orders": []},
                "capture_origin": "direct_connector_response",
                "observed_at": canonical["as_of"],
                "source_refs": [],
            },
        },
        {
            "producer": "account_trades",
            "call": {
                "tool_call_id": "semantic-trades",
                "kind": "connector_lookup",
                "tool": "IBKR",
                "action": "get_account_trades",
                "arguments": {},
                "result": {"trades": []},
                "capture_origin": "direct_connector_response",
                "observed_at": canonical["as_of"],
                "source_refs": [],
            },
        },
    ]
    for index, market in enumerate(canonical["market_sessions"]["markets"]):
        evidence.append({
            "producer": "market_sessions",
            "market_region": market["region"],
            "call": {
                "tool_call_id": f"semantic-market-{index}",
                "kind": "connector_lookup",
                "tool": "market clock",
                "action": "get_market_session",
                "arguments": {"region": market["region"]},
                "result": {"is_open": market["is_open"]},
                "capture_origin": "direct_connector_response",
                "observed_at": canonical["as_of"],
                "source_refs": [],
            },
        })
    stages = {
        row["stage_id"]: {
            "status": row["status"],
            "tools_used": copy.deepcopy(row["tools_used"]),
            **{
                key: copy.deepcopy(value)
                for key, value in row["output"].items()
                if key not in {
                    "market_scout_report",
                    "research_agenda",
                    "decision_status",
                    "rationale",
                }
            },
        }
        for row in canonical["cognitive_stages"]
    }
    scout = next(
        row["output"]["market_scout_report"]
        for row in canonical["cognitive_stages"]
        if row["stage_id"] == "market_scout"
    )
    agenda = next(
        row["output"]["research_agenda"]
        for row in canonical["cognitive_stages"]
        if row["stage_id"] == "research_director"
    )
    semantic = {
        key: copy.deepcopy(value)
        for key, value in canonical.items()
        if key not in {
            "host_input_schema_version",
            "evidence_coverage_schema_version",
            "evidence_calls",
            "cognitive_stages",
            "research",
        }
    }
    semantic.update({
        "semantic_input_schema_version": 1,
        "market_scout_report": copy.deepcopy(scout),
        "research_agenda": copy.deepcopy(agenda),
        "stage_outputs": stages,
        "evidence_calls": evidence,
        "research": copy.deepcopy(canonical["research"]),
    })
    semantic["market_scout_report"]["tool_calls"] = [
        compact_call(call)
        for call in semantic["market_scout_report"]["tool_calls"]
    ]
    for row in semantic["research"]:
        row["tool_calls"] = [
            compact_call(call)
            for call in row["tool_calls"]
        ]
    return semantic


def finalized_carry_records(semantic):
    built = build_semantic_candidate(
        semantic,
        filename="cycle-prior.semantic.json",
    )
    stages = built.canonical["cognitive_stages"]
    records = [{
        "record_id": "cycle-receipt:cycle-prior",
        "record_type": "cycle_receipt",
        "payload": {"cycle_id": "cycle-prior"},
    }]
    records.extend({
        "record_id": f"cycle-stage:cycle-prior:{stage['stage_id']}",
        "record_type": "cycle_stage",
        "payload": {
            "cycle_id": "cycle-prior",
            "agent_id": stage["stage_id"],
            "output": copy.deepcopy(stage["output"]),
        },
    } for stage in stages)
    records.append({
        "record_id": "cycle-finalization:cycle-prior",
        "record_type": "cycle_finalization",
        "payload": {"cycle_id": "cycle-prior"},
    })
    return records


def finalized_tool_inventory_records(report, *, count=0):
    payload = copy.deepcopy(report)
    if count:
        payload["carry_forward"] = {
            "source_cycle_id": "cycle-origin",
            "count": count,
        }
    return [
        {
            "record_id": "cycle-receipt:cycle-prior",
            "record_type": "cycle_receipt",
            "payload": {"cycle_id": "cycle-prior"},
        },
        {
            "record_id": "tool-inventory:cycle-prior",
            "record_type": "tool_inventory",
            "caused_by": ["cycle-receipt:cycle-prior"],
            "payload": payload,
        },
        {
            "record_id": "cycle-finalization:cycle-prior",
            "record_type": "cycle_finalization",
            "payload": {"cycle_id": "cycle-prior"},
        },
    ]


class SemanticCandidateBuilderTests(unittest.TestCase):
    def test_feedback_derived_scout_keys_are_dropped_by_the_builder(self):
        """v2r108 copied a scout candidate from the FEEDBACK market_scout
        view, which adds runtime-derived identity_fingerprint and
        matching_opportunity_ids; the input validator rejects both."""
        semantic = semantic_candidate()
        candidates = semantic["market_scout_report"]["candidates"]
        self.assertTrue(candidates)
        for candidate in candidates:
            candidate["identity_fingerprint"] = "sha256:" + "0" * 64
            candidate["matching_opportunity_ids"] = ["opportunity-x"]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-derived-keys.semantic.json",
        )

        scout = next(
            stage["output"]["market_scout_report"]
            for stage in built.canonical["cognitive_stages"]
            if stage["stage_id"] == "market_scout"
        )
        for candidate in scout["candidates"]:
            self.assertNotIn("identity_fingerprint", candidate)
            self.assertNotIn("matching_opportunity_ids", candidate)
            self.assertIn("identity", candidate)
        self.assertIn("identity_fingerprint", candidates[0])

    def test_web_summary_origin_aliases_downgrade_to_host_summary(self):
        for alias in ("web_search_result", "web_result_summary"):
            with self.subTest(alias=alias):
                semantic = semantic_candidate()
                call = semantic["research"][0]["tool_calls"][0]
                call["capture_origin"] = alias

                built = build_semantic_candidate(
                    semantic,
                    filename="cycle-web-alias.semantic.json",
                )

                provenance = built.canonical["research"][0]["tool_calls"][
                    0
                ]["provenance"]
                self.assertEqual(
                    provenance["result_origin"],
                    "host_summary",
                )
                self.assertEqual(
                    provenance["capture"]["capture_origin"],
                    "host_summary",
                )

    def test_probe_reports_non_projecting_evidence_call_same_pass(self):
        semantic = semantic_candidate()
        semantic["evidence_calls"].append({
            "producer": "research",
            "tool_call_id": "duplicate-research-call",
            "action": "web.search",
            "arguments": {"query": "current primary evidence"},
            "result": {"summary": "host-generated research summary"},
            "observed_at": "2026-09-23T12:59:00Z",
        })

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-duplicate-research.semantic.json",
        )

        self.assertIn(
            SemanticIssue(
                "semantic_tool_call_missing",
                "/evidence_calls/6/tool",
                "tool",
            ),
            issues,
        )
        self.assertIn(
            SemanticIssue(
                "semantic_evidence_target_missing",
                "/evidence_calls/6",
                "research",
            ),
            issues,
        )

    def test_probe_reports_invented_producer_on_host_summary_same_pass(self):
        semantic = semantic_candidate()
        semantic["evidence_calls"].append({
            "producer": "option_research",
            "call": {
                "tool_call_id": "summary-with-invented-producer",
                "kind": "external_search",
                "tool": "web.search",
                "action": "web.search",
                "arguments": {"query": "current option chain"},
                "result": "Host summary of a real search.",
                "capture_origin": "host_summary",
                "observed_at": "2026-09-23T12:59:00Z",
                "source_refs": [{"kind": "url", "value": "https://example.com/a"}],
            },
        })

        issues = probe_semantic_candidate(
            semantic, filename="cycle-invented-producer.semantic.json",
        )

        self.assertIn(
            SemanticIssue(
                "semantic_evidence_producer_invalid",
                "/evidence_calls/6/producer",
                "option_research",
            ),
            issues,
        )
        self.assertNotIn(
            "semantic_evidence_target_missing",
            {issue.code for issue in issues},
        )
        semantic["evidence_calls"][-1]["producer"] = "market_sessions"
        self.assertNotIn(
            "semantic_evidence_producer_invalid",
            {issue.code for issue in probe_semantic_candidate(
                semantic, filename="cycle-valid-producer.semantic.json",
            )},
        )

    def test_probe_exposes_conflicting_result_behind_invalid_evidence_wrapper(
        self,
    ):
        semantic = semantic_candidate()
        research_call = copy.deepcopy(
            semantic["research"][0]["tool_calls"][0]
        )
        semantic["evidence_calls"].append({
            "producer": "option_research",
            "tool_call_id": research_call["tool_call_id"],
            "action": research_call["action"],
            "arguments": research_call["arguments"],
            "result": {"observations": [{"status": "DELAYED"}]},
            "observed_at": research_call["observed_at"],
        })

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-conflicting-option.semantic.json",
        )

        self.assertIn(
            SemanticIssue(
                "semantic_tool_call_id_conflict",
                "/evidence_calls/6/tool_call_id",
                "/research/0/tool_calls/0",
            ),
            issues,
        )
        self.assertIn(
            SemanticIssue(
                "semantic_evidence_target_missing",
                "/evidence_calls/6",
                "option_research",
            ),
            issues,
        )
        self.assertIn(
            SemanticIssue(
                "semantic_tool_call_missing",
                "/evidence_calls/6/tool",
                "tool",
            ),
            issues,
        )

        semantic["evidence_calls"][-1]["result"] = research_call["result"]
        self.assertNotIn(
            "semantic_tool_call_id_conflict",
            {
                issue.code for issue in probe_semantic_candidate(
                    semantic,
                    filename="cycle-identical-option.semantic.json",
                )
            },
        )

    def test_direct_web_search_origin_downgrades_to_host_summary(self):
        semantic = semantic_candidate()
        call = semantic["research"][0]["tool_calls"][0]
        call["capture_origin"] = "direct_web_search"

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-direct-web.semantic.json",
        )
        built = build_semantic_candidate(
            semantic,
            filename="cycle-direct-web.semantic.json",
        )
        provenance = built.canonical["research"][0]["tool_calls"][0][
            "provenance"
        ]

        self.assertEqual(issues, [])
        self.assertEqual(provenance["result_origin"], "host_summary")
        self.assertEqual(
            provenance["capture"]["capture_origin"],
            "host_summary",
        )

    def test_direct_web_response_envelope_downgrades_to_host_summary(self):
        semantic = semantic_candidate()
        source = semantic["research"][0]["tool_calls"][0]
        canonical = build_semantic_candidate(
            semantic,
            filename="cycle-web-source.semantic.json",
        ).canonical["research"][0]["tool_calls"][0]
        canonical["provenance"]["result_origin"] = "web_response"
        canonical["provenance"]["capture"][
            "capture_origin"
        ] = "direct_web_response"
        source.clear()
        source.update(canonical)

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-direct-web-response.semantic.json",
        )
        built = build_semantic_candidate(
            semantic,
            filename="cycle-direct-web-response.semantic.json",
        )
        provenance = built.canonical["research"][0]["tool_calls"][0][
            "provenance"
        ]

        self.assertEqual(issues, [])
        self.assertEqual(provenance["result_origin"], "host_summary")
        self.assertEqual(
            provenance["capture"]["capture_origin"],
            "host_summary",
        )
        self.assertEqual(
            provenance["capture"]["representation"],
            "host_summary_no_response",
        )

    def test_direct_web_result_envelope_downgrades_to_host_summary(self):
        semantic = semantic_candidate()
        source = semantic["research"][0]["tool_calls"][0]
        canonical = build_semantic_candidate(
            semantic,
            filename="cycle-web-result-source.semantic.json",
        ).canonical["research"][0]["tool_calls"][0]
        canonical["provenance"]["result_origin"] = "web_source"
        canonical["provenance"]["capture"][
            "capture_origin"
        ] = "direct_web_result"
        source.clear()
        source.update(canonical)

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-direct-web-result.semantic.json",
        )
        built = build_semantic_candidate(
            semantic,
            filename="cycle-direct-web-result.semantic.json",
        )
        provenance = built.canonical["research"][0]["tool_calls"][0][
            "provenance"
        ]

        self.assertEqual(issues, [])
        self.assertEqual(provenance["result_origin"], "host_summary")
        self.assertEqual(
            provenance["capture"]["capture_origin"],
            "host_summary",
        )
        self.assertEqual(
            provenance["capture"]["representation"],
            "host_summary_no_response",
        )

    def test_direct_file_analysis_origin_downgrades_to_transcribed(self):
        semantic = semantic_candidate()
        call = semantic["research"][0]["tool_calls"][0]
        call.update({
            "kind": "file_analysis",
            "tool": "Library file",
            "action": "read_csv",
            "result": {
                "rows": 1018,
                "sha256": "a" * 64,
            },
            "capture_origin": "direct_file_analysis",
            "source_refs": [{
                "kind": "file",
                "value": "MSP-Portfolios-2026-09-20.csv",
            }],
            "web_sources": [],
        })

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-file-analysis.semantic.json",
        )
        built = build_semantic_candidate(
            semantic,
            filename="cycle-file-analysis.semantic.json",
        )
        provenance = built.canonical["research"][0]["tool_calls"][0][
            "provenance"
        ]

        self.assertEqual(issues, [])
        self.assertEqual(provenance["result_origin"], "connector_response")
        self.assertEqual(
            provenance["capture"]["capture_origin"],
            "host_transcribed_response",
        )
        self.assertEqual(
            validate_input(
                built.canonical,
                "cycle-file-analysis.json",
            ),
            [],
        )

    def test_nested_call_error_pointer_exists_in_submitted_source(self):
        semantic = semantic_candidate()
        canonical = build_semantic_candidate(
            semantic,
            filename="cycle-source.semantic.json",
        ).canonical["research"][0]["tool_calls"][0]
        canonical["provenance"]["capture"][
            "capture_origin"
        ] = "invented_origin"
        semantic["research"][0]["tool_calls"][0] = canonical

        with self.assertRaises(SemanticCandidateError) as context:
            build_semantic_candidate(
                semantic,
                filename="cycle-pointer.semantic.json",
            )

        self.assertEqual(
            context.exception.issues[0].pointer,
            "/research/0/tool_calls/0/provenance/capture/"
            "capture_origin",
        )

    def test_semantic_and_canonical_versions_are_mechanical(self):
        semantic = semantic_candidate()
        semantic.pop("semantic_input_schema_version")
        semantic["host_input_schema_version"] = 4

        built = build_semantic_candidate(
            semantic,
            filename="cycle-versions.semantic.json",
        )

        self.assertEqual(built.canonical["host_input_schema_version"], 4)
        self.assertNotIn(
            "semantic_input_schema_version",
            built.canonical,
        )

    def test_probe_enumerates_all_r112_structural_defects(self):
        root = Path(__file__).resolve().parent.parent
        matches = list(
            (root / "host_staging" / "rejected").glob(
                "*r112x9*.json"
            )
        )
        if not matches:
            self.skipTest("profile rejection corpus not present")
        path = matches[0]
        value = json.loads(path.read_text(encoding="utf-8"))

        issues = probe_semantic_candidate(
            value,
            filename=(
                path.name.split(".semantic-", 1)[0]
                + ".semantic.json"
            ),
        )

        self.assertGreaterEqual(len(issues), 30)
        self.assertIn(
            (
                "semantic_top_level_missing",
                "/learning_stage_dispositions",
            ),
            {(issue.code, issue.pointer) for issue in issues},
        )

    def test_probe_accepts_committed_semantic_example(self):
        path = (
            Path(__file__).resolve().parent.parent
            / "schemas"
            / "host_semantic_v1.example.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            probe_semantic_candidate(
                value,
                filename="example.semantic.json",
            ),
            [],
        )

    def test_minimal_evidence_call_gets_mechanical_provenance(self):
        semantic = semantic_candidate()
        source = semantic["evidence_calls"][0]
        original = source["call"]
        semantic["evidence_calls"][0] = {
            "producer": source["producer"],
            "tool_call_id": original["tool_call_id"],
            "action": original["action"],
            "arguments": original["arguments"],
            "result": original["result"],
            "observed_at": original["observed_at"],
        }

        built = build_semantic_candidate(
            semantic,
            filename="cycle-minimal.semantic.json",
        )

        call = built.canonical["evidence_calls"][0]["call"]
        self.assertEqual(call["kind"], "connector_lookup")
        self.assertEqual(call["tool"], "IBKR")
        self.assertEqual(
            call["provenance"]["result_origin"],
            "connector_response",
        )
        self.assertEqual(
            call["provenance"]["capture"],
            {
                "schema_version": 2,
                "representation": "canonical_response",
                "redactions": [],
                "capture_origin": "direct_connector_response",
                "request_redactions": [],
                "reconstruction_status": "exact_response",
            },
        )
        self.assertEqual(call["provenance"]["source_refs"], [])
        self.assertEqual(call["provenance"]["web_sources"], [])

    def test_market_scout_evidence_defaults_to_web_search_tool(self):
        semantic = semantic_candidate()
        semantic["evidence_calls"].append({
            "producer": "market_scout",
            "tool_call_id": "scout-msft",
            "action": "web.search",
            "arguments": {"query": "fresh MSFT evidence"},
            "result": "Fresh Microsoft evidence reviewed.",
            "observed_at": semantic["as_of"],
        })

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-scout-tool.semantic.json",
        )
        built = build_semantic_candidate(
            semantic,
            filename="cycle-scout-tool.semantic.json",
        )

        self.assertEqual(issues, [SemanticIssue(
            "semantic_evidence_producer_invalid",
            "/evidence_calls/6/producer",
            "market_scout",
        )])
        call = built.canonical["evidence_calls"][-1]["call"]
        self.assertEqual(call["tool"], "web.search")
        self.assertEqual(
            call["provenance"]["result_origin"],
            "host_summary",
        )

    def test_web_prose_capture_origin_cannot_claim_connector_response(self):
        semantic = semantic_candidate()
        scout = semantic["market_scout_report"]["tool_calls"][0]
        self.assertIsInstance(scout["result"], str)
        scout["capture_origin"] = "connector_response"

        issues = probe_semantic_candidate(
            semantic, filename="cycle-web-origin.semantic.json"
        )

        self.assertIn(
            SemanticIssue(
                "semantic_capture_origin",
                "/market_scout_report/tool_calls/0/capture_origin",
                "connector_response",
            ),
            issues,
        )
        scout["capture_origin"] = "host_summary"
        built = build_semantic_candidate(
            semantic, filename="cycle-web-origin.semantic.json"
        )
        scout_stage = next(
            stage for stage in built.canonical["cognitive_stages"]
            if stage["stage_id"] == "market_scout"
        )
        call = scout_stage["output"]["market_scout_report"]["tool_calls"][0]
        self.assertEqual(
            call["provenance"]["capture"]["capture_origin"],
            "host_summary",
        )
        self.assertEqual(call["provenance"]["result_origin"], "host_summary")

    def test_prose_result_defaults_to_host_summary(self):
        semantic = semantic_candidate()
        call = semantic["evidence_calls"][0]["call"]
        call["result"] = "Portfolio was unchanged."
        del call["capture_origin"]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-prose.semantic.json",
        )

        canonical = built.canonical["evidence_calls"][0]
        self.assertIsNone(canonical["projection"])
        self.assertEqual(
            canonical["call"]["provenance"]["result_origin"],
            "host_summary",
        )
        self.assertEqual(
            canonical["call"]["provenance"]["capture"]["capture_origin"],
            "host_summary",
        )

    def test_explicit_transcribed_origin_is_preserved(self):
        semantic = semantic_candidate()
        call = semantic["evidence_calls"][0]["call"]
        call["capture_origin"] = "host_transcribed_response"

        built = build_semantic_candidate(
            semantic,
            filename="cycle-transcribed.semantic.json",
        )

        self.assertEqual(
            built.canonical["evidence_calls"][0]["call"][
                "provenance"
            ]["capture"]["capture_origin"],
            "host_transcribed_response",
        )

    def test_canonical_v4_call_is_accepted_inside_semantic_source(self):
        semantic = semantic_candidate()
        original = semantic["evidence_calls"][0]["call"]
        canonical = build_semantic_candidate(
            semantic,
            filename="cycle-source.semantic.json",
        ).canonical["evidence_calls"][0]["call"]
        semantic["evidence_calls"][0]["call"] = canonical

        rebuilt = build_semantic_candidate(
            semantic,
            filename="cycle-rebuilt.semantic.json",
        )

        self.assertEqual(
            rebuilt.canonical["evidence_calls"][0]["call"],
            canonical,
        )
        self.assertEqual(
            original["tool_call_id"],
            canonical["tool_call_id"],
        )

    def test_flat_call_reads_explicit_canonical_provenance(self):
        semantic = semantic_candidate()
        source = semantic["evidence_calls"][0]
        original = source["call"]
        semantic["evidence_calls"][0] = {
            "producer": source["producer"],
            "projection": source.get("projection"),
            "tool_call_id": original["tool_call_id"],
            "kind": original["kind"],
            "tool": original["tool"],
            "action": original["action"],
            "arguments": original["arguments"],
            "result": original["result"],
            "provenance": {
                "result_origin": "connector_response",
                "observed_at": original["observed_at"],
                "source_refs": [],
                "web_sources": [],
                "capture": {
                    "capture_origin": "direct_connector_response",
                    "redactions": [],
                    "request_redactions": [],
                },
            },
        }

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-flat-canonical.semantic.json",
        )
        built = build_semantic_candidate(
            semantic,
            filename="cycle-flat-canonical.semantic.json",
        )

        self.assertEqual(issues, [])
        call = built.canonical["evidence_calls"][0]["call"]
        self.assertEqual(call["provenance"]["observed_at"], original["observed_at"])
        self.assertEqual(
            call["provenance"]["capture"]["capture_origin"],
            "direct_connector_response",
        )

    def test_action_name_object_becomes_action_and_arguments(self):
        semantic = semantic_candidate()
        source = semantic["evidence_calls"][0]
        original = source["call"]
        source["call"] = {
            **original,
            "action": {
                "name": "get_account_trades",
                "period": "TODAY",
            },
        }
        source["call"].pop("arguments")

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-action-name.semantic.json",
        )
        built = build_semantic_candidate(
            semantic,
            filename="cycle-action-name.semantic.json",
        )

        self.assertEqual(issues, [])
        action = built.canonical["evidence_calls"][0]["call"]["call"]
        self.assertEqual(action["action"], "get_account_trades")
        self.assertEqual(action["arguments"], {"period": "TODAY"})

    def test_action_container_alias_is_normalized(self):
        semantic = semantic_candidate()
        canonical = build_semantic_candidate(
            semantic,
            filename="cycle-source.semantic.json",
        ).canonical["evidence_calls"][0]["call"]
        canonical["action"] = canonical.pop("call")
        semantic["evidence_calls"][0]["call"] = canonical

        rebuilt = build_semantic_candidate(
            semantic,
            filename="cycle-hybrid.semantic.json",
        )

        actual = rebuilt.canonical["evidence_calls"][0]["call"]
        self.assertEqual(actual["call"]["action"], "get_portfolio")
        self.assertEqual(actual["call"]["arguments"], {})
        self.assertEqual(
            actual["provenance"]["observed_at"],
            semantic["as_of"],
        )

    def test_explicit_projection_is_preserved_for_validation(self):
        semantic = semantic_candidate()
        semantic["evidence_calls"][0]["projection"] = {
            "extractor": "json_pointer_v1",
            "bindings": [{
                "source_path": "/cash",
                "target_path": "/snapshot/net_liquidation_value",
            }],
        }

        built = build_semantic_candidate(
            semantic,
            filename="cycle-explicit-projection.semantic.json",
        )

        errors = validate_input(
            built.canonical,
            built.target_name,
            records=[],
            require_full_schema=True,
        )
        self.assertTrue(any(
            error.endswith("binding:0:mismatch")
            for error in errors
        ))

    def test_repetition_review_is_preserved_in_decision_and_stage(self):
        semantic = semantic_candidate()
        review = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "new_evidence",
            "evidence_delta": ["finding:x"],
            "unresolved_question_ids": [],
            "rationale": "Fresh evidence supports the repeated status.",
        }
        semantic["decision"]["repetition_review"] = review

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        self.assertEqual(
            built.canonical["decision"]["repetition_review"],
            review,
        )
        decision_index, decision_stage = next(
            (index, row)
            for index, row in enumerate(built.canonical["cognitive_stages"])
            if row["stage_id"] == "decision"
        )
        self.assertEqual(
            decision_stage["output"]["repetition_review"],
            review,
        )
        self.assertEqual(
            built.pointer_map[
                f"/cognitive_stages/{decision_index}/output/"
                "repetition_review"
            ],
            "/decision/repetition_review",
        )

    def test_cycle_id_is_derived_from_cycle_filename(self):
        semantic = semantic_candidate()
        del semantic["cycle_id"]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-derived.semantic.json",
        )

        self.assertEqual(built.canonical["cycle_id"], "cycle-derived")

    def test_allowlisted_fields_carry_from_finalized_cycle(self):
        prior = semantic_candidate()
        records = finalized_carry_records(prior)
        semantic = semantic_candidate()
        del semantic["market_scout_report"]
        del semantic["research_agenda"]
        semantic["unchanged_from_prior"] = [
            "market_scout_report",
            "research_agenda",
        ]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-carried.semantic.json",
            records=records,
        )

        fields = built.canonical["carry_forward"]["fields"]
        self.assertEqual(fields["market_scout_report"]["count"], 1)
        self.assertEqual(fields["research_agenda"]["count"], 1)
        self.assertEqual(
            fields["market_scout_report"]["source_cycle_id"],
            "cycle-prior",
        )
        scout = next(
            row for row in built.canonical["cognitive_stages"]
            if row["stage_id"] == "market_scout"
        )
        self.assertIn(
            "market_scout_report",
            scout["output"]["carry_forward"],
        )

    def test_carry_from_unfinalized_cycle_is_refused(self):
        records = finalized_carry_records(semantic_candidate())
        records = [
            record for record in records
            if record["record_type"] != "cycle_finalization"
        ]
        semantic = semantic_candidate()
        del semantic["market_scout_report"]
        semantic["unchanged_from_prior"] = ["market_scout_report"]

        with self.assertRaises(SemanticCandidateError) as context:
            build_semantic_candidate(
                semantic,
                filename="cycle-carried.semantic.json",
                records=records,
            )

        self.assertEqual(
            context.exception.issues[0].code,
            "carry_forward_missing_prior",
        )

    def test_forbidden_carry_field_is_refused(self):
        semantic = semantic_candidate()
        semantic["unchanged_from_prior"] = ["decision"]

        with self.assertRaises(SemanticCandidateError) as context:
            build_semantic_candidate(
                semantic,
                filename="cycle-carried.semantic.json",
            )

        self.assertEqual(
            context.exception.issues[0].code,
            "carry_forward_forbidden",
        )

    def test_host_cannot_forge_carry_metadata(self):
        semantic = semantic_candidate()
        semantic["carry_forward"] = {
            "fields": {
                "decision": {
                    "source_cycle_id": "fabricated",
                    "count": 1,
                },
            },
        }

        built = build_semantic_candidate(
            semantic,
            filename="cycle-fresh.semantic.json",
        )

        self.assertNotIn("carry_forward", built.canonical)

    def test_fourth_research_carry_is_refused(self):
        prior = semantic_candidate()
        records = finalized_carry_records(prior)
        for record in records:
            if (
                record["record_type"] == "cycle_stage"
                and record["payload"]["agent_id"] == "market_scout"
            ):
                record["payload"]["output"]["carry_forward"] = {
                    "market_scout_report": {
                        "source_cycle_id": "cycle-origin",
                        "count": 3,
                    },
                }
        semantic = semantic_candidate()
        del semantic["market_scout_report"]
        semantic["unchanged_from_prior"] = ["market_scout_report"]

        with self.assertRaises(SemanticCandidateError) as context:
            build_semantic_candidate(
                semantic,
                filename="cycle-carried.semantic.json",
                records=records,
            )

        self.assertEqual(
            context.exception.issues[0].code,
            "carry_forward_exhausted",
        )

    def test_carried_cycle_cannot_register_forecast(self):
        records = finalized_carry_records(semantic_candidate())
        semantic = semantic_candidate()
        del semantic["market_scout_report"]
        semantic["unchanged_from_prior"] = ["market_scout_report"]
        semantic["forecast_registrations"] = [{"forecast_id": "f-1"}]

        with self.assertRaises(SemanticCandidateError) as context:
            build_semantic_candidate(
                semantic,
                filename="cycle-carried.semantic.json",
                records=records,
            )

        self.assertEqual(
            context.exception.issues[0].code,
            "carry_forward_forecast_forbidden",
        )

    def test_omitted_tool_manifest_carries_finalized_inventory(self):
        report = ToolInventoryRunsThroughTheRealCycleTests().report()
        semantic = semantic_candidate()
        semantic.pop("tool_manifest_report", None)

        built = build_semantic_candidate(
            semantic,
            filename="cycle-carried.semantic.json",
            records=finalized_tool_inventory_records(report),
        )

        carried = built.canonical["tool_manifest_report"]
        self.assertTrue(carried["stale"])
        self.assertEqual(carried["carry_forward"], {
            "source_cycle_id": "cycle-prior",
            "count": 1,
        })
        self.assertEqual(
            built.canonical["carry_forward"]["fields"][
                "tool_manifest_report"
            ]["count"],
            1,
        )

    def test_twenty_fifth_tool_manifest_carry_is_refused(self):
        report = ToolInventoryRunsThroughTheRealCycleTests().report()
        semantic = semantic_candidate()
        semantic.pop("tool_manifest_report", None)

        with self.assertRaises(SemanticCandidateError) as context:
            build_semantic_candidate(
                semantic,
                filename="cycle-carried.semantic.json",
                records=finalized_tool_inventory_records(
                    report,
                    count=24,
                ),
            )

        self.assertEqual(
            context.exception.issues[0].code,
            "carry_forward_exhausted",
        )

    def test_committed_semantic_example_builds_valid_canonical_v4(self):
        path = (
            Path(__file__).resolve().parent.parent
            / "schemas"
            / "host_semantic_v1.example.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))

        built = build_semantic_candidate(
            value,
            filename="example.semantic.json",
        )

        self.assertEqual(
            validate_input(
                built.canonical,
                built.target_name,
                records=[],
                require_full_schema=True,
            ),
            [],
        )

    def test_semantic_candidate_builds_valid_canonical_v4(self):
        built = build_semantic_candidate(
            semantic_candidate(),
            filename="cycle-semantic.semantic.json",
        )

        self.assertEqual(built.target_name, "cycle-semantic.json")
        self.assertEqual(
            validate_input(
                built.canonical,
                built.target_name,
                records=[],
                require_full_schema=True,
            ),
            [],
        )
        self.assertEqual(
            json.loads(built.canonical_bytes),
            built.canonical,
        )

    def test_missing_substantive_stage_output_is_never_invented(self):
        semantic = semantic_candidate()
        del semantic["stage_outputs"]["adversarial"]["observations"]

        with self.assertRaises(SemanticCandidateError) as context:
            build_semantic_candidate(
                semantic,
                filename="cycle.semantic.json",
            )

        self.assertEqual(
            context.exception.issues[0].pointer,
            "/stage_outputs/adversarial/observations",
        )

    def test_mechanical_aliases_are_normalized(self):
        semantic = semantic_candidate()
        semantic["stage_outputs"]["portfolio"][
            "evidence_status"
        ] = "partially_verified"
        semantic["market_sessions"]["markets"][0][
            "status"
        ] = "closed_weekend"

        built = build_semantic_candidate(
            semantic,
            filename="cycle.semantic.json",
        )

        portfolio = next(
            row for row in built.canonical["cognitive_stages"]
            if row["stage_id"] == "portfolio"
        )
        self.assertEqual(
            portfolio["output"]["evidence_status"],
            "partial",
        )
        self.assertEqual(
            built.canonical["market_sessions"]["markets"][0]["status"],
            "closed",
        )

    def test_canonical_stage_pointer_maps_to_semantic_source(self):
        built = build_semantic_candidate(
            semantic_candidate(),
            filename="cycle.semantic.json",
        )
        self.assertEqual(
            translate_pointer(
                "/cognitive_stages/2/output/research_agenda/"
                "candidates/0/trigger",
                built.pointer_map,
            ),
            "/research_agenda/candidates/0/trigger",
        )

    def test_selected_candidate_must_match_research_stage_id(self):
        """Genuinely ambiguous: two non-core stage_outputs keys, so the
        widened stale-specialist repair declines (there is no single
        unambiguous old name to rename) and the mismatch still fires."""
        semantic = semantic_candidate()
        semantic["stage_outputs"]["scout-other-specialist"] = copy.deepcopy(
            semantic["stage_outputs"]["macro_specialist"]
        )
        selected = next(
            candidate
            for candidate in semantic["research_agenda"]["candidates"]
            if candidate["selected"] is True
        )
        selected["candidate_id"] = "scout-vst-ppa"

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-stage-mismatch.semantic.json",
        )

        self.assertIn(
            (
                "semantic_selected_specialist_mismatch",
                "/research_agenda/candidates/0/candidate_id",
            ),
            {(issue.code, issue.pointer) for issue in issues},
        )
        self.assertNotIn(
            (
                "semantic_stage_output_missing",
                "/stage_outputs/scout-vst-ppa",
            ),
            {(issue.code, issue.pointer) for issue in issues},
        )

    def test_flat_evidence_provenance_pointer_maps_to_flat_source(self):
        semantic = semantic_candidate()
        source = semantic["evidence_calls"][0]
        semantic["evidence_calls"][0] = {
            "producer": source["producer"],
            **source["call"],
        }
        built = build_semantic_candidate(
            semantic,
            filename="cycle.semantic.json",
        )
        self.assertEqual(
            translate_pointer(
                "/evidence_calls/0/call/provenance/source_refs",
                built.pointer_map,
            ),
            "/evidence_calls/0/source_refs",
        )

    def test_compact_tool_manifest_expands_from_immutable_inventory(self):
        semantic = semantic_candidate()
        report = ToolInventoryRunsThroughTheRealCycleTests().report()
        semantic["tool_manifest_report"] = {
            **copy.deepcopy(report),
            "connectors": [{
                **copy.deepcopy(connector),
                "actions": [
                    action["name"] for action in connector["actions"]
                ],
            } for connector in report["connectors"]],
        }

        built = build_semantic_candidate(
            semantic,
            filename="cycle.semantic.json",
            records=[{
                "record_id": "tool-inventory:prior",
                "record_type": "tool_inventory",
                "payload": report,
            }],
        )

        self.assertTrue(all(
            isinstance(action, dict)
            for connector in built.canonical[
                "tool_manifest_report"
            ]["connectors"]
            for action in connector["actions"]
        ))

    def test_one_hundred_mechanical_variants_build_cleanly(self):
        for index in range(100):
            semantic = semantic_candidate()
            semantic["cycle_id"] = f"cycle-semantic-{index}"
            if index % 2:
                semantic["stage_outputs"]["portfolio"][
                    "evidence_status"
                ] = "partially_verified"
            if index % 3 == 0:
                market = semantic["market_sessions"]["markets"][0]
                if market["is_open"] is False:
                    market["status"] = "closed_weekend"
            built = build_semantic_candidate(
                semantic,
                filename=f"cycle-{index}.semantic.json",
            )
            self.assertEqual(
                validate_input(
                    built.canonical,
                    built.target_name,
                    records=[],
                    require_full_schema=True,
                ),
                [],
            )

    def test_cli_preflight_writes_canonical_bytes(self):
        root = Path(tempfile.mkdtemp(prefix="semantic-cli-"))
        source = root / "cycle.semantic.json"
        output = root / "cycle.json"
        source.write_text(
            json.dumps(semantic_candidate()),
            encoding="utf-8",
        )

        self.assertEqual(
            main([str(source), "--output", str(output)]),
            0,
        )
        self.assertEqual(
            json.loads(output.read_text()),
            build_semantic_candidate(
                semantic_candidate(),
                filename=source.name,
            ).canonical,
        )

    def test_absent_from_state_is_derived_for_existing_opportunity(self):
        semantic = semantic_candidate()
        semantic["opportunity_updates"] = [{
            "event_id": "vrt-screen",
            "opportunity_id": "vrt-special-situation",
            "to_state": "screened",
            "identity": identity(),
            "thesis": (
                "A corporate action may change normalized earnings power."
            ),
            "rationale": (
                "Fresh transaction evidence makes the idea worth retaining."
            ),
            "evidence": ["finding:x"],
        }]
        records = [{
            "record_id": "opportunity-event:vrt-new",
            "record_type": "opportunity_event",
            "payload": {
                "cycle_id": "cycle-prior",
                "opportunity_id": "vrt-special-situation",
                "identity_fingerprint": identity_fingerprint(identity()),
                "to_state": "new",
            },
        }]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
            records=records,
        )

        row = built.canonical["opportunity_updates"][0]
        self.assertEqual(row["from_state"], "new")
        self.assertEqual(
            [
                error for error in validate_opportunity_updates(
                    built.canonical["opportunity_updates"],
                    data=built.canonical,
                    records=records,
                )
                if error.startswith("opportunity_state_mismatch")
            ],
            [],
        )

    def test_explicit_wrong_from_state_is_left_untouched_and_refused(self):
        semantic = semantic_candidate()
        semantic["opportunity_updates"] = [{
            "event_id": "vrt-screen",
            "opportunity_id": "vrt-special-situation",
            "from_state": "watch",
            "to_state": "screened",
            "identity": identity(),
            "thesis": (
                "A corporate action may change normalized earnings power."
            ),
            "rationale": (
                "Fresh transaction evidence makes the idea worth retaining."
            ),
            "evidence": ["finding:x"],
        }]
        records = [{
            "record_id": "opportunity-event:vrt-new",
            "record_type": "opportunity_event",
            "payload": {
                "cycle_id": "cycle-prior",
                "opportunity_id": "vrt-special-situation",
                "identity_fingerprint": identity_fingerprint(identity()),
                "to_state": "new",
            },
        }]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
            records=records,
        )

        row = built.canonical["opportunity_updates"][0]
        self.assertEqual(row["from_state"], "watch")
        self.assertIn(
            "opportunity_state_mismatch:0:vrt-special-situation:new!=watch",
            validate_opportunity_updates(
                built.canonical["opportunity_updates"],
                data=built.canonical,
                records=records,
            ),
        )

    def test_absent_from_state_for_new_opportunity_is_unchanged(self):
        semantic = semantic_candidate()
        semantic["opportunity_updates"] = [{
            "event_id": "vrt-new",
            "opportunity_id": "vrt-special-situation",
            "to_state": "new",
            "identity": identity(),
            "thesis": (
                "A corporate action may change normalized earnings power."
            ),
            "rationale": (
                "Fresh transaction evidence makes the idea worth retaining."
            ),
            "evidence": ["finding:x"],
        }]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
            records=[],
        )

        row = built.canonical["opportunity_updates"][0]
        self.assertNotIn("from_state", row)
        self.assertEqual(
            validate_opportunity_updates(
                built.canonical["opportunity_updates"],
                data=built.canonical,
                records=[],
            ),
            [],
        )

    def test_absent_and_stale_local_time_is_derived_for_market_sessions(self):
        semantic = semantic_candidate()
        observed_at = semantic["market_sessions"]["observed_at"]
        expected_eu = _parse_market_timestamp(observed_at).astimezone(
            ZoneInfo("Europe/Berlin")
        ).isoformat()
        expected_us = _parse_market_timestamp(observed_at).astimezone(
            ZoneInfo("America/New_York")
        ).isoformat()
        semantic["market_sessions"]["markets"][0].pop("local_time")
        semantic["market_sessions"]["markets"][1]["local_time"] = (
            "2026-01-01T00:00:00-05:00"
        )

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        markets = built.canonical["market_sessions"]["markets"]
        self.assertEqual(markets[0]["local_time"], expected_eu)
        self.assertEqual(markets[1]["local_time"], expected_us)
        self.assertEqual(
            [
                error for error in validate_market_sessions(
                    built.canonical["market_sessions"],
                )
                if "local_time_mismatch" in error
            ],
            [],
        )

    def test_invalid_timezone_leaves_local_time_error_untouched(self):
        semantic = semantic_candidate()
        semantic["market_sessions"]["markets"][0]["timezone"] = (
            "Not/ARealZone"
        )
        semantic["market_sessions"]["markets"][0].pop("local_time")

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        self.assertNotIn(
            "local_time", built.canonical["market_sessions"]["markets"][0]
        )
        errors = validate_market_sessions(built.canonical["market_sessions"])
        self.assertIn("market_session_timezone_invalid:0", errors)
        self.assertIn("market_session_local_time_invalid:0", errors)

    def _existing_uncertainty_records(self):
        return [{
            "record_id": "opportunity-event:vrt-screened",
            "record_type": "opportunity_event",
            "payload": {
                "cycle_id": "cycle-prior",
                "opportunity_id": "vrt-special-situation",
                "identity_fingerprint": identity_fingerprint(identity()),
                "to_state": "screened",
                "research_state": {
                    "missing_information": [],
                    "uncertainties": [{
                        "id": (
                            "vrt-covered-premium-option-economics"
                            "-uncertainty"
                        ),
                        "description": (
                            "Uncertain whether covered-call premium "
                            "economics hold."
                        ),
                        "status": "open",
                    }],
                    "review_triggers": [],
                    "next_question_id": None,
                },
            },
        }]

    def _opportunity_update_with_uncertainty(self, uncertainty_overrides):
        uncertainty = {
            "id": "vrt-covered-premium-option-economics-uncertainty",
            "status": "open",
        }
        uncertainty.update(uncertainty_overrides)
        return {
            "event_id": "vrt-actionable",
            "opportunity_id": "vrt-special-situation",
            "from_state": "screened",
            "to_state": "actionable",
            "identity": identity(),
            "thesis": (
                "A corporate action may change normalized earnings power."
            ),
            "rationale": (
                "Fresh transaction evidence makes the idea worth retaining."
            ),
            "evidence": ["finding:x"],
            "research_state": {
                "missing_information": [],
                "uncertainties": [uncertainty],
                "review_triggers": [],
                "next_question_id": None,
            },
        }

    def test_absent_stable_description_is_filled_for_existing_row(self):
        semantic = semantic_candidate()
        semantic["opportunity_updates"] = [
            self._opportunity_update_with_uncertainty({})
        ]
        records = self._existing_uncertainty_records()

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
            records=records,
        )

        row = built.canonical["opportunity_updates"][0]
        uncertainty = row["research_state"]["uncertainties"][0]
        self.assertEqual(
            uncertainty["description"],
            "Uncertain whether covered-call premium economics hold.",
        )
        self.assertEqual(
            validate_opportunity_updates(
                built.canonical["opportunity_updates"],
                data=built.canonical,
                records=records,
            ),
            [],
        )

    def test_explicit_different_stable_description_is_refused(self):
        semantic = semantic_candidate()
        semantic["opportunity_updates"] = [
            self._opportunity_update_with_uncertainty({
                "description": "New unrelated wording.",
            })
        ]
        records = self._existing_uncertainty_records()

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
            records=records,
        )

        row = built.canonical["opportunity_updates"][0]
        uncertainty = row["research_state"]["uncertainties"][0]
        self.assertEqual(uncertainty["description"], "New unrelated wording.")
        self.assertIn(
            "opportunity_research_state_invalid:0:uncertainties:changed:"
            "vrt-covered-premium-option-economics-uncertainty:description",
            validate_opportunity_updates(
                built.canonical["opportunity_updates"],
                data=built.canonical,
                records=records,
            ),
        )

    def test_stale_specialist_stage_id_is_repaired_unambiguously(self):
        semantic = semantic_candidate()
        semantic["stage_outputs"]["macro_specialist_v2"] = (
            semantic["stage_outputs"].pop("macro_specialist")
        )
        for candidate in semantic["research_agenda"]["candidates"]:
            if candidate["selected"] is True:
                candidate["candidate_id"] = "macro_specialist_v2"
        # research[].specialist_stage_id is left as the stale pre-rename id.

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-stale-specialist.semantic.json",
        )
        self.assertNotIn(
            "semantic_selected_specialist_mismatch",
            {issue.code for issue in issues},
        )

        built = build_semantic_candidate(
            semantic,
            filename="cycle-stale-specialist.semantic.json",
        )
        self.assertEqual(
            {
                row.get("specialist_stage_id")
                for row in built.canonical["research"]
            },
            {"macro_specialist_v2"},
        )

    def test_no_repair_with_two_selected_candidates(self):
        semantic = semantic_candidate()
        semantic["stage_outputs"]["macro_specialist_v2"] = (
            semantic["stage_outputs"].pop("macro_specialist")
        )
        for candidate in semantic["research_agenda"]["candidates"]:
            if candidate["selected"] is True:
                candidate["candidate_id"] = "macro_specialist_v2"
            candidate["selected"] = True

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-stale-specialist.semantic.json",
        )
        self.assertIn(
            "semantic_selected_specialist_mismatch",
            {issue.code for issue in issues},
        )
        self.assertEqual(
            semantic["research"][0]["specialist_stage_id"],
            "macro_specialist",
        )

    def test_no_repair_when_stale_id_is_an_existing_stage_key(self):
        semantic = semantic_candidate()
        semantic["stage_outputs"]["macro_specialist_v2"] = copy.deepcopy(
            semantic["stage_outputs"]["macro_specialist"]
        )
        for candidate in semantic["research_agenda"]["candidates"]:
            if candidate["selected"] is True:
                candidate["candidate_id"] = "macro_specialist_v2"
        # The stale id "macro_specialist" is still a real stage_outputs key,
        # so it must not be blindly rewritten.

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-stale-specialist.semantic.json",
        )
        self.assertIn(
            "semantic_selected_specialist_mismatch",
            {issue.code for issue in issues},
        )
        self.assertEqual(
            semantic["research"][0]["specialist_stage_id"],
            "macro_specialist",
        )

    def test_stale_stage_output_key_is_renamed_when_unambiguous(self):
        """The widened case: the agenda candidate was renamed, the
        research row's specialist_stage_id is a THIRD, dangling id (not
        even the stale stage_outputs key), and stage_outputs itself was
        never renamed. Since exactly one stage_outputs key is not a core
        stage id, it is unambiguously the old name for the new
        candidate_id and gets renamed too."""
        semantic = semantic_candidate()
        for candidate in semantic["research_agenda"]["candidates"]:
            if candidate["selected"] is True:
                candidate["candidate_id"] = "macro_specialist_v2r111"
        semantic["research"][0]["specialist_stage_id"] = (
            "macro_specialist_v2r107"
        )
        semantic["findings"][0]["evidence"] = [
            "stage:macro_specialist", "finding:x",
        ]
        # Negative-check: the fixture really is stale/dangling before any
        # repair runs -- otherwise this test would pass vacuously.
        self.assertNotIn(
            "macro_specialist_v2r111", semantic["stage_outputs"],
        )
        self.assertIn("macro_specialist", semantic["stage_outputs"])
        self.assertNotIn(
            semantic["research"][0]["specialist_stage_id"],
            semantic["stage_outputs"],
        )

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-stale-stage-output.semantic.json",
        )
        codes = {issue.code for issue in issues}
        self.assertNotIn("semantic_selected_specialist_mismatch", codes)
        self.assertNotIn("semantic_stage_output_missing", codes)

        built = build_semantic_candidate(
            semantic,
            filename="cycle-stale-stage-output.semantic.json",
        )
        stage_ids = {
            row["stage_id"] for row in built.canonical["cognitive_stages"]
        }
        self.assertIn("macro_specialist_v2r111", stage_ids)
        self.assertNotIn("macro_specialist", stage_ids)
        self.assertEqual(
            {
                row.get("specialist_stage_id")
                for row in built.canonical["research"]
            },
            {"macro_specialist_v2r111"},
        )
        self.assertEqual(
            built.canonical["findings"][0]["evidence"],
            ["stage:macro_specialist_v2r111", "finding:x"],
        )

    def test_no_repair_with_two_non_core_stage_output_keys(self):
        semantic = semantic_candidate()
        semantic["stage_outputs"]["macro_specialist_other"] = copy.deepcopy(
            semantic["stage_outputs"]["macro_specialist"]
        )
        for candidate in semantic["research_agenda"]["candidates"]:
            if candidate["selected"] is True:
                candidate["candidate_id"] = "macro_specialist_v2r111"
        semantic["research"][0]["specialist_stage_id"] = (
            "macro_specialist_v2r107"
        )

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-stale-stage-output.semantic.json",
        )
        self.assertIn(
            "semantic_selected_specialist_mismatch",
            {issue.code for issue in issues},
        )
        self.assertEqual(
            semantic["research"][0]["specialist_stage_id"],
            "macro_specialist_v2r107",
        )

    def test_no_repair_with_zero_non_core_stage_output_keys(self):
        semantic = semantic_candidate()
        del semantic["stage_outputs"]["macro_specialist"]
        for candidate in semantic["research_agenda"]["candidates"]:
            if candidate["selected"] is True:
                candidate["candidate_id"] = "macro_specialist_v2r111"
        semantic["research"][0]["specialist_stage_id"] = (
            "macro_specialist_v2r107"
        )

        issues = probe_semantic_candidate(
            semantic,
            filename="cycle-stale-stage-output.semantic.json",
        )
        self.assertIn(
            "semantic_selected_specialist_mismatch",
            {issue.code for issue in issues},
        )
        self.assertEqual(
            semantic["research"][0]["specialist_stage_id"],
            "macro_specialist_v2r107",
        )

    def test_missing_url_source_ref_derived_from_web_sources(self):
        """A web_sources row with no matching url/link source_ref gets its
        source_ref added by the builder, and a date-only published_at is
        normalized -- clearing both web_sources_url_refs_mismatch and
        web_source_0_published_at."""
        semantic = semantic_candidate()
        call = semantic["research"][0]["tool_calls"][0]
        call["web_sources"] = [{
            "url": "https://example.com/article",
            "title": "Example article",
            "published_at": "2026-09-17",
            "retrieved_at": "2026-09-17T16:01:00Z",
        }]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        built_call = built.canonical["research"][0]["tool_calls"][0]
        provenance = built_call["provenance"]
        self.assertIn(
            {"kind": "url", "value": "https://example.com/article"},
            provenance["source_refs"],
        )
        self.assertEqual(
            provenance["web_sources"][0]["published_at"],
            "2026-09-17T00:00:00Z",
        )
        problems = validate_tool_call_provenance(
            built_call,
            cycle_as_of=built.canonical["as_of"],
            schema_version=4,
            validation_now=datetime(2026, 9, 17, 16, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [p for p in problems if "web_sources" in p or "published_at" in p],
            [],
        )

    def test_extra_url_source_ref_without_web_source_still_errors(self):
        """A url source_ref with no corresponding web_sources row cannot
        be derived (the builder would have to invent a title/dates/
        excerpt) so it remains an error."""
        semantic = semantic_candidate()
        call = semantic["research"][0]["tool_calls"][0]
        call["source_refs"].append({
            "kind": "url",
            "value": "https://example.com/unbacked",
        })

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        built_call = built.canonical["research"][0]["tool_calls"][0]
        problems = validate_tool_call_provenance(
            built_call,
            cycle_as_of=built.canonical["as_of"],
            schema_version=4,
            validation_now=datetime(2026, 9, 17, 16, 5, tzinfo=timezone.utc),
        )
        self.assertIn("web_sources_url_refs_mismatch", problems)

    def test_unparseable_published_at_left_untouched(self):
        """A published_at that is not a bare date and not a full timestamp
        cannot be format-normalized, so it still errors."""
        semantic = semantic_candidate()
        call = semantic["research"][0]["tool_calls"][0]
        call["source_refs"].append({
            "kind": "url",
            "value": "https://example.com/article",
        })
        call["web_sources"] = [{
            "url": "https://example.com/article",
            "title": "Example article",
            "published_at": "not-a-real-date",
            "retrieved_at": "2026-09-17T16:01:00Z",
        }]

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        built_call = built.canonical["research"][0]["tool_calls"][0]
        self.assertEqual(
            built_call["provenance"]["web_sources"][0]["published_at"],
            "not-a-real-date",
        )
        problems = validate_tool_call_provenance(
            built_call,
            cycle_as_of=built.canonical["as_of"],
            schema_version=4,
            validation_now=datetime(2026, 9, 17, 16, 5, tzinfo=timezone.utc),
        )
        self.assertIn("web_source_0_published_at", problems)

    def test_evidence_delta_prefix_derived_for_current_finding(self):
        """An evidence_delta entry that omits the finding: prefix but
        exactly matches a current-cycle finding id gets the prefix
        restored."""
        semantic = semantic_candidate()
        semantic["findings"] = [
            {"id": "finding-alpha", "statement": "Fresh evidence for X."},
        ]
        semantic["decision"]["repetition_review"] = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "new_evidence",
            "evidence_delta": ["finding-alpha"],
            "unresolved_question_ids": [],
            "rationale": "Reviewed against a fresh finding.",
        }

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        review = built.canonical["decision"]["repetition_review"]
        self.assertEqual(review["evidence_delta"], ["finding:finding-alpha"])
        records = finalized_decision_records("cycle-prior", "wait")
        errors = validate_decision_repetition_review(
            built.canonical,
            records=records,
            required=True,
        )
        self.assertEqual(
            [e for e in errors
             if e.startswith("decision_repetition_evidence_ref_invalid")],
            [],
        )

    def test_evidence_delta_prose_replaced_with_current_findings(self):
        """Free-form prose that cannot be mapped to one specific finding
        is replaced by every current-cycle finding:<id> ref, and the
        host's original text is preserved in rationale rather than
        discarded."""
        semantic = semantic_candidate()
        semantic["findings"] = [
            {"id": "finding-alpha", "statement": "Fresh evidence for X."},
            {"id": "finding-beta", "statement": "Fresh evidence for Y."},
        ]
        semantic["decision"]["repetition_review"] = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "new_evidence",
            "evidence_delta": [
                "Fresh IBKR reads show updated option pricing.",
            ],
            "unresolved_question_ids": [],
            "rationale": "Reviewed with fresh evidence.",
        }

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        review = built.canonical["decision"]["repetition_review"]
        self.assertEqual(
            set(review["evidence_delta"]),
            {"finding:finding-alpha", "finding:finding-beta"},
        )
        self.assertIn(
            "Fresh IBKR reads show updated option pricing.",
            review["rationale"],
        )
        records = finalized_decision_records("cycle-prior", "wait")
        errors = validate_decision_repetition_review(
            built.canonical,
            records=records,
            required=True,
        )
        self.assertEqual(
            [e for e in errors
             if e.startswith("decision_repetition_evidence_ref_invalid")],
            [],
        )

    def test_evidence_delta_prose_with_no_findings_still_errors(self):
        """new_evidence with no current findings at all has nothing
        honest to cite, so the runtime leaves the prose untouched and the
        error still fires."""
        semantic = semantic_candidate()
        semantic["findings"] = []
        semantic["decision"]["repetition_review"] = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "new_evidence",
            "evidence_delta": ["Some prose with no matching finding."],
            "unresolved_question_ids": [],
            "rationale": "Reviewed but nothing new to cite.",
        }

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        review = built.canonical["decision"]["repetition_review"]
        self.assertEqual(
            review["evidence_delta"],
            ["Some prose with no matching finding."],
        )
        records = finalized_decision_records("cycle-prior", "wait")
        errors = validate_decision_repetition_review(
            built.canonical,
            records=records,
            required=True,
        )
        self.assertIn("decision_repetition_evidence_ref_invalid:0", errors)

    def test_position_symbol_derived_from_contract_description(self):
        """An IBKR-shaped STK position with no symbol/contract_id_ex/conid
        string identifier gets its ticker exposed under symbol from
        contract_description, so portfolio_risk_ref: "position:<ticker>"
        resolves. Synthetic tickers only, not a real portfolio holding."""
        semantic = semantic_candidate()
        semantic["snapshot"]["positions"] = [
            {
                "contract_id": 504546674,
                "contract_description": "ZTST",
                "asset_class": "STK",
                "position": 1800,
            },
            {
                "contract_id": 895242605,
                "contract_description": "ZTST Dec15'28 30 PUT @AMEX",
                "asset_class": "OPT",
                "position": -1,
            },
        ]
        candidate = semantic["research_agenda"]["candidates"][1]
        candidate["portfolio_risk_ref"] = "position:ZTST"

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        positions = built.canonical["snapshot"]["positions"]
        self.assertEqual(positions[0]["symbol"], "ZTST")
        self.assertNotIn("symbol", positions[1])
        errors = validate_research_allocation(
            built.canonical,
            required=False,
        )
        self.assertEqual(
            [e for e in errors if e.endswith("1:portfolio_risk_ref_unresolved")],
            [],
        )

    def test_position_symbol_not_held_still_errors(self):
        """A portfolio_risk_ref naming a symbol the account genuinely does
        not hold cannot be resolved by any derivation, so it stays an
        error. Synthetic tickers only."""
        semantic = semantic_candidate()
        semantic["snapshot"]["positions"] = [
            {
                "contract_id": 504546674,
                "contract_description": "ZTST",
                "asset_class": "STK",
                "position": 1800,
            },
        ]
        candidate = semantic["research_agenda"]["candidates"][1]
        candidate["portfolio_risk_ref"] = "position:QQZZ"

        built = build_semantic_candidate(
            semantic,
            filename="cycle-semantic.semantic.json",
        )

        errors = validate_research_allocation(
            built.canonical,
            required=False,
        )
        self.assertIn(
            "research_allocation_candidate_invalid:"
            "1:portfolio_risk_ref_unresolved",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
