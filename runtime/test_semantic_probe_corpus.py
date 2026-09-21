import hashlib
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from . import semantic_candidate


ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = Path(__file__).resolve().parent / "fixtures" / "semantic_probe"
MALFORMED_CORPUS_DIR = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "staged_intake_malformed"
)
EXPECTED_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "semantic_probe_expected.json"
)
ARCHIVE_SUFFIX = re.compile(r"\.semantic-([0-9a-f]{64})\.json$")
URL = re.compile(r"https?://|://", re.IGNORECASE)
ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
SENSITIVE_PATTERNS = {
    "cycle_id": re.compile(r"cycle-\d{8}T\d{6}Z", re.IGNORECASE),
    "record_id": re.compile(
        r"\b(?:cycle-receipt|cycle-stage|opportunity-event|forecast|"
        r"learning-disposition|goal-event|tool-provenance):[\w.:/-]+",
        re.IGNORECASE,
    ),
    "account_number": re.compile(r"\bU\d{6,}\b"),
    "money_amount": re.compile(
        r"(?:\$|\u20ac|\u00a3)\s?\d[\d,]{3,}(?:\.\d+)?"
    ),
    "position_size": re.compile(r"\b\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?\b"),
    "home_path": re.compile(r"/(?:Users|home)/[\w.-]+"),
    "profile_path": re.compile(
        r"\bsovereign-research-(?!core\b)[\w-]+",
        re.IGNORECASE,
    ),
    "credential": re.compile(
        r"\b(?:ghp_|gho_|ghs_|github_pat_|sk-|AKIA|ASIA)"
        r"[A-Za-z0-9_-]{8,}"
    ),
}
SYNTHETIC_ID = re.compile(
    r"(?:specialist|auxiliary)_acme_\d{2}_\d{2}$"
)
DETERMINISTIC_CORE_STAGE_IDS = tuple(
    sorted(semantic_candidate.CORE_STAGE_IDS)
)
ALLOWED_FIXTURE_STRINGS = {
    "ACME",
    "SAMPLE",
    "cycle-sample",
    "direct_file_analysis",
    "host_summary",
    "sample",
    "synthetic",
    *DETERMINISTIC_CORE_STAGE_IDS,
}


def load_expected():
    manifest = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise AssertionError("unsupported semantic probe corpus manifest")
    return manifest["cases"]


def fixture_privacy_categories(path):
    text = path.read_text(encoding="utf-8")
    categories = {
        name
        for name, pattern in SENSITIVE_PATTERNS.items()
        if pattern.search(text)
    }
    if URL.search(text):
        categories.add("url")
    if ISO_TIMESTAMP.search(text):
        categories.add("timestamp")
    return categories


def synthetic_value_categories(value):
    categories = set()
    if isinstance(value, dict):
        outputs = value.get("stage_outputs")
        if isinstance(outputs, dict):
            for stage_id in outputs:
                if (
                    stage_id not in DETERMINISTIC_CORE_STAGE_IDS
                    and SYNTHETIC_ID.fullmatch(stage_id) is None
                ):
                    categories.add("stage_identifier")
        for item in value.values():
            categories.update(synthetic_value_categories(item))
    elif isinstance(value, list):
        for item in value:
            categories.update(synthetic_value_categories(item))
    elif isinstance(value, str):
        if (
            value not in ALLOWED_FIXTURE_STRINGS
            and SYNTHETIC_ID.fullmatch(value) is None
        ):
            categories.add("unexpected_string")
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and abs(value) > 100
    ):
        categories.add("large_numeric_value")
    return categories


class SemanticProbeRegressionCorpusTests(unittest.TestCase):
    def test_committed_example_has_no_structural_defects(self):
        path = ROOT / "schemas" / "host_semantic_v1.example.json"
        value = json.loads(path.read_text(encoding="utf-8"))

        with patch.object(
            semantic_candidate,
            "CORE_STAGE_IDS",
            DETERMINISTIC_CORE_STAGE_IDS,
        ):
            self.assertEqual(
                semantic_candidate.probe_semantic_candidate(
                    value,
                    filename="example.semantic.json",
                    records=[],
                ),
                [],
            )

    def test_synthetic_candidates_have_exact_ordered_signatures(self):
        expected = load_expected()
        fixture_names = {
            path.name for path in CORPUS_DIR.glob("cycle-sample-*.json")
        }
        self.assertEqual(fixture_names, set(expected))

        observed_signatures = []
        with patch.object(
            semantic_candidate,
            "CORE_STAGE_IDS",
            DETERMINISTIC_CORE_STAGE_IDS,
        ):
            for name, case in expected.items():
                with self.subTest(candidate=name):
                    path = CORPUS_DIR / name
                    match = ARCHIVE_SUFFIX.search(name)
                    self.assertIsNotNone(match)
                    self.assertEqual(
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        match.group(1),
                    )
                    value = json.loads(path.read_text(encoding="utf-8"))
                    issues = semantic_candidate.probe_semantic_candidate(
                        value,
                        filename=case["source_filename"],
                        records=[],
                    )
                    actual = [
                        (issue.code, issue.pointer)
                        for issue in issues
                    ]
                    expected_signature = [
                        (defect["code"], defect["json_pointer"])
                        for defect in case["defects"]
                    ]
                    self.assertEqual(actual, expected_signature)
                    observed_signatures.append(tuple(actual))

        self.assertEqual(
            len(observed_signatures),
            len(set(observed_signatures)),
            "corpus must contain one candidate per defect signature",
        )

    def test_corpus_contains_only_bounded_synthetic_values(self):
        paths = [
            EXPECTED_PATH,
            CORPUS_DIR / "README.md",
            *sorted(CORPUS_DIR.glob("cycle-sample-*.json")),
            MALFORMED_CORPUS_DIR / "README.md",
            *sorted(MALFORMED_CORPUS_DIR.glob("cycle-sample-*.json")),
        ]
        for path in paths:
            with self.subTest(path=path.name):
                self.assertEqual(
                    fixture_privacy_categories(path),
                    set(),
                    f"{path.name} contains prohibited fixture data",
                )
        for path in sorted(CORPUS_DIR.glob("cycle-sample-*.json")):
            with self.subTest(fixture=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    synthetic_value_categories(value),
                    set(),
                    f"{path.name} contains a non-synthetic value",
                )


if __name__ == "__main__":
    unittest.main()
