"""evolution.py has derived lessons since it was written. The journal held zero."""

import unittest

from .lessons import (
    MAX_SURFACED,
    recorded_lessons,
    summarise,
    validate_lesson,
    validate_lessons,
)


def lesson_record(lesson_id, *, supersedes=(), text="a lesson"):
    return {"record_type": "lesson", "at": "2026-09-16T10:00:00Z",
            "payload": {"lesson_id": lesson_id, "lesson": text,
                        "evidence": "e", "falsified_if": "f",
                        "supersedes": list(supersedes)}}


class ALessonNeedsAFalsifierTests(unittest.TestCase):
    """A lesson nothing could ever contradict is a slogan. It accumulates
    forever and quietly shapes every later decision, which is exactly what an
    unfalsifiable belief should not be allowed to do."""

    def complete(self, **over):
        row = {"lesson": "x", "evidence": "y", "falsified_if": "z"}
        row.update(over)
        return row

    def test_a_complete_lesson_is_accepted(self):
        self.assertEqual(validate_lesson(self.complete()), [])

    def test_a_lesson_without_a_falsifier_is_refused(self):
        self.assertIn("lesson_missing_falsified_if",
                      validate_lesson(self.complete(falsified_if="")))

    def test_a_lesson_without_evidence_is_refused(self):
        self.assertIn("lesson_missing_evidence",
                      validate_lesson(self.complete(evidence="")))

    def test_a_non_object_is_refused(self):
        self.assertEqual(validate_lesson("just a string"), ["lesson_not_an_object"])

    def test_absent_lessons_are_fine(self):
        """Most cycles conclude nothing durable, and that is not a failure."""
        self.assertEqual(validate_lessons({}), [])

    def test_an_empty_list_is_fine(self):
        self.assertEqual(validate_lessons({"lessons": []}), [])

    def test_a_non_list_is_refused(self):
        self.assertEqual(validate_lessons({"lessons": "x"}),
                         ["lessons_must_be_a_list"])

    def test_the_offending_index_is_named(self):
        problems = validate_lessons({"lessons": [self.complete(),
                                                 self.complete(evidence="")]})
        self.assertEqual(problems, ["lesson_missing_evidence:1"])


class LessonsOutliveTheirCycleTests(unittest.TestCase):

    def test_a_recorded_lesson_is_surfaced(self):
        rows = recorded_lessons([lesson_record("l1")])
        self.assertEqual(rows[0]["lesson_id"], "l1")

    def test_superseding_drops_the_earlier_one(self):
        rows = recorded_lessons([lesson_record("l1"),
                                 lesson_record("l2", supersedes=["l1"])])
        self.assertEqual([r["lesson_id"] for r in rows], ["l2"])

    def test_the_surfaced_list_stays_bounded(self):
        """ACTIVE_BRAIN.md refuses to become a raw archive, and the host pays
        to read this every cycle."""
        many = [lesson_record(f"l{i}") for i in range(MAX_SURFACED + 8)]
        self.assertEqual(len(recorded_lessons(many)), MAX_SURFACED)

    def test_non_lesson_records_are_ignored(self):
        self.assertEqual(recorded_lessons([{"record_type": "cycle_receipt",
                                            "payload": {}}]), [])

    def test_the_guidance_requires_a_falsifier(self):
        text = summarise([lesson_record("l1")])["what_this_means"]
        self.assertIn("falsified_if", text)
        self.assertIn("slogan", text)

    def test_an_empty_board_points_at_decision_outcomes(self):
        self.assertIn("decision_outcomes", summarise([])["what_this_means"])
