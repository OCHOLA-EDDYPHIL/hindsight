"""Server-owned tenant identities that are never selected by request data."""

from __future__ import annotations

from hindsight.tenant import normalize_tenant_id

PUBLIC_DEMO_TENANT_ID = "00000000-0000-0000-0000-000000000002"
ACCEPTANCE_TENANT_ID = "00000000-0000-0000-0000-000000000003"
LEARNING_TENANT_ID = "00000000-0000-0000-0000-000000000004"


def public_demo_tenant_id() -> str:
    """Return the fixed public product tenant."""

    return PUBLIC_DEMO_TENANT_ID


def learning_tenant_id() -> str:
    """Return the fixed claim-bearing learning tenant."""

    return LEARNING_TENANT_ID


def worker_tenant_id(value: object) -> str:
    """Validate a tenant identifier carried by a trusted internal message."""

    return normalize_tenant_id(str(value))
