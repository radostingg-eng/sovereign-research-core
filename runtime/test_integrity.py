import json
import time
import unittest
from io import StringIO
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import hash_record, make_record, verify_chain
from .integrity import (Failure, _cli_entry_points, _imported_modules,
                        apply_baseline, check_causal_integrity, check_hash_format,
                        check_record_validity, load_baseline,
                        check_referenced_paths, check_runtime_adoption,
                        check_state_integrity, load_journal_records, main,
                        order_chain, run_all, superseded_defects,
                        supersession_errors)
from .tool_provenance import persisted_tool_provenance_errors
from .tool_artifacts import build_artifact_specs


def chain(*specs):
    """Build a correctly-linked chain of records."""
    records, prior = [], None
    for i, (rid, rtype, caused_by) in enumerate(specs):
        record = make_record(rid, rtype, "test", {"i": i}, caused_by, prior,
                             created_at=f"2026-01-01T0{i}:00:00Z")
        prior = record["record_hash"]
        records.append(record)
    return records


class OrderChainTests(unittest.TestCase):
    def test_order_is_rebuilt_from_prev_hash_not_input_order(self):
        # The real journal is split across files whose names sort in the
        # WRONG order: "2026-09-15-orchestrator-1205.jsonl" sorts before
        # "2026-09-15.jsonl" because "-" < ".". If ordering ever regresses
        # to filename or input order, this fails.
        records = chain(("a", "finding", []), ("b", "decision", ["a"]), ("c", "outcome", ["b"]))
        shuffled = [records[2], records[0], records[1]]
        ordered, failures = order_chain(shuffled)
        self.assertEqual([r["record_id"] for r in ordered], ["a", "b", "c"])
        self.assertEqual(failures, [])

    def test_split_across_files_is_one_chain_not_three(self):
        records = chain(("a", "finding", []), ("b", "decision", ["a"]),
                        ("c", "outcome", ["b"]), ("d", "learning", ["c"]))
        # Simulate per-file validation being wrong: halves recombined.
        ordered, failures = order_chain(records[2:] + records[:2])
        self.assertEqual([r["record_id"] for r in ordered], ["a", "b", "c", "d"])
        self.assertEqual(check_causal_integrity(records[2:] + records[:2]), [])

    def test_orphan_record_is_reported_not_silently_dropped(self):
        records = chain(("a", "finding", []), ("b", "decision", ["a"]))
        # A well-formed hash that matches no record. "deadbeef" used to work
        # here; make_record now refuses malformed parents at the write site,
        # which makes this the truer orphan case anyway: correct shape,
        # nonexistent target.
        orphan = make_record("orphan", "finding", "test", {}, [], "f" * 64,
                             created_at="2026-01-01T09:00:00Z")
        ordered, failures = order_chain(records + [orphan])
        self.assertEqual(len(ordered), 2)
        self.assertEqual([f.key for f in failures], ["orphan"])
        self.assertIn("unreachable", failures[0].detail)

    def test_fork_is_reported(self):
        records = chain(("a", "finding", []), ("b", "decision", ["a"]))
        sibling = make_record("b2", "decision", "test", {}, ["a"],
                              records[0]["record_hash"], created_at="2026-01-01T05:00:00Z")
        _, failures = order_chain(records + [sibling])
        self.assertTrue(any("fork" in f.detail for f in failures), failures)

    def test_missing_root_is_reported(self):
        records = chain(("a", "finding", []), ("b", "decision", ["a"]))
        _, failures = order_chain(records[1:])
        self.assertTrue(any("no chain root" in f.detail for f in failures), failures)


class HashFormatTests(unittest.TestCase):
    """Regressions for issue #3: writer truncated prev_hash by one char.

    The failure surfaced downstream as six orphans, four hops from the
    culprit. These tests pin the check that names the root cause the
    moment it happens.
    """

    _GOOD = "a" * 64

    def _rec(self, **overrides):
        base = {
            "record_id": "r",
            "record_type": "finding",
            "created_at": "2026-01-01T00:00:00Z",
            "agent": "test",
            "payload": {},
            "caused_by": [],
            "prev_hash": None,
            "record_hash": self._GOOD,
        }
        base.update(overrides)
        return base

    def test_clean_records_produce_no_failures(self):
        r1 = self._rec(record_id="a")
        r2 = self._rec(record_id="b", prev_hash=self._GOOD, record_hash="b" * 64)
        self.assertEqual(check_hash_format([r1, r2]), [])

    def test_prev_hash_truncated_by_one_char_is_caught(self):
        # The exact bug in issue #3.
        r = self._rec(prev_hash="a" * 63)
        failures = check_hash_format([r])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].check, "hash_format")
        self.assertIn("len=63", failures[0].detail)

    def test_uppercase_hex_is_refused(self):
        # The chain format is documented as lowercase hex. Accepting mixed
        # case would let two writers disagree on a hash's identity.
        self.assertEqual(
            [f.check for f in check_hash_format([self._rec(record_hash="A" * 64)])],
            ["hash_format"],
        )

    def test_non_hex_characters_are_refused(self):
        self.assertEqual(
            [f.check for f in check_hash_format([self._rec(record_hash="z" * 64)])],
            ["hash_format"],
        )

    def test_wrong_type_is_refused(self):
        self.assertEqual(
            [f.check for f in check_hash_format([self._rec(prev_hash=12345)])],
            ["hash_format"],
        )

    def test_null_prev_hash_is_allowed_for_root(self):
        self.assertEqual(check_hash_format([self._rec(prev_hash=None)]), [])

    def test_null_record_hash_is_never_allowed(self):
        self.assertEqual(
            [f.check for f in check_hash_format([self._rec(record_hash=None)])],
            ["hash_format"],
        )


class RecordValidityTests(unittest.TestCase):
    """Regressions for the off-chain validation hole.

    verify_chain only inspects records on the selected chain path. Two of
    this journal's three content-tamper records were ALSO orphaned, so the
    gate could not see them -- seven records went content-unvalidated
    entirely. Reachability is a graph property; "these bytes hash to this
    value" is not, and must never be conditional on it.
    """

    def test_clean_records_produce_no_failures(self):
        self.assertEqual(check_record_validity(chain(("a", "finding", []),
                                                     ("b", "decision", ["a"]))), [])

    def test_tampered_record_is_caught_even_when_orphaned(self):
        # The load-bearing case: BOTH tampered and unreachable.
        records = chain(("a", "finding", []))
        orphan = make_record("orphan", "finding", "t", {"v": 1}, [], "f" * 64,
                             created_at="2026-01-01T09:00:00Z")
        orphan["payload"]["v"] = 999  # edited after hashing
        failures = check_record_validity(records + [orphan])
        self.assertEqual([(f.check, f.defect, f.key) for f in failures],
                         [("record", "content_mismatch", "orphan")])

    def test_duplicate_record_id_is_caught(self):
        records = chain(("a", "finding", []))
        failures = check_record_validity(records + [dict(records[0])])
        self.assertIn("duplicate_id", [f.defect for f in failures])

    def test_duplicate_hash_across_distinct_records_is_caught(self):
        # Two records sharing a record_hash make every prev_hash reference
        # to that value ambiguous.
        a = make_record("a", "finding", "t", {}, [], None,
                        created_at="2026-01-01T00:00:00Z")
        b = make_record("b", "finding", "t", {}, [], None,
                        created_at="2026-01-01T01:00:00Z")
        b["record_hash"] = a["record_hash"]
        self.assertIn("duplicate_hash",
                      [f.defect for f in check_record_validity([a, b])])

    def test_the_real_journal_has_off_chain_records_that_are_still_validated(self):
        # Guards the wiring: validation must cover all parsed records, not
        # the ordered subset. Uses the archived real journal precisely
        # because that is where the hole was found.
        records = load_journal_records(
            Path(__file__).resolve().parent.parent / "audit_archive")
        ordered, _ = order_chain(records)
        self.assertLess(len(ordered), len(records),
                        "fixture assumption: this journal has off-chain records")
        on_path = {id(r) for r in ordered}
        off_path_tampers = {r["record_id"] for r in records
                            if id(r) not in on_path
                            and r.get("record_hash") != hash_record(r)}
        self.assertTrue(off_path_tampers,
                        "fixture assumption: an off-path tamper exists")
        reported = {f.key for f in check_record_validity(records)}
        self.assertTrue(
            off_path_tampers <= reported,
            f"off-chain tampers invisible to the gate: {off_path_tampers - reported}")


class DefectTypedBaselineTests(unittest.TestCase):
    """The baseline key must carry a defect type.

    Without it one line covers every possible defect on a record. Repair
    an orphan on a record that is ALSO content-tampered and the same line
    keeps reproducing: the debt looks paid and is not.
    """

    def test_ident_includes_the_defect_type(self):
        self.assertEqual(
            Failure("chain", "rec-1", "d", defect="orphan").ident,
            "chain:orphan:rec-1")

    def test_two_defects_on_one_record_have_distinct_idents(self):
        self.assertNotEqual(
            Failure("chain", "rec-1", "d", defect="orphan").ident,
            Failure("record", "rec-1", "d", defect="content_mismatch").ident)

    def test_clearing_one_defect_does_not_grandfather_the_other(self):
        # The exact scenario the defect type exists to prevent.
        baseline = {"chain:orphan:rec-1": "known orphan"}
        after_repair = [Failure("record", "rec-1", "d", defect="content_mismatch")]
        new, stale = apply_baseline(after_repair, baseline)
        self.assertEqual([f.ident for f in new], ["record:content_mismatch:rec-1"])
        self.assertEqual(stale, ["chain:orphan:rec-1"])

    def test_every_live_baseline_key_is_defect_typed(self):
        # check:defect:subject. Record ids contain colons, so require at
        # least three segments rather than exactly three.
        for key in load_baseline():
            self.assertGreaterEqual(
                len(key.split(":")), 3,
                f"baseline key {key!r} predates defect-typed idents")


class CausalIntegrityTests(unittest.TestCase):
    def test_clean_chain_has_no_failures(self):
        self.assertEqual(check_causal_integrity(chain(("a", "finding", []),
                                                      ("b", "decision", ["a"]))), [])

    def test_altered_record_is_caught(self):
        records = chain(("a", "finding", []), ("b", "decision", ["a"]))
        records[1]["payload"]["i"] = 999  # edited after hashing
        failures = check_causal_integrity(records)
        self.assertTrue(any(f.check == "chain" and "record_hash" in f.detail for f in failures),
                        failures)

    def test_dangling_caused_by_is_caught(self):
        records = chain(("a", "finding", []), ("b", "decision", ["nope"]))
        failures = check_causal_integrity(records)
        self.assertTrue(any(f.check == "causal" for f in failures), failures)


class StateAndPathTests(unittest.TestCase):
    def test_missing_required_state_file_is_caught(self):
        with TemporaryDirectory() as tmp:
            failures = check_state_integrity(Path(tmp))
            self.assertTrue(any("missing" in f.detail for f in failures), failures)

    def test_broken_json_is_caught(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "STATE.json").write_text("{not json", encoding="utf-8")
            (root / "SOURCE_MANIFEST.json").write_text("{}", encoding="utf-8")
            failures = check_state_integrity(root)
            self.assertEqual([f.key for f in failures], ["STATE.json"])
            self.assertIn("invalid JSON", failures[0].detail)

    def test_referenced_path_that_does_not_exist_is_caught(self):
        # This is the check that catches an agent describing files it never
        # wrote, and the slow drift of a renamed file left stale in STATE.
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "investment-system"
            root.mkdir()
            (root / "STATE.json").write_text(
                json.dumps({"runtime": "runtime/ghost.py"}), encoding="utf-8")
            failures = check_referenced_paths(root)
            self.assertEqual([f.key for f in failures], ["runtime/ghost.py"])

    def test_referenced_jsonl_path_that_does_not_exist_is_caught(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "investment-system"
            root.mkdir()
            (root / "STATE.json").write_text(
                json.dumps({"audit": "audit/missing.jsonl"}), encoding="utf-8")
            failures = check_referenced_paths(root)
            self.assertEqual([f.key for f in failures], ["audit/missing.jsonl"])

    def test_referenced_path_that_exists_passes(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = repo / "investment-system"
            (root / "runtime").mkdir(parents=True)
            (root / "runtime" / "real.py").write_text("", encoding="utf-8")
            (root / "STATE.json").write_text(
                json.dumps({"runtime": "runtime/real.py"}), encoding="utf-8")
            self.assertEqual(check_referenced_paths(root), [])


class AdoptionTests(unittest.TestCase):
    def _pkg(self, tmp, files):
        root = Path(tmp)
        for name, body in files.items():
            (root / name).write_text(body, encoding="utf-8")
        return root

    def test_module_with_no_caller_is_caught(self):
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {
                "__init__.py": "from .used import *\n",
                "used.py": "X = 1\n",
                "orphaned.py": "Y = 2\n",
                "test_orphaned.py": "from .orphaned import Y\n",
            })
            failures = check_runtime_adoption(root, search_roots=[])
            self.assertEqual([f.key for f in failures], ["orphaned", "used"])

    def test_reexport_from_init_is_not_adoption(self):
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {"__init__.py": "from .used import *\n", "used.py": "X = 1\n"})
            self.assertEqual([f.key for f in
                              check_runtime_adoption(root, search_roots=[])],
                             ["used"])

    def test_a_dead_import_cluster_is_still_dead(self):
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {
                "__init__.py": "from .caller import *\n",
                "caller.py": "from .helper import H\n",
                "helper.py": "H = 1\n",
            })
            self.assertEqual(
                [f.key for f in check_runtime_adoption(root, search_roots=[])],
                ["caller", "helper"])

    def test_sibling_import_counts_when_the_importer_is_a_root(self):
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {
                "__init__.py": "",
                "caller.py": "from .helper import H\n",
                "helper.py": "H = 1\n",
            })
            workflow = Path(tmp) / "callers"
            workflow.mkdir()
            (workflow / "ci.yml").write_text(
                "run: python -m runtime.caller\n", encoding="utf-8")
            self.assertEqual(
                check_runtime_adoption(root, search_roots=[workflow]), [])

    def test_mention_in_a_docstring_is_not_adoption(self):
        # The naive `grep -l <name>` version of this check passes here,
        # which is exactly how three dead modules stayed invisible.
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {
                "__init__.py": "from .caller import *\n",
                "caller.py": '"""Does capital-allocation things."""\n',
                "allocation.py": "A = 1\n",
            })
            self.assertEqual(
                [f.key for f in check_runtime_adoption(
                    root, search_roots=[])],
                ["allocation", "caller"])


class AdoptionPrecisionTests(unittest.TestCase):
    """Regressions for the false-pass that hid this module from its own gate."""

    def _pkg(self, tmp, files):
        root = Path(tmp)
        for name, body in files.items():
            (root / name).write_text(body, encoding="utf-8")
        return root

    def test_module_name_ending_a_sentence_is_not_adoption(self):
        # The exact bug: governance.py line 1 ends '...and integrity."""',
        # and a r"\bintegrity\." pattern matched it, so integrity.py was
        # reported adopted while nothing imported it.
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {
                "__init__.py": "from .governance import *\n",
                "governance.py": '"""Primitives for goals and integrity."""\n',
                "integrity.py": "X = 1\n",
            })
            failures = check_runtime_adoption(root, search_roots=[])
            self.assertEqual(
                [f.key for f in failures], ["governance", "integrity"])

    def test_sibling_function_sharing_a_prefix_is_not_adoption(self):
        # portfolio.py defines stress_nav/correlated_stress; that must not
        # make the separate stress.py module look reached.
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {
                "__init__.py": "from .portfolio import *\n",
                "portfolio.py": "def stress_nav(x):\n    return x\n",
                "stress.py": "def worst_case(x):\n    return x\n",
            })
            self.assertEqual(
                [f.key for f in check_runtime_adoption(
                    root, search_roots=[])],
                ["portfolio", "stress"])

    def test_cli_invocation_counts_as_adoption(self):
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {"__init__.py": "", "cli.py": "X = 1\n"})
            caller = Path(tmp) / "callers"
            caller.mkdir()
            (caller / "ci.yml").write_text("run: python -m runtime.cli\n", encoding="utf-8")
            self.assertEqual(check_runtime_adoption(root, search_roots=[caller]), [])

    def test_a_markdown_mention_is_not_an_entry_point(self):
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {"__init__.py": "", "cli.py": "X = 1\n"})
            caller = Path(tmp) / "callers"
            caller.mkdir()
            (caller / "README.md").write_text(
                "Run python -m runtime.cli when debugging.\n",
                encoding="utf-8")
            self.assertEqual(
                [f.key for f in check_runtime_adoption(
                    root, search_roots=[caller])],
                ["cli"])

    def test_main_block_alone_is_not_adoption(self):
        # Having a __main__ block is not evidence anyone runs it.
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {
                "__init__.py": "",
                "cli.py": "if __name__ == '__main__':\n    pass\n",
            })
            self.assertEqual([f.key for f in check_runtime_adoption(root, search_roots=[])],
                             ["cli"])

    def test_default_search_roots_do_not_walk_an_unbounded_tree(self):
        # Deriving search roots from the runtime_dir argument made this
        # rglob the whole system temp directory and hang.
        with TemporaryDirectory() as tmp:
            root = self._pkg(tmp, {"__init__.py": "", "mod.py": "X = 1\n"})
            start = time.monotonic()
            check_runtime_adoption(root)
            self.assertLess(time.monotonic() - start, 10.0)

    def test_cli_entry_points_finds_the_documented_invocations(self):
        # Guards the CLI-adoption path itself against silent regressions.
        # cycle.py used to be listed here; it was removed when ChatGPT
        # replaced GitHub Actions as the scheduler and the local CLI became
        # redundant. When another module gets a documented `python -m`
        # invocation, add it here.
        rt = Path(__file__).resolve().parent
        invoked = _cli_entry_points(rt.parent)
        self.assertIn("integrity", invoked)

    def test_the_real_package_has_no_adoption_debt(self):
        failures = check_runtime_adoption()
        baseline = load_baseline()
        self.assertEqual(failures, [])
        self.assertEqual(
            [key for key in baseline
             if key.startswith("adoption:unreachable:")],
            [])


class HistoricalToolProvenanceReplayTests(unittest.TestCase):
    def test_real_persisted_indexes_match_their_immutable_source_fields(self):
        root = Path(__file__).resolve().parent.parent
        records = load_journal_records(root / "audit")
        checked = 0
        for record in records:
            if record.get("record_type") != "tool_provenance":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            path = root / "host_input" / f"{payload.get('cycle_id')}.json"
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            specs = build_artifact_specs(data, records=records)
            self.assertEqual(
                persisted_tool_provenance_errors(
                    data,
                    payload,
                    recorded_at=record.get("created_at"),
                    artifact_specs=specs,
                ),
                [],
                payload.get("cycle_id"),
            )
            checked += 1
        self.assertGreaterEqual(checked, 10)


class StrictModeTests(unittest.TestCase):
    def test_strict_refuses_what_default_mode_tolerates(self):
        # Stated as a relationship, not a fixed exit code: this stays
        # correct when the last baseline entry is finally cleared, instead
        # of failing on the day the debt is paid off.
        baseline = load_baseline()
        with patch("sys.stdout", StringIO()):
            self.assertEqual(main([]), 0)
            strict = main(["--strict"])
        if baseline:
            self.assertEqual(strict, 1,
                             "strict must refuse while entries are grandfathered")
        else:
            self.assertEqual(strict, 0,
                             "with an empty baseline, strict and default agree")


class BaselineTests(unittest.TestCase):
    def test_grandfathered_failure_is_suppressed(self):
        failures = [Failure("adoption", "opportunity", "no caller",
                            defect="unreachable")]
        new, stale = apply_baseline(
            failures, {"adoption:unreachable:opportunity": "known"})
        self.assertEqual(new, [])
        self.assertEqual(stale, [])

    def test_new_failure_is_not_suppressed(self):
        failures = [Failure("adoption", "brand_new", "no caller",
                            defect="unreachable")]
        new, stale = apply_baseline(
            failures, {"adoption:unreachable:opportunity": "known"})
        self.assertEqual([f.key for f in new], ["brand_new"])

    def test_baseline_entry_that_no_longer_reproduces_is_reported(self):
        # Shrink-only: the list must not keep granting permission for
        # something already fixed.
        new, stale = apply_baseline([], {"adoption:unreachable:opportunity": "known"})
        self.assertEqual(stale, ["adoption:unreachable:opportunity"])


class RealRepoTests(unittest.TestCase):
    def test_the_actual_journal_loads_and_the_gate_passes(self):
        records = load_journal_records()
        self.assertGreater(len(records), 0)
        with patch("sys.stdout", StringIO()):
            self.assertEqual(main([]), 0)

    def test_every_baseline_entry_still_reproduces(self):
        # If someone fixes a defect, this fails until they delete the line.
        with patch("sys.stdout", StringIO()):
            self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()


class OrphansAreKeyedByRootCauseTests(unittest.TestCase):
    """A shrink-only list that grows on every run is a contradiction.

    One malformed prev_hash orphans its whole descendant lineage, and each
    later cycle anchored onto that lineage added another victim entry. The
    baseline went 13 -> 23 -> 25 while the underlying damage stayed a single
    unrepaired write. Keying collateral by the record that BROKE makes the
    count track defects instead of casualties.
    """

    def record(self, record_id, prev_hash):
        row = {"record_id": record_id, "record_type": "note", "agent": "t",
               "payload": {"n": record_id}, "prev_hash": prev_hash}
        row["record_hash"] = hash_record(dict(row))
        return row

    def broken_chain(self, extra_descendants=0):
        """A root, a good child, then a record with an unresolvable parent."""
        root = self.record("root", None)
        good = self.record("good", root["record_hash"])
        broken = self.record("broken", "f" * 64)
        records = [root, good, broken]
        parent = broken
        for index in range(extra_descendants):
            child = self.record(f"child-{index}", parent["record_hash"])
            records.append(child)
            parent = child
        return records

    def idents(self, records):
        _, failures = order_chain(records)
        return sorted({f.ident for f in failures})

    def test_the_broken_record_is_named_as_the_orphan(self):
        self.assertIn("chain:orphan:broken", self.idents(self.broken_chain()))

    def test_descendants_do_not_each_get_their_own_ident(self):
        one = self.idents(self.broken_chain(extra_descendants=1))
        five = self.idents(self.broken_chain(extra_descendants=5))
        self.assertEqual(one, five)

    def test_descendants_are_attributed_to_the_record_that_broke(self):
        self.assertIn("chain:orphan_broken_link:broken",
                      self.idents(self.broken_chain(extra_descendants=2)))

    def test_the_reported_failure_list_does_not_grow_with_victims(self):
        """Operators read the failure list, not a deduplicated set of keys.

        Five stranded descendants of one break must not print five findings;
        that is the noise that made the real defect count unreadable.
        """
        _, one = order_chain(self.broken_chain(extra_descendants=1))
        _, five = order_chain(self.broken_chain(extra_descendants=5))
        self.assertEqual(len(one), len(five))

    def test_a_clean_chain_reports_nothing(self):
        root = self.record("root", None)
        child = self.record("child", root["record_hash"])
        self.assertEqual(self.idents([root, child]), [])

    def test_a_genuinely_new_break_still_produces_a_new_ident(self):
        """Stability must not mean blindness.

        Collapsing collateral is only safe if an independent break is still
        reported on its own.
        """
        records = self.broken_chain(extra_descendants=2)
        second = self.record("second-break", "e" * 64)
        before = self.idents(records)
        after = self.idents(records + [second])
        self.assertNotIn("chain:orphan:second-break", before)
        self.assertIn("chain:orphan:second-break", after)


class BaselineTracksRootCausesTests(unittest.TestCase):
    """The committed baseline must name defects, not casualties."""

    def test_the_two_malformed_writes_stay_documented_after_acknowledgement(self):
        """The 63/65 diagnosis must survive leaving the baseline.

        Both entries were retired by supersession, so the detail that made
        them fixable now lives in the supersession records. Losing it there
        would mean acknowledging the damage had erased what it was.
        """
        reasons = " ".join(
            str(r["payload"].get("reason", ""))
            for r in load_journal_records(
                Path(__file__).resolve().parent.parent / "audit_archive")
            if r.get("record_type") == "supersession"
        )
        self.assertIn("65 characters", reasons)
        self.assertIn("63 characters", reasons)

    def test_the_archive_manifest_cites_the_tracking_issue(self):
        import json

        path = (
            Path(__file__).resolve().parent.parent
            / "audit_archive" / "manifest.json"
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["tracking_issue"], "#3")
        self.assertTrue(manifest["known_defects"])


class AppendOnlySupersessionTests(unittest.TestCase):
    """Every baseline entry said to resolve damage by appending a
    superseding replacement, and there was no way to append one.

    A documented recovery path with no implementation meant the only route
    out of a defect was editing the baseline, which is how the list became
    permanent instead of shrinking.
    """

    def record(self, record_id, prev_hash, record_type="note", payload=None):
        row = {"record_id": record_id, "record_type": record_type, "agent": "t",
               "payload": payload if payload is not None else {"n": record_id},
               "prev_hash": prev_hash}
        row["record_hash"] = hash_record(dict(row))
        return row

    def damaged_journal(self):
        """A root, then a record whose stored hash no longer matches it."""
        root = self.record("root", None)
        broken = self.record("broken", root["record_hash"])
        broken["payload"] = {"n": "REWRITTEN"}
        return [root, broken]

    def supersession(self, records, target_id, defect, **over):
        target = next(r for r in records if r["record_id"] == target_id)
        payload = {"supersedes": target_id, "defect": defect,
                   "observed_hash": target["record_hash"],
                   "reason": "identity broken by the historical writer defect; "
                             "original preserved as evidence"}
        payload.update(over.pop("payload", {}))
        # Chain onto the journal TIP, not the root. Sharing a parent with an
        # existing record forks the chain, order_chain follows one branch, and
        # the supersession lands unreachable -- which correctly authorizes
        # nothing, but is not what this helper is trying to build.
        row = self.record(over.pop("record_id", "sup-1"),
                          over.pop("prev_hash", records[-1]["record_hash"]),
                          record_type="supersession", payload=payload)
        return row

    def idents(self, records):
        return sorted(f.ident for f in run_all(records))

    def test_the_damage_is_reported_without_a_supersession(self):
        self.assertIn("record:content_mismatch:broken", self.idents(self.damaged_journal()))

    def test_a_valid_supersession_retires_the_named_defect(self):
        records = self.damaged_journal()
        records.append(self.supersession(records, "broken", "content_mismatch"))
        self.assertNotIn("record:content_mismatch:broken", self.idents(records))

    def test_it_retires_only_the_defect_it_names(self):
        """Superseding one defect must not clear a different one."""
        records = self.damaged_journal()
        records.append(self.supersession(records, "broken", "some_other_defect"))
        self.assertIn("record:content_mismatch:broken", self.idents(records))

    def test_a_stale_observed_hash_authorizes_nothing(self):
        """Bound to the damaged state, not to the record's name.

        Quoting the observed hash is what stops a supersession being written
        ahead of time or retargeted after the record changes again.
        """
        records = self.damaged_journal()
        bad = self.supersession(records, "broken", "content_mismatch",
                                payload={"observed_hash": "0" * 64})
        records.append(bad)
        idents = self.idents(records)
        self.assertIn("record:content_mismatch:broken", idents)
        self.assertIn("supersession:invalid:sup-1", idents)

    def test_a_short_reason_authorizes_nothing(self):
        records = self.damaged_journal()
        records.append(self.supersession(records, "broken", "content_mismatch",
                                         payload={"reason": "fixed"}))
        self.assertIn("record:content_mismatch:broken", self.idents(records))

    def test_superseding_an_absent_record_is_reported(self):
        records = self.damaged_journal()
        records.append(self.supersession(records, "broken", "content_mismatch",
                                         payload={"supersedes": "ghost"}))
        self.assertIn("supersession:invalid:sup-1", self.idents(records))

    def test_an_unreachable_supersession_authorizes_nothing(self):
        """Retiring a defect requires writing into the part of the journal
        that still verifies, where the act is attributable."""
        records = self.damaged_journal()
        orphan = self.supersession(records, "broken", "content_mismatch",
                                   prev_hash="f" * 64)
        records.append(orphan)
        self.assertIn("record:content_mismatch:broken", self.idents(records))

    def test_the_original_record_is_never_removed(self):
        records = self.damaged_journal()
        records.append(self.supersession(records, "broken", "content_mismatch"))
        run_all(records)
        self.assertIn("broken", [r["record_id"] for r in records])

    def test_the_committed_supersessions_are_all_valid(self):
        """Applied on 2026-09-16 to the five broken-identity records.

        A supersession is an administrative claim about a hash, not a claim
        about anyone's reasoning, which is why this was mine to sign. Each
        one must still satisfy every constraint, or it authorizes nothing.
        """
        records = load_journal_records(
            Path(__file__).resolve().parent.parent / "audit_archive")
        by_id = {r.get("record_id"): r for r in records}
        supersessions = [r for r in records if r.get("record_type") == "supersession"]
        self.assertGreaterEqual(len(supersessions), 9)
        for record in supersessions:
            self.assertEqual(supersession_errors(record, by_id), [], record.get("record_id"))

    def test_acknowledgement_did_not_repair_the_chain(self):
        """Signed for is not fixed.

        The root defects were acknowledged because they blocked every future
        cycle, not because they were resolved. Their CONSEQUENCE -- sixteen
        records unreachable from the root -- stays an open finding, because a
        mechanism that quietly cleared the chain damage would be worse than
        the damage.
        """
        records = load_journal_records(
            Path(__file__).resolve().parent.parent / "audit_archive")
        ordered, _ = order_chain(records)
        # 15, not 16: walking both fork branches recovered one validly
        # linked record that the old traversal abandoned.
        self.assertEqual(len(records) - len(ordered), 15)
        open_idents = {f.ident for f in run_all(records)}
        self.assertTrue(any(i.startswith("chain:orphan_broken_link") for i in open_idents),
                        open_idents)

    def test_every_retired_defect_names_a_real_record(self):
        records = load_journal_records(
            Path(__file__).resolve().parent.parent / "audit_archive")
        ids = {r.get("record_id") for r in records}
        for record_id, _defect in superseded_defects(records):
            self.assertIn(record_id, ids)

    def test_the_original_damaged_records_are_still_present(self):
        records = load_journal_records(
            Path(__file__).resolve().parent.parent / "audit_archive")
        ids = {r.get("record_id") for r in records}
        for record_id in ("cycle-receipt:cycle-20260916-0726",
                          "cycle-receipt:cycle-20260916-0722-host",
                          "finding-orchestrator-run-20260915-1203"):
            self.assertIn(record_id, ids)


class ForkDoesNotStrandTheOtherBranchTests(unittest.TestCase):
    """Reporting a fork and then abandoning one branch hid its victims.

    order_chain followed successors[0], so every record on the branch it did
    not take was counted unreachable -- a validly linked record stranded by a
    defect in a sibling it had no relationship to.
    """

    def record(self, record_id, prev_hash):
        row = {"record_id": record_id, "record_type": "note", "agent": "t",
               "payload": {"n": record_id}, "prev_hash": prev_hash}
        row["record_hash"] = hash_record(dict(row))
        return row

    def forked(self):
        """root with TWO children, each carrying a descendant."""
        root = self.record("root", None)
        left = self.record("left", root["record_hash"])
        left_child = self.record("left-child", left["record_hash"])
        right = self.record("right", root["record_hash"])
        right_child = self.record("right-child", right["record_hash"])
        return [root, left, left_child, right, right_child]

    def test_every_record_on_both_branches_is_reachable(self):
        records = self.forked()
        ordered, _ = order_chain(records)
        self.assertEqual(len(ordered), len(records))

    def test_the_fork_is_still_reported(self):
        """Recovering the records must not hide the defect that stranded them."""
        _, failures = order_chain(self.forked())
        self.assertTrue(any(f.defect == "fork" for f in failures), [f.ident for f in failures])

    def test_a_linear_chain_keeps_its_order(self):
        root = self.record("root", None)
        second = self.record("second", root["record_hash"])
        third = self.record("third", second["record_hash"])
        ordered, failures = order_chain([root, second, third])
        self.assertEqual([r["record_id"] for r in ordered], ["root", "second", "third"])
        self.assertEqual(failures, [])

    def test_verify_chain_does_not_invent_a_mismatch_across_a_fork(self):
        """The record before a branch sibling is not its parent.

        Comparing against the sequence predecessor reports a mismatch that
        does not exist, which is why the traversal fix alone was not enough.
        """
        ordered, _ = order_chain(self.forked())
        self.assertEqual(verify_chain(ordered), [])

    def test_verify_chain_still_catches_a_real_break(self):
        records = self.forked()
        records.append(self.record("orphan", "f" * 64))
        ordered, _ = order_chain(records)
        broken = list(ordered) + [records[-1]]
        self.assertTrue(any("prev_hash mismatch" in e for e in verify_chain(broken)))
