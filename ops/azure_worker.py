"""Run one optional Azure research worker into an external outbox."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
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
    validate_inbox_record,
)

DEFAULT_STALE_MINUTES = 180
DEFAULT_MAX_OUTPUT_TOKENS = 1200
DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"
FORBIDDEN_TARGET_KEYS = frozenset({
    "account",
    "account_id",
    "cash",
    "net_liquidation_value",
    "positions",
    "quantity",
})


def _az_cli() -> str:
    return os.environ.get("SOVEREIGN_AZURE_CLI_BIN", "az")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def select_target(feedback: Mapping[str, Any]) -> dict[str, Any] | None:
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
    selected = dict(candidates[0])
    selected["candidate_count"] = len(candidates)
    selected["selection_rule"] = (
        "lexicographically_lowest_identity_fingerprint_then_question_id"
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


def build_request(target: Mapping[str, Any]) -> dict[str, Any]:
    if _target_has_forbidden_key(target):
        raise ValueError("azure_worker_target_contains_private_account_data")
    instructions = (
        "You are an independent investment research worker with no broker "
        "access and no authority to create, modify, delete, or transmit "
        "orders. Analyze only the supplied research question. Separate "
        "durable reasoning from facts requiring fresh external evidence. "
        "Return strict JSON with keys summary, hypotheses, evidence_needed, "
        "counterevidence, uncertainties, and suggested_next_question."
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
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
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
        raise PermissionError(
            "azure_token_failed:"
            + result.stderr.strip()[:300]
        )
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
        raise PermissionError(
            "azure_api_key_failed:"
            + result.stderr.strip()[:300]
        )
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
        raise PermissionError(
            "azure_key_vault_secret_failed:"
            + result.stderr.strip()[:300]
        )
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


def _parse_model_result(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"summary": text[:12_000]}
    if isinstance(value, Mapping):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) <= 16_000:
            return dict(value)
    return {"summary": text[:12_000]}


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
    timeout_seconds: int = 90,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
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
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        status = (
            "quota_exhausted" if error.code == 429
            else "auth_error" if error.code in {401, 403}
            else "model_error"
        )
        raise RuntimeError(f"{status}:{error.code}:{detail}") from error
    except URLError as error:
        raise RuntimeError(f"model_error:network:{error.reason}") from error
    value = json.loads(raw)
    text = _response_text(value)
    if not text:
        raise RuntimeError("model_error:empty_response")
    return _parse_model_result(text), value


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
    now: datetime | None = None,
    caller: Callable[..., tuple[dict[str, Any], Mapping[str, Any]]] = (
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
    }
    try:
        feedback = json.loads(Path(feedback_path).read_text(encoding="utf-8"))
        target = select_target(feedback)
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
            })
        request_body = build_request(target)
        request_sha = hashlib.sha256(
            json.dumps(
                request_body,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        result, raw = caller(
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
                "endpoint": endpoint,
                "deployment": deployment,
                "subscription_id": subscription_id,
                "auth_mode": auth_mode,
            },
            "selection": {
                "rule": target["selection_rule"],
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
            "request": {
                "sha256": request_sha,
                "max_output_tokens": request_body["max_output_tokens"],
            },
            "result": result,
            "response": {
                "sha256": response_sha,
                "response_id": raw.get("id"),
                "model": raw.get("model"),
                "usage": raw.get("usage"),
            },
        })
    except PermissionError as error:
        status = "auth_error"
        detail = str(error)
    except RuntimeError as error:
        detail = str(error)
        status = (
            detail.split(":", 1)[0]
            if detail.split(":", 1)[0] in {
                "quota_exhausted",
                "auth_error",
                "model_error",
            }
            else "model_error"
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        status = "configuration_error"
        detail = str(error)
    return _write_record(Path(outbox_dir), {
        **base,
        "status": status,
        "error": {
            "code": status,
            "message": detail[:1000],
        },
    })


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
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
