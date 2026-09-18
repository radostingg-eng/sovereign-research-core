import unittest

from .memory import (
    active_brain_admission_check,
    distillation_ratio,
    evaluate_retirements,
    reconstruction_check,
    research_memory,
    validate_active_brain,
    validate_memory_object,
)


class MemoryGovernanceTests(unittest.TestCase):
    def entry(self, *, status="active", reconstruction_status="passed"):
        return {
            "memory_id": "m1",
            "layer": "active_brain",
            "status": status,
            "as_of": "2026-09-16T00:00:00Z",
            "claim": "example durable claim",
            "source_ids": ["s1"],
            "evidence_status": "verified",
            "confidence": 0.8,
            "reconstruction_status": reconstruction_status,
            "claim_ids": ["c1"],
        }

    def test_valid_active_entry(self):
        self.assertTrue(validate_memory_object(self.entry()).valid)
        self.assertTrue(validate_active_brain([self.entry()]).valid)

    def test_active_brain_requires_reconstruction(self):
        result = validate_active_brain([self.entry(reconstruction_status="failed")])
        self.assertFalse(result.valid)
        self.assertIn("reconstruction_not_passed:m1", result.errors)

    def test_reconstruction_fails_on_missing_required_claim(self):
        result = reconstruction_check(
            required_claim_ids=["c1", "c2"],
            distilled_claim_ids=["c1"],
            source_claim_ids=["c1", "c2"],
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.missing_claims, ("c2",))

    def test_reconstruction_fails_on_unsupported_claim(self):
        result = reconstruction_check(
            required_claim_ids=["c1"],
            distilled_claim_ids=["c1", "c3"],
            source_claim_ids=["c1", "c2"],
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.unsupported_claims, ("c3",))

    def test_reconstruction_preserves_contradiction_group(self):
        result = reconstruction_check(
            required_claim_ids=["a", "b"],
            distilled_claim_ids=["a", "b"],
            source_claim_ids=["a", "b"],
            contradiction_groups={"g1": ["a", "b"]},
            distilled_contradiction_groups={"g1": ["a", "b"]},
        )
        self.assertTrue(result.valid)
        self.assertTrue(result.contradictions_preserved)

    def test_reconstruction_accepts_group_objects_with_claim_ids(self):
        result = reconstruction_check(
            required_claim_ids=["a", "b"],
            distilled_claim_ids=["a", "b"],
            source_claim_ids=["a", "b"],
            contradiction_groups={
                "g1": {"claim_ids": ["a", "b"], "status": "unresolved"}},
            distilled_contradiction_groups={"g1": ["a", "b"]},
        )
        self.assertTrue(result.valid)
        self.assertTrue(result.contradictions_preserved)

    def test_admission_is_fail_closed(self):
        result = active_brain_admission_check(
            [self.entry()],
            required_claim_ids=["c1", "c2"],
            distilled_claim_ids=["c1"],
            source_claim_ids=["c1", "c2"],
        )
        self.assertFalse(result["admitted"])
        self.assertIn("reconstruction_missing_required_claims", result["errors"])

    def test_compression_ratio(self):
        self.assertEqual(distillation_ratio(1000, 100), 10.0)
        self.assertIsNone(distillation_ratio(0, 100))


class DistillationEnvelopeTests(unittest.TestCase):
    def entry(self, **over):
        value = {
            "memory_id": "m1",
            "layer": "active_brain",
            "status": "active",
            "as_of": "2026-09-16T20:00:00Z",
            "claim": "A durable, source-grounded process lesson.",
            "source_ids": ["source-1"],
            "evidence_status": "verified",
            "confidence": 0.8,
            "reconstruction_status": "passed",
            "claim_ids": ["c1"],
        }
        value.update(over)
        return value

    def distillation(self, **over):
        entry = self.entry()
        value = {
            "distillation_id": "d1",
            "source_ids": ["source-1"],
            "source_time_bounds": {
                "from": "2026-09-16T00:00:00Z",
                "to": "2026-09-16T20:00:00Z",
            },
            "memory_objects": [entry],
            "claim_ids": ["c1"],
            "contradiction_groups": {},
            "active_brain_proposals": [entry],
            "retirements": [],
            "reconstruction_spec": {
                "required_claim_ids": ["c1"],
                "distilled_claim_ids": ["c1"],
                "source_claim_ids": ["c1"],
                "distilled_contradiction_groups": {},
            },
            "compression_metrics": {"raw_units": 100, "distilled_units": 10},
            "blockers": [],
            "ex_post_material": [],
            "brain_version": 1,
        }
        value.update(over)
        return value

    def test_a_complete_distillation_is_admitted(self):
        from .memory import evaluate_distillation

        result = evaluate_distillation(self.distillation())
        self.assertTrue(result["admitted"])
        self.assertEqual(result["compression_ratio"], 10.0)

    def test_failed_reconstruction_is_not_admitted(self):
        from .memory import evaluate_distillation

        value = self.distillation()
        value["reconstruction_spec"]["distilled_claim_ids"] = []
        result = evaluate_distillation(value)
        self.assertFalse(result["admitted"])
        self.assertIn(
            "reconstruction_missing_required_claims", result["errors"])

    def test_research_memory_admission_is_independent_of_active_brain(self):
        from .memory import evaluate_distillation

        value = self.distillation()
        value["memory_objects"] = [self.entry(
            layer="research_memory",
            status="validated",
            reconstruction_status="passed",
        )]
        value["active_brain_proposals"] = [
            self.entry(status="experimental")]
        result = evaluate_distillation(value)
        self.assertTrue(result["research_admitted"])
        self.assertFalse(result["admitted"])

    def test_an_invalid_envelope_is_refused_before_execution(self):
        from .memory import validate_distillation_envelope
        errors = validate_distillation_envelope({"distillation_id": "d1"})
        self.assertTrue(
            any(error.startswith("memory_distillation_missing:")
                for error in errors))

    def test_not_performed_marker_is_one_direct_refusal(self):
        from .memory import validate_distillation_envelope

        self.assertEqual(
            validate_distillation_envelope({
                "status": "not_performed",
                "reason": "No deep-memory work this cycle.",
            }),
            ["memory_distillation_not_performed_object"],
        )

    def test_nested_memory_objects_are_validated_before_execution(self):
        from .memory import validate_distillation_envelope

        value = self.distillation()
        del value["memory_objects"][0]["reconstruction_status"]
        self.assertTrue(
            any(error.startswith(
                "memory_object_invalid:memory_objects:m1:"
                "missing:reconstruction_status")
                for error in validate_distillation_envelope(value))
        )
        self.assertFalse(
            any(error.endswith("invalid:reconstruction_status")
                for error in validate_distillation_envelope(value))
        )

    def test_evidence_status_and_claim_ids_are_required(self):
        for field in ("evidence_status", "claim_ids"):
            with self.subTest(field=field):
                entry = self.entry()
                del entry[field]
                self.assertIn(
                    f"missing:{field}",
                    validate_memory_object(entry).errors,
                )

    def test_reconstruction_nested_types_are_validated(self):
        from .memory import validate_distillation_envelope

        value = self.distillation()
        value["reconstruction_spec"]["distilled_contradiction_groups"] = []
        self.assertIn(
            "memory_reconstruction_invalid:distilled_contradiction_groups",
            validate_distillation_envelope(value),
        )

    def test_contradiction_group_object_with_claim_ids_is_valid(self):
        from .memory import validate_distillation_envelope

        value = self.distillation()
        value["contradiction_groups"] = {
            "g1": {"claim_ids": ["a", "b"], "status": "unresolved"}}
        self.assertNotIn(
            "memory_contradiction_group_invalid:g1",
            validate_distillation_envelope(value),
        )

    def test_latest_distillation_surfaces_admission_errors(self):
        from .memory import latest_distillation_evaluation

        records = [{
            "record_type": "memory_distillation",
            "payload": {
                "distillation_id": "d1",
                "evaluation": {
                    "admitted": False,
                    "research_admitted": True,
                    "errors": ["reconstruction_collapsed_contradiction"],
                    "research_errors": [],
                    "reconstruction": {"valid": False},
                    "compression_ratio": 3.0,
                },
                "lifecycle": {
                    "research_memory": [{"memory_id": "m1"}],
                },
                "at": "2026-09-16T22:00:00Z",
            },
        }]
        self.assertEqual(
            latest_distillation_evaluation(records),
            {
                "distillation_id": "d1",
                "admitted": False,
                "research_admitted": True,
                "errors": ["reconstruction_collapsed_contradiction"],
                "research_errors": [],
                "reconstruction": {"valid": False},
                "compression_ratio": 3.0,
                "lifecycle": {
                    "research_memory": [{"memory_id": "m1"}],
                },
                "at": "2026-09-16T22:00:00Z",
            },
        )

    def test_compression_uses_the_executable_metric_names(self):
        from .memory import validate_distillation_envelope

        value = self.distillation()
        value["compression_metrics"] = {"source_records": 5}
        errors = validate_distillation_envelope(value)
        self.assertIn("memory_compression_missing:raw_units", errors)
        self.assertIn("memory_compression_missing:distilled_units", errors)

    def test_active_memory_drops_superseded_and_stale_objects(self):
        from .memory import active_memory

        records = [
            {"record_type": "memory",
             "payload": self.entry(memory_id="old")},
            {"record_type": "memory",
             "payload": self.entry(memory_id="new", supersedes=["old"])},
            {"record_type": "memory",
             "payload": self.entry(memory_id="stale", status="stale")},
        ]
        self.assertEqual(
            [item["memory_id"] for item in active_memory(records)], ["new"])

    def test_retirement_rows_have_an_explicit_contract(self):
        from .memory import validate_distillation_envelope

        value = self.distillation(retirements=[{
            "memory_id": "m1",
            "status": "stale",
            "reason": "New evidence invalidated the claim.",
            "source_ids": ["source-1"],
        }])
        self.assertEqual(validate_distillation_envelope(value), [])

        value["retirements"] = ["m1"]
        self.assertIn(
            "memory_retirement_not_object:0",
            validate_distillation_envelope(value),
        )

    def test_research_memory_separates_available_and_inactive(self):
        records = [
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id="available",
                    layer="research_memory",
                    status="validated",
                    research_admitted=True,
                ),
            },
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id="experimental",
                    layer="research_memory",
                    status="experimental",
                    reconstruction_status="failed",
                    research_admitted=True,
                ),
            },
        ]
        summary = research_memory(records)
        self.assertEqual(
            [item["memory_id"] for item in summary["items"]],
            ["available"],
        )
        self.assertEqual(summary["inactive_count"], 1)
        self.assertEqual(
            summary["retrieval_order"],
            ["active_memory", "research_memory", "raw_archive"],
        )

    def test_failed_revision_does_not_replace_validated_version(self):
        records = [
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id="m1",
                    layer="research_memory",
                    status="validated",
                    research_admitted=True,
                    claim="Validated claim.",
                ),
            },
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id="m1",
                    layer="research_memory",
                    status="validated",
                    research_admitted=False,
                    claim="Failed replacement.",
                ),
            },
        ]
        summary = research_memory(records)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["items"][0]["claim"], "Validated claim.")
        self.assertEqual(summary["inactive_count"], 1)
        self.assertTrue(
            summary["inactive"][0]["validated_version_retained"])

    def test_research_memory_is_bounded_with_visible_omissions(self):
        records = [
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id=f"m{index}",
                    layer="research_memory",
                    status="validated",
                    research_admitted=True,
                ),
            }
            for index in range(70)
        ]
        summary = research_memory(records)
        self.assertEqual(summary["count"], 70)
        self.assertEqual(len(summary["items"]), 64)
        self.assertEqual(summary["omitted"], 6)

    def test_stale_retirement_is_reversible_but_archive_is_terminal(self):
        old = {
            "record_type": "research_memory",
            "payload": self.entry(
                memory_id="m1",
                layer="research_memory",
                status="validated",
                research_admitted=True,
            ),
        }
        stale = {
            "record_type": "memory_retirement",
            "payload": {"memory_id": "m1", "status": "stale"},
        }
        new = {
            "record_type": "research_memory",
            "payload": self.entry(
                memory_id="m1",
                layer="research_memory",
                status="validated",
                research_admitted=True,
                claim="Revalidated claim.",
            ),
        }
        self.assertEqual(research_memory([old, stale])["count"], 0)
        self.assertEqual(research_memory([old, stale, new])["count"], 1)

        archived = {
            "record_type": "memory_retirement",
            "payload": {"memory_id": "m1", "status": "archived"},
        }
        self.assertEqual(
            research_memory([old, archived, new])["count"],
            0,
        )
        later_stale = {
            "record_type": "memory_retirement",
            "payload": {"memory_id": "m1", "status": "stale"},
        }
        self.assertEqual(
            research_memory([old, archived, new, later_stale])["count"],
            0,
        )

    def test_active_brain_hides_the_same_research_memory_id(self):
        records = [
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id="m1",
                    layer="research_memory",
                    status="validated",
                    research_admitted=True,
                ),
            },
            {
                "record_type": "memory",
                "payload": self.entry(
                    memory_id="m1",
                    distillation_admitted=True,
                ),
            },
        ]
        summary = research_memory(records)
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["active_brain_overlap_count"], 1)

    def test_research_memory_supersession_is_layer_local(self):
        records = [
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id="old",
                    layer="research_memory",
                    status="validated",
                    research_admitted=True,
                ),
            },
            {
                "record_type": "research_memory",
                "payload": self.entry(
                    memory_id="new",
                    layer="research_memory",
                    status="validated",
                    research_admitted=True,
                    supersedes=["old"],
                ),
            },
            {
                "record_type": "memory",
                "payload": self.entry(
                    memory_id="active",
                    supersedes=["new"],
                    distillation_admitted=True,
                ),
            },
        ]
        summary = research_memory(records)
        self.assertEqual(
            [item["memory_id"] for item in summary["items"]],
            ["new"],
        )
        self.assertEqual(summary["retired_ids"], ["old"])

    def test_retirement_events_apply_to_active_brain_temporally(self):
        old = {
            "record_type": "memory",
            "payload": self.entry(
                memory_id="m1",
                distillation_admitted=True,
            ),
        }
        stale = {
            "record_type": "memory_retirement",
            "payload": {"memory_id": "m1", "status": "stale"},
        }
        new = {
            "record_type": "memory",
            "payload": self.entry(
                memory_id="m1",
                claim="Revalidated active claim.",
                distillation_admitted=True,
            ),
        }
        from .memory import active_memory

        self.assertEqual(active_memory([old, stale]), [])
        self.assertEqual(
            active_memory([old, stale, new])[0]["claim"],
            "Revalidated active claim.",
        )

    def test_retirement_requires_known_target_and_retrieved_provenance(self):
        records = [{
            "record_id": "research-memory:old",
            "record_type": "research_memory",
            "payload": self.entry(
                memory_id="m1",
                layer="research_memory",
                status="validated",
                source_ids=["source-old"],
                research_admitted=True,
            ),
        }]
        value = self.distillation(
            source_ids=["source-old", "source-new"],
            retirements=[{
                "memory_id": "m1",
                "status": "stale",
                "reason": "New evidence invalidated the claim.",
                "source_ids": ["source-new"],
            }],
        )
        result = evaluate_retirements(
            value,
            records,
            research_admitted=True,
        )
        self.assertEqual(
            [row["memory_id"] for row in result["applied"]],
            ["m1"],
        )
        self.assertEqual(result["rejected"], [])

        value["retirements"][0]["memory_id"] = "unknown"
        result = evaluate_retirements(
            value,
            records,
            research_admitted=True,
        )
        self.assertEqual(
            result["rejected"][0]["error"],
            "retirement_target_unknown",
        )


if __name__ == "__main__":
    unittest.main()
