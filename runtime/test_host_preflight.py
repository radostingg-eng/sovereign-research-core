"""Synthetic parity tests for host_tools/preflight.py.

host_tools/preflight.py is a standalone, stdlib-only file the host execs in
its own sandbox before committing a candidate: it re-implements the
self-contained slice of the real intake validators that is decidable from
the candidate JSON plus FEEDBACK.json alone.

These tests use ONLY synthetic fixtures built from the checked-in schema
example (schemas/host_semantic_v1.example.json) and the existing
semantic_candidate() test helper -- never real production data. They cover:
one baseline candidate preflight must accept, one broken variant per major
check family, and -- the parity property that matters most -- proof that
preflight does not flag any of the four mechanical derivations
build_semantic_candidate performs, nor the stale specialist_stage_id repair,
since a host that trusts preflight and then hits the real builder must see
the same verdict.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from .test_semantic_candidate import semantic_candidate

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT_PATH = REPO_ROOT / "host_tools" / "preflight.py"

_spec = importlib.util.spec_from_file_location(
    "host_tools_preflight_under_test", PREFLIGHT_PATH,
)
preflight_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight_module)
preflight = preflight_module.preflight


def _valid_candidate() -> dict:
    """A synthetic candidate preflight accepts cleanly.

    Built from the existing semantic_candidate() test fixture (already
    exercised against the real builder/validator elsewhere), with an
    explicit empty web_sources on each evidence_calls entry -- an optional
    field the documented schema example omits but preflight's ported
    tool_provenance check expects present as a list, matching what a real
    accepted candidate's evidence_calls always carry in practice.
    """
    candidate = copy.deepcopy(semantic_candidate())
    for wrapper in candidate.get("evidence_calls", []):
        wrapper["call"].setdefault("web_sources", [])
    return candidate


class PreflightBaselineTests(unittest.TestCase):
    def test_valid_candidate_passes(self):
        result = preflight(json.dumps(_valid_candidate()))
        self.assertEqual(result, {"ok": True, "problems": []})

    def test_invalid_json_is_reported(self):
        result = preflight("{not valid json")
        self.assertFalse(result["ok"])
        self.assertEqual(result["problems"][0]["code"], "invalid_json")

    def test_non_object_top_level_is_reported(self):
        result = preflight(json.dumps([1, 2, 3]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["problems"][0]["code"], "invalid_json")


class PreflightBrokenVariantTests(unittest.TestCase):
    def test_market_sessions_observed_at_mismatch_is_reported(self):
        candidate = _valid_candidate()
        candidate["market_sessions"]["observed_at"] = (
            "2020-01-01T00:00:00Z"
        )
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertIn("market_sessions_observed_at_mismatch", codes)

    def test_duplicate_tool_call_id_with_conflicting_content_is_reported(
        self,
    ):
        candidate = _valid_candidate()
        first = candidate["research"][0]["tool_calls"][0]
        duplicate = copy.deepcopy(first)
        duplicate["tool_call_id"] = first["tool_call_id"]
        duplicate["result"] = {"different": "content"}
        candidate["research"][0]["tool_calls"].append(duplicate)
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertIn("semantic_tool_call_id_conflict", codes)

    def test_evidence_call_with_no_tool_is_reported(self):
        candidate = _valid_candidate()
        candidate["evidence_calls"][0]["call"]["tool"] = None
        result = preflight(json.dumps(candidate))
        self.assertFalse(result["ok"])
        codes = {p["code"].split(":")[0] for p in result["problems"]}
        self.assertIn("evidence_call_invalid", codes)


class PreflightMirrorsBuilderDerivationsTests(unittest.TestCase):
    """Preflight must never flag what build_semantic_candidate silently
    repairs -- otherwise a host that fixes every preflight problem would
    "fix" a field the builder was always going to derive correctly."""

    def test_missing_url_source_ref_is_not_flagged(self):
        """The builder adds a url source_ref for every web_sources row
        missing one; preflight's one-directional url_refs-subset-of-urls
        check already tolerates this (a web_sources row with no matching
        source_ref is never itself an error)."""
        candidate = _valid_candidate()
        call = candidate["research"][0]["tool_calls"][0]
        call["web_sources"] = [{
            "url": "https://example.com/article",
            "title": "Example article",
            "published_at": "2026-09-17T00:00:00Z",
            "retrieved_at": "2026-09-17T16:01:00Z",
        }]
        # Deliberately do NOT add a matching url source_ref: this is the
        # exact shape build_semantic_candidate repairs.
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertNotIn("web_sources_url_refs_mismatch", codes)

    def test_bare_date_published_at_is_not_flagged(self):
        """A bare YYYY-MM-DD published_at is anchored at midnight UTC by
        the builder; preflight's own _normalized_published_at_ok already
        accepts the bare-date shorthand directly."""
        candidate = _valid_candidate()
        call = candidate["research"][0]["tool_calls"][0]
        call["source_refs"].append({
            "kind": "url", "value": "https://example.com/article",
        })
        call["web_sources"] = [{
            "url": "https://example.com/article",
            "title": "Example article",
            "published_at": "2026-09-17",
            "retrieved_at": "2026-09-17T16:01:00Z",
        }]
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertNotIn("web_source_0_published_at", codes)

    def test_decision_repetition_bare_id_prefix_is_not_flagged(self):
        """A bare id matching a current finding (missing the finding:
        prefix) has its prefix restored by the builder; preflight's own
        bare_ids fallback already tolerates it directly."""
        candidate = _valid_candidate()
        candidate["findings"] = [
            {"id": "finding-alpha", "statement": "Fresh evidence for X."},
        ]
        candidate["decision"]["repetition_review"] = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "new_evidence",
            "evidence_delta": ["finding-alpha"],
            "unresolved_question_ids": [],
            "rationale": "Reviewed against a fresh finding.",
        }
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertNotIn("decision_repetition_evidence_ref_invalid:0", codes)

    def test_decision_repetition_prose_with_current_findings_is_not_flagged(
        self,
    ):
        """Prose that cannot be mapped to one specific finding is replaced
        by the builder with every current finding:<id> ref, as long as at
        least one current finding exists; preflight's own guard already
        tolerates this exact shape."""
        candidate = _valid_candidate()
        candidate["findings"] = [
            {"id": "finding-alpha", "statement": "Fresh evidence for X."},
        ]
        candidate["decision"]["repetition_review"] = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "new_evidence",
            "evidence_delta": [
                "Fresh IBKR reads show updated option pricing.",
            ],
            "unresolved_question_ids": [],
            "rationale": "Reviewed with fresh evidence.",
        }
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertNotIn("decision_repetition_evidence_ref_invalid:0", codes)

    def test_decision_repetition_prose_with_no_findings_still_flagged(self):
        """Negative/guard: with no current findings at all, the claim has
        nothing honest to cite, so this must still be an error -- proving
        the tolerance above is narrow, not a blanket skip."""
        candidate = _valid_candidate()
        candidate["findings"] = []
        candidate["decision"]["repetition_review"] = {
            "prior_cycle_id": "cycle-prior",
            "disposition": "new_evidence",
            "evidence_delta": ["Some prose with no matching finding."],
            "unresolved_question_ids": [],
            "rationale": "Reviewed but nothing new to cite.",
        }
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertIn("decision_repetition_evidence_ref_invalid:0", codes)

    def test_position_symbol_from_contract_description_is_not_flagged(self):
        """A held STK position with no symbol/contract_id_ex/conid string
        identifier but a contract_description equal to the ticker is
        resolved by the builder's symbol derivation; preflight's own
        _portfolio_risk_references already grants the same equivalence
        directly. Synthetic ticker only, not a real portfolio holding."""
        candidate = _valid_candidate()
        candidate["snapshot"]["positions"] = [{
            "contract_id": 504546674,
            "contract_description": "ZTST",
            "asset_class": "STK",
            "position": 1800,
        }]
        candidate["research_agenda"]["candidates"][1]["portfolio_risk_ref"] = (
            "position:ZTST"
        )
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertFalse(
            any(c.endswith("portfolio_risk_ref_unresolved") for c in codes)
        )

    def test_position_symbol_not_held_is_still_flagged(self):
        """Negative/guard: a portfolio_risk_ref naming a symbol the account
        genuinely does not hold cannot be resolved by any derivation, so it
        stays an error. Synthetic ticker only."""
        candidate = _valid_candidate()
        candidate["snapshot"]["positions"] = [{
            "contract_id": 504546674,
            "contract_description": "ZTST",
            "asset_class": "STK",
            "position": 1800,
        }]
        candidate["research_agenda"]["candidates"][1]["portfolio_risk_ref"] = (
            "position:QQZZ"
        )
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertTrue(
            any(c.endswith("portfolio_risk_ref_unresolved") for c in codes)
        )

    def test_stale_specialist_stage_output_key_is_repaired_not_flagged(self):
        """The widened stale-specialist repair: the agenda candidate was
        renamed, the research row's specialist_stage_id is a THIRD,
        dangling id, and stage_outputs itself was never renamed. Since
        exactly one stage_outputs key is not a core stage id, preflight's
        own copy of the repair renames it before probing, exactly like
        build_semantic_candidate, so semantic_selected_specialist_mismatch
        never fires."""
        candidate = _valid_candidate()
        for c in candidate["research_agenda"]["candidates"]:
            if c["selected"] is True:
                c["candidate_id"] = "macro_specialist_v2r111"
        candidate["research"][0]["specialist_stage_id"] = (
            "macro_specialist_v2r107"
        )
        # Negative pre-check: the fixture really is stale before repair.
        self.assertNotIn(
            "macro_specialist_v2r111", candidate["stage_outputs"],
        )
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertNotIn("semantic_selected_specialist_mismatch", codes)

    def test_two_non_core_stage_output_keys_still_flagged(self):
        """Negative/guard: genuinely ambiguous (two non-core stage_outputs
        keys) so the widened repair declines and the mismatch still
        fires."""
        candidate = _valid_candidate()
        candidate["stage_outputs"]["macro_specialist_other"] = copy.deepcopy(
            candidate["stage_outputs"]["macro_specialist"]
        )
        for c in candidate["research_agenda"]["candidates"]:
            if c["selected"] is True:
                c["candidate_id"] = "macro_specialist_v2r111"
        candidate["research"][0]["specialist_stage_id"] = (
            "macro_specialist_v2r107"
        )
        result = preflight(json.dumps(candidate))
        codes = {p["code"] for p in result["problems"]}
        self.assertIn("semantic_selected_specialist_mismatch", codes)


class PreflightIsStdlibOnlyAndSmall(unittest.TestCase):
    def test_under_42kb(self):
        # 42KB ceiling: mirrors build_semantic_candidate's widened
        # stale-specialist stage-output-key repair (preflight's own copy of
        # _normalize_stale_specialist_stage_ids/_rewrite_stage_id_refs)
        # so preflight never flags what the builder now silently repairs.
        size = PREFLIGHT_PATH.stat().st_size
        self.assertLess(size, 42 * 1024, msg=f"{size} bytes")

    def test_imports_nothing_outside_stdlib(self):
        import ast

        tree = ast.parse(PREFLIGHT_PATH.read_text(encoding="utf-8"))
        stdlib_roots = set(sys.stdlib_module_names) | {"__future__"}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root not in stdlib_roots:
                        offenders.append(root)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.module is None:
                    continue
                root = (node.module or "").split(".", 1)[0]
                if root and root not in stdlib_roots:
                    offenders.append(root)
        self.assertEqual(offenders, [])

    def test_cli_entry_point_runs(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "cycle.semantic.json"
            candidate_path.write_text(
                json.dumps(_valid_candidate()), encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(PREFLIGHT_PATH), str(candidate_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload, {"ok": True, "problems": []})


if __name__ == "__main__":
    unittest.main()
