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


class SemanticCandidateBuilderTests(unittest.TestCase):
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
