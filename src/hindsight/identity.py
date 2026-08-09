"""Cognito identity resolution for tenant-bound product requests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import psycopg

from hindsight.db import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    connect,
    database_url_with_tls_roots,
)

COGNITO_ISSUER_ENV = "HINDSIGHT_COGNITO_ISSUER"
COGNITO_CLIENT_ID_ENV = "HINDSIGHT_COGNITO_CLIENT_ID"
ROLE_SCOPES = {
    "viewer": frozenset({"read", "realtime"}),
    "operator": frozenset({"read", "realtime", "write"}),
}
_GROUP_SEPARATOR = re.compile(r"[\s,]+")


class IdentityError(RuntimeError):
    """Base class for deliberately generic identity failures."""


class IdentityUnauthenticated(IdentityError):
    """API Gateway did not supply one valid access-token identity."""


class IdentityForbidden(IdentityError):
    """The verified principal has no active product authorization."""


class IdentityUnavailable(IdentityError):
    """Identity configuration or its database directory is unavailable."""


@dataclass(frozen=True)
class PrincipalMapping:
    id: str
    tenant_id: str
    role: str
    status: str


@dataclass(frozen=True)
class ProductIdentity:
    principal_id: str
    tenant_id: str
    tenant_slug: str
    token_role: str
    mapped_role: str
    effective_role: str
    scopes: frozenset[str]
    expires_at: int

    @property
    def actor(self) -> str:
        return f"product:{self.effective_role}:{self.principal_id}"

    def public_payload(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "tenant_slug": self.tenant_slug,
            "token_role": self.token_role,
            "mapped_role": self.mapped_role,
            "effective_role": self.effective_role,
            "scopes": sorted(self.scopes),
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class VerifiedTokenIdentity:
    principal_hash: str
    role: str
    expires_at: int


def resolve_product_identity(
    scope: Mapping[str, Any],
    *,
    db_url: str,
    expected_issuer: str | None = None,
    expected_client_id: str | None = None,
    now: int | None = None,
    mapping_lookup: Callable[[str, str], PrincipalMapping | None] | None = None,
    tenant_lookup: Callable[[str, str], tuple[str, str] | None] | None = None,
) -> ProductIdentity:
    """Resolve verified Gateway claims through the PII-free role directory."""

    issuer = expected_issuer or os.environ.get(COGNITO_ISSUER_ENV, "")
    client_id = expected_client_id or os.environ.get(COGNITO_CLIENT_ID_ENV, "")
    if not issuer or not client_id:
        raise IdentityUnavailable("product identity is not configured")
    claims = _gateway_claims(scope)
    token = verify_gateway_claims(
        claims,
        expected_issuer=issuer,
        expected_client_id=client_id,
        now=now,
    )
    load_mapping = mapping_lookup or _lookup_principal_mapping
    load_tenant = tenant_lookup or _lookup_active_tenant
    try:
        mapping = load_mapping(token.principal_hash, db_url)
    except IdentityError:
        raise
    except Exception as exc:
        raise IdentityUnavailable("product identity directory is unavailable") from exc
    if mapping is None or mapping.status != "active" or mapping.role not in ROLE_SCOPES:
        raise IdentityForbidden("product authorization is unavailable")
    try:
        tenant = load_tenant(mapping.tenant_id, db_url)
    except IdentityError:
        raise
    except Exception as exc:
        raise IdentityUnavailable("product tenant directory is unavailable") from exc
    if tenant is None or tenant[1] != "active":
        raise IdentityForbidden("product authorization is unavailable")
    scopes = ROLE_SCOPES[token.role] & ROLE_SCOPES[mapping.role]
    if not {"read", "realtime"}.issubset(scopes):
        raise IdentityForbidden("product authorization is unavailable")
    effective_role = "operator" if "write" in scopes else "viewer"
    return ProductIdentity(
        principal_id=mapping.id,
        tenant_id=mapping.tenant_id,
        tenant_slug=tenant[0],
        token_role=token.role,
        mapped_role=mapping.role,
        effective_role=effective_role,
        scopes=scopes,
        expires_at=token.expires_at,
    )


def verify_gateway_claims(
    claims: Mapping[str, Any],
    *,
    expected_issuer: str,
    expected_client_id: str,
    now: int | None = None,
) -> VerifiedTokenIdentity:
    """Validate defense-in-depth claims after API Gateway JWT verification."""

    issuer = _required_claim(claims, "iss")
    client_id = _required_claim(claims, "client_id")
    token_use = _required_claim(claims, "token_use")
    subject = _required_claim(claims, "sub")
    if issuer != expected_issuer or client_id != expected_client_id or token_use != "access":
        raise IdentityUnauthenticated("protected identity is invalid")
    if len(subject) > 2048:
        raise IdentityUnauthenticated("protected identity is invalid")
    try:
        expires_at = int(claims.get("exp"))
    except (TypeError, ValueError) as exc:
        raise IdentityUnauthenticated("protected identity is invalid") from exc
    current_time = int(time.time()) if now is None else now
    if expires_at <= current_time:
        raise IdentityUnauthenticated("protected identity has expired")
    groups = _claim_groups(claims.get("cognito:groups"))
    if groups not in (["viewer"], ["operator"]):
        raise IdentityForbidden("product authorization is unavailable")
    role = groups[0]
    principal_hash = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
    return VerifiedTokenIdentity(
        principal_hash=principal_hash,
        role=role,
        expires_at=expires_at,
    )


def _gateway_claims(scope: Mapping[str, Any]) -> Mapping[str, Any]:
    event = scope.get("aws.event")
    if not isinstance(event, Mapping):
        raise IdentityUnauthenticated("verified Gateway identity is required")
    request_context = event.get("requestContext")
    authorizer = request_context.get("authorizer") if isinstance(request_context, Mapping) else None
    jwt = authorizer.get("jwt") if isinstance(authorizer, Mapping) else None
    claims = jwt.get("claims") if isinstance(jwt, Mapping) else None
    if not isinstance(claims, Mapping):
        raise IdentityUnauthenticated("verified Gateway identity is required")
    return claims


def _required_claim(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise IdentityUnauthenticated("protected identity is invalid")
    return value.strip()


def _claim_groups(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        groups = [item for item in value if isinstance(item, str)]
        if len(groups) != len(value):
            return []
        return [item.strip() for item in groups if item.strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return _claim_groups(parsed)
        raw = raw[1:-1]
    return [group for group in _GROUP_SEPARATOR.split(raw) if group]


def _lookup_principal_mapping(principal_hash: str, db_url: str) -> PrincipalMapping | None:
    """Read the global opaque-hash directory before tenant context is known."""

    with psycopg.connect(
        database_url_with_tls_roots(db_url),
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        application_name="hindsight-product-identity",
    ) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute(
            """
                SELECT id, tenant_id, role, status
                FROM product_principal_roles
                WHERE principal_hash = %s
            """,
            (principal_hash,),
        ).fetchone()
    if row is None:
        return None
    return PrincipalMapping(
        id=str(row[0]),
        tenant_id=str(row[1]),
        role=str(row[2]),
        status=str(row[3]),
    )


def _lookup_active_tenant(tenant_id: str, db_url: str) -> tuple[str, str] | None:
    with connect(
        db_url,
        tenant_id=tenant_id,
        application_name="hindsight-product-tenant",
    ) as conn:
        row = conn.execute(
            "SELECT slug, status FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None
