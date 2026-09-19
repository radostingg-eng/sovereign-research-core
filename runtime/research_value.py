"""Descriptive research census and explicit adversarial dispute records."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .accepted_inputs import input_fingerprint
from .input_artifacts import load_input_data
from .audit_store import AuditJournal
from .calibration_dataset import empirical_calibration_summary
from .integrity import order_chain
from .schema_versions import STRUCTURED_FULL_CYCLE_VERSIONS

MAX_DISPUTES_PER_CYCLE = 8
MAX_DISPUTED_CLAIMS = 8
MAX_TEXT_CHARS = 600
MAX_FEEDBACK_ROWS = 12
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _bounded(value: Any, limit: int = 240) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def _ordered_records(
    records: Sequence[Mapping[str, Any]],
    record_type: str,
) -> list[Mapping[str, Any]]:
    ordered, failures = order_chain(records)
    rows = ordered if not failures else list(records)
    return [
        row for row in rows
        if row.get("record_type") == record_type
        and isinstance(row.get("payload"), Mapping)
    ]


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
    decision_anchor = f"cycle-stage:{cycle_id}:decision"
    anchors.update({
        f"finding:{finding_id}": decision_anchor
        for row in data.get("findings") or ()
        if isinstance(row, Mapping)
        and (finding_id := _text(row.get("id")))
    })
    return anchors


def _known_opportunities(
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> set[str]:
    known = {
        _text(row["payload"].get("opportunity_id"))
        for row in _ordered_records(records, "opportunity_event")
        if _text(row["payload"].get("opportunity_id"))
    }
    updates = data.get("opportunity_updates")
    updates = updates if isinstance(updates, list) else []
    known.update(
        _text(row.get("opportunity_id"))
        for row in updates
        if isinstance(row, Mapping) and _text(row.get("opportunity_id"))
    )
    return known


def validate_adversarial_disputes(
    disputes: Any,
    *,
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    if disputes is None:
        return []
    if data.get("host_input_schema_version") not in (
            STRUCTURED_FULL_CYCLE_VERSIONS):
        return ["adversarial_disputes_require_schema_v3"]
    if not isinstance(disputes, list):
        return ["adversarial_disputes_must_be_a_list"]
    if len(disputes) > MAX_DISPUTES_PER_CYCLE:
        return ["adversarial_disputes_too_many"]
    anchors = _current_evidence_anchors(data)
    known_opportunities = _known_opportunities(data, records)
    existing = {
        _text(row["payload"].get("dispute_id"))
        for row in _ordered_records(records, "adversarial_dispute")
        if _text(row["payload"].get("dispute_id"))
    }
    expected = {
        "dispute_id",
        "opportunity_id",
        "emerging_position",
        "adversarial_position",
        "disputed_claims",
        "governance_resolution",
        "final_decision_changed",
        "evidence",
    }
    seen = set()
    errors = []
    for index, row in enumerate(disputes):
        if not isinstance(row, Mapping):
            errors.append(f"adversarial_dispute_invalid:{index}:object")
            continue
        if set(row) != expected:
            errors.append(f"adversarial_dispute_invalid:{index}:fields")
        dispute_id = _text(row.get("dispute_id"))
        if not dispute_id or _SAFE_ID.fullmatch(dispute_id) is None:
            errors.append(f"adversarial_dispute_invalid:{index}:id")
        elif dispute_id in seen or dispute_id in existing:
            errors.append(
                f"adversarial_dispute_duplicate_id:{index}:{dispute_id}"
            )
        seen.add(dispute_id)
        opportunity_id = row.get("opportunity_id")
        if opportunity_id is not None and (
            not _text(opportunity_id)
            or _text(opportunity_id) not in known_opportunities
        ):
            errors.append(
                f"adversarial_dispute_opportunity_invalid:{index}"
            )
        for field in (
            "emerging_position",
            "adversarial_position",
            "governance_resolution",
        ):
            text = _text(row.get(field))
            if not text or len(text) > MAX_TEXT_CHARS:
                errors.append(
                    f"adversarial_dispute_invalid:{index}:{field}"
                )
        if not isinstance(row.get("final_decision_changed"), bool):
            errors.append(
                f"adversarial_dispute_invalid:{index}:final_decision_changed"
            )
        claims = row.get("disputed_claims")
        if (
            not isinstance(claims, list)
            or not claims
            or len(claims) > MAX_DISPUTED_CLAIMS
        ):
            errors.append(
                f"adversarial_dispute_claims_invalid:{index}:count"
            )
        else:
            claim_ids = set()
            for claim_index, claim in enumerate(claims):
                if not isinstance(claim, Mapping) or set(claim) != {
                    "claim_id",
                    "emerging_claim",
                    "adversarial_claim",
                }:
                    errors.append(
                        "adversarial_dispute_claims_invalid:"
                        f"{index}:{claim_index}:fields"
                    )
                    continue
                claim_id = _text(claim.get("claim_id"))
                if (
                    not claim_id
                    or _SAFE_ID.fullmatch(claim_id) is None
                    or claim_id in claim_ids
                ):
                    errors.append(
                        "adversarial_dispute_claims_invalid:"
                        f"{index}:{claim_index}:id"
                    )
                claim_ids.add(claim_id)
                for field in ("emerging_claim", "adversarial_claim"):
                    text = _text(claim.get(field))
                    if not text or len(text) > MAX_TEXT_CHARS:
                        errors.append(
                            "adversarial_dispute_claims_invalid:"
                            f"{index}:{claim_index}:{field}"
                        )
        evidence = row.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or len(evidence) > 8
        ):
            errors.append(
                f"adversarial_dispute_evidence_invalid:{index}:count"
            )
        else:
            refs = set()
            for evidence_index, raw in enumerate(evidence):
                ref = _text(raw)
                if not (
                    ref.startswith("stage:")
                    or ref.startswith("finding:")
                ):
                    errors.append(
                        "adversarial_dispute_evidence_invalid:"
                        f"{index}:invalid:{evidence_index}"
                    )
                elif ref in refs:
                    errors.append(
                        "adversarial_dispute_evidence_invalid:"
                        f"{index}:duplicate:{ref}"
                    )
                elif ref not in anchors:
                    errors.append(
                        "adversarial_dispute_evidence_invalid:"
                        f"{index}:dangling:{ref}"
                    )
                refs.add(ref)
    return sorted(set(errors))


def _dispute_payload(
    row: Mapping[str, Any],
    data: Mapping[str, Any],
) -> dict[str, Any]:
    anchors = _current_evidence_anchors(data)
    evidence = [_text(ref) for ref in row.get("evidence") or ()]
    return {
        "schema_version": 1,
        "dispute_id": _text(row.get("dispute_id")),
        "cycle_id": _text(data.get("cycle_id")),
        "opportunity_id": (
            _text(row.get("opportunity_id"))
            if row.get("opportunity_id") is not None
            else None
        ),
        "emerging_position": _text(row.get("emerging_position")),
        "adversarial_position": _text(row.get("adversarial_position")),
        "disputed_claims": [
            dict(claim) for claim in row.get("disputed_claims") or ()
        ],
        "governance_resolution": _text(
            row.get("governance_resolution")),
        "final_decision_changed": row.get("final_decision_changed"),
        "evidence": evidence,
        "evidence_record_ids": list(dict.fromkeys(
            anchors[ref] for ref in evidence
        )),
        "sampling_independence": "single_host_role_execution",
    }


def persist_adversarial_disputes(
    data: Mapping[str, Any],
    journal: AuditJournal,
    receipt: Mapping[str, Any],
) -> int:
    disputes = data.get("adversarial_disputes")
    if not isinstance(disputes, list) or not disputes:
        return 0
    records = journal.read()
    cycle_id = _text(receipt.get("cycle_id"))
    existing = {
        _text(row.get("record_id")): row
        for row in records
        if row.get("record_type") == "adversarial_dispute"
    }
    new_rows = []
    for row in disputes:
        dispute_id = _text(row.get("dispute_id"))
        record_id = f"adversarial-dispute:{dispute_id}"
        prior = existing.get(record_id)
        if prior is None:
            new_rows.append(row)
            continue
        payload = _dispute_payload(row, data)
        caused_by = [
            f"cycle-receipt:{cycle_id}",
            f"cycle-stage:{cycle_id}:adversarial",
            f"cycle-stage:{cycle_id}:governance_review",
            f"cycle-stage:{cycle_id}:decision",
            *payload["evidence_record_ids"],
        ]
        if (
            prior.get("payload") != payload
            or prior.get("caused_by") != list(dict.fromkeys(caused_by))
        ):
            raise ValueError(
                f"adversarial_dispute_payload_mismatch:{record_id}"
            )
    if not new_rows:
        return 0
    validation_records = [
        record for record in records
        if not (
            record.get("record_type") == "adversarial_dispute"
            and isinstance(record.get("payload"), Mapping)
            and _text(record["payload"].get("cycle_id")) == cycle_id
        )
    ]
    errors = validate_adversarial_disputes(
        new_rows, data=data, records=validation_records)
    if errors:
        raise ValueError(
            "adversarial_dispute_reconciliation_failed:"
            + ",".join(errors)
        )
    added = 0
    for row in new_rows:
        dispute_id = _text(row.get("dispute_id"))
        record_id = f"adversarial-dispute:{dispute_id}"
        payload = _dispute_payload(row, data)
        caused_by = [
            f"cycle-receipt:{cycle_id}",
            f"cycle-stage:{cycle_id}:adversarial",
            f"cycle-stage:{cycle_id}:governance_review",
            f"cycle-stage:{cycle_id}:decision",
            *payload["evidence_record_ids"],
        ]
        caused_by = list(dict.fromkeys(caused_by))
        prior = existing.get(record_id)
        if prior is not None:
            if (
                prior.get("payload") != payload
                or prior.get("caused_by") != caused_by
            ):
                raise ValueError(
                    f"adversarial_dispute_payload_mismatch:{record_id}"
                )
            continue
        journal.append(
            record_id=record_id,
            record_type="adversarial_dispute",
            agent="sovereign-host",
            caused_by=caused_by,
            payload=payload,
        )
        added += 1
    return added


def backfill_adversarial_disputes(
    inputs: Sequence[Path],
    journal: AuditJournal,
) -> int:
    candidates = {}
    for path in inputs:
        try:
            import json
            data = load_input_data(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("adversarial_disputes"), list)
            or not data.get("adversarial_disputes")
        ):
            continue
        cycle_id = _text(data.get("cycle_id"))
        if cycle_id:
            candidates[(cycle_id, input_fingerprint(data))] = data
    records = journal.read()
    added = 0
    for record in _ordered_records(records, "cycle_receipt"):
        payload = record["payload"]
        cycle_id = _text(payload.get("cycle_id"))
        snapshot_id = _text(payload.get("snapshot_id"))
        fingerprint = (
            snapshot_id.rsplit(":", 1)[-1] if ":" in snapshot_id else ""
        )
        data = candidates.get((cycle_id, fingerprint))
        if data is not None:
            added += persist_adversarial_disputes(
                data, journal, payload)
    return added


def _research_rows(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for record in _ordered_records(records, "cycle_stage"):
        payload = record["payload"]
        if payload.get("agent_id") != "decision":
            continue
        output = payload.get("output")
        output = output if isinstance(output, Mapping) else {}
        snapshot = output.get("ex_ante_snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        for research in snapshot.get("evidence") or ():
            if isinstance(research, Mapping):
                rows.append({
                    "cycle_id": payload.get("cycle_id"),
                    "question": _text(research.get("question")),
                    "strategy_family": research.get("strategy_family"),
                    "specialist_stage_id":
                        research.get("specialist_stage_id"),
                })
    return rows


def research_value_census(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    research = _research_rows(records)
    normalized_questions = Counter(
        " ".join(row["question"].casefold().split())
        for row in research if row["question"]
    )
    provenance = [
        call
        for record in _ordered_records(records, "tool_provenance")
        for call in record["payload"].get("calls") or ()
        if isinstance(call, Mapping)
    ]
    hashes = Counter(
        _text(call.get("result_sha256"))
        for call in provenance if _text(call.get("result_sha256"))
    )
    tools = Counter(
        _text(call.get("tool"))
        for call in provenance if _text(call.get("tool"))
    )
    origins = Counter(
        _text(call.get("result_origin"))
        for call in provenance if _text(call.get("result_origin"))
    )
    receipts = [
        row["payload"] for row in _ordered_records(records, "cycle_receipt")
    ]
    statuses = [
        _text(row.get("decision_status")) for row in receipts
        if _text(row.get("decision_status"))
    ]
    changes = sum(
        current != previous
        for previous, current in zip(statuses, statuses[1:])
    )
    disputes = [
        row["payload"]
        for row in _ordered_records(records, "adversarial_dispute")
    ]
    calibration = empirical_calibration_summary(records)
    opportunity_rows = [
        row["payload"] for row in _ordered_records(
            records, "opportunity_event")
    ]
    latest_opportunities = {}
    for row in opportunity_rows:
        opportunity_id = _text(row.get("opportunity_id"))
        if opportunity_id:
            latest_opportunities[opportunity_id] = row
    missing_open = 0
    uncertainty_open = 0
    for row in latest_opportunities.values():
        state = row.get("research_state")
        state = state if isinstance(state, Mapping) else {}
        missing_open += sum(
            item.get("status") == "open"
            for item in state.get("missing_information") or ()
            if isinstance(item, Mapping)
        )
        uncertainty_open += sum(
            item.get("status") == "open"
            for item in state.get("uncertainties") or ()
            if isinstance(item, Mapping)
        )
    unique_hashes = len(hashes)
    result_count = sum(hashes.values())
    dispute_count = len(disputes)
    changed_count = sum(
        row.get("final_decision_changed") is True for row in disputes)
    return {
        "cycles_examined": len(receipts),
        "research_rows": len(research),
        "specialist_stage_ids": dict(sorted(Counter(
            _text(row.get("specialist_stage_id")) or "unknown"
            for row in research
        ).items())),
        "questions": {
            "unique": len(normalized_questions),
            "repeated": sum(
                count - 1 for count in normalized_questions.values()
                if count > 1
            ),
            "top_repeated": [
                {"question": question, "count": count}
                for question, count in normalized_questions.most_common(8)
                if count > 1
            ],
        },
        "source_tool_use": {
            "call_count": len(provenance),
            "tools": dict(sorted(tools.items())),
            "origins": dict(sorted(origins.items())),
        },
        "result_novelty": {
            "observations": result_count,
            "unique_result_hashes": unique_hashes,
            "repeated_result_observations": result_count - unique_hashes,
            "unique_rate": (
                unique_hashes / result_count if result_count else None
            ),
        },
        "decision_statuses": {
            "counts": dict(sorted(Counter(statuses).items())),
            "consecutive_changes": changes,
        },
        "adversarial_disputes": {
            "count": dispute_count,
            "decision_changed_count": changed_count,
            "decision_change_rate": (
                changed_count / dispute_count if dispute_count else None
            ),
            "sampling_independence": "single_host_role_execution",
            "recent": [{
                "dispute_id": row.get("dispute_id"),
                "opportunity_id": row.get("opportunity_id"),
                "final_decision_changed":
                    row.get("final_decision_changed"),
                "governance_resolution": _bounded(
                    row.get("governance_resolution")),
            } for row in reversed(disputes[-MAX_FEEDBACK_ROWS:])],
        },
        "dimensions": {
            "thesis_quality": {
                "forecast_rows": calibration["forecast_rows"],
                "measured_forecasts":
                    calibration["forecast_outcome_calibration"]["n"],
                "status": "descriptive_only",
            },
            "evidence_quality": {
                "connector_response_count":
                    origins.get("connector_response", 0),
                "host_summary_count": origins.get("host_summary", 0),
                "unique_result_hash_rate": (
                    unique_hashes / result_count if result_count else None
                ),
                "status": "descriptive_only",
            },
            "valuation_attractiveness": {
                "structured_observations": 0,
                "status": "not_structured",
            },
            "portfolio_fit": {
                "completed_stages": sum(
                    row["payload"].get("agent_id") == "portfolio_fit"
                    for row in _ordered_records(records, "cycle_stage")
                ),
                "status": "stage_completion_only",
            },
            "timing": {
                "groups": calibration["timing_groups"],
                "status": "unit_stratified",
            },
            "implementation_quality": {
                **calibration["implementation"],
                "status": "descriptive_only",
            },
            "expected_risk_reward": {
                "forecast_rows": calibration["forecast_rows"],
                "status": "forecast_contract_coverage",
            },
            "uncertainty": {
                "open_missing_information": missing_open,
                "open_uncertainties": uncertainty_open,
                "status": "ledger_counts",
            },
        },
        "what_this_means": (
            "Descriptive census only. Result-hash uniqueness is not evidence "
            "quality, stage completion is not portfolio-fit quality, and no "
            "dimension is collapsed into an investment or source ranking. "
            "Adversarial stages currently run as roles in one host thread, "
            "not independently sampled agents."
        ),
    }
