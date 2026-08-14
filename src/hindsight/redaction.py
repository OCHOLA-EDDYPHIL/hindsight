"""Public API redaction helpers."""

from __future__ import annotations

import re
from typing import Any


_PUBLIC_IDENTIFIER_KEY_SUFFIXES = ("accountid",)
_PUBLIC_SECRET_KEY_SUFFIXES = (
    "authorization",
    "password",
    "passwd",
    "apikey",
    "accesskey",
    "accesskeyid",
    "secretkey",
    "secretaccesskey",
    "sessiontoken",
    "accesstoken",
    "idtoken",
    "refreshtoken",
    "token",
    "clientsecret",
    "privatekey",
    "credential",
    "credentials",
    "secret",
)
_PUBLIC_SECRET_KEYS = frozenset(
    {
        "authorization",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "secret_access_key",
        "session_token",
        "access_token",
        "id_token",
        "refresh_token",
        "api_key",
        "api-key",
        "x-api-key",
        "password",
        "passwd",
        "credential",
        "credentials",
    }
)
_AWS_ACCOUNT_VALUE = re.compile(r"(?<![A-Za-z0-9-])[0-9]{12}(?![A-Za-z0-9-])")
_SECRET_LABEL = (
    r"(?:authorization|api[_-]?key|x[_-]?api[_-]?key|password|passwd|"
    r"access[_-]?key[_-]?id|secret(?:[_-]?(?:access[_-]?)?key)?|session[_-]?token|"
    r"access[_-]?token|id[_-]?token|refresh[_-]?token|auth[_-]?token|token|"
    r"client[_-]?secret|credentials?)"
)
_QUOTED_INLINE_SECRET = re.compile(
    rf"(?P<prefix>[\"']?{_SECRET_LABEL}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9])[\"']?{_SECRET_LABEL}[\"']?\s*[:=]\s*"
    r"(?:bearer\s+)?)(?P<value>(?![\"'])[^\s,;}\]]+)",
    re.IGNORECASE,
)
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_URL_CREDENTIAL = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(@)")
_KNOWN_SECRET_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"sk[-_](?:proj[-_])?[A-Za-z0-9_-]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|glpat-[A-Za-z0-9_-]{8,}|"
    r"AIza[0-9A-Za-z_-]{20,}|npm_[A-Za-z0-9]{8,}|"
    r"hf_[A-Za-z0-9]{8,}|pypi-[A-Za-z0-9_-]{8,}"
    r")(?![A-Za-z0-9])"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_account_identifiers(value: Any) -> Any:
    """Remove account identifiers and credentials from nested public projections."""

    if isinstance(value, dict):
        return {
            key: redact_account_identifiers(item)
            for key, item in value.items()
            if not _sensitive_public_key(key)
        }
    if isinstance(value, list):
        return [redact_account_identifiers(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_account_identifiers(item) for item in value)
    if isinstance(value, str):
        redacted = _PRIVATE_KEY.sub("[redacted-private-key]", value)
        redacted = _QUOTED_INLINE_SECRET.sub(
            lambda match: (
                f"{match.group('prefix')}{match.group('quote')}"
                f"[redacted-secret]{match.group('quote')}"
            ),
            redacted,
        )
        redacted = _INLINE_SECRET.sub(
            lambda match: f"{match.group('prefix')}[redacted-secret]",
            redacted,
        )
        redacted = _URL_CREDENTIAL.sub(r"\1[redacted-secret]\2", redacted)
        redacted = _BEARER_SECRET.sub("Bearer [redacted-secret]", redacted)
        redacted = _AWS_ACCESS_KEY.sub("[redacted-access-key]", redacted)
        redacted = _KNOWN_SECRET_TOKEN.sub("[redacted-token]", redacted)
        return _AWS_ACCOUNT_VALUE.sub("[redacted-account]", redacted)
    return value


def _sensitive_public_key(value: Any) -> bool:
    text = str(value)
    normalized = re.sub(r"[^a-z0-9]", "", text.lower())
    return bool(
        text.lower() in _PUBLIC_SECRET_KEYS
        or normalized.endswith(_PUBLIC_IDENTIFIER_KEY_SUFFIXES)
        or normalized.endswith(_PUBLIC_SECRET_KEY_SUFFIXES)
    )
