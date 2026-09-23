"""Run one optional Azure research worker into an external outbox."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.research_inbox import (
    RESEARCH_INBOX_SCHEMA_VERSION,
    normalize_worker_telemetry,
    safe_worker_telemetry,
    validate_inbox_record,
)
from runtime.worker_role_contracts import (
    ROLE_OUTPUT_CONTRACT_VERSION,
    role_result_schema,
    role_result_validation_errors,
)

DEFAULT_STALE_MINUTES = 180
DEFAULT_MAX_OUTPUT_TOKENS = 8000
MAX_RETRY_OUTPUT_TOKENS = 16000
DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"
ROLE_INSTRUCTIONS = {
    "primary_frame": (
        "Build the strongest decision-relevant research frame."
    ),
    "evidence_map": (
        "Map the exact primary evidence needed to resolve the question."
    ),
    "adversarial_challenge": (
        "Attack the leading thesis and prioritize disconfirming evidence."
    ),
    "independent_synthesis": (
        "Form an independent synthesis and identify disagreements worth "
        "resolving."
    ),
    "deep_research": (
        "Produce a deep, multi-step investigation: chase the strongest "
        "primary evidence, reason through second-order effects, and flag "
        "where cheaper mini workers would predictably shallow-stop."
    ),
}
FORBIDDEN_TARGET_KEYS = frozenset({
    "account",
    "account_id",
    "cash",
    "net_liquidation_value",
    "positions",
    "quantity",
})
SAFE_RESPONSE_STATUSES = frozenset({
    "completed",
    "failed",
    "in_progress",
    "incomplete",
    "queued",
})
SAFE_INCOMPLETE_REASONS = frozenset({
    "content_filter",
    "max_output_tokens",
})
SAFE_ERROR_MESSAGES = {
    "auth_error": "Azure authentication failed.",
    "configuration_error": "Azure worker configuration failed.",
    "model_error": "Azure/OpenAI model request failed.",
    "quota_exhausted": "Azure/OpenAI request quota was unavailable.",
}
SAFE_ERROR_DETAIL_CODES = frozenset({
    "azure_api_key_failed",
    "azure_key_vault_secret_failed",
    "azure_token_failed",
    "azure_auth_mode_invalid",
    "azure_worker_caller_result_invalid",
    "azure_worker_max_output_tokens_invalid",
    "azure_worker_role_invalid",
    "azure_worker_target_contains_private_account_data",
    "empty_response",
    "invalid_json",
    "invalid_response_shape",
    "invalid_structured_output",
    "io_error",
    "network",
    "quota_exhausted",
})
SAFE_METADATA_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}")


class AzureCallError(RuntimeError):
    def __init__(
        self,
        status: str,
        *,
        http_status: int,
        telemetry: Mapping[str, Any],
    ) -> None:
        super().__init__(status)
        self.status = status
        self.http_status = http_status
        self.telemetry = normalize_worker_telemetry(telemetry)


def _az_cli() -> str:
    return os.environ.get("SOVEREIGN_AZURE_CLI_BIN", "az")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def select_target(
    feedback: Mapping[str, Any],
    *,
    target_offset: int = 0,
    rotation_index: int = 0,
) -> dict[str, Any] | None:
    ledger = feedback.get("opportunity_ledger")
    if not isinstance(ledger, Mapping):
        return None
    if int(ledger.get("not_shown") or 0) != 0:
        return None
    candidates = []
    for item in ledger.get("items") or ():
        if not isinstance(item, Mapping):
            continue
        opportunity_id = _text(item.get("opportunity_id"))
        fingerprint = _text(item.get("identity_fingerprint"))
        state = _text(item.get("state"))
        research_state = item.get("research_state")
        research_state = (
            research_state
            if isinstance(research_state, Mapping)
            else {}
        )
        for question in research_state.get("missing_information") or ():
            if (
                not isinstance(question, Mapping)
                or question.get("status") != "open"
            ):
                continue
            question_id = _text(question.get("id"))
            text = _text(question.get("question"))
            if not opportunity_id or not fingerprint or not question_id or not text:
                continue
            candidates.append({
                "opportunity_id": opportunity_id,
                "identity_fingerprint": fingerprint,
                "opportunity_state": state,
                "question_id": question_id,
                "question": text[:1200],
                "why_it_matters": _text(
                    question.get("why_it_matters")
                )[:800],
            })
    if not candidates:
        return None
    candidates.sort(key=lambda row: (
        row["identity_fingerprint"],
        row["question_id"],
        row["opportunity_id"],
    ))
    selected_index = (target_offset + rotation_index) % len(candidates)
    selected = dict(candidates[selected_index])
    selected["candidate_count"] = len(candidates)
    selected["selection_index"] = selected_index
    selected["selection_rule"] = (
        "rotating_offset_over_lexicographically_sorted_identity_and_question"
    )
    return selected


def _target_has_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in FORBIDDEN_TARGET_KEYS
            or _target_has_forbidden_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_target_has_forbidden_key(child) for child in value)
    return False


def build_request(
    target: Mapping[str, Any],
    *,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    role: str = "primary_frame",
) -> dict[str, Any]:
    if _target_has_forbidden_key(target):
        raise ValueError("azure_worker_target_contains_private_account_data")
    if max_output_tokens <= 0:
        raise ValueError("azure_worker_max_output_tokens_invalid")
    role_instruction = ROLE_INSTRUCTIONS.get(role)
    if role_instruction is None:
        raise ValueError(f"azure_worker_role_invalid:{role}")
    instructions = (
        "You are an independent investment research worker with no broker "
        "access and no authority to create, modify, delete, or transmit "
        "orders. Analyze only the supplied research question. Separate "
        "durable reasoning from facts requiring fresh external evidence. "
        f"{role_instruction} Return concise strict JSON. Keep each list to "
        "the highest-value findings and avoid repeating the question."
    )
    prompt = json.dumps({
        "opportunity_id": target.get("opportunity_id"),
        "question_id": target.get("question_id"),
        "question": target.get("question"),
        "why_it_matters": target.get("why_it_matters"),
        "task": (
            "Develop a bounded research frame that could change the decision. "
            "Do not claim current market facts without cited fresh evidence."
        ),
    }, ensure_ascii=False, sort_keys=True)
    return {
        "instructions": instructions,
        "input": prompt,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": "low"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": f"sovereign_research_{role}",
                "schema": role_result_schema(role),
                "strict": True,
            },
        },
    }


def _access_token(
    subscription_id: str,
    scope: str,
) -> str:
    result = subprocess.run(
        [
            _az_cli(),
            "account",
            "get-access-token",
            "--subscription",
            subscription_id,
            "--scope",
            scope,
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise PermissionError("azure_token_failed")
    return token


def _api_key(
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> str:
    if not resource_group or not account_name:
        raise ValueError("azure_api_key_resource_required")
    result = subprocess.run(
        [
            _az_cli(),
            "cognitiveservices",
            "account",
            "keys",
            "list",
            "--subscription",
            subscription_id,
            "--resource-group",
            resource_group,
            "--name",
            account_name,
            "--query",
            "key1",
            "-o",
            "tsv",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        raise PermissionError("azure_api_key_failed")
    return key


def _key_vault_secret(
    *,
    vault_name: str,
    secret_name: str,
    subscription_id: str,
) -> str:
    if not vault_name or not secret_name or not subscription_id:
        raise ValueError("azure_key_vault_configuration_required")
    result = subprocess.run(
        [
            _az_cli(),
            "keyvault",
            "secret",
            "show",
            "--vault-name",
            vault_name,
            "--name",
            secret_name,
            "--subscription",
            subscription_id,
            "--query",
            "value",
            "-o",
            "tsv",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        raise PermissionError("azure_key_vault_secret_failed")
    return key


def _response_text(value: Mapping[str, Any]) -> str:
    parts = []
    for output in value.get("output") or ():
        if not isinstance(output, Mapping):
            continue
        for content in output.get("content") or ():
            if (
                isinstance(content, Mapping)
                and isinstance(content.get("text"), str)
            ):
                parts.append(content["text"])
    return "".join(parts).strip()


def _result_validation_errors(
    value: Any,
    *,
    role: str = "primary_frame",
) -> list[str]:
    return role_result_validation_errors(value, role=role)


def _parse_model_result(
    text: str,
    *,
    role: str = "primary_frame",
) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if _result_validation_errors(value, role=role):
        return None
    return dict(value)


def _safe_metadata_id(value: Any) -> str | None:
    text = _text(value)
    if not text or SAFE_METADATA_ID.fullmatch(text) is None:
        return None
    return text


def _safe_response_status(value: Any) -> str | None:
    text = _text(value)
    return text if text in SAFE_RESPONSE_STATUSES else None


def _safe_incomplete_reason(value: Any) -> str:
    text = _text(value)
    return text if text in SAFE_INCOMPLETE_REASONS else "unknown"


def _safe_error_detail_code(error: BaseException) -> str:
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    text = str(error)
    for code in SAFE_ERROR_DETAIL_CODES:
        if text == code or text.startswith(code + ":"):
            return code
        if text.startswith("model_error:" + code):
            return code
    if isinstance(error, URLError):
        return "network"
    return "io_error" if isinstance(error, OSError) else "model_error"


def call_azure(
    *,
    endpoint: str,
    deployment: str,
    subscription_id: str,
    token_scope: str,
    request_body: Mapping[str, Any],
    auth_mode: str = "entra",
    resource_group: str = "",
    account_name: str = "",
    key_vault_name: str = "",
    key_secret_name: str = "",
    key_vault_subscription: str = "",
    role: str = "primary_frame",
    timeout_seconds: int = 90,
) -> tuple[
    dict[str, Any] | None,
    Mapping[str, Any],
    Mapping[str, Any],
]:
    body = dict(request_body)
    body["model"] = deployment
    if auth_mode == "entra":
        headers = {
            "Authorization": (
                "Bearer "
                + _access_token(subscription_id, token_scope)
            ),
        }
    elif auth_mode == "azure_cli_key":
        headers = {
            "api-key": _api_key(
                subscription_id,
                resource_group,
                account_name,
            ),
        }
    elif auth_mode == "key_vault":
        headers = {
            "api-key": _key_vault_secret(
                vault_name=key_vault_name,
                secret_name=key_secret_name,
                subscription_id=key_vault_subscription,
            ),
        }
    else:
        raise ValueError(f"azure_auth_mode_invalid:{auth_mode}")
    headers["Content-Type"] = "application/json"
    request = Request(
        endpoint.rstrip("/") + "/openai/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
            response_headers = response.headers
    except HTTPError as error:
        status = (
            "quota_exhausted" if error.code == 429
            else "auth_error" if error.code in {401, 403}
            else "model_error"
        )
        raise AzureCallError(
            status,
            http_status=error.code,
            telemetry=safe_worker_telemetry(
                response_headers=error.headers,
            ),
        ) from error
    except URLError as error:
        raise RuntimeError("model_error:network") from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("model_error:invalid_json") from error
    if not isinstance(value, Mapping):
        raise RuntimeError("model_error:invalid_response_shape")
    text = _response_text(value)
    if not text and value.get("status") != "incomplete":
        raise RuntimeError("model_error:empty_response")
    return (
        _parse_model_result(text, role=role) if text else None,
        value,
        safe_worker_telemetry(
            response_headers=response_headers,
            response_usage=value.get("usage"),
        ),
    )


def _record_id(worker_id: str, observed_at: datetime) -> str:
    stamp = observed_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{worker_id}-{stamp}"


def _write_record(outbox: Path, record: Mapping[str, Any]) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    errors = validate_inbox_record(record, byte_length=len(content))
    if errors:
        raise ValueError(
            "azure_worker_record_invalid:" + ",".join(errors)
        )
    target = outbox / f"{record['record_id']}.json"
    with tempfile.NamedTemporaryFile(
        dir=outbox,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        temp = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    return target


def _call_result(
    value: Any,
) -> tuple[
    dict[str, Any] | None,
    Mapping[str, Any],
    dict[str, Any],
]:
    if not isinstance(value, tuple) or len(value) not in {2, 3}:
        raise ValueError("azure_worker_caller_result_invalid")
    result, raw = value[:2]
    if result is not None and not isinstance(result, Mapping):
        raise ValueError("azure_worker_caller_result_invalid")
    if not isinstance(raw, Mapping):
        raise ValueError("azure_worker_caller_result_invalid")
    telemetry = (
        normalize_worker_telemetry(value[2])
        if len(value) == 3
        else safe_worker_telemetry(response_usage=raw.get("usage"))
    )
    return (
        dict(result) if isinstance(result, Mapping) else None,
        raw,
        telemetry,
    )


def _quality(
    attempts: Sequence[Mapping[str, Any]],
    *,
    result_schema_complete: bool | None,
) -> dict[str, Any]:
    return {
        "result_schema_complete": result_schema_complete,
        "attempt_count": len(attempts),
        "completed_attempt_count": sum(
            attempt.get("outcome") == "completed"
            for attempt in attempts
        ),
        "retried": len(attempts) > 1,
        "truncated": any(
            str(attempt.get("outcome", "")).startswith("incomplete:")
            for attempt in attempts
        ),
    }


def _request_summary(
    *,
    attempts: Sequence[Mapping[str, Any]],
    role: str,
    request_sha: str = "",
    max_output_tokens: Any = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "attempts": list(attempts),
        "role": role,
        "output_contract": {
            "schema_version": ROLE_OUTPUT_CONTRACT_VERSION,
            "role": role,
        },
    }
    if request_sha:
        value["sha256"] = request_sha
    if isinstance(max_output_tokens, int):
        value["max_output_tokens"] = max_output_tokens
    return value


def run_worker(
    *,
    feedback_path: Path | str,
    outbox_dir: Path | str,
    worker_id: str,
    endpoint: str,
    deployment: str,
    subscription_id: str,
    token_scope: str = DEFAULT_TOKEN_SCOPE,
    auth_mode: str = "entra",
    resource_group: str = "",
    account_name: str = "",
    key_vault_name: str = "",
    key_secret_name: str = "",
    key_vault_subscription: str = "",
    stale_minutes: int = DEFAULT_STALE_MINUTES,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    target_offset: int = 0,
    role: str = "primary_frame",
    now: datetime | None = None,
    caller: Callable[
        ...,
        tuple[
            dict[str, Any] | None,
            Mapping[str, Any],
        ] | tuple[
            dict[str, Any] | None,
            Mapping[str, Any],
            Mapping[str, Any],
        ],
    ] = (
        call_azure
    ),
) -> Path:
    observed_at = now or _utc_now()
    record_id = _record_id(worker_id, observed_at)
    base = {
        "schema_version": RESEARCH_INBOX_SCHEMA_VERSION,
        "record_id": record_id,
        "worker_id": worker_id,
        "origin": "worker_attested",
        "observed_at": _iso(observed_at),
        "expires_at": _iso(
            observed_at + timedelta(minutes=stale_minutes)
        ),
        "deployment": {
            "deployment": deployment,
            "subscription_id": subscription_id,
            "auth_mode": auth_mode,
        },
    }
    attempts: list[dict[str, Any]] = []
    latest_telemetry = safe_worker_telemetry()
    result_schema_complete: bool | None = None
    request_body: dict[str, Any] = {}
    request_sha = ""
    try:
        feedback = json.loads(Path(feedback_path).read_text(encoding="utf-8"))
        target = select_target(
            feedback,
            target_offset=target_offset,
            rotation_index=int(observed_at.timestamp() // 3600),
        )
        if target is None:
            return _write_record(Path(outbox_dir), {
                **base,
                "status": "no_work",
                "error": {
                    "code": "no_complete_open_target",
                    "message": (
                        "No complete bounded open missing-information "
                        "projection was available."
                    ),
                },
                "request": _request_summary(
                    attempts=attempts,
                    role=role,
                ),
                "quality": _quality(
                    attempts,
                    result_schema_complete=None,
                ),
                "telemetry": latest_telemetry,
            })
        ceilings = (
            max_output_tokens,
            min(max_output_tokens * 2, MAX_RETRY_OUTPUT_TOKENS),
        )
        result = None
        raw: Mapping[str, Any] = {}
        for attempt_number, ceiling in enumerate(dict.fromkeys(ceilings), 1):
            request_body = build_request(
                target,
                max_output_tokens=ceiling,
                role=role,
            )
            request_sha = hashlib.sha256(
                json.dumps(
                    request_body,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            attempt = {
                "attempt": attempt_number,
                "max_output_tokens": ceiling,
                "request_sha256": request_sha,
                "outcome": "unknown",
                "telemetry": safe_worker_telemetry(),
            }
            attempts.append(attempt)
            try:
                result, raw, latest_telemetry = _call_result(caller(
                    endpoint=endpoint,
                    deployment=deployment,
                    subscription_id=subscription_id,
                    token_scope=token_scope,
                    request_body=request_body,
                    auth_mode=auth_mode,
                    resource_group=resource_group,
                    account_name=account_name,
                    key_vault_name=key_vault_name,
                    key_secret_name=key_secret_name,
                    key_vault_subscription=key_vault_subscription,
                    role=role,
                ))
            except AzureCallError as error:
                latest_telemetry = error.telemetry
                attempt["outcome"] = error.status
                attempt["telemetry"] = latest_telemetry
                raise
            except PermissionError:
                attempt["outcome"] = "auth_error"
                raise
            except RuntimeError as error:
                error_status = str(error).split(":", 1)[0]
                attempt["outcome"] = (
                    error_status
                    if error_status in {
                        "quota_exhausted",
                        "auth_error",
                        "model_error",
                    }
                    else "model_error"
                )
                raise
            except ValueError:
                attempt["outcome"] = "configuration_error"
                raise
            attempt["telemetry"] = latest_telemetry
            response_status = _safe_response_status(
                raw.get("status")
            ) or "completed"
            incomplete = raw.get("incomplete_details")
            incomplete = (
                incomplete
                if isinstance(incomplete, Mapping)
                else {}
            )
            reason = _safe_incomplete_reason(incomplete.get("reason"))
            validation_errors = _result_validation_errors(
                result,
                role=role,
            )
            outcome = (
                f"incomplete:{reason}"
                if response_status == "incomplete"
                else (
                    "invalid_structured_output"
                    if validation_errors
                    else "completed"
                )
            )
            attempt["outcome"] = outcome
            result_schema_complete = not validation_errors
            if outcome == "completed":
                break
        else:
            raise RuntimeError(
                "model_error:"
                + str(attempts[-1]["outcome"])
            )
        response_sha = hashlib.sha256(
            json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return _write_record(Path(outbox_dir), {
            **base,
            "status": "completed",
            "deployment": {
                "deployment": deployment,
                "subscription_id": subscription_id,
                "auth_mode": auth_mode,
            },
            "selection": {
                "rule": target["selection_rule"],
                "index": target["selection_index"],
                "target_offset": target_offset,
                "candidate_count": target["candidate_count"],
                "projection_complete": True,
            },
            "target": {
                key: target[key]
                for key in (
                    "opportunity_id",
                    "identity_fingerprint",
                    "opportunity_state",
                    "question_id",
                    "question",
                    "why_it_matters",
                )
            },
            "request": _request_summary(
                attempts=attempts,
                role=role,
                request_sha=request_sha,
                max_output_tokens=request_body.get("max_output_tokens"),
            ),
            "result": result,
            "quality": _quality(
                attempts,
                result_schema_complete=result_schema_complete,
            ),
            "telemetry": latest_telemetry,
            "response": {
                "sha256": response_sha,
                "response_id": _safe_metadata_id(raw.get("id")),
                "model": _safe_metadata_id(raw.get("model")),
                "status": _safe_response_status(raw.get("status")),
                "usage": latest_telemetry["usage"],
            },
        })
    except AzureCallError as error:
        status = error.status
        detail_code = status
        http_status = error.http_status
    except PermissionError as error:
        status = "auth_error"
        detail_code = _safe_error_detail_code(error)
        http_status = None
    except RuntimeError as error:
        status = (
            str(error).split(":", 1)[0]
            if str(error).split(":", 1)[0] in {
                "quota_exhausted",
                "auth_error",
                "model_error",
            }
            else "model_error"
        )
        detail_code = _safe_error_detail_code(error)
        http_status = None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        status = "configuration_error"
        detail_code = _safe_error_detail_code(error)
        http_status = None
    error_record = {
        **base,
        "status": status,
        "request": _request_summary(
            attempts=attempts,
            role=role,
            request_sha=request_sha,
            max_output_tokens=request_body.get("max_output_tokens"),
        ),
        "quality": _quality(
            attempts,
            result_schema_complete=result_schema_complete,
        ),
        "telemetry": latest_telemetry,
        "error": {
            "code": status,
            "detail_code": detail_code,
            "message": SAFE_ERROR_MESSAGES[status],
        },
    }
    if http_status is not None:
        error_record["error"]["http_status"] = http_status
    return _write_record(Path(outbox_dir), error_record)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--feedback", required=True)
    parser.add_argument("--outbox-dir", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument(
        "--token-scope",
        default=DEFAULT_TOKEN_SCOPE,
    )
    parser.add_argument(
        "--auth-mode",
        choices=("entra", "azure_cli_key", "key_vault"),
        default="entra",
    )
    parser.add_argument("--resource-group", default="")
    parser.add_argument("--account-name", default="")
    parser.add_argument("--key-vault-name", default="")
    parser.add_argument("--key-secret-name", default="")
    parser.add_argument("--key-vault-subscription", default="")
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=DEFAULT_STALE_MINUTES,
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
    )
    parser.add_argument("--target-offset", type=int, default=0)
    parser.add_argument(
        "--role",
        choices=tuple(ROLE_INSTRUCTIONS),
        default="primary_frame",
    )
    args = parser.parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    path = run_worker(
        feedback_path=args.feedback,
        outbox_dir=args.outbox_dir,
        worker_id=args.worker_id,
        endpoint=args.endpoint,
        deployment=args.deployment,
        subscription_id=args.subscription,
        token_scope=args.token_scope,
        auth_mode=args.auth_mode,
        resource_group=args.resource_group,
        account_name=args.account_name,
        key_vault_name=args.key_vault_name,
        key_secret_name=args.key_secret_name,
        key_vault_subscription=args.key_vault_subscription,
        stale_minutes=args.stale_minutes,
        max_output_tokens=args.max_output_tokens,
        target_offset=args.target_offset,
        role=args.role,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
