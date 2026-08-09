"""Server-resolved tenant context for database and worker boundaries."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_TENANT_ID: ContextVar[str | None] = ContextVar("hindsight_tenant_id", default=None)
_LIFECYCLE_FENCE_DOMAIN = b"hindsight.tenant-lifecycle-fence.v1\0"


def normalize_tenant_id(value: str | UUID) -> str:
    """Return one canonical UUID tenant identifier."""

    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("tenant_id must be a UUID") from exc


def tenant_lifecycle_fence_key(value: str | UUID) -> str:
    """Return the non-reversible DynamoDB key that permanently fences a tenant."""

    tenant_id = normalize_tenant_id(value)
    digest = hashlib.sha256(_LIFECYCLE_FENCE_DOMAIN + tenant_id.encode()).hexdigest()
    return f"tenant-lifecycle-fence:{digest}"


def current_tenant_id(*, required: bool = False) -> str | None:
    """Return the current server-bound tenant, optionally requiring one."""

    value = _TENANT_ID.get()
    if required and value is None:
        raise RuntimeError("tenant context is required")
    return value


@contextmanager
def tenant_scope(tenant_id: str | UUID) -> Iterator[str]:
    """Bind one tenant to the current request or worker execution context."""

    normalized = normalize_tenant_id(tenant_id)
    token = _TENANT_ID.set(normalized)
    try:
        yield normalized
    finally:
        _TENANT_ID.reset(token)


def resolved_tenant_id(explicit: str | UUID | None = None) -> str | None:
    """Resolve an explicit tenant without allowing context override."""

    current = current_tenant_id()
    if explicit is None:
        return current
    normalized = normalize_tenant_id(explicit)
    if current is not None and current != normalized:
        raise RuntimeError("explicit tenant differs from the bound tenant context")
    return normalized
