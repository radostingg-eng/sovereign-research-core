"""Strict role-specific contracts for optional research workers."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

ROLE_OUTPUT_CONTRACT_VERSION = 3
SUPPORTED_ROLE_OUTPUT_CONTRACT_VERSIONS = frozenset({1, 2, 3})
FALSIFICATION_FIELD = "falsification_conditions"
FALSIFICATION_FIELDS = frozenset({
    "claim",
    "condition",
    "evidence_needed",
})
FALSIFICATION_TEXT_LIMIT = 600
LEADS_FIELD = "leads"
MAX_LEADS = 3
LEAD_FIELDS = frozenset({
    "instrument_or_theme",
    "strategy_family",
    "mechanism_or_thesis",
    "why_now",
    "strongest_primary_evidence",
    "strongest_counterevidence",
    "cheap_test",
    "novelty_vs_existing",
})
LEAD_TEXT_LIMIT = 600
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
    "discovery": frozenset({LEADS_FIELD}),
}
ROLE_STRING_FIELDS = {
    "primary_frame": frozenset(),
    "evidence_map": frozenset(),
    "adversarial_challenge": frozenset(),
    "independent_synthesis": frozenset({"independent_conclusion"}),
    "deep_research": frozenset(),
    "discovery": frozenset(),
}


def _fold_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _fold_strategy_family(value: str) -> str:
    return re.sub(r"[\s-]+", "_", _fold_text(value))


def discovery_identity_key(instrument: Any, strategy_family: Any) -> str:
    """Normalize instrument + strategy_family the same way the opportunity
    ledger folds identities, so a discovery lead can be compared for
    duplication against existing ledger identities."""
    instrument_text = instrument.strip() if isinstance(instrument, str) else ""
    family_text = (
        strategy_family.strip() if isinstance(strategy_family, str) else ""
    )
    return f"{_fold_text(instrument_text)}::{_fold_strategy_family(family_text)}"


def _existing_identity_keys(
    existing_identities: Sequence[Mapping[str, Any]] | None,
) -> frozenset[str]:
    if not existing_identities:
        return frozenset()
    keys = set()
    for entry in existing_identities:
        if not isinstance(entry, Mapping):
            continue
        keys.add(discovery_identity_key(
            entry.get("instrument"),
            entry.get("strategy_family"),
        ))
    return frozenset(keys)


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
    if (
        type(contract_version) is not int
        or contract_version not in SUPPORTED_ROLE_OUTPUT_CONTRACT_VERSIONS
    ):
        raise ValueError(
            f"azure_worker_output_contract_invalid:{contract_version}"
        )
    common = COMMON_FIELDS if contract_version >= 2 else COMMON_FIELDS_V1
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
    properties = {}
    for field in sorted(fields):
        if field in string_fields:
            properties[field] = {"type": "string"}
        elif field == FALSIFICATION_FIELD:
            properties[field] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        item_field: {"type": "string"}
                        for item_field in sorted(FALSIFICATION_FIELDS)
                    },
                    "required": sorted(FALSIFICATION_FIELDS),
                    "additionalProperties": False,
                },
            }
        elif field == LEADS_FIELD:
            properties[field] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        lead_field: {"type": "string"}
                        for lead_field in sorted(LEAD_FIELDS)
                    },
                    "required": sorted(LEAD_FIELDS),
                    "additionalProperties": False,
                },
            }
        else:
            properties[field] = {
                "type": "array",
                "items": {"type": "string"},
            }
    if role == "independent_synthesis" and contract_version >= 3:
        for field in ("agreements", "disagreements"):
            properties[field]["description"] = (
                "Must be empty: no peer worker output is supplied."
            )
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(fields),
        "additionalProperties": False,
    }


def role_result_validation_errors(
    value: Any,
    *,
    role: str,
    contract_version: int = ROLE_OUTPUT_CONTRACT_VERSION,
    existing_identities: Sequence[Mapping[str, Any]] | None = None,
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
    list_fields = (
        expected - string_fields - {FALSIFICATION_FIELD, LEADS_FIELD}
    )
    for field in list_fields:
        items = value.get(field)
        if (
            not isinstance(items, list)
            or any(not _text(item) for item in items)
        ):
            errors.append(field)
    if LEADS_FIELD in expected:
        leads = value.get(LEADS_FIELD)
        if not isinstance(leads, list) or len(leads) > MAX_LEADS:
            errors.append(LEADS_FIELD)
        else:
            existing_keys = _existing_identity_keys(existing_identities)
            seen_keys: set[str] = set()
            for index, lead in enumerate(leads):
                if (
                    not isinstance(lead, Mapping)
                    or set(lead) != LEAD_FIELDS
                    or any(
                        not _text(lead.get(field))
                        or len(str(lead[field])) > LEAD_TEXT_LIMIT
                        for field in LEAD_FIELDS
                    )
                ):
                    errors.append(f"{LEADS_FIELD}:{index}")
                    continue
                key = discovery_identity_key(
                    lead["instrument_or_theme"],
                    lead["strategy_family"],
                )
                if key in seen_keys:
                    errors.append(f"{LEADS_FIELD}:{index}:duplicate")
                seen_keys.add(key)
                if key in existing_keys:
                    errors.append(
                        f"{LEADS_FIELD}:{index}:duplicate_of_existing"
                    )
    if role == "independent_synthesis" and contract_version >= 3:
        if any(
            isinstance(value.get(field), list) and value[field]
            for field in ("agreements", "disagreements")
        ):
            errors.append("unattributed_peer_comparison")
    if contract_version >= 2:
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
        if field == LEADS_FIELD:
            role_output[field] = [
                {
                    lead_field: str(lead[lead_field])[:LEAD_TEXT_LIMIT]
                    for lead_field in sorted(LEAD_FIELDS)
                }
                for lead in value[field][:MAX_LEADS]
            ]
        elif field in ROLE_STRING_FIELDS[role]:
            role_output[field] = str(value[field])[:1200]
        else:
            role_output[field] = _bounded_list(value[field])
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
    elif role == "discovery":
        compatibility = {
            "hypotheses": [
                f"{lead['instrument_or_theme']} / {lead['strategy_family']}"
                for lead in value[LEADS_FIELD]
            ],
            "evidence_needed": [
                lead["cheap_test"] for lead in value[LEADS_FIELD]
            ],
            "counterevidence": [
                lead["strongest_counterevidence"]
                for lead in value[LEADS_FIELD]
            ],
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
    if contract_version >= 2:
        digest[FALSIFICATION_FIELD] = [
            {
                field: str(condition[field])[:FALSIFICATION_TEXT_LIMIT]
                for field in sorted(FALSIFICATION_FIELDS)
            }
            for condition in value[FALSIFICATION_FIELD][:3]
        ]
    return digest
