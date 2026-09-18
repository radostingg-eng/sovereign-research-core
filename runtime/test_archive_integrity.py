import shutil
import tempfile
import unittest
from pathlib import Path

from .archive_integrity import ARCHIVE_DIR, verify_archive


class TheCommittedArchiveIsPinnedTests(unittest.TestCase):
    def test_the_expected_damaged_corpus_is_intact(self):
        self.assertEqual(verify_archive(), [])

    def copied_archive(self):
        root = Path(tempfile.mkdtemp(prefix="archive-integrity-"))
        target = root / "audit_archive"
        shutil.copytree(ARCHIVE_DIR, target)
        return target

    def test_rewriting_one_byte_is_caught(self):
        archive = self.copied_archive()
        path = sorted(archive.glob("*.jsonl"))[0]
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"agent"', '"Agent"', 1),
                        encoding="utf-8")
        self.assertTrue(
            any(error.startswith("archive_file_hash_mismatch")
                for error in verify_archive(archive)))

    def test_truncating_a_file_is_caught(self):
        archive = self.copied_archive()
        path = max(archive.glob("*.jsonl"),
                   key=lambda item: len(item.read_text().splitlines()))
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        errors = verify_archive(archive)
        self.assertTrue(
            any(error.startswith("archive_file_record_count")
                for error in errors))

    def test_deleting_a_file_is_caught(self):
        archive = self.copied_archive()
        sorted(archive.glob("*.jsonl"))[0].unlink()
        self.assertTrue(
            any(error.startswith("archive_file_missing")
                for error in verify_archive(archive)))

    def test_adding_a_file_is_caught(self):
        archive = self.copied_archive()
        (archive / "extra.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertIn(
            "archive_file_unexpected:extra.jsonl",
            verify_archive(archive),
        )


if __name__ == "__main__":
    unittest.main()
