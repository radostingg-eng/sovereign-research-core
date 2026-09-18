import tempfile
import json
import os
import unittest
from pathlib import Path

from .engine import verify_chain
from .audit_store import AuditJournal
from .test_cycle_receipt import sample_receipt


class AuditJournalTests(unittest.TestCase):
    def test_append_chain_and_links(self):
        with tempfile.TemporaryDirectory() as d:
            journal = AuditJournal(Path(d) / "audit.jsonl")
            journal.append(record_id="r1", record_type="recommendation", agent="research", payload={"x": 1})
            journal.append(record_id="r2", record_type="decision", agent="decision", payload={"x": 2}, caused_by=("r1",))
            report = journal.validate()
            self.assertTrue(report["valid"])
            self.assertEqual(report["records"], 2)

    def test_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            journal = AuditJournal(Path(d) / "audit.jsonl")
            journal.append(record_id="r1", record_type="x", agent="a", payload={})
            with self.assertRaises(ValueError):
                journal.append(record_id="r1", record_type="x", agent="a", payload={})

    def test_dangling_cause_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            journal = AuditJournal(Path(d) / "audit.jsonl")
            journal.append(record_id="r2", record_type="x", agent="a", payload={}, caused_by=("missing",))
            report = journal.validate()
            self.assertFalse(report["valid"])
            self.assertTrue(report["dangling_causes"])

    def test_cycle_receipt_is_validated_and_appended(self):
        with tempfile.TemporaryDirectory() as d:
            journal = AuditJournal(Path(d) / "audit.jsonl")
            receipt = sample_receipt()
            record = journal.append_cycle_receipt(receipt)
            self.assertEqual(record["record_type"], "cycle_receipt")
            self.assertEqual(record["record_id"], "cycle-receipt:cycle-20260916-0600")
            self.assertTrue(journal.validate()["valid"])

    def test_cycle_receipt_with_bad_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            journal = AuditJournal(Path(d) / "audit.jsonl")
            receipt = sample_receipt()
            receipt["receipt_hash"] = "tampered"
            with self.assertRaises(ValueError):
                journal.append_cycle_receipt(receipt)
            self.assertEqual(journal.read(), [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
class ConcurrentAppendTests(unittest.TestCase):
    """Two writers must not fork the chain.

    append was read-tip-then-write with nothing in between: both writers
    read the same last record, both computed the same prev_hash, and both
    appended. The journal is append-only, so the fork cannot be repaired
    by retrying.

    This forks REAL processes. An in-process test would pass against a
    completely broken lock, because a single thread never races itself --
    the same reason the engine's cron guard needed a real child process.
    Negative control run while building this: with the flock call removed,
    four writers produced 27 duplicate prev_hash values and 43 chain
    defects. With it, zero and zero.
    """

    WRITERS = 4
    PER_WRITER = 12

    def _run_writers(self, journal_path):
        children = []
        for writer in range(self.WRITERS):
            pid = os.fork()
            if pid == 0:
                try:
                    for n in range(self.PER_WRITER):
                        AuditJournal(journal_path).append(
                            record_id=f"w{writer}-{n}", record_type="finding",
                            agent="test", payload={"writer": writer, "n": n})
                except Exception:
                    os._exit(1)
                os._exit(0)
            children.append(pid)
        return sum(0 if os.waitpid(pid, 0)[1] == 0 else 1 for pid in children)

    def test_concurrent_writers_do_not_fork_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            AuditJournal(path).append(record_id="root", record_type="finding",
                                      agent="test", payload={})
            failures = self._run_writers(path)
            self.assertEqual(failures, 0, "a writer process failed")

            records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            self.assertEqual(len(records), 1 + self.WRITERS * self.PER_WRITER,
                             "a record was lost or duplicated")

            parents = [r["prev_hash"] for r in records if r["prev_hash"]]
            self.assertEqual(len(parents), len(set(parents)),
                             "two records share a parent: the chain forked")
            self.assertEqual(verify_chain(records), [],
                             "chain does not verify after concurrent appends")

    def test_concurrent_writers_do_not_duplicate_record_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            AuditJournal(path).append(record_id="root", record_type="finding",
                                      agent="test", payload={})
            self._run_writers(path)
            records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            ids = [r["record_id"] for r in records]
            self.assertEqual(len(ids), len(set(ids)))

    def test_a_duplicate_id_is_still_refused_under_the_lock(self):
        # The duplicate check moved inside the critical section; make sure
        # it still refuses rather than being bypassed by the lock rewrite.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            journal = AuditJournal(path)
            journal.append(record_id="only-once", record_type="finding",
                           agent="test", payload={})
            with self.assertRaises(ValueError) as ctx:
                journal.append(record_id="only-once", record_type="finding",
                               agent="test", payload={})
            self.assertIn("duplicate_record_id", str(ctx.exception))

    def test_the_lock_file_does_not_pollute_the_journal_directory_scan(self):
        # The lock lives next to the journal; a .lock file must not be
        # mistaken for journal content by anything globbing *.jsonl.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            AuditJournal(path).append(record_id="r", record_type="finding",
                                      agent="test", payload={})
            self.assertEqual(sorted(p.name for p in Path(tmp).glob("*.jsonl")),
                             ["journal.jsonl"])
