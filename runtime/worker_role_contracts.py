"""Strict role-specific contracts for optional research workers."""
from __future__ import annotations

from typing import Any, Mapping

ROLE_OUTPUT_CONTRACT_VERSION = 2
SUPPORTED_ROLE_OUTPUT_CONTRACT_VERSIONS = frozenset({1, 2})
FALSIFICATION_FIELD = "falsification_conditions"
FALSIFICATION_FIELDS = frozenset({
    "claim",
    "condition",
    "evidence_needed",
})
FALSIFICATION_TEXT_LIMIT = 600
COMMON_FIELDS_V1 = frozenset({
    "summary",
    "uncertainties",
    "suggested_next_question",
})
COMMON_FIELDS = COMMON_FIELDS_V1 | {FALSIFICATION_FIELD}
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
    "deep_research": frozenset({
        "investigation_chain",
        "primary_evidence_targets",
        "second_order_effects",
        "shallow_stop_flags",
    }),
}
ROLE_STRING_FIELDS = {
    "primary_frame": frozenset(),
    "evidence_map": frozenset(),
    "adversarial_challenge": frozenset(),
    "independent_synthesis": frozenset({"independent_conclusion"}),
    "deep_research": frozenset(),
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def role_result_fields(
    role: str,
    *,
    contract_version: int = ROLE_OUTPUT_CONTRACT_VERSION,
) -> frozenset[str]:
    specific = ROLE_SPECIFIC_FIELDS.get(role)
    if specific is None:
        raise ValueError(f"azure_worker_role_invalid:{role}")
    if contract_version not in SUPPORTED_ROLE_OUTPUT_CONTRACT_VERSIONS:
        raise ValueError(
            f"azure_worker_output_contract_invalid:{contract_version}"
        )
    common = COMMON_FIELDS if contract_version == 2 else COMMON_FIELDS_V1
    return common | specific


def role_result_schema(
    role: str,
    *,
    contract_version: int = ROLE_OUTPUT_CONTRACT_VERSION,
) -> dict[str, Any]:
    fields = role_result_fields(
        role,
        contract_version=contract_version,
    )
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
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            item_field: {
                                "type": "string",
                                "maxLength": FALSIFICATION_TEXT_LIMIT,
                            }
                            for item_field in sorted(FALSIFICATION_FIELDS)
                        },
                        "required": sorted(FALSIFICATION_FIELDS),
                        "additionalProperties": False,
                    },
                }
                if field == FALSIFICATION_FIELD
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
    contract_version: int = ROLE_OUTPUT_CONTRACT_VERSION,
) -> list[str]:
    if not isinstance(value, Mapping):
        return ["not_object"]
    expected = role_result_fields(
        role,
        contract_version=contract_version,
    )
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
    list_fields = expected - string_fields - {FALSIFICATION_FIELD}
    for field in list_fields:
        items = value.get(field)
        if (
            not isinstance(items, list)
            or any(not _text(item) for item in items)
        ):
            errors.append(field)
    if contract_version == 2:
        conditions = value.get(FALSIFICATION_FIELD)
        if not isinstance(conditions, list) or not conditions:
            errors.append(FALSIFICATION_FIELD)
        else:
            for index, condition in enumerate(conditions):
                if (
                    not isinstance(condition, Mapping)
                    or set(condition) != FALSIFICATION_FIELDS
                    or any(
                        not _text(condition.get(field))
                        or len(str(condition[field])) >
                        FALSIFICATION_TEXT_LIMIT
                        for field in FALSIFICATION_FIELDS
                    )
                ):
                    errors.append(
                        f"{FALSIFICATION_FIELD}:{index}"
                    )
    return sorted(set(errors))


def _bounded_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:600] for item in value[:3]]


def role_result_digest(
    value: Any,
    *,
    role: str,
    contract_version: int = ROLE_OUTPUT_CONTRACT_VERSION,
) -> dict[str, Any] | None:
    if role not in ROLE_SPECIFIC_FIELDS:
        return None
    if role_result_validation_errors(
        value,
        role=role,
        contract_version=contract_version,
    ):
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
    elif role == "independent_synthesis":
        compatibility = {
            "hypotheses": value["agreements"],
            "evidence_needed": value["arbitration_questions"],
            "counterevidence": value["disagreements"],
        }
    elif role == "deep_research":
        compatibility = {
            "hypotheses": [],
            "evidence_needed": value["primary_evidence_targets"],
            "counterevidence": [],
        }
    else:
        return None
    digest = {
        "role": role,
        "output_contract_version": contract_version,
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
    if contract_version == 2:
        digest[FALSIFICATION_FIELD] = [
            {
                field: str(condition[field])[:FALSIFICATION_TEXT_LIMIT]
                for field in sorted(FALSIFICATION_FIELDS)
            }
            for condition in value[FALSIFICATION_FIELD][:3]
        ]
    return digest
