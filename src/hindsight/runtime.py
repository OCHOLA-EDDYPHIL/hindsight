"""Lightweight runtime configuration shared by HTTP and worker Lambdas."""

from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

import boto3

from hindsight.aws import aws_client_config
from hindsight.db import database_url_with_tls_roots

DATABASE_URL_PARAM_ENV = "HINDSIGHT_DATABASE_URL_PARAM"
GEMINI_API_KEY_PARAM_ENV = "HINDSIGHT_GEMINI_API_KEY_PARAM"
GEMINI_API_KEYS_PARAM_ENV = "HINDSIGHT_GEMINI_API_KEYS_PARAM"
FUNCTION_AUTH_TOKEN_PARAM_ENV = "HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM"
FUNCTION_AUTH_TOKEN_ENV = "HINDSIGHT_FUNCTION_AUTH_TOKEN"
REASONING_MAX_ATTEMPTS_ENV = "REASONING_MAX_ATTEMPTS"
SETTINGS_CACHE_TTL_ENV = "HINDSIGHT_SETTINGS_CACHE_TTL_SECONDS"
_SETTINGS_CACHE: RuntimeSettings | None = None
_SETTINGS_CACHE_AT: float | None = None
_AUTH_TOKEN_CACHE: str | None = None


@dataclass(frozen=True)
class RuntimeSettings:
    database_url: str
    provider_env: dict[str, str]
    reasoning_max_attempts: int = 2


def function_auth_token(
    *,
    environ: Mapping[str, str] | None = None,
    ssm_client: Any | None = None,
    use_cache: bool = True,
) -> str:
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
    global _SETTINGS_CACHE, _SETTINGS_CACHE_AT
    env = os.environ if environ is None else environ
    cache_ttl = _int_env(env, SETTINGS_CACHE_TTL_ENV, default=300)
    cache_current = (
        _SETTINGS_CACHE_AT is not None and time.monotonic() - _SETTINGS_CACHE_AT < cache_ttl
    )
    if (
        use_cache
        and environ is None
        and ssm_client is None
        and _SETTINGS_CACHE is not None
        and cache_current
    ):
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
        "GEMINI_EMBEDDING_MODEL": env.get("GEMINI_EMBEDDING_MODEL", ""),
        "EMBEDDING_PROVIDER": env.get("EMBEDDING_PROVIDER", "deterministic"),
        "HINDSIGHT_GEMINI_KEY_HEALTH_TABLE": env.get(
            "HINDSIGHT_GEMINI_KEY_HEALTH_TABLE", ""
        ),
        "BEDROCK_MODEL": env.get("BEDROCK_MODEL", ""),
        "BEDROCK_EMBEDDING_MODEL": env.get("BEDROCK_EMBEDDING_MODEL", ""),
        "AWS_REGION": env.get("AWS_REGION", ""),
        "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION", ""),
    }
    needs_gemini = any(
        provider_env[name].strip().lower() == "gemini"
        for name in ("LLM_PROVIDER", "EMBEDDING_PROVIDER")
    )
    if needs_gemini:
        gemini_keys = _optional_secret_value(
            env=env,
            client=client,
            param_env=GEMINI_API_KEYS_PARAM_ENV,
            fallback_env="GEMINI_API_KEYS",
        )
        if not gemini_keys and not _running_in_lambda(env):
            local_keys = [
                env[name]
                for name in ("GEMINI_API_KEY", *(f"GEMINI_API_KEY_{i}" for i in range(1, 20)))
                if env.get(name)
            ]
            if local_keys:
                gemini_keys = json.dumps(
                    {
                        "version": 1,
                        "keys": [
                            {"id": f"gemini-{index + 1}", "api_key": key}
                            for index, key in enumerate(local_keys)
                        ],
                    }
                )
        if not gemini_keys:
            gemini_keys = _optional_secret_value(
                env=env,
                client=client,
                param_env=GEMINI_API_KEY_PARAM_ENV,
                fallback_env="GEMINI_API_KEY",
            )
        if gemini_keys:
            provider_env["GEMINI_API_KEYS"] = gemini_keys
    settings = RuntimeSettings(
        database_url=database_url,
        provider_env=provider_env,
        reasoning_max_attempts=_int_env(env, REASONING_MAX_ATTEMPTS_ENV, default=2),
    )
    if use_cache and environ is None and ssm_client is None:
        _SETTINGS_CACHE = settings
        _SETTINGS_CACHE_AT = time.monotonic()
    return settings


def runtime_database_url(
    *,
    environ: Mapping[str, str] | None = None,
    ssm_client: Any | None = None,
) -> str:
    """Resolve only the database setting without touching provider credentials."""

    env = os.environ if environ is None else environ
    client = ssm_client
    if client is None and _needs_ssm(env):
        client = _ssm_client(env)
    return _database_url_for_lambda(
        _secret_value(
            env=env,
            client=client,
            param_env=DATABASE_URL_PARAM_ENV,
            fallback_env="DATABASE_URL",
        )
    )


def invalidate_runtime_settings_cache() -> None:
    """Force the next invocation to reload mutable SSM-backed settings."""

    global _SETTINGS_CACHE, _SETTINGS_CACHE_AT
    _SETTINGS_CACHE = None
    _SETTINGS_CACHE_AT = None


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
        or env.get(GEMINI_API_KEYS_PARAM_ENV)
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


def _database_url_for_lambda(url: str) -> str:
    return database_url_with_tls_roots(url)


def _ssm_client(env: Mapping[str, str]) -> Any:
    return boto3.client(
        "ssm",
        region_name=env.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )
