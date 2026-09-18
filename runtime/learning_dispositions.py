"""Validate, persist, and summarize schema-v3 learning dispositions."""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

LEARNING_DISPOSITION_SCHEMA_VERSION = 3
LEARNING_STAGES = (
    "learning_audit",
    "meta_research",
    "self_improvement",
)
DISPOSITIONS = frozenset({"artifact", "no_change"})
MAX_RATIONALE_CHARS = 600
MAX_REFS = 8
MAX_REF_CHARS = 160
MAX_FEEDBACK_ROWS = 3

_ID = r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}"
_EVIDENCE_REF = re.compile(rf"^(stage|finding):({_ID})$")
_ARTIFACT_REF = re.compile(
    rf"^(?:lesson:({_ID})|memory-distillation:({_ID})|"
    rf"goal:({_ID})(?::(progress|closed))?|mutation:({_ID}))$"
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _current_evidence_refs(data: Mapping[str, Any]) -> set[str]:
    refs = {
        f"stage:{stage_id}"
        for row in data.get("cognitive_stages") or ()
        if isinstance(row, Mapping)
        and (stage_id := _text(row.get("stage_id")))
    }
    refs.update(
        f"finding:{finding_id}"
        for row in data.get("findings") or ()
        if isinstance(row, Mapping)
        and (finding_id := _text(row.get("id")))
    )
    return refs


def _declared_artifact_refs(data: Mapping[str, Any]) -> set[str]:
    refs = {
        f"lesson:{lesson_id}"
        for row in data.get("lessons") or ()
        if isinstance(row, Mapping)
        and (lesson_id := _text(row.get("lesson_id")))
    }
    distillation = data.get("memory_distillation")
    if isinstance(distillation, Mapping):
        distillation_id = _text(distillation.get("distillation_id"))
        if distillation_id:
            refs.add(f"memory-distillation:{distillation_id}")
    for row in data.get("goal_observations") or ():
        if not isinstance(row, Mapping):
            continue
        mode = _text(row.get("mode"))
        goal = row.get("goal")
        goal_id = (
            _text(goal.get("goal_id"))
            if mode == "create" and isinstance(goal, Mapping)
            else _text(row.get("goal_id"))
        )
        if not goal_id:
            continue
        suffix = (
            "" if mode == "create"
            else ":progress" if mode == "progress"
            else ":closed" if mode == "close"
            else None
        )
        if suffix is not None:
            refs.add(f"goal:{goal_id}{suffix}")
    mutation = data.get("mutation")
    if isinstance(mutation, Mapping):
        mutation_id = _text(mutation.get("mutation_id"))
        if mutation_id:
            refs.add(f"mutation:{mutation_id}")
    return refs


def _prior_artifact_refs(
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    refs: set[str] = set()
    for record in records:
        record_type = record.get("record_type")
        payload = record.get("payload")
        if record_type == "lesson":
            lesson_id = _text(
                payload.get("lesson_id") if isinstance(payload, Mapping)
                else None
            )
            if lesson_id:
                refs.add(f"lesson:{lesson_id}")
        elif record_type == "memory_distillation":
            distillation_id = _text(
                payload.get("distillation_id")
                if isinstance(payload, Mapping)
                else None
            )
            if distillation_id:
                refs.add(f"memory-distillation:{distillation_id}")
        elif record_type == "cycle_receipt" and isinstance(payload, Mapping):
            self_improvement = payload.get("self_improvement")
            if isinstance(self_improvement, Mapping):
                refs.update(
                    f"mutation:{mutation_id}"
                    for value in self_improvement.get("mutation_ids") or ()
                    if (mutation_id := _text(value))
                )
    return refs


def validate_learning_dispositions(
    value: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Validate schema-v3 dispositions before any cycle record is appended."""
    if not isinstance(value, list):
        return ["learning_dispositions_required"]

    errors: list[str] = []
    rows_by_stage: dict[str, Mapping[str, Any]] = {}
    current_evidence = _current_evidence_refs(data)
    declared_artifacts = _declared_artifact_refs(data)
    prior_artifacts = _prior_artifact_refs(records)

    reused = sorted(declared_artifacts.intersection(prior_artifacts))
    errors.extend(f"learning_artifact_id_reused:{ref}" for ref in reused)

    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            errors.append(f"learning_disposition_invalid:{index}:not_object")
            continue
        stage_id = _text(row.get("stage_id"))
        if stage_id not in LEARNING_STAGES:
            errors.append(
                f"learning_disposition_invalid:{index}:unknown_stage:"
                f"{stage_id or 'missing'}"
            )
        elif stage_id in rows_by_stage:
            errors.append(
                f"learning_disposition_invalid:{index}:duplicate_stage:"
                f"{stage_id}"
            )
        else:
            rows_by_stage[stage_id] = row

        allowed_fields = {
            "stage_id", "disposition", "rationale", "evidence",
            "artifact_refs",
        }
        for field in sorted(set(row) - allowed_fields):
            errors.append(
                f"learning_disposition_invalid:{index}:unexpected_field:"
                f"{field}"
            )

        disposition = _text(row.get("disposition"))
        if disposition not in DISPOSITIONS:
            errors.append(
                f"learning_disposition_invalid:{index}:disposition")
        rationale = _text(row.get("rationale"))
        if not rationale:
            errors.append(
                f"learning_disposition_invalid:{index}:rationale_empty")
        elif len(rationale) > MAX_RATIONALE_CHARS:
            errors.append(
                f"learning_disposition_invalid:{index}:rationale_too_long")

        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(
                f"learning_disposition_evidence_invalid:{index}:required")
        elif len(evidence) > MAX_REFS:
            errors.append(
                f"learning_disposition_evidence_invalid:{index}:too_many")
        else:
            seen_evidence: set[str] = set()
            for ref_index, raw_ref in enumerate(evidence):
                ref = _text(raw_ref)
                if (
                    not ref
                    or len(ref) > MAX_REF_CHARS
                    or _EVIDENCE_REF.fullmatch(ref) is None
                ):
                    errors.append(
                        "learning_disposition_evidence_invalid:"
                        f"{index}:invalid_ref:{ref_index}"
                    )
                    continue
                if ref in seen_evidence:
                    errors.append(
                        "learning_disposition_evidence_invalid:"
                        f"{index}:duplicate_ref:{ref}"
                    )
                seen_evidence.add(ref)
                if ref not in current_evidence:
                    errors.append(
                        "learning_disposition_evidence_invalid:"
                        f"{index}:dangling_ref:{ref}"
                    )

        artifact_refs = row.get("artifact_refs")
        if disposition == "no_change":
            if "artifact_refs" in row:
                errors.append(
                    f"learning_disposition_artifact_invalid:{index}:"
                    "forbidden_for_no_change"
                )
            continue
        if disposition != "artifact":
            continue
        if not isinstance(artifact_refs, list) or not artifact_refs:
            errors.append(
                f"learning_disposition_artifact_invalid:{index}:required")
            continue
        if len(artifact_refs) > MAX_REFS:
            errors.append(
                f"learning_disposition_artifact_invalid:{index}:too_many")
            continue
        seen_artifacts: set[str] = set()
        for ref_index, raw_ref in enumerate(artifact_refs):
            ref = _text(raw_ref)
            match = (
                _ARTIFACT_REF.fullmatch(ref)
                if ref and len(ref) <= MAX_REF_CHARS
                else None
            )
            if match is None:
                errors.append(
                    "learning_disposition_artifact_invalid:"
                    f"{index}:invalid_ref:{ref_index}"
                )
                continue
            if ref in seen_artifacts:
                errors.append(
                    "learning_disposition_artifact_invalid:"
                    f"{index}:duplicate_ref:{ref}"
                )
            seen_artifacts.add(ref)
            if ref not in declared_artifacts:
                errors.append(
                    "learning_disposition_artifact_invalid:"
                    f"{index}:dangling_ref:{ref}"
                )
            if ref.startswith("mutation:") and stage_id != "self_improvement":
                errors.append(
                    "learning_disposition_artifact_invalid:"
                    f"{index}:mutation_wrong_stage"
                )

    for stage_id in LEARNING_STAGES:
        if stage_id not in rows_by_stage:
            errors.append(f"learning_disposition_missing_stage:{stage_id}")
    return sorted(set(errors))


def _record_caused_by(
    record_id: str,
    ancestor_id: str,
    records: Sequence[Mapping[str, Any]],
) -> bool:
    by_id = {
        _text(record.get("record_id")): record
        for record in records
        if _text(record.get("record_id"))
    }
    pending = [record_id]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        record = by_id.get(current)
        if not isinstance(record, Mapping):
            continue
        causes = {
            _text(cause)
            for cause in record.get("caused_by") or ()
            if _text(cause)
        }
        if ancestor_id in causes:
            return True
        pending.extend(causes - seen)
    return False


def _artifact_record_id(
    ref: str,
    *,
    cycle_id: str,
    records: Sequence[Mapping[str, Any]],
) -> str | None:
    match = _ARTIFACT_REF.fullmatch(ref)
    if match is None:
        return None
    lesson_id, distillation_id, goal_id, goal_event, mutation_id = (
        match.groups()
    )
    if lesson_id:
        return f"lesson:{lesson_id}"
    if distillation_id:
        for record in records:
            payload = record.get("payload")
            if (
                record.get("record_type") == "memory_distillation"
                and isinstance(payload, Mapping)
                and _text(payload.get("distillation_id")) == distillation_id
            ):
                return _text(record.get("record_id")) or None
        return None
    if goal_id:
        if goal_event is None:
            return f"goal:{goal_id}"
        suffix = "progress" if goal_event == "progress" else "closed"
        return f"goal:{goal_id}:{suffix}:{cycle_id}"
    if mutation_id:
        return None
    return None


def _persisted_evidence_refs(
    cycle_id: str,
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    refs: set[str] = set()
    for record in records:
        payload = record.get("payload")
        if (
            record.get("record_type") != "cycle_stage"
            or not isinstance(payload, Mapping)
            or _text(payload.get("cycle_id")) != cycle_id
        ):
            continue
        stage_id = _text(payload.get("stage_id"))
        if stage_id:
            refs.add(f"stage:{stage_id}")
        if stage_id != "decision":
            continue
        output = payload.get("output")
        if not isinstance(output, Mapping):
            continue
        refs.update(
            f"finding:{finding_id}"
            for row in output.get("findings") or ()
            if isinstance(row, Mapping)
            and (finding_id := _text(row.get("id")))
        )
    return refs


def disposition_reconciliation_errors(
    receipt_record: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Prove persisted dispositions and artifacts belong to one v3 receipt."""
    receipt = receipt_record.get("payload")
    if not isinstance(receipt, Mapping):
        return ["learning_disposition_receipt_payload_invalid"]
    cycle_id = _text(receipt.get("cycle_id"))
    receipt_id = _text(receipt_record.get("record_id"))
    if not cycle_id or receipt_id != f"cycle-receipt:{cycle_id}":
        return ["learning_disposition_receipt_identity_invalid"]

    evidence_refs = _persisted_evidence_refs(cycle_id, records)
    mutation_ids = {
        _text(value)
        for value in (
            receipt.get("self_improvement", {}).get("mutation_ids", ())
            if isinstance(receipt.get("self_improvement"), Mapping)
            else ()
        )
        if _text(value)
    }
    errors: list[str] = []
    for stage_id in LEARNING_STAGES:
        record_id = f"learning-disposition:{cycle_id}:{stage_id}"
        matches = [
            record for record in records
            if record.get("record_id") == record_id
        ]
        if len(matches) != 1:
            errors.append(
                f"learning_disposition_record_count:{stage_id}:{len(matches)}")
            continue
        record = matches[0]
        payload = record.get("payload")
        if record.get("record_type") != "learning_disposition":
            errors.append(
                f"learning_disposition_record_type:{stage_id}")
            continue
        if receipt_id not in (record.get("caused_by") or ()):
            errors.append(
                f"learning_disposition_receipt_cause_missing:{stage_id}")
        if not isinstance(payload, Mapping):
            errors.append(
                f"learning_disposition_payload_invalid:{stage_id}")
            continue
        if payload.get("host_input_schema_version") != (
                LEARNING_DISPOSITION_SCHEMA_VERSION):
            errors.append(
                f"learning_disposition_schema_version:{stage_id}")
        if payload.get("stage_id") != stage_id:
            errors.append(f"learning_disposition_stage_mismatch:{stage_id}")
        disposition = payload.get("disposition")
        artifact_refs = payload.get("artifact_refs")
        if disposition == "no_change":
            if artifact_refs not in ([], ()):
                errors.append(
                    f"learning_disposition_no_change_has_artifacts:{stage_id}")
        elif disposition == "artifact":
            if not isinstance(artifact_refs, list) or not artifact_refs:
                errors.append(
                    f"learning_disposition_artifacts_missing:{stage_id}")
        else:
            errors.append(
                f"learning_disposition_value_invalid:{stage_id}")
        rationale = _text(payload.get("rationale"))
        if not rationale or len(rationale) > MAX_RATIONALE_CHARS:
            errors.append(
                f"learning_disposition_rationale_invalid:{stage_id}")
        evidence = payload.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(
                f"learning_disposition_evidence_missing:{stage_id}")
        else:
            for ref in evidence:
                if _text(ref) not in evidence_refs:
                    errors.append(
                        "learning_disposition_evidence_unresolved:"
                        f"{stage_id}:{ref}"
                    )
        if not isinstance(artifact_refs, list):
            continue
        for ref_value in artifact_refs:
            ref = _text(ref_value)
            mutation_match = _ARTIFACT_REF.fullmatch(ref)
            mutation_id = (
                mutation_match.group(5)
                if mutation_match is not None
                else None
            )
            if mutation_id:
                if (
                    stage_id != "self_improvement"
                    or mutation_id not in mutation_ids
                ):
                    errors.append(
                        "learning_disposition_artifact_unresolved:"
                        f"{stage_id}:{ref}"
                    )
                continue
            artifact_record_id = _artifact_record_id(
                ref, cycle_id=cycle_id, records=records)
            if (
                artifact_record_id is None
                or not _record_caused_by(
                    artifact_record_id, receipt_id, records)
            ):
                errors.append(
                    "learning_disposition_artifact_unresolved:"
                    f"{stage_id}:{ref}"
                )
    return sorted(set(errors))


def submitted_disposition_reconciliation_errors(
    data: Mapping[str, Any],
    receipt_record: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Reconcile submitted refs before any disposition record is appended."""
    receipt = receipt_record.get("payload")
    if not isinstance(receipt, Mapping):
        return ["learning_disposition_receipt_payload_invalid"]
    cycle_id = _text(receipt.get("cycle_id"))
    receipt_id = _text(receipt_record.get("record_id"))
    if not cycle_id or receipt_id != f"cycle-receipt:{cycle_id}":
        return ["learning_disposition_receipt_identity_invalid"]
    evidence_refs = _persisted_evidence_refs(cycle_id, records)
    mutation_ids = {
        _text(value)
        for value in (
            receipt.get("self_improvement", {}).get("mutation_ids", ())
            if isinstance(receipt.get("self_improvement"), Mapping)
            else ()
        )
        if _text(value)
    }
    errors: list[str] = []
    for row in data.get("learning_stage_dispositions") or ():
        if not isinstance(row, Mapping):
            continue
        stage_id = _text(row.get("stage_id"))
        for ref_value in row.get("evidence") or ():
            ref = _text(ref_value)
            if ref not in evidence_refs:
                errors.append(
                    "learning_disposition_evidence_unresolved:"
                    f"{stage_id}:{ref}"
                )
        for ref_value in row.get("artifact_refs") or ():
            ref = _text(ref_value)
            match = _ARTIFACT_REF.fullmatch(ref)
            mutation_id = match.group(5) if match is not None else None
            if mutation_id:
                if (
                    stage_id != "self_improvement"
                    or mutation_id not in mutation_ids
                ):
                    errors.append(
                        "learning_disposition_artifact_unresolved:"
                        f"{stage_id}:{ref}"
                    )
                continue
            artifact_record_id = _artifact_record_id(
                ref, cycle_id=cycle_id, records=records)
            if (
                artifact_record_id is None
                or not _record_caused_by(
                    artifact_record_id, receipt_id, records)
            ):
                errors.append(
                    "learning_disposition_artifact_unresolved:"
                    f"{stage_id}:{ref}"
                )
    return sorted(set(errors))


def persist_learning_dispositions(
    data: Mapping[str, Any],
    journal: Any,
    receipt: Mapping[str, Any],
) -> None:
    """Append deterministic disposition records after their artifacts exist."""
    if receipt.get("host_input_schema_version") != (
            LEARNING_DISPOSITION_SCHEMA_VERSION):
        return
    rows = data.get("learning_stage_dispositions")
    if not isinstance(rows, list):
        raise ValueError("learning_dispositions_missing_after_validation")
    cycle_id = _text(receipt.get("cycle_id"))
    receipt_id = f"cycle-receipt:{cycle_id}"
    records = journal.read()
    receipt_record = next(
        (
            record for record in records
            if record.get("record_id") == receipt_id
        ),
        None,
    )
    if not isinstance(receipt_record, Mapping):
        raise ValueError(
            f"learning_disposition_receipt_missing:{receipt_id}")

    reconciliation_errors = submitted_disposition_reconciliation_errors(
        data, receipt_record, records)
    if reconciliation_errors:
        raise ValueError(
            "learning_disposition_reconciliation_failed:"
            + ",".join(reconciliation_errors)
        )

    by_stage = {
        _text(row.get("stage_id")): row
        for row in rows
        if isinstance(row, Mapping)
    }
    for stage_id in LEARNING_STAGES:
        row = by_stage[stage_id]
        artifact_refs = list(row.get("artifact_refs") or ())
        payload = {
            "host_input_schema_version": LEARNING_DISPOSITION_SCHEMA_VERSION,
            "stage_id": stage_id,
            "disposition": row["disposition"],
            "rationale": row["rationale"],
            "evidence": list(row["evidence"]),
            "artifact_refs": artifact_refs,
        }
        record_id = f"learning-disposition:{cycle_id}:{stage_id}"
        existing = next(
            (
                record for record in records
                if record.get("record_id") == record_id
            ),
            None,
        )
        if existing is not None:
            raise ValueError(
                f"learning_disposition_record_reused:{record_id}")
        journal.append(
            record_id=record_id,
            record_type="learning_disposition",
            agent="sovereign-host",
            caused_by=(receipt_id,),
            payload=payload,
        )
        records = journal.read()

    errors = disposition_reconciliation_errors(
        receipt_record, journal.read())
    if errors:
        raise ValueError(
            "learning_disposition_reconciliation_failed:"
            + ",".join(errors)
        )


def learning_disposition_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a bounded newest-first projection for host feedback."""
    rows = []
    for record in records:
        if record.get("record_type") != "learning_disposition":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        rows.append({
            "record_id": record.get("record_id"),
            "stage_id": payload.get("stage_id"),
            "disposition": payload.get("disposition"),
            "rationale": payload.get("rationale"),
            "evidence": list(payload.get("evidence") or ()),
            "artifact_refs": list(payload.get("artifact_refs") or ()),
        })
    recent = rows[-MAX_FEEDBACK_ROWS:]
    return {
        "count": len(rows),
        "recent": recent,
        "not_shown": max(0, len(rows) - len(recent)),
        "what_this_means": (
            "Schema-v3 learning stages must persist an artifact produced by "
            "the same cycle or an evidence-backed no-change disposition."
        ),
    }
