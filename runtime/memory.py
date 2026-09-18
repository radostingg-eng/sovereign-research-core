"""Deterministic governance helpers for LLM-produced memory distillation.

The LLM decides semantic relevance, synthesis and retrieval questions. This
module only verifies structure, provenance, budgets and reconstruction
coverage. It never generates or ranks investment knowledge itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence


ACTIVE_BRAIN_MAX_ITEMS = 64
ACTIVE_BRAIN_MAX_CLAIMS = 256
RESEARCH_MEMORY_MAX_ITEMS = 64
RESEARCH_MEMORY_META_MAX_ITEMS = 20
MAX_RETIREMENTS_PER_DISTILLATION = 64


@dataclass(frozen=True)
class MemoryValidation:
    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ReconstructionResult:
    valid: bool
    required_claims: tuple[str, ...]
    covered_claims: tuple[str, ...]
    missing_claims: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    contradictions_preserved: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "required_claims": list(self.required_claims),
            "covered_claims": list(self.covered_claims),
            "missing_claims": list(self.missing_claims),
            "unsupported_claims": list(self.unsupported_claims),
            "contradictions_preserved": self.contradictions_preserved,
        }


def validate_memory_object(obj: Mapping[str, Any]) -> MemoryValidation:
    """Validate the minimum structure for a distilled memory object."""
    errors: list[str] = []
    for key in (
        "memory_id",
        "layer",
        "status",
        "as_of",
        "claim",
        "source_ids",
        "evidence_status",
        "confidence",
        "reconstruction_status",
        "claim_ids",
    ):
        if key not in obj:
            errors.append(f"missing:{key}")

    layer = obj.get("layer")
    if (
        "layer" in obj
        and layer not in {"research_memory", "active_brain", "raw_archive"}
    ):
        errors.append("invalid:layer")

    for field in ("source_ids", "claim_ids"):
        if field not in obj:
            continue
        if not isinstance(obj.get(field), Sequence) or isinstance(
                obj.get(field), (str, bytes)):
            errors.append(f"invalid:{field}")
        elif not obj[field]:
            errors.append(f"empty:{field}")

    if "confidence" in obj:
        try:
            confidence = float(obj.get("confidence"))
            if not 0.0 <= confidence <= 1.0:
                errors.append("invalid:confidence")
        except (TypeError, ValueError):
            errors.append("invalid:confidence")

    if (
        "status" in obj
        and obj.get("status") not in {
            "raw", "distilled", "validated", "active", "stale", "archived",
            "experimental",
        }
    ):
        errors.append("invalid:status")

    if (
        "reconstruction_status" in obj
        and obj.get("reconstruction_status") not in {
            "not_run", "passed", "failed", "blocked",
        }
    ):
        errors.append("invalid:reconstruction_status")

    if (
        "evidence_status" in obj
        and obj.get("evidence_status") not in {
            "verified", "cross_checked", "partial", "unknown",
            "not_applicable",
        }
    ):
        errors.append("invalid:evidence_status")

    for field in ("memory_id", "as_of", "claim"):
        if field in obj and not str(obj.get(field, "")).strip():
            errors.append(f"empty:{field}")

    return MemoryValidation(not errors, tuple(errors))


def validate_active_brain(
    entries: Sequence[Mapping[str, Any]],
    *,
    max_items: int = ACTIVE_BRAIN_MAX_ITEMS,
    max_claims: int = ACTIVE_BRAIN_MAX_CLAIMS,
) -> MemoryValidation:
    """Validate a bounded Active Brain without deciding semantic admission."""
    errors: list[str] = []
    if len(entries) > max_items:
        errors.append(f"active_brain_item_budget_exceeded:{len(entries)}>{max_items}")

    for entry in entries:
        result = validate_memory_object(entry)
        errors.extend(f"{entry.get('memory_id','unknown')}:{e}" for e in result.errors)
        if entry.get("layer") != "active_brain":
            errors.append(f"not_active_brain:{entry.get('memory_id','unknown')}")
        if entry.get("status") != "active":
            errors.append(f"not_active_status:{entry.get('memory_id','unknown')}")
        if entry.get("reconstruction_status") != "passed":
            errors.append(f"reconstruction_not_passed:{entry.get('memory_id','unknown')}")

    claim_count = sum(
        len(entry.get("claim_ids", []))
        if isinstance(entry.get("claim_ids"), Sequence)
        and not isinstance(entry.get("claim_ids"), (str, bytes))
        else 1
        for entry in entries
    )
    if claim_count > max_claims:
        errors.append(f"active_brain_claim_budget_exceeded:{claim_count}>{max_claims}")

    return MemoryValidation(not errors, tuple(errors))


def reconstruction_check(
    *,
    required_claim_ids: Iterable[str],
    distilled_claim_ids: Iterable[str],
    source_claim_ids: Iterable[str],
    contradiction_groups: Mapping[str, Sequence[str]] | None = None,
    distilled_contradiction_groups: Mapping[str, Sequence[str]] | None = None,
) -> ReconstructionResult:
    """Check whether a distillation preserves required claims and contradictions."""
    required = tuple(dict.fromkeys(str(x) for x in required_claim_ids))
    distilled = set(str(x) for x in distilled_claim_ids)
    source = set(str(x) for x in source_claim_ids)

    missing = tuple(sorted(set(required) - distilled))
    unsupported = tuple(sorted(distilled - source))

    groups = contradiction_groups or {}
    distilled_groups = distilled_contradiction_groups or {}
    contradictions_ok = True
    for group_id, claim_ids in groups.items():
        if isinstance(claim_ids, Mapping):
            claim_ids = claim_ids.get("claim_ids", ())
        if (
            isinstance(claim_ids, Sequence)
            and not isinstance(claim_ids, (str, bytes))
            and len(claim_ids) > 1
        ):
            required_group = set(str(x) for x in claim_ids)
            if required_group.intersection(distilled) and not required_group.issubset(distilled):
                contradictions_ok = False
                break
            distilled_claim_ids = distilled_groups.get(group_id, [])
            if isinstance(distilled_claim_ids, Mapping):
                distilled_claim_ids = distilled_claim_ids.get("claim_ids", ())
            distilled_group = set(str(x) for x in distilled_claim_ids)
            if required_group.intersection(distilled) and distilled_group != required_group:
                contradictions_ok = False
                break

    valid = not missing and not unsupported and contradictions_ok
    return ReconstructionResult(
        valid=valid,
        required_claims=required,
        covered_claims=tuple(sorted(set(required).intersection(distilled))),
        missing_claims=missing,
        unsupported_claims=unsupported,
        contradictions_preserved=contradictions_ok,
    )


def active_brain_admission_check(
    candidate_entries: Sequence[Mapping[str, Any]],
    *,
    required_claim_ids: Iterable[str],
    distilled_claim_ids: Iterable[str],
    source_claim_ids: Iterable[str],
    contradiction_groups: Mapping[str, Sequence[str]] | None = None,
    distilled_contradiction_groups: Mapping[str, Sequence[str]] | None = None,
    max_items: int = ACTIVE_BRAIN_MAX_ITEMS,
    max_claims: int = ACTIVE_BRAIN_MAX_CLAIMS,
) -> dict[str, Any]:
    """Return an explicit, fail-closed admission decision."""
    structure = validate_active_brain(
        candidate_entries,
        max_items=max_items,
        max_claims=max_claims,
    )
    reconstruction = reconstruction_check(
        required_claim_ids=required_claim_ids,
        distilled_claim_ids=distilled_claim_ids,
        source_claim_ids=source_claim_ids,
        contradiction_groups=contradiction_groups,
        distilled_contradiction_groups=distilled_contradiction_groups,
    )
    errors = list(structure.errors)
    if not reconstruction.valid:
        if reconstruction.missing_claims:
            errors.append("reconstruction_missing_required_claims")
        if reconstruction.unsupported_claims:
            errors.append("reconstruction_contains_unsupported_claims")
        if not reconstruction.contradictions_preserved:
            errors.append("reconstruction_collapsed_contradiction")
    return {
        "admitted": not errors,
        "errors": tuple(errors),
        "structure_valid": structure.valid,
        "reconstruction": reconstruction,
    }


def distillation_ratio(raw_units: int, distilled_units: int) -> float | None:
    """Return raw/distilled compression ratio; None when inputs are invalid."""
    if raw_units <= 0 or distilled_units <= 0:
        return None
    return raw_units / distilled_units


_DISTILLATION_FIELDS = {
    "distillation_id", "source_ids", "source_time_bounds", "memory_objects",
    "claim_ids", "contradiction_groups", "active_brain_proposals",
    "retirements", "reconstruction_spec", "compression_metrics", "blockers",
    "ex_post_material", "brain_version",
}
_RECONSTRUCTION_FIELDS = {
    "required_claim_ids",
    "distilled_claim_ids",
    "source_claim_ids",
    "distilled_contradiction_groups",
}
_RETIREMENT_FIELDS = {"memory_id", "status", "reason", "source_ids"}


def validate_distillation_envelope(value: Any) -> list[str]:
    """Validate the host/runtime handoff, not the semantic memory choice."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return ["memory_distillation_not_an_object"]
    if value.get("status") == "not_performed":
        return ["memory_distillation_not_performed_object"]
    errors = [
        f"memory_distillation_missing:{field}"
        for field in sorted(_DISTILLATION_FIELDS - set(value))
    ]
    for field in (
        "source_ids", "memory_objects", "claim_ids",
        "active_brain_proposals", "retirements", "blockers",
        "ex_post_material",
    ):
        if field in value and (
            not isinstance(value[field], Sequence)
            or isinstance(value[field], (str, bytes))
        ):
            errors.append(f"memory_distillation_invalid:{field}")
    for field in (
        "source_time_bounds", "contradiction_groups",
        "reconstruction_spec", "compression_metrics",
    ):
        if field in value and not isinstance(value[field], Mapping):
            errors.append(f"memory_distillation_invalid:{field}")

    for collection_name in ("memory_objects", "active_brain_proposals"):
        collection = value.get(collection_name)
        if not isinstance(collection, Sequence) or isinstance(
                collection, (str, bytes)):
            continue
        seen_memory_ids: set[str] = set()
        for index, item in enumerate(collection):
            if not isinstance(item, Mapping):
                errors.append(
                    f"memory_distillation_item_not_object:"
                    f"{collection_name}:{index}")
                continue
            result = validate_memory_object(item)
            errors.extend(
                f"memory_object_invalid:{collection_name}:"
                f"{item.get('memory_id', index)}:{error}"
                for error in result.errors
            )
            memory_id = str(item.get("memory_id", "")).strip()
            if memory_id in seen_memory_ids:
                errors.append(
                    f"memory_distillation_duplicate_memory_id:"
                    f"{collection_name}:{memory_id}")
            elif memory_id:
                seen_memory_ids.add(memory_id)

    retirements = value.get("retirements")
    if isinstance(retirements, Sequence) and not isinstance(
            retirements, (str, bytes)):
        if len(retirements) > MAX_RETIREMENTS_PER_DISTILLATION:
            errors.append(
                "memory_retirement_invalid:"
                f"too_many:{len(retirements)}>"
                f"{MAX_RETIREMENTS_PER_DISTILLATION}")
        seen_retirements: set[str] = set()
        for index, retirement in enumerate(retirements):
            if not isinstance(retirement, Mapping):
                errors.append(f"memory_retirement_not_object:{index}")
                continue
            missing = _RETIREMENT_FIELDS - set(retirement)
            errors.extend(
                f"memory_retirement_missing:{index}:{field}"
                for field in sorted(missing)
            )
            memory_id = str(retirement.get("memory_id", "")).strip()
            if "memory_id" in retirement and not memory_id:
                errors.append(
                    f"memory_retirement_invalid:{index}:empty_memory_id")
            if memory_id in seen_retirements:
                errors.append(
                    f"memory_retirement_invalid:{index}:"
                    f"duplicate_memory_id:{memory_id}")
            elif memory_id:
                seen_retirements.add(memory_id)
            if (
                "status" in retirement
                and retirement.get("status") not in {"stale", "archived"}
            ):
                errors.append(
                    f"memory_retirement_invalid:{index}:status")
            if (
                "reason" in retirement
                and not str(retirement.get("reason", "")).strip()
            ):
                errors.append(
                    f"memory_retirement_invalid:{index}:empty_reason")
            source_ids = retirement.get("source_ids")
            if "source_ids" in retirement and (
                not isinstance(source_ids, Sequence)
                or isinstance(source_ids, (str, bytes))
                or not source_ids
            ):
                errors.append(
                    f"memory_retirement_invalid:{index}:source_ids")

    contradiction_groups = value.get("contradiction_groups")
    if isinstance(contradiction_groups, Mapping):
        for group_id, claim_ids in contradiction_groups.items():
            if isinstance(claim_ids, Mapping):
                claim_ids = claim_ids.get("claim_ids")
            if (
                not isinstance(claim_ids, Sequence)
                or isinstance(claim_ids, (str, bytes))
            ):
                errors.append(
                    f"memory_contradiction_group_invalid:{group_id}")

    reconstruction = value.get("reconstruction_spec")
    if isinstance(reconstruction, Mapping):
        errors.extend(
            f"memory_reconstruction_missing:{field}"
            for field in sorted(_RECONSTRUCTION_FIELDS - set(reconstruction))
        )
        for field in (
            "required_claim_ids", "distilled_claim_ids", "source_claim_ids",
        ):
            field_value = reconstruction.get(field)
            if field in reconstruction and (
                not isinstance(field_value, Sequence)
                or isinstance(field_value, (str, bytes))
            ):
                errors.append(f"memory_reconstruction_invalid:{field}")
        groups = reconstruction.get("distilled_contradiction_groups")
        if (
            "distilled_contradiction_groups" in reconstruction
            and not isinstance(groups, Mapping)
        ):
            errors.append(
                "memory_reconstruction_invalid:"
                "distilled_contradiction_groups")

    compression = value.get("compression_metrics")
    if isinstance(compression, Mapping):
        for field in ("raw_units", "distilled_units"):
            if field not in compression:
                errors.append(f"memory_compression_missing:{field}")
                continue
            field_value = compression[field]
            if (
                isinstance(field_value, bool)
                or not isinstance(field_value, (int, float))
                or field_value <= 0
            ):
                errors.append(f"memory_compression_invalid:{field}")
    return errors


def evaluate_distillation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Run structure, provenance, reconstruction, contradiction and budgets."""
    envelope_errors = validate_distillation_envelope(value)
    if envelope_errors:
        return {
            "admitted": False,
            "research_admitted": False,
            "errors": envelope_errors,
            "research_errors": envelope_errors,
        }

    objects = [
        dict(item) for item in value.get("memory_objects", ())
        if isinstance(item, Mapping)
    ]
    proposals = [
        dict(item) for item in value.get("active_brain_proposals", ())
        if isinstance(item, Mapping)
    ]
    object_errors = []
    for item in objects:
        result = validate_memory_object(item)
        object_errors.extend(
            f"{item.get('memory_id', 'unknown')}:{error}"
            for error in result.errors
        )

    reconstruction = value.get("reconstruction_spec", {})
    reconstruction_result = reconstruction_check(
        required_claim_ids=reconstruction.get("required_claim_ids", ()),
        distilled_claim_ids=reconstruction.get("distilled_claim_ids", ()),
        source_claim_ids=reconstruction.get("source_claim_ids", ()),
        contradiction_groups=value.get("contradiction_groups", {}),
        distilled_contradiction_groups=reconstruction.get(
            "distilled_contradiction_groups", {}),
    )
    active_structure = validate_active_brain(proposals)
    compression = value.get("compression_metrics", {})
    ratio = distillation_ratio(
        int(compression.get("raw_units", 0) or 0),
        int(compression.get("distilled_units", 0) or 0),
    )
    research_errors = list(object_errors)
    if reconstruction_result.missing_claims:
        research_errors.append("reconstruction_missing_required_claims")
    if reconstruction_result.unsupported_claims:
        research_errors.append("reconstruction_contains_unsupported_claims")
    if not reconstruction_result.contradictions_preserved:
        research_errors.append("reconstruction_collapsed_contradiction")
    if ratio is None:
        research_errors.append(
            "memory_distillation_invalid:compression_metrics")
    errors = list(research_errors) + list(active_structure.errors)
    return {
        "admitted": not errors,
        "research_admitted": not research_errors,
        "errors": errors,
        "research_errors": research_errors,
        "object_count": len(objects),
        "active_proposal_count": len(proposals),
        "compression_ratio": ratio,
        "reconstruction": reconstruction_result.as_dict(),
    }


def _retirement_events(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[int, str]]:
    events: dict[str, tuple[int, str]] = {}
    for index, record in enumerate(records):
        if record.get("record_type") != "memory_retirement":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        memory_id = str(payload.get("memory_id", "")).strip()
        status = str(payload.get("status", "")).strip()
        if memory_id and status in {"stale", "archived"}:
            previous = events.get(memory_id)
            if previous is not None and previous[1] == "archived":
                continue
            events[memory_id] = (index, status)
    return events


def _current_memory_versions(
    records: Sequence[Mapping[str, Any]],
    *,
    record_type: str,
    admitted_field: str,
    eligible: Callable[[Mapping[str, Any]], bool] | None = None,
) -> tuple[dict[str, tuple[int, dict[str, Any]]], set[str]]:
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    superseded_at: dict[str, int] = {}
    retirements = _retirement_events(records)
    for index, record in enumerate(records):
        if record.get("record_type") != record_type:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        memory_id = str(payload.get("memory_id", "")).strip()
        if not memory_id:
            continue
        item = dict(payload)
        if eligible is not None and not eligible(item):
            continue
        latest[memory_id] = (index, item)
        if item.get(admitted_field, True) is True:
            for earlier in item.get("supersedes") or ():
                earlier_id = str(earlier).strip()
                if earlier_id:
                    superseded_at[earlier_id] = index

    current: dict[str, tuple[int, dict[str, Any]]] = {}
    retired: set[str] = set()
    for memory_id, (index, item) in latest.items():
        retirement = retirements.get(memory_id)
        if retirement is not None:
            retirement_index, status = retirement
            if status == "archived" or index <= retirement_index:
                retired.add(memory_id)
                continue
        if index <= superseded_at.get(memory_id, -1):
            retired.add(memory_id)
            continue
        current[memory_id] = (index, item)
    return current, retired


def active_memory(records: Sequence[Mapping[str, Any]],
                  *, limit: int = ACTIVE_BRAIN_MAX_ITEMS
                  ) -> list[dict[str, Any]]:
    """Latest admitted Active Brain versions after lifecycle events."""
    current, _retired = _current_memory_versions(
        records,
        record_type="memory",
        admitted_field="distillation_admitted",
        eligible=lambda item: (
            item.get("status") == "active"
            and item.get("reconstruction_status") == "passed"
            and item.get("distillation_admitted", True) is True
        ),
    )
    return [
        item for _index, item in sorted(
            current.values(), key=lambda row: row[0])
    ][-limit:]


def research_memory(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = RESEARCH_MEMORY_MAX_ITEMS,
    metadata_limit: int = RESEARCH_MEMORY_META_MAX_ITEMS,
) -> dict[str, Any]:
    """Bounded Research Memory for host-selected, query-driven retrieval."""
    current, retired = _current_memory_versions(
        records,
        record_type="research_memory",
        admitted_field="research_admitted",
        eligible=lambda item: (
            item.get("status") == "validated"
            and item.get("reconstruction_status") == "passed"
            and item.get("research_admitted") is True
        ),
    )
    active_ids = {
        str(item.get("memory_id", ""))
        for item in active_memory(records)
    }
    available: list[dict[str, Any]] = []
    inactive_latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, record in enumerate(records):
        if record.get("record_type") != "research_memory":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        memory_id = str(payload.get("memory_id", "")).strip()
        if not memory_id:
            continue
        if (
            payload.get("status") == "validated"
            and payload.get("reconstruction_status") == "passed"
            and payload.get("research_admitted") is True
        ):
            continue
        inactive_latest[memory_id] = (index, dict(payload))
    inactive: list[dict[str, Any]] = []
    active_overlap = 0
    for memory_id, (_index, item) in sorted(
            current.items(), key=lambda row: row[1][0]):
        if memory_id in active_ids:
            active_overlap += 1
            continue
        available.append(item)
    for memory_id, (_index, item) in sorted(
            inactive_latest.items(), key=lambda row: row[1][0]):
        inactive.append({
            "memory_id": memory_id,
            "status": item.get("status"),
            "reconstruction_status": item.get("reconstruction_status"),
            "research_admitted": item.get("research_admitted") is True,
            "validated_version_retained": memory_id in current,
        })

    visible = available[-limit:]
    inactive_visible = inactive[-metadata_limit:]
    retired_visible = sorted(retired)[-metadata_limit:]
    return {
        "count": len(available),
        "items": visible,
        "omitted": max(0, len(available) - len(visible)),
        "inactive_count": len(inactive),
        "inactive": inactive_visible,
        "inactive_omitted": max(0, len(inactive) - len(inactive_visible)),
        "retired_count": len(retired),
        "retired_ids": retired_visible,
        "retired_omitted": max(0, len(retired) - len(retired_visible)),
        "active_brain_overlap_count": active_overlap,
        "retrieval_order": [
            "active_memory",
            "research_memory",
            "raw_archive",
        ],
    }


def evaluate_retirements(
    value: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    research_admitted: bool,
) -> dict[str, Any]:
    """Apply only source-grounded retirements of known memory objects."""
    known: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        if record.get("record_type") not in {"memory", "research_memory"}:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        memory_id = str(payload.get("memory_id", "")).strip()
        if memory_id:
            known.setdefault(memory_id, []).append(record)

    envelope_sources = {
        str(source_id) for source_id in value.get("source_ids") or ()
    }
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in value.get("retirements") or ():
        if not isinstance(row, Mapping):
            continue
        retirement = dict(row)
        memory_id = str(retirement.get("memory_id", "")).strip()
        if not research_admitted:
            rejected.append({
                "memory_id": memory_id,
                "error": "retirement_distillation_not_admitted",
            })
            continue
        targets = known.get(memory_id, [])
        if not targets:
            rejected.append({
                "memory_id": memory_id,
                "error": "retirement_target_unknown",
            })
            continue
        retirement_sources = {
            str(source_id) for source_id in retirement.get("source_ids") or ()
        }
        if not retirement_sources.issubset(envelope_sources):
            rejected.append({
                "memory_id": memory_id,
                "error": "retirement_sources_outside_distillation",
            })
            continue
        target_references = {
            str(record.get("record_id", ""))
            for record in targets
        }
        for record in targets:
            payload = record.get("payload")
            if isinstance(payload, Mapping):
                target_references.update(
                    str(source_id)
                    for source_id in payload.get("source_ids") or ()
                )
        if not envelope_sources.intersection(target_references):
            rejected.append({
                "memory_id": memory_id,
                "error": "retirement_target_provenance_not_retrieved",
            })
            continue
        retirement["target_layers"] = sorted({
            str(record.get("payload", {}).get("layer", ""))
            for record in targets
            if isinstance(record.get("payload"), Mapping)
        })
        applied.append(retirement)
    return {"applied": applied, "rejected": rejected}


def latest_distillation_evaluation(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Latest admission result for host self-correction."""
    for record in reversed(list(records)):
        if record.get("record_type") != "memory_distillation":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        evaluation = payload.get("evaluation")
        if not isinstance(evaluation, Mapping):
            continue
        return {
            "distillation_id": payload.get("distillation_id"),
            "admitted": evaluation.get("admitted") is True,
            "research_admitted": (
                evaluation.get(
                    "research_admitted",
                    evaluation.get("admitted"),
                ) is True),
            "errors": list(evaluation.get("errors") or ()),
            "research_errors": list(
                evaluation.get("research_errors") or ()),
            "reconstruction": dict(evaluation.get("reconstruction") or {}),
            "compression_ratio": evaluation.get("compression_ratio"),
            "lifecycle": dict(payload.get("lifecycle") or {}),
            "at": payload.get("at"),
        }
    return {}
