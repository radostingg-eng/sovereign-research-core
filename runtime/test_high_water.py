"""A prefix of a valid chain is a valid chain, which is the hole."""

import json
import tempfile
import unittest
from pathlib import Path

from .audit_store import AuditJournal
from .high_water import (
    MARK_FILENAME,
    check_high_water,
    mark_path,
    observed_state,
    read_mark,
    update_mark,
)


class TruncationIsNotVisibleToTheChainTests(unittest.TestCase):
    """Rewriting a record is caught because hashes link forward. Dropping the
    last N is not, at any depth, because what remains verifies perfectly.

    The newest records are the receipts saying what the system just decided,
    so the tail is both the most valuable thing to erase and the least likely
    deletion to be noticed.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sovereign-hw-"))
        self.journal = AuditJournal(self.dir / "a.jsonl")
        for index in range(6):
            self.journal.append(record_id=f"r{index}", record_type="system_change",
                                agent="t", payload={"n": index}, caused_by=())

    def truncate(self, count):
        path = self.dir / "a.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-count]) + "\n", encoding="utf-8")

    def test_no_mark_is_not_a_violation(self):
        """The state before the first mark exists is not tampering."""
        self.assertEqual(check_high_water(self.journal.read(), self.dir), [])

    def test_a_clean_journal_passes_against_its_own_mark(self):
        update_mark(self.journal.read(), self.dir)
        self.assertEqual(check_high_water(self.journal.read(), self.dir), [])

    def test_dropping_the_last_record_is_caught(self):
        update_mark(self.journal.read(), self.dir)
        self.truncate(1)
        problems = check_high_water(self.journal.read(), self.dir)
        self.assertTrue([p for p in problems if p.startswith("journal_truncated")])

    def test_dropping_several_records_is_caught(self):
        update_mark(self.journal.read(), self.dir)
        self.truncate(4)
        self.assertTrue([p for p in check_high_water(self.journal.read(), self.dir)
                         if p.startswith("journal_truncated")])

    def test_growth_is_not_flagged(self):
        """A journal is supposed to grow. Only shrinking is the signal."""
        update_mark(self.journal.read(), self.dir)
        self.journal.append(record_id="r6", record_type="system_change",
                            agent="t", payload={"n": 6}, caused_by=())
        self.assertEqual(check_high_water(self.journal.read(), self.dir), [])

    def test_a_tail_replaced_with_padding_is_caught_by_the_tip(self):
        """Truncate, then append filler back to the original length.

        The count check alone would pass; the recorded tip hash is gone.
        """
        update_mark(self.journal.read(), self.dir)
        original_tip = read_mark(self.dir)["tip_hash"]
        self.truncate(1)
        self.journal.append(record_id="filler", record_type="system_change",
                            agent="t", payload={"n": 99}, caused_by=())
        problems = check_high_water(self.journal.read(), self.dir)
        self.assertEqual(len(self.journal.read()), 6)
        self.assertTrue([p for p in problems if p.startswith("high_water_tip_missing")])
        self.assertIn(original_tip[:16], " ".join(problems))


class TheMarkOnlyEverGrowsTests(unittest.TestCase):
    """Otherwise a truncation is laundered by running the updater afterwards."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sovereign-hw2-"))
        self.journal = AuditJournal(self.dir / "a.jsonl")
        for index in range(4):
            self.journal.append(record_id=f"r{index}", record_type="system_change",
                                agent="t", payload={"n": index}, caused_by=())

    def test_updating_after_a_truncation_does_not_lower_the_mark(self):
        update_mark(self.journal.read(), self.dir)
        path = self.dir / "a.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")
        update_mark(self.journal.read(), self.dir)
        self.assertEqual(read_mark(self.dir)["records"], 4)
        self.assertTrue(check_high_water(self.journal.read(), self.dir))

    def test_a_genuine_append_advances_it(self):
        update_mark(self.journal.read(), self.dir)
        self.journal.append(record_id="r4", record_type="system_change",
                            agent="t", payload={"n": 4}, caused_by=())
        update_mark(self.journal.read(), self.dir)
        self.assertEqual(read_mark(self.dir)["records"], 5)

    def test_a_corrupt_mark_is_refused_not_ignored(self):
        """Otherwise corrupting the mark is the way around the check."""
        update_mark(self.journal.read(), self.dir)
        mark_path(self.dir).write_text("{not json", encoding="utf-8")
        self.assertEqual(check_high_water(self.journal.read(), self.dir),
                         ["high_water_mark_unreadable"])

    def test_a_mark_without_a_count_is_refused(self):
        mark_path(self.dir).write_text(json.dumps({"tip_hash": "x"}),
                                       encoding="utf-8")
        self.assertEqual(check_high_water(self.journal.read(), self.dir),
                         ["high_water_mark_malformed"])

    def test_the_observed_state_reports_the_real_tip(self):
        records = self.journal.read()
        self.assertEqual(observed_state(records)["records"], 4)
        self.assertEqual(observed_state(records)["tip_hash"],
                         records[-1]["record_hash"])

    def test_an_empty_journal_has_no_tip(self):
        self.assertEqual(observed_state([]), {"records": 0, "tip_hash": ""})


class TheGateRunsInProductionTests(unittest.TestCase):
    """A check nothing calls is not a check."""

    def test_integrity_runs_the_truncation_check(self):
        from . import integrity
        self.assertTrue(hasattr(integrity, "check_journal_not_truncated"))
        source = Path(integrity.__file__).read_text(encoding="utf-8")
        self.assertIn("failures += check_journal_not_truncated(records)", source)

    def test_the_executor_advances_the_mark(self):
        source = (Path(__file__).with_name("run_host_cycle.py")
                  .read_text(encoding="utf-8"))
        self.assertIn("update_mark(", source)

    def test_the_mark_filename_is_inside_the_audit_directory(self):
        self.assertEqual(mark_path("audit").name, MARK_FILENAME)
        self.assertEqual(mark_path("audit").parent.name, "audit")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DeclaredSchemaVersionsAreCheckedTests(unittest.TestCase):
    """Three state files declared a schema_version that nothing read.

    They are not meant to equal each other -- three different schemas with
    independent histories -- so the SELF_INTEGRITY bullet cannot mean
    cross-file equality. What it can mean is that the declared version agrees
    with what the runtime expects, so a bump fails the gate until someone
    confirms the readers were updated too.
    """

    def test_the_repository_matches_its_own_expectations(self):
        from .integrity import check_schema_versions
        self.assertEqual(check_schema_versions(), [])

    def test_a_bumped_version_is_caught(self):
        from . import integrity
        original = dict(integrity.EXPECTED_SCHEMA_VERSIONS)
        try:
            integrity.EXPECTED_SCHEMA_VERSIONS["STATE.json"] = original[
                "STATE.json"] + 1
            failures = integrity.check_schema_versions()
            self.assertTrue([f for f in failures if "STATE.json" in f.key])
        finally:
            integrity.EXPECTED_SCHEMA_VERSIONS.clear()
            integrity.EXPECTED_SCHEMA_VERSIONS.update(original)

    def test_a_missing_pinned_file_is_caught(self):
        from . import integrity
        original = dict(integrity.EXPECTED_SCHEMA_VERSIONS)
        try:
            integrity.EXPECTED_SCHEMA_VERSIONS["NOT_A_REAL_FILE.json"] = 1
            failures = integrity.check_schema_versions()
            self.assertTrue([f for f in failures if "missing_file" in f.key])
        finally:
            integrity.EXPECTED_SCHEMA_VERSIONS.clear()
            integrity.EXPECTED_SCHEMA_VERSIONS.update(original)

    def test_the_gate_runs_in_production(self):
        source = (Path(__file__).with_name("integrity.py")
                  .read_text(encoding="utf-8"))
        self.assertIn("failures += check_schema_versions()", source)


class TheMarkCannotLaunderTheTamperItFoundTests(unittest.TestCase):
    """update_mark ran BEFORE the integrity gate.

    A tail that was replaced and padded back to the original length was
    detected by the tip check, then overwritten by the updater, then reported
    clean. The detector was erasing the warning it had just raised, and the
    same path overwrote a corrupted mark. The current mark must be SATISFIED
    before it may be replaced.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sovereign-launder-"))
        self.journal = AuditJournal(self.dir / "a.jsonl")
        for index in range(5):
            self.journal.append(record_id=f"r{index}", record_type="system_change",
                                agent="t", payload={"n": index}, caused_by=())
        update_mark(self.journal.read(), self.dir)

    def replace_the_tail(self):
        path = self.dir / "a.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        self.journal.append(record_id="forged", record_type="system_change",
                            agent="t", payload={"n": 99}, caused_by=())

    def test_updating_does_not_clear_a_detected_tamper(self):
        self.replace_the_tail()
        self.assertTrue(check_high_water(self.journal.read(), self.dir))
        update_mark(self.journal.read(), self.dir)
        self.assertTrue(check_high_water(self.journal.read(), self.dir),
                        "update_mark erased the warning it had just raised")

    def test_updating_does_not_overwrite_a_corrupt_mark(self):
        mark_path(self.dir).write_text("{not json", encoding="utf-8")
        update_mark(self.journal.read(), self.dir)
        self.assertEqual(check_high_water(self.journal.read(), self.dir),
                         ["high_water_mark_unreadable"])

    def test_an_honest_append_still_advances_it(self):
        self.journal.append(record_id="r5", record_type="system_change",
                            agent="t", payload={"n": 5}, caused_by=())
        update_mark(self.journal.read(), self.dir)
        self.assertEqual(read_mark(self.dir)["records"], 6)


class AnIdlePassLeavesTheMarkAloneTests(unittest.TestCase):
    """Every unchanged pass rewrote updated_at.

    That dirties the tree, so the scheduler commits and pushes a one-line
    change to a timestamp each time it runs and finds nothing to do. It is
    the same bug that was just fixed in the feedback file, reintroduced here.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sovereign-idle-"))
        self.journal = AuditJournal(self.dir / "a.jsonl")
        self.journal.append(record_id="r0", record_type="system_change",
                            agent="t", payload={}, caused_by=())
        update_mark(self.journal.read(), self.dir)

    def test_an_unchanged_journal_does_not_rewrite_the_file(self):
        before = mark_path(self.dir).read_text(encoding="utf-8")
        mtime = mark_path(self.dir).stat().st_mtime_ns
        update_mark(self.journal.read(), self.dir)
        self.assertEqual(mark_path(self.dir).read_text(encoding="utf-8"), before)
        self.assertEqual(mark_path(self.dir).stat().st_mtime_ns, mtime)

    def test_growth_still_writes(self):
        before = mark_path(self.dir).read_text(encoding="utf-8")
        self.journal.append(record_id="r1", record_type="system_change",
                            agent="t", payload={}, caused_by=())
        update_mark(self.journal.read(), self.dir)
        self.assertNotEqual(mark_path(self.dir).read_text(encoding="utf-8"), before)
