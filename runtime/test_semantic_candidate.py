import copy
import json
from pathlib import Path
import tempfile
import unittest

from .run_host_cycle import validate_input
from .semantic_candidate import (
    SemanticCandidateError,
    build_semantic_candidate,
    main,
    probe_semantic_candidate,
    translate_pointer,
)
from .test_run_host_cycle import (
    add_market_scout,
    v4_post_effective_full_cycle,
)
from .test_tool_provenance import upgrade_tool_calls_to_v4
from .test_run_host_cycle import ToolInventoryRunsThroughTheRealCycleTests


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
    def test_web_search_result_origin_downgrades_to_host_summary(self):
        semantic = semantic_candidate()
        call = semantic["research"][0]["tool_calls"][0]
        call["capture_origin"] = "web_search_result"

        built = build_semantic_candidate(
            semantic,
            filename="cycle-web-alias.semantic.json",
        )

        provenance = built.canonical["research"][0]["tool_calls"][0][
            "provenance"
        ]
        self.assertEqual(provenance["result_origin"], "host_summary")
        self.assertEqual(
            provenance["capture"]["capture_origin"],
            "host_summary",
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

        self.assertEqual(issues, [])
        call = built.canonical["evidence_calls"][-1]["call"]
        self.assertEqual(call["tool"], "web.search")
        self.assertEqual(
            call["provenance"]["result_origin"],
            "host_summary",
        )

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
        semantic = semantic_candidate()
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


if __name__ == "__main__":
    unittest.main()
