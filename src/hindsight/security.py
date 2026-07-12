"""Small security helpers shared by runtime entrypoints."""

from __future__ import annotations

import re


def safe_error_detail(exc: Exception | None, *, max_chars: int = 500) -> str | None:
    """Return a human-readable exception detail with common secrets redacted."""

    if exc is None:
        return None
    detail = str(exc).replace("\n", " ")
    detail = re.sub(r"(postgres(?:ql)?://[^:\s/]+:)[^@\s/]+@", r"\1***@", detail)
    detail = re.sub(
        r"(?i)\b(password|token|secret|api[_-]?key)=([^&\s]+)",
        r"\1=***",
        detail,
    )
    return detail[:max_chars]
