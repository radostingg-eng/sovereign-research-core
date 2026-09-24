"""Test content-addressed bounded worker leads."""
import unittest

from .worker_projection import (
    WORKER_RESEARCH_PROJECTION_SCHEMA_VERSION,
    worker_projection_id,
    _projection_id,
)


class WorkerProjectionTests(unittest.TestCase):
    def test_projection_id_deterministic(self):
        summary = {
            "items": [{
                "record_id": "test-record",
                "worker_id": "test-worker",
                "status": "completed",
                "observed_at": "2026-09-20T11:00:00Z",
                "target": {
                    "question_id": "q1",
                    "question": "Test question?",
                },
                "result": {
                    "role": "scout",
                    "output_contract_version": 1,
                    "summary": "A test result.",
                },
            }],
            "adoption_required_record_ids": ["test-record"],
        }
        proj_id = worker_projection_id(summary)
        self.assertEqual(proj_id, worker_projection_id(summary))
        self.assertTrue(proj_id.startswith("worker-research-projection:v1:"))
        self.assertEqual(len(proj_id), 94)  # prefix (30) + 64-char hex

    def test_projection_id_empty_items(self):
        summary = {
            "items": [],
            "adoption_required_record_ids": [],
        }
        self.assertIsNone(worker_projection_id(summary))

    def test_projection_id_requires_all_fields(self):
        incomplete = {
            "items": [{
                "record_id": "test",
                "worker_id": "test-worker",
                "status": "completed",
            }],
            "adoption_required_record_ids": ["test"],
        }
        with self.assertRaises(ValueError):
            worker_projection_id(incomplete)

    def test_projection_id_canonical_serialization(self):
        summary = {
            "items": [{
                "record_id": "test-record",
                "worker_id": "test-worker",
                "status": "completed",
                "observed_at": "2026-09-20T11:00:00Z",
                "target": {
                    "question": "Test question?",
                    "question_id": "q1",
                },
                "result": {
                    "role": "scout",
                    "output_contract_version": 1,
                    "summary": "A test result.",
                },
            }],
            "adoption_required_record_ids": ["test-record"],
        }
        reordered = {
            "items": [{
                "record_id": "test-record",
                "worker_id": "test-worker",
                "status": "completed",
                "observed_at": "2026-09-20T11:00:00Z",
                "target": {
                    "question_id": "q1",
                    "question": "Test question?",
                },
                "result": {
                    "role": "scout",
                    "output_contract_version": 1,
                    "summary": "A test result.",
                },
            }],
            "adoption_required_record_ids": ["test-record"],
        }
        self.assertEqual(worker_projection_id(summary), worker_projection_id(reordered))


if __name__ == "__main__":
    unittest.main()
