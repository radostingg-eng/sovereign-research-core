"""Strict role-specific contracts for optional research workers."""
from __future__ import annotations

from typing import Any, Mapping

ROLE_OUTPUT_CONTRACT_VERSION = 1
COMMON_FIELDS = frozenset({
    "summary",
    "uncertainties",
    "suggested_next_question",
})
ROLE_SPECIFIC_FIELDS = {
    "primary_frame": frozenset({
        "hypotheses",
        "evidence_needed",
        "counterevidence",
    }),
    "evidence_map": frozenset({
        "claims_to_verify",
        "primary_sources",
        "evidence_gaps",
        "conflict_checks",
    }),
    "adversarial_challenge": frozenset({
        "challenged_claims",
        "disconfirming_evidence_needed",
        "failure_modes",
        "alternative_explanations",
    }),
    "independent_synthesis": frozenset({
        "agreements",
        "disagreements",
        "independent_conclusion",
        "arbitration_questions",
    }),
}
ROLE_STRING_FIELDS = {
    "primary_frame": frozenset(),
    "evidence_map": frozenset(),
    "adversarial_challenge": frozenset(),
    "independent_synthesis": frozenset({"independent_conclusion"}),
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def role_result_fields(role: str) -> frozenset[str]:
    specific = ROLE_SPECIFIC_FIELDS.get(role)
    if specific is None:
        raise ValueError(f"azure_worker_role_invalid:{role}")
    return COMMON_FIELDS | specific


def role_result_schema(role: str) -> dict[str, Any]:
    fields = role_result_fields(role)
    string_fields = {
        "summary",
        "suggested_next_question",
        *ROLE_STRING_FIELDS[role],
    }
    return {
        "type": "object",
        "properties": {
            field: (
                {"type": "string"}
                if field in string_fields
                else {
                    "type": "array",
                    "items": {"type": "string"},
                }
            )
            for field in sorted(fields)
        },
        "required": sorted(fields),
        "additionalProperties": False,
    }


def role_result_validation_errors(
    value: Any,
    *,
    role: str,
) -> list[str]:
    if not isinstance(value, Mapping):
        return ["not_object"]
    expected = role_result_fields(role)
    errors = []
    if set(value) != expected:
        errors.append("fields")
    string_fields = {
        "summary",
        "suggested_next_question",
        *ROLE_STRING_FIELDS[role],
    }
    for field in string_fields:
        if not _text(value.get(field)):
            errors.append(field)
    for field in expected - string_fields:
        items = value.get(field)
        if (
            not isinstance(items, list)
            or any(not _text(item) for item in items)
        ):
            errors.append(field)
    return sorted(set(errors))


def _bounded_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:600] for item in value[:3]]


def role_result_digest(
    value: Any,
    *,
    role: str,
) -> dict[str, Any] | None:
    if role_result_validation_errors(value, role=role):
        return None
    assert isinstance(value, Mapping)
    role_output = {}
    for field in sorted(ROLE_SPECIFIC_FIELDS[role]):
        role_output[field] = (
            str(value[field])[:1200]
            if field in ROLE_STRING_FIELDS[role]
            else _bounded_list(value[field])
        )
    if role == "primary_frame":
        compatibility = {
            "hypotheses": value["hypotheses"],
            "evidence_needed": value["evidence_needed"],
            "counterevidence": value["counterevidence"],
        }
    elif role == "evidence_map":
        compatibility = {
            "hypotheses": value["claims_to_verify"],
            "evidence_needed": [
                *value["primary_sources"],
                *value["evidence_gaps"],
            ],
            "counterevidence": value["conflict_checks"],
        }
    elif role == "adversarial_challenge":
        compatibility = {
            "hypotheses": value["challenged_claims"],
            "evidence_needed": value["disconfirming_evidence_needed"],
            "counterevidence": [
                *value["failure_modes"],
                *value["alternative_explanations"],
            ],
        }
    else:
        compatibility = {
            "hypotheses": value["agreements"],
            "evidence_needed": value["arbitration_questions"],
            "counterevidence": value["disagreements"],
        }
    return {
        "role": role,
        "summary": str(value["summary"])[:1200],
        "suggested_next_question": str(
            value["suggested_next_question"]
        )[:600],
        "hypotheses": _bounded_list(compatibility["hypotheses"]),
        "evidence_needed": _bounded_list(
            compatibility["evidence_needed"]
        ),
        "counterevidence": _bounded_list(
            compatibility["counterevidence"]
        ),
        "uncertainties": _bounded_list(value["uncertainties"]),
        "role_output": role_output,
    }
