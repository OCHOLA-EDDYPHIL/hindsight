"""Public API redaction helpers."""

from __future__ import annotations

from typing import Any


_PUBLIC_IDENTIFIER_KEYS = frozenset({"account_id", "aws_account_id"})


def redact_account_identifiers(value: Any) -> Any:
    """Remove AWS account identifiers from nested public projections."""

    if isinstance(value, dict):
        return {
            key: redact_account_identifiers(item)
            for key, item in value.items()
            if key not in _PUBLIC_IDENTIFIER_KEYS
        }
    if isinstance(value, list):
        return [redact_account_identifiers(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_account_identifiers(item) for item in value)
    return value
