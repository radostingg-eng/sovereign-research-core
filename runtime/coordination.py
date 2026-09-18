"""Deterministic validation for durable helper-agent coordination envelopes.

The LLM owns collaboration decisions. This module only validates the shape,
identity, timestamps, and local references of messages, claims, and acks.
It never submits brokerage orders and never grants authority to a helper agent.
"""
from __future__ import annotations

from .profile_paths import code_root, profile_root
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .engine import canonical_json, sha256_text

MESSAGE_TYPES = frozenset({"request", "finding", "challenge", "handoff", "status", "incident"})
PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
ACK_STATUSES = frozenset({"accepted", "rejected", "needs-more-info"})
CLAIM_STATUSES = frozenset({"active", "completed", "expired", "superseded", "released"})


def _ts(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def envelope_hash(envelope: Mapping[str, Any]) -> str:
    body = dict(envelope)
    body.pop("envelope_hash", None)
    return sha256_text(canonical_json(body))


def _common(envelope: Mapping[str, Any], prefix: str, *, agent_key: str = "agent_id") -> list[str]:
    """Shared envelope checks. Messages and acks use `agent_id`;
    claims use `owner_agent_id` per COORDINATION.md. Making the key a
    parameter avoids demanding both from a claim, which was refusing
    correctly-formed claims as `claim_missing:agent_id`."""
    errors: list[str] = []
    for key in (agent_key, "created_at"):
        if not str(envelope.get(key, "")).strip():
            errors.append(f"{prefix}_missing:{key}")
    try:
        _ts(envelope.get("created_at"))
    except (TypeError, ValueError):
        errors.append(f"{prefix}_invalid:created_at")
    return errors


def validate_message(message: Mapping[str, Any]) -> list[str]:
    errors = _common(message, "message")
    for key in ("message_id", "type", "subject", "body", "priority"):
        if key not in message or (isinstance(message[key], str) and not message[key].strip()):
            errors.append(f"message_missing:{key}")
    if message.get("type") not in MESSAGE_TYPES:
        errors.append(f"message_invalid:type:{message.get('type')}")
    if message.get("priority") not in PRIORITIES:
        errors.append(f"message_invalid:priority:{message.get('priority')}")
    if "caused_by" not in message or not isinstance(message["caused_by"], list):
        errors.append("message_invalid:caused_by")
    if "references" in message and not isinstance(message["references"], list):
        errors.append("message_invalid:references")
    if "envelope_hash" in message and message["envelope_hash"] != envelope_hash(message):
        errors.append("message_hash_mismatch")
    return errors


def validate_claim(claim: Mapping[str, Any], *, now: datetime | None = None) -> list[str]:
    errors = _common(claim, "claim", agent_key="owner_agent_id")
    for key in ("claim_id", "task_id", "owner_agent_id", "expires_at", "scope", "status"):
        if key not in claim or (isinstance(claim[key], str) and not claim[key].strip()):
            errors.append(f"claim_missing:{key}")
    try:
        created = _ts(claim.get("created_at"))
        expires = _ts(claim.get("expires_at"))
        if expires <= created:
            errors.append("claim_expiry_not_after_creation")
        if now is not None and claim.get("status") == "active" and expires <= now.astimezone(timezone.utc):
            errors.append("claim_expired_but_active")
    except (TypeError, ValueError):
        errors.append("claim_invalid:timestamp")
    if claim.get("status") not in CLAIM_STATUSES:
        errors.append(f"claim_invalid:status:{claim.get('status')}")
    if "supersedes" in claim and claim["supersedes"] is not None and not str(claim["supersedes"]).strip():
        errors.append("claim_invalid:supersedes")
    if "parent_request" in claim and claim["parent_request"] is not None and not str(claim["parent_request"]).strip():
        errors.append("claim_invalid:parent_request")
    if "envelope_hash" in claim and claim["envelope_hash"] != envelope_hash(claim):
        errors.append("claim_hash_mismatch")
    return errors


def validate_ack(ack: Mapping[str, Any]) -> list[str]:
    errors = _common(ack, "ack")
    for key in ("ack_id", "message_id", "status"):
        if key not in ack or (isinstance(ack[key], str) and not ack[key].strip()):
            errors.append(f"ack_missing:{key}")
    if ack.get("status") not in ACK_STATUSES:
        errors.append(f"ack_invalid:status:{ack.get('status')}")
    if "comment" in ack and not isinstance(ack["comment"], str):
        errors.append("ack_invalid:comment")
    if "envelope_hash" in ack and ack["envelope_hash"] != envelope_hash(ack):
        errors.append("ack_hash_mismatch")
    return errors


def validate_snapshot(
    *,
    messages: Iterable[Mapping[str, Any]] = (),
    claims: Iterable[Mapping[str, Any]] = (),
    acks: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> list[str]:
    """Validate a coordination snapshot and its local references."""
    errors: list[str] = []
    messages = list(messages)
    claims = list(claims)
    acks = list(acks)

    collections = (("message", messages, validate_message), ("claim", claims, validate_claim), ("ack", acks, validate_ack))
    id_keys = {"message": "message_id", "claim": "claim_id", "ack": "ack_id"}
    for name, collection, validator in collections:
        seen: set[str] = set()
        for item in collection:
            errors.extend(validator(item, now=now) if name == "claim" else validator(item))
            ident = str(item.get(id_keys[name], ""))
            if ident and ident in seen:
                errors.append(f"duplicate_{name}_id:{ident}")
            seen.add(ident)

    message_ids = {str(x.get("message_id")) for x in messages}
    claim_ids = {str(x.get("claim_id")) for x in claims}
    for ack in acks:
        mid = str(ack.get("message_id", ""))
        if mid and mid not in message_ids:
            errors.append(f"ack_missing_message:{mid}")
    for claim in claims:
        parent = claim.get("parent_request")
        if parent is not None and str(parent) and str(parent) not in message_ids:
            errors.append(f"claim_missing_parent_request:{parent}")
        supersedes = claim.get("supersedes")
        if supersedes is not None and str(supersedes) and str(supersedes) not in claim_ids:
            errors.append(f"claim_missing_superseded_claim:{supersedes}")
    for message in messages:
        for parent in message.get("caused_by", []):
            if str(parent) not in message_ids:
                errors.append(f"message_missing_cause:{parent}")
    return errors


def with_hash(envelope: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(envelope)
    result["envelope_hash"] = envelope_hash(result)
    return result


def _load_envelopes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for item in sorted(path.glob("*.json")):
        try:
            value = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"coordination_file_unreadable:{item}:{error}") from error
        if not isinstance(value, Mapping):
            raise ValueError(f"coordination_file_not_object:{item}")
        rows.append(dict(value))
    return rows


def main(argv: list[str] | None = None) -> int:
    """Validate every committed helper-agent envelope."""
    argv = sys.argv[1:] if argv is None else argv
    root = (
        Path(argv[0]).resolve()
        if argv
        else profile_root() / "coordination"
    )
    try:
        messages = _load_envelopes(root / "messages")
        claims = _load_envelopes(root / "claims")
        acks = _load_envelopes(root / "acks")
    except ValueError as error:
        print(error)
        return 1
    errors = validate_snapshot(messages=messages, claims=claims, acks=acks)
    if errors:
        for error in errors:
            print(f"COORDINATION INVALID: {error}")
        return 1
    print(
        "coordination: valid "
        f"({len(messages)} messages, {len(claims)} claims, {len(acks)} acks)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
