"""Lightweight runtime configuration shared by HTTP and worker Lambdas."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3

from hindsight.aws import aws_client_config

DATABASE_URL_PARAM_ENV = "HINDSIGHT_DATABASE_URL_PARAM"
GEMINI_API_KEY_PARAM_ENV = "HINDSIGHT_GEMINI_API_KEY_PARAM"
FUNCTION_AUTH_TOKEN_PARAM_ENV = "HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM"
FUNCTION_AUTH_TOKEN_ENV = "HINDSIGHT_FUNCTION_AUTH_TOKEN"
REASONING_MAX_ATTEMPTS_ENV = "REASONING_MAX_ATTEMPTS"
_SETTINGS_CACHE: RuntimeSettings | None = None
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
