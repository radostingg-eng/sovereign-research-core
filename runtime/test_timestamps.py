import unittest

from .timestamps import (
    effective_as_of,
    normalize_iso_timestamp,
    parse_iso_timestamp,
)


class PortableTimestampTests(unittest.TestCase):
    def test_nanosecond_precision_is_normalized_deterministically(self):
        value = "2026-09-17T10:31:54.69185424Z"

        self.assertEqual(
            normalize_iso_timestamp(value),
            "2026-09-17T10:31:54.691854+00:00",
        )
        parsed = parse_iso_timestamp(value)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.microsecond, 691854)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_fractional_precision_is_padded_or_truncated(self):
        self.assertEqual(
            normalize_iso_timestamp("2026-09-17T10:31:54.6918Z"),
            "2026-09-17T10:31:54.691800+00:00",
        )
        self.assertEqual(
            normalize_iso_timestamp("2026-09-17T10:31:54,1+02:00"),
            "2026-09-17T10:31:54.100000+02:00",
        )

    def test_offset_forms_are_normalized(self):
        self.assertEqual(
            normalize_iso_timestamp("2026-09-17T10:31:54+02"),
            "2026-09-17T10:31:54+02:00",
        )
        self.assertEqual(
            normalize_iso_timestamp("2026-09-17T10:31:54+0230"),
            "2026-09-17T10:31:54+02:30",
        )

    def test_naive_timestamp_remains_naive_for_caller_validation(self):
        parsed = parse_iso_timestamp("2026-09-17 10:31:54")

        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.tzinfo)

    def test_malformed_timestamp_is_rejected(self):
        self.assertIsNone(parse_iso_timestamp("not-a-date"))

    def test_nested_cycle_time_is_authoritative_when_present(self):
        self.assertEqual(
            effective_as_of({
                "as_of": "top-level",
                "snapshot": {"as_of": "nested"},
            }),
            "nested",
        )

    def test_top_level_cycle_time_is_the_compatibility_fallback(self):
        self.assertEqual(
            effective_as_of({"as_of": "top-level", "snapshot": {}}),
            "top-level",
        )

    def test_present_but_empty_nested_time_does_not_fall_back(self):
        self.assertIsNone(effective_as_of({
            "as_of": "top-level",
            "snapshot": {"as_of": None},
        }))


if __name__ == "__main__":
    unittest.main()
