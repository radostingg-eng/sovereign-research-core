"""Validate and persist host dispositions for projected worker research."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .audit_store import AuditJournal
from .opportunity_ledger import _current_state, _fold
from .research_inbox import load_inbox_record, research_inbox_summary
from .timestamps import parse_iso_timestamp

WORKER_RESEARCH_DISPOSITION_SCHEMA_VERSION = 1
WORKER_RESEARCH_DISPOSITIONS = frozenset({
    "used_as_lead",
    "rejected",
    "deferred",
})
MAX_TEXT_CHARS = 600
MAX_EVIDENCE_REFS = 8
MAX_FEEDBACK_ROWS = 12
MAX_QUESTION_ADOPTIONS = 4
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_EVIDENCE_REF = re.compile(
    r"^(?:stage|finding):[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"
)
_ROW_FIELDS = frozenset({
    "worker_record_id",
    "disposition",
    "evidence",
    "rationale",
    "revisit_condition",
})
_EVIDENCE_KEYS = frozenset({
    "account_orders_tool_call_id",
    "account_trades_tool_call_id",
    "caused_by",
    "evidence",
    "evidence_ids",
    "evidence_refs",
    "evidence_tool_call_ids",
    "rests_on",
    "source_refs",
    "tool_call_id",
})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _source_observed_at(data: Mapping[str, Any]) -> str:
    context = data.get("schedule_context")
    if not isinstance(context, Mapping):
        return ""
    return _text(context.get("source_observed_at"))


def projected_worker_records(
    profile_root: Path | str | None,
    *,
    source_observed_at: str,
) -> list[dict[str, Any]]:
    """Rebuild the completed worker rows visible in the bounded host context."""
    if profile_root is None:
        return []
    observed_at = parse_iso_timestamp(source_observed_at)
    if observed_at is None or observed_at.utcoffset() is None:
        return []
    summary = research_inbox_summary(
        profile_root,
        now=observed_at,
    )
    return [
        dict(row)
        for row in summary.get("items") or ()
        if isinstance(row, Mapping)
        and row.get("status") == "completed"
        and _text(row.get("record_id"))
    ]


def _known_worker_record_ids(
    profile_root: Path | str | None,
) -> set[str]:
    if profile_root is None:
        return set()
    root = Path(profile_root) / "research_inbox"
    ids = set()
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else ():
        try:
            row = load_inbox_record(path)
        except (OSError, ValueError):
            continue
        record_id = _text(row.get("record_id"))
        if record_id:
            ids.add(record_id)
    return ids


def _current_evidence_anchors(
    data: Mapping[str, Any],
) -> dict[str, str]:
    cycle_id = _text(data.get("cycle_id"))
    anchors = {
        f"stage:{stage_id}": f"cycle-stage:{cycle_id}:{stage_id}"
        for row in data.get("cognitive_stages") or ()
        if isinstance(row, Mapping)
        and (stage_id := _text(row.get("stage_id")))
    }
    decision_id = f"cycle-stage:{cycle_id}:decision"
    anchors.update({
        f"finding:{finding_id}": decision_id
        for row in data.get("findings") or ()
        if isinstance(row, Mapping)
        and (finding_id := _text(row.get("id")))
    })
    return anchors


def _worker_authority_errors(
    value: Any,
    *,
    worker_ids: set[str],
    pointer: str = "",
    evidence_context: bool = False,
) -> list[str]:
    errors = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_pointer = f"{pointer}/{key}"
            errors.extend(_worker_authority_errors(
                child,
                worker_ids=worker_ids,
                pointer=child_pointer,
                evidence_context=(
                    evidence_context or key in _EVIDENCE_KEYS
                ),
            ))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_worker_authority_errors(
                child,
                worker_ids=worker_ids,
                pointer=f"{pointer}/{index}",
                evidence_context=evidence_context,
            ))
    elif evidence_context and _text(value) in worker_ids:
        errors.append(
            f"worker_research_record_authority_forbidden:{pointer}"
        )
    return errors


def validate_worker_research_dispositions(
    value: Any,
    *,
    data: Mapping[str, Any],
    profile_root: Path | str | None,
) -> list[str]:
    projected = projected_worker_records(
        profile_root,
        source_observed_at=_source_observed_at(data),
    )
    projected_ids = {
        _text(row.get("record_id"))
        for row in projected
        if _text(row.get("record_id"))
    }
    if value is None:
        return (
            ["worker_research_dispositions_required"]
            if projected_ids
            else []
        )
    if not isinstance(value, list):
        return ["worker_research_dispositions_must_be_a_list"]

    errors: list[str] = []
    seen_ids: set[str] = set()
    anchors = _current_evidence_anchors(data)
    worker_ids = _known_worker_record_ids(profile_root)
    errors.extend(_worker_authority_errors(
        data,
        worker_ids=worker_ids,
    ))
    for index, row in enumerate(value):
        prefix = f"worker_research_disposition_invalid:{index}"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}:object")
            continue
        if set(row) != _ROW_FIELDS:
            errors.append(f"{prefix}:fields")

        worker_record_id = _text(row.get("worker_record_id"))
        if (
            not worker_record_id
            or _SAFE_ID.fullmatch(worker_record_id) is None
        ):
            errors.append(f"{prefix}:worker_record_id")
        elif worker_record_id in seen_ids:
            errors.append(
                "worker_research_disposition_duplicate:"
                f"{worker_record_id}"
            )
        seen_ids.add(worker_record_id)

        disposition = _text(row.get("disposition"))
        if disposition not in WORKER_RESEARCH_DISPOSITIONS:
            errors.append(f"{prefix}:disposition")

        rationale = _text(row.get("rationale"))
        if not rationale or len(rationale) > MAX_TEXT_CHARS:
            errors.append(f"{prefix}:rationale")

        evidence = row.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{prefix}:evidence")
            evidence = []
        elif (
            len(evidence) > MAX_EVIDENCE_REFS
            or len(evidence) != len(set(map(str, evidence)))
        ):
            errors.append(f"{prefix}:evidence")
        if disposition == "used_as_lead" and not evidence:
            errors.append(f"{prefix}:evidence_required")
        for evidence_index, raw_ref in enumerate(evidence):
            ref = _text(raw_ref)
            if (
                not ref
                or _EVIDENCE_REF.fullmatch(ref) is None
                or ref not in anchors
            ):
                errors.append(
                    f"{prefix}:evidence_ref:{evidence_index}"
                )
            if ref in worker_ids:
                errors.append(
                    f"{prefix}:worker_record_is_not_evidence:"
                    f"{evidence_index}"
                )

        revisit = row.get("revisit_condition")
        if disposition == "deferred":
            if (
                not _text(revisit)
                or len(_text(revisit)) > MAX_TEXT_CHARS
            ):
                errors.append(f"{prefix}:revisit_condition")
        elif revisit is not None:
            errors.append(f"{prefix}:revisit_condition_forbidden")

    for record_id in sorted(projected_ids - seen_ids):
        errors.append(
            f"worker_research_disposition_missing:{record_id}"
        )
    for record_id in sorted(seen_ids - projected_ids):
        errors.append(
            f"worker_research_disposition_unexpected:{record_id}"
        )
    return sorted(set(errors))


def _projection_sha256(row: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_id(cycle_id: str, worker_record_id: str) -> str:
    return (
        f"worker-research-disposition:{cycle_id}:"
        f"{worker_record_id}"
    )


def _worker_question_adoptions(
    data: Mapping[str, Any],
    projected_row: Mapping[str, Any],
    *,
    prior_opportunities: Mapping[str, Mapping[str, Any]],
    claimed_questions: set[tuple[str, str]],
) -> tuple[list[dict[str, str]], int]:
    result = projected_row.get("result")
    if not isinstance(result, Mapping):
        return ([], 0)
    proposals: dict[str, tuple[str, str]] = {}
    suggested = _text(result.get("suggested_next_question"))
    if suggested:
        proposals[_fold(suggested)] = (
            "suggested_next_question",
            suggested,
        )
    for index, raw_question in enumerate(result.get("evidence_needed") or ()):
        question = _text(raw_question)
        if question:
            proposals.setdefault(
                _fold(question),
                (f"evidence_needed:{index}", question),
            )
    target = projected_row.get("target")
    target = target if isinstance(target, Mapping) else {}
    proposals.pop(_fold(_text(target.get("question"))), None)

    adoptions = []
    match_count = 0
    for update in data.get("opportunity_updates") or ():
        if not isinstance(update, Mapping):
            continue
        event_id = _text(update.get("event_id"))
        opportunity_id = _text(update.get("opportunity_id"))
        research_state = update.get("research_state")
        if (
            not event_id
            or not opportunity_id
            or not isinstance(research_state, Mapping)
        ):
            continue
        prior = prior_opportunities.get(opportunity_id)
        prior_state = (
            prior.get("research_state")
            if isinstance(prior, Mapping)
            else None
        )
        prior_ids = {
            _text(row.get("id"))
            for row in (
                prior_state.get("missing_information") or ()
                if isinstance(prior_state, Mapping)
                else ()
            )
            if isinstance(row, Mapping)
        }
        for row in research_state.get("missing_information") or ():
            if not isinstance(row, Mapping):
                continue
            question = _text(row.get("question"))
            proposal = proposals.get(_fold(question))
            missing_information_id = _text(row.get("id"))
            if (
                proposal is None
                or not missing_information_id
                or missing_information_id in prior_ids
                or _text(row.get("status")).casefold() != "open"
            ):
                continue
            key = (opportunity_id, missing_information_id)
            if key in claimed_questions:
                continue
            claimed_questions.add(key)
            match_count += 1
            if len(adoptions) >= MAX_QUESTION_ADOPTIONS:
                continue
            source, worker_question = proposal
            adoptions.append({
                "opportunity_id": opportunity_id,
                "opportunity_event_record_id":
                    f"opportunity-event:{event_id}",
                "missing_information_id": missing_information_id,
                "question": question,
                "worker_question": worker_question,
                "worker_question_source": source,
            })
    return (adoptions, match_count)


def worker_research_disposition_record_ids(
    data: Mapping[str, Any],
    *,
    cycle_id: str | None = None,
) -> dict[str, str]:
    resolved_cycle_id = _text(cycle_id) or _text(data.get("cycle_id"))
    return {
        _record_id(
            resolved_cycle_id,
            _text(row.get("worker_record_id")),
        ): "worker_research_disposition"
        for row in data.get("worker_research_dispositions") or ()
        if isinstance(row, Mapping)
        and _text(row.get("worker_record_id"))
    }


def persist_worker_research_dispositions(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
    *,
    profile_root: Path | str,
) -> int:
    if receipt.get("host_input_schema_version") != 4:
        return 0
    rows = data.get("worker_research_dispositions")
    projected = projected_worker_records(
        profile_root,
        source_observed_at=_source_observed_at(data),
    )
    if rows is None and not projected:
        return 0
    errors = validate_worker_research_dispositions(
        rows,
        data=data,
        profile_root=profile_root,
    )
    if errors:
        raise ValueError(
            "worker_research_disposition_reconciliation_failed:"
            + ",".join(errors)
        )
    if not isinstance(rows, list):
        raise ValueError("worker_research_dispositions_missing_after_validation")

    cycle_id = _text(receipt.get("cycle_id"))
    receipt_id = f"cycle-receipt:{cycle_id}"
    records = journal.read()
    by_id = {
        _text(record.get("record_id")): record
        for record in records
        if _text(record.get("record_id"))
    }
    if receipt_id not in by_id:
        raise ValueError(
            f"worker_research_disposition_receipt_missing:{receipt_id}"
        )
    projected_by_id = {
        _text(row.get("record_id")): row
        for row in projected
    }
    prior_opportunities, _identities = _current_state(
        records,
        exclude_cycle_id=cycle_id,
    )
    claimed_questions: set[tuple[str, str]] = set()
    anchors = _current_evidence_anchors(data)
    written = 0
    for row in rows:
        worker_record_id = _text(row.get("worker_record_id"))
        projected_row = projected_by_id[worker_record_id]
        evidence = [_text(ref) for ref in row.get("evidence") or ()]
        evidence_record_ids = []
        for ref in evidence:
            evidence_record_id = anchors.get(ref)
            record = by_id.get(evidence_record_id or "")
            payload = (
                record.get("payload")
                if isinstance(record, Mapping)
                else None
            )
            if (
                not evidence_record_id
                or not isinstance(record, Mapping)
                or record.get("record_type") != "cycle_stage"
                or not isinstance(payload, Mapping)
                or _text(payload.get("cycle_id")) != cycle_id
            ):
                raise ValueError(
                    "worker_research_disposition_evidence_unresolved:"
                    f"{worker_record_id}:{ref}"
                )
            evidence_record_ids.append(evidence_record_id)

        question_adoptions, question_adoption_count = (
            _worker_question_adoptions(
                data,
                projected_row,
                prior_opportunities=prior_opportunities,
                claimed_questions=claimed_questions,
            )
            if row.get("disposition") == "used_as_lead"
            else ([], 0)
        )
        question_event_ids = []
        for adoption in question_adoptions:
            event_record_id = adoption["opportunity_event_record_id"]
            event_record = by_id.get(event_record_id)
            event_payload = (
                event_record.get("payload")
                if isinstance(event_record, Mapping)
                else None
            )
            if (
                not isinstance(event_record, Mapping)
                or event_record.get("record_type") != "opportunity_event"
                or not isinstance(event_payload, Mapping)
                or _text(event_payload.get("cycle_id")) != cycle_id
            ):
                raise ValueError(
                    "worker_question_adoption_event_unresolved:"
                    f"{worker_record_id}:{event_record_id}"
                )
            question_event_ids.append(event_record_id)

        payload = {
            "schema_version":
                WORKER_RESEARCH_DISPOSITION_SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "worker_record_id": worker_record_id,
            "worker_id": projected_row.get("worker_id"),
            "worker_observed_at": projected_row.get("observed_at"),
            "source_observed_at": _source_observed_at(data),
            "projection_sha256": _projection_sha256(projected_row),
            "disposition": row.get("disposition"),
            "evidence": evidence,
            "rationale": row.get("rationale"),
            "revisit_condition": row.get("revisit_condition"),
        }
        if question_adoptions:
            payload["question_adoptions"] = question_adoptions
            payload["question_adoptions_not_shown"] = max(
                0,
                question_adoption_count - len(question_adoptions),
            )
        _record, created = journal.append_idempotent(
            record_id=_record_id(cycle_id, worker_record_id),
            record_type="worker_research_disposition",
            agent="sovereign-host",
            caused_by=list(dict.fromkeys([
                receipt_id,
                *evidence_record_ids,
                *question_event_ids,
            ])),
            payload=payload,
        )
        written += int(created)
        if created:
            by_id[_text(_record.get("record_id"))] = _record
    return written


def worker_research_adoption_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = MAX_FEEDBACK_ROWS,
) -> dict[str, Any]:
    rows = []
    for record in records:
        if record.get("record_type") != "worker_research_disposition":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        rows.append({
            "record_id": record.get("record_id"),
            "cycle_id": payload.get("cycle_id"),
            "worker_record_id": payload.get("worker_record_id"),
            "worker_id": payload.get("worker_id"),
            "disposition": payload.get("disposition"),
            "evidence": list(payload.get("evidence") or ()),
            "rationale": payload.get("rationale"),
            "revisit_condition": payload.get("revisit_condition"),
            "question_adoptions": [
                {
                    key: adoption.get(key)
                    for key in (
                        "opportunity_id",
                        "opportunity_event_record_id",
                        "missing_information_id",
                        "worker_question_source",
                    )
                }
                for adoption in payload.get("question_adoptions") or ()
                if isinstance(adoption, Mapping)
            ],
            "question_adoptions_not_shown": payload.get(
                "question_adoptions_not_shown",
                0,
            ),
            "source_observed_at": payload.get("source_observed_at"),
        })
    counts = {
        disposition: sum(
            row.get("disposition") == disposition
            for row in rows
        )
        for disposition in sorted(WORKER_RESEARCH_DISPOSITIONS)
    }
    recent = rows[-limit:]
    return {
        "total_records": len(rows),
        "counts_by_disposition": counts,
        "question_adoption_count": (
            len({
                (
                    adoption.get("opportunity_id"),
                    adoption.get("missing_information_id"),
                )
                for row in rows
                for adoption in row.get("question_adoptions") or ()
                if isinstance(adoption, Mapping)
            })
            + sum(
                int(row.get("question_adoptions_not_shown") or 0)
                for row in rows
            )
        ),
        "recent": recent,
        "not_shown": max(0, len(rows) - len(recent)),
        "what_this_means": (
            "These measure host handling of optional worker leads. "
            "used_as_lead rows cite independent current-cycle evidence; "
            "question_adoptions link exact worker-proposed questions to "
            "brain-authored opportunity events without granting the worker "
            "authority to mutate the ledger. "
            "worker records are never connector or decision authority."
        ),
    }
