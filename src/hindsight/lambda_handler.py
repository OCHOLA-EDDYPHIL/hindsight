"""AWS Lambda entrypoint for the Hindsight incident agent."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3

from hindsight.aws import aws_client_config
from hindsight.agent import (
    IncidentAgentResult,
    IncidentInput,
    resume_incident_agent,
    run_incident_agent,
)
from hindsight.embeddings import DeterministicEmbeddingProvider
from hindsight.reasoning import reasoning_provider_from_env, retrying_reasoning_provider
from hindsight.security import safe_error_detail
from hindsight.tracing import configure_tracing_from_env

DATABASE_URL_PARAM_ENV = "HINDSIGHT_DATABASE_URL_PARAM"
GEMINI_API_KEY_PARAM_ENV = "HINDSIGHT_GEMINI_API_KEY_PARAM"
FUNCTION_AUTH_TOKEN_PARAM_ENV = "HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM"
FUNCTION_AUTH_TOKEN_ENV = "HINDSIGHT_FUNCTION_AUTH_TOKEN"
REASONING_MAX_ATTEMPTS_ENV = "REASONING_MAX_ATTEMPTS"
MAX_BODY_BYTES = 64 * 1024
MAX_USER_INPUT_CHARS = 8_000
MAX_IDENTIFIER_CHARS = 128
MAX_TITLE_CHARS = 200
MAX_SERVICE_CHARS = 64
MAX_METADATA_BYTES = 8 * 1024

_COLD_START = True
_SETTINGS_CACHE: RuntimeSettings | None = None
_AUTH_TOKEN_CACHE: str | None = None


@dataclass(frozen=True)
class RuntimeSettings:
    """Lambda runtime configuration after resolving SSM parameters."""

    database_url: str
    provider_env: dict[str, str]
    reasoning_max_attempts: int = 2


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda handler compatible with Function URL events."""

    configure_tracing_from_env(service_name="hindsight-agent")
    return handle_request(event, context)


def handle_request(
    event: dict[str, Any],
    context: Any,
    *,
    settings: RuntimeSettings | None = None,
    auth_token: str | None = None,
    auth_ssm_client: Any | None = None,
) -> dict[str, Any]:
    """Handle one start/resume request and return a Function URL response."""

    global _COLD_START
    started = time.perf_counter()
    cold_start = _COLD_START
    _COLD_START = False
    route = _route(event)

    try:
        supplied_token = _authorization_bearer_token(event)
        if supplied_token is None:
            return _error_response(401, "missing bearer token", started, cold_start, route)
        expected_token = auth_token or function_auth_token(ssm_client=auth_ssm_client)
        if not hmac.compare_digest(supplied_token, expected_token):
            return _error_response(403, "invalid bearer token", started, cold_start, route)
        if _http_method(event) != "POST":
            return _error_response(405, "method not allowed", started, cold_start, route)
        if route not in {"/incident/resume", "/incident", "/incident/start", "/"}:
            return _error_response(404, f"Unsupported route: {route}", started, cold_start, route)
        payload = _json_body(event)
        _validate_payload(route, payload)
        resolved_settings = settings or runtime_settings()
        provider = retrying_reasoning_provider(
            reasoning_provider_from_env(resolved_settings.provider_env),
            max_attempts=resolved_settings.reasoning_max_attempts,
        )
        if route == "/incident/resume":
            result = _resume(payload, resolved_settings=resolved_settings, provider=provider)
        else:
            result = _start(payload, resolved_settings=resolved_settings, provider=provider)
        response = _success_response(result, started, cold_start)
        _log_turn(
            route=route,
            status_code=response["statusCode"],
            elapsed_ms=response["_elapsed_ms"],
            cold_start=cold_start,
            result=result,
        )
        response.pop("_elapsed_ms", None)
        return response
    except ValueError as exc:
        return _error_response(400, str(exc), started, cold_start, route)
    except Exception as exc:
        return _error_response(500, "incident agent request failed", started, cold_start, route, exc)


def function_auth_token(
    *,
    environ: Mapping[str, str] | None = None,
    ssm_client: Any | None = None,
    use_cache: bool = True,
) -> str:
    """Resolve the bearer token required for Lambda Function URL calls."""

    global _AUTH_TOKEN_CACHE
    env = os.environ if environ is None else environ
    if use_cache and environ is None and ssm_client is None and _AUTH_TOKEN_CACHE is not None:
        return _AUTH_TOKEN_CACHE

    client = ssm_client
    if client is None and env.get(FUNCTION_AUTH_TOKEN_PARAM_ENV):
        client = _ssm_client(env)
    token = _secret_value(
        env=env,
        client=client,
        param_env=FUNCTION_AUTH_TOKEN_PARAM_ENV,
        fallback_env=FUNCTION_AUTH_TOKEN_ENV,
    )
    if use_cache and environ is None and ssm_client is None:
        _AUTH_TOKEN_CACHE = token
    return token


def runtime_settings(
    *,
    environ: Mapping[str, str] | None = None,
    ssm_client: Any | None = None,
    use_cache: bool = True,
) -> RuntimeSettings:
    """Resolve Lambda runtime settings from env and SSM Parameter Store."""

    global _SETTINGS_CACHE
    env = os.environ if environ is None else environ
    if use_cache and environ is None and ssm_client is None and _SETTINGS_CACHE is not None:
        return _SETTINGS_CACHE

    client = ssm_client
    if client is None and _needs_ssm(env):
        client = _ssm_client(env)
    database_url = _database_url_for_lambda(
        _secret_value(
            env=env,
            client=client,
            param_env=DATABASE_URL_PARAM_ENV,
            fallback_env="DATABASE_URL",
        )
    )
    provider_env = {
        "LLM_PROVIDER": env.get("LLM_PROVIDER", "gemini"),
        "GEMINI_MODEL": env.get("GEMINI_MODEL", ""),
        "BEDROCK_MODEL": env.get("BEDROCK_MODEL", ""),
        "AWS_REGION": env.get("AWS_REGION", ""),
        "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION", ""),
    }
    if provider_env["LLM_PROVIDER"].strip().lower() == "gemini":
        gemini_key = _optional_secret_value(
            env=env,
            client=client,
            param_env=GEMINI_API_KEY_PARAM_ENV,
            fallback_env="GEMINI_API_KEY",
        )
        if gemini_key:
            provider_env["GEMINI_API_KEY"] = gemini_key
    settings = RuntimeSettings(
        database_url=database_url,
        provider_env=provider_env,
        reasoning_max_attempts=_int_env(env, REASONING_MAX_ATTEMPTS_ENV, default=2),
    )
    if use_cache and environ is None and ssm_client is None:
        _SETTINGS_CACHE = settings
    return settings


def _start(
    payload: Mapping[str, Any],
    *,
    resolved_settings: RuntimeSettings,
    provider: Any,
) -> IncidentAgentResult:
    user_input = _required_limited_str(
        payload,
        "user_input",
        max_chars=MAX_USER_INPUT_CHARS,
        alternate_name="input",
    )
    incident_id = _required_limited_str(payload, "incident_id", max_chars=MAX_IDENTIFIER_CHARS)
    metadata = _metadata(payload)
    return run_incident_agent(
        IncidentInput(
            user_input=user_input,
            incident_id=incident_id,
            namespace=_optional_limited_str(payload.get("namespace"), "namespace"),
            service_slug=_optional_limited_str(
                payload.get("service_slug"),
                "service_slug",
                max_chars=MAX_SERVICE_CHARS,
            ),
            severity=_optional_limited_str(
                payload.get("severity"),
                "severity",
                max_chars=MAX_SERVICE_CHARS,
            ),
            title=_optional_limited_str(
                payload.get("title"),
                "title",
                max_chars=MAX_TITLE_CHARS,
            ),
            metadata=metadata,
        ),
        thread_id=_optional_limited_str(payload.get("thread_id"), "thread_id"),
        pause_before_act=_optional_bool(payload, "pause_before_act", default=False),
        db_url=resolved_settings.database_url,
        reasoning_provider=provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )


def _resume(
    payload: Mapping[str, Any],
    *,
    resolved_settings: RuntimeSettings,
    provider: Any,
) -> IncidentAgentResult:
    thread_id = payload.get("thread_id")
    thread_id = _required_limited_str(payload, "thread_id", max_chars=MAX_IDENTIFIER_CHARS)
    return resume_incident_agent(
        thread_id=thread_id,
        approved=_optional_bool(payload, "approved", default=True),
        db_url=resolved_settings.database_url,
        reasoning_provider=provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )


def _success_response(
    result: IncidentAgentResult,
    started: float,
    cold_start: bool,
) -> dict[str, Any]:
    elapsed_ms = _elapsed_ms(started)
    reasoning = result.state.get("reasoning") or {}
    body = {
        "thread_id": result.thread_id,
        "interrupted": result.interrupted,
        "interrupt": result.interrupt,
        "plan": result.plan,
        "proposed_action": result.proposed_action,
        "reflected_memory_id": result.reflected_memory_id,
        "provider": reasoning.get("provider"),
        "model": reasoning.get("model"),
        "usage": reasoning.get("usage", {}),
        "elapsed_ms": elapsed_ms,
        "cold_start": cold_start,
    }
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, sort_keys=True),
        "_elapsed_ms": elapsed_ms,
    }


def _error_response(
    status_code: int,
    message: str,
    started: float,
    cold_start: bool,
    route: str,
    exc: Exception | None = None,
) -> dict[str, Any]:
    elapsed_ms = _elapsed_ms(started)
    _log(
        {
            "event": "incident_agent_error",
            "route": route,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "cold_start": cold_start,
            "error_type": type(exc).__name__ if exc else "ValueError",
            "error_detail": safe_error_detail(exc),
        }
    )
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "error": message,
                "elapsed_ms": elapsed_ms,
                "cold_start": cold_start,
            },
            sort_keys=True,
        ),
    }


def _log_turn(
    *,
    route: str,
    status_code: int,
    elapsed_ms: int,
    cold_start: bool,
    result: IncidentAgentResult,
) -> None:
    reasoning = result.state.get("reasoning") or {}
    _log(
        {
            "event": "incident_agent_turn",
            "route": route,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "cold_start": cold_start,
            "thread_id": result.thread_id,
            "provider": reasoning.get("provider"),
            "model": reasoning.get("model"),
            "usage": reasoning.get("usage", {}),
            "interrupted": result.interrupted,
        }
    )


def _log(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _route(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}
    return str(event.get("rawPath") or http.get("path") or "/")


def _http_method(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "").upper()


def _authorization_bearer_token(event: Mapping[str, Any]) -> str | None:
    headers = event.get("headers") or {}
    authorization = None
    for name, value in headers.items():
        if str(name).lower() == "authorization":
            authorization = str(value)
            break
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _json_body(event: Mapping[str, Any]) -> dict[str, Any]:
    body = event.get("body") or "{}"
    if not isinstance(body, str):
        raise ValueError("request body must be a string")
    if event.get("isBase64Encoded"):
        try:
            body_bytes = base64.b64decode(body, validate=True)
        except binascii.Error as exc:
            raise ValueError("request body must be valid base64") from exc
        if len(body_bytes) > MAX_BODY_BYTES:
            raise ValueError(f"request body must be at most {MAX_BODY_BYTES} bytes")
        try:
            body = body_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("request body must be valid UTF-8") from exc
    elif len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(f"request body must be at most {MAX_BODY_BYTES} bytes")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def _required_limited_str(
    payload: Mapping[str, Any],
    name: str,
    *,
    max_chars: int,
    alternate_name: str | None = None,
) -> str:
    value = payload.get(name)
    if value is None and alternate_name:
        value = payload.get(alternate_name)
    text = _optional_limited_str(value, name, max_chars=max_chars)
    if text is None:
        raise ValueError(f"{name} is required")
    return text


def _optional_limited_str(
    value: Any,
    name: str,
    *,
    max_chars: int = MAX_IDENTIFIER_CHARS,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters")
    return text


def _optional_bool(payload: Mapping[str, Any], name: str, *, default: bool) -> bool:
    value = payload.get(name, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a JSON boolean")


def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata") or {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata must be at most {MAX_METADATA_BYTES} bytes")
    return dict(value)


def _validate_payload(route: str, payload: Mapping[str, Any]) -> None:
    if route == "/incident/resume":
        _required_limited_str(payload, "thread_id", max_chars=MAX_IDENTIFIER_CHARS)
        _optional_bool(payload, "approved", default=True)
        return
    _required_limited_str(
        payload,
        "user_input",
        max_chars=MAX_USER_INPUT_CHARS,
        alternate_name="input",
    )
    _required_limited_str(payload, "incident_id", max_chars=MAX_IDENTIFIER_CHARS)
    _optional_limited_str(payload.get("namespace"), "namespace")
    _optional_limited_str(payload.get("thread_id"), "thread_id")
    _optional_limited_str(payload.get("service_slug"), "service_slug", max_chars=MAX_SERVICE_CHARS)
    _optional_limited_str(payload.get("severity"), "severity", max_chars=MAX_SERVICE_CHARS)
    _optional_limited_str(payload.get("title"), "title", max_chars=MAX_TITLE_CHARS)
    _optional_bool(payload, "pause_before_act", default=False)
    _metadata(payload)


def _secret_value(
    *,
    env: Mapping[str, str],
    client: Any | None,
    param_env: str,
    fallback_env: str,
) -> str:
    value = _optional_secret_value(
        env=env,
        client=client,
        param_env=param_env,
        fallback_env=fallback_env,
    )
    if not value:
        raise RuntimeError(f"{param_env} is required")
    return value


def _optional_secret_value(
    *,
    env: Mapping[str, str],
    client: Any | None,
    param_env: str,
    fallback_env: str,
) -> str | None:
    parameter_name = env.get(param_env)
    if parameter_name:
        if client is None:
            raise RuntimeError("SSM client is required when parameter names are configured")
        response = client.get_parameter(Name=parameter_name, WithDecryption=True)
        return str(response["Parameter"]["Value"])
    if not _running_in_lambda(env):
        return env.get(fallback_env)
    return None


def _running_in_lambda(env: Mapping[str, str]) -> bool:
    return bool(env.get("AWS_LAMBDA_FUNCTION_NAME"))


def _needs_ssm(env: Mapping[str, str]) -> bool:
    return bool(
        env.get(DATABASE_URL_PARAM_ENV)
        or env.get(GEMINI_API_KEY_PARAM_ENV)
        or _running_in_lambda(env)
    )


def _int_env(env: Mapping[str, str], name: str, *, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _database_url_for_lambda(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("sslmode") == "verify-full" and "sslrootcert" not in query:
        import certifi

        query["sslrootcert"] = certifi.where()
    return urlunsplit(parts._replace(query=urlencode(query)))


def _ssm_client(env: Mapping[str, str]) -> Any:
    return boto3.client(
        "ssm",
        region_name=env.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )
