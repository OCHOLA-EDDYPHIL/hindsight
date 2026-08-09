"""Short-lived, one-time WebSocket tickets for server-bound tenants."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast

import boto3
from boto3.dynamodb.conditions import Attr

from hindsight.aws import aws_client_config
from hindsight.tenant import normalize_tenant_id

TICKET_TABLE_ENV = "HINDSIGHT_REALTIME_TICKET_TABLE"
MAX_TICKET_TTL_SECONDS = 300
DEFAULT_TICKET_TTL_SECONDS = 60
TICKET_ENTROPY_BYTES = 32
_TICKET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PRINCIPAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ACCESS_CLASSES = frozenset({"public", "viewer", "operator"})

AccessClass = Literal["public", "viewer", "operator"]


@dataclass(frozen=True)
class RealtimeTicketClaims:
    """The server-bound session attributes carried by a consumed ticket."""

    tenant_id: str
    access_class: AccessClass
    principal_id: str | None
    redeem_before: int
    session_expires_at: int


def issue_realtime_ticket(
    *,
    tenant_id: str,
    access_class: AccessClass,
    session_expires_at: int,
    principal_id: str | None = None,
    ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
    now: int | None = None,
    table: Any | None = None,
) -> str:
    """Persist a digest-only ticket record and return its 256-bit bearer value."""

    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise ValueError("realtime ticket lifetime must be an integer")
    if not 1 <= ttl_seconds <= MAX_TICKET_TTL_SECONDS:
        raise ValueError("realtime ticket lifetime is out of bounds")
    issued_at = _epoch_seconds(now, field="now")
    session_expiry = _epoch_seconds(session_expires_at, field="session_expires_at")
    if session_expiry <= issued_at:
        raise ValueError("realtime session has already expired")

    normalized_tenant = normalize_tenant_id(tenant_id)
    normalized_access, normalized_principal = _normalize_access(access_class, principal_id)
    redeem_before = min(issued_at + ttl_seconds, session_expiry)
    resolved_table = table or _ticket_table()

    for _attempt in range(3):
        ticket = secrets.token_urlsafe(TICKET_ENTROPY_BYTES)
        digest = _ticket_digest(ticket)
        item = {
            "ticket_digest": digest,
            "tenant_id": normalized_tenant,
            "access_class": normalized_access,
            "principal_id": normalized_principal or "",
            "redeem_before": redeem_before,
            "session_expires_at": session_expiry,
            "expires_at": redeem_before,
        }
        try:
            resolved_table.put_item(
                Item=item,
                ConditionExpression=Attr("ticket_digest").not_exists(),
            )
        except resolved_table.meta.client.exceptions.ConditionalCheckFailedException:
            continue
        return ticket
    raise RuntimeError("could not allocate a unique realtime ticket")


def consume_realtime_ticket(
    ticket: str,
    *,
    now: int | None = None,
    table: Any | None = None,
) -> RealtimeTicketClaims:
    """Atomically redeem a valid ticket once and return its bound session claims."""

    digest = _ticket_digest(ticket)
    consumed_at = _epoch_seconds(now, field="now")
    resolved_table = table or _ticket_table()
    try:
        response = resolved_table.delete_item(
            Key={"ticket_digest": digest},
            ConditionExpression=(
                Attr("ticket_digest").exists()
                & Attr("redeem_before").gt(consumed_at)
                & Attr("session_expires_at").gt(consumed_at)
            ),
            ReturnValues="ALL_OLD",
        )
    except resolved_table.meta.client.exceptions.ConditionalCheckFailedException as exc:
        raise ValueError("realtime ticket is invalid or expired") from exc

    item = response.get("Attributes")
    if not isinstance(item, dict) or item.get("ticket_digest") != digest:
        raise ValueError("realtime ticket is invalid or expired")
    try:
        tenant_id = normalize_tenant_id(item["tenant_id"])
        access_class, principal_id = _normalize_access(
            item["access_class"],
            item.get("principal_id") or None,
        )
        redeem_before = _stored_epoch_seconds(item["redeem_before"], field="redeem_before")
        session_expires_at = _stored_epoch_seconds(
            item["session_expires_at"],
            field="session_expires_at",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("realtime ticket is invalid or expired") from exc
    if redeem_before <= consumed_at or session_expires_at <= consumed_at:
        raise ValueError("realtime ticket is invalid or expired")
    return RealtimeTicketClaims(
        tenant_id=tenant_id,
        access_class=access_class,
        principal_id=principal_id,
        redeem_before=redeem_before,
        session_expires_at=session_expires_at,
    )


def _ticket_digest(ticket: str) -> str:
    if not isinstance(ticket, str) or not _TICKET_PATTERN.fullmatch(ticket):
        raise ValueError("realtime ticket is invalid or expired")
    try:
        decoded = base64.urlsafe_b64decode(ticket + "=")
    except (binascii.Error, ValueError) as exc:
        raise ValueError("realtime ticket is invalid or expired") from exc
    if len(decoded) != TICKET_ENTROPY_BYTES:
        raise ValueError("realtime ticket is invalid or expired")
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != ticket:
        raise ValueError("realtime ticket is invalid or expired")
    return hashlib.sha256(ticket.encode()).hexdigest()


def _normalize_access(
    access_class: str,
    principal_id: str | None,
) -> tuple[AccessClass, str | None]:
    if access_class not in _ACCESS_CLASSES:
        raise ValueError("realtime ticket access class is invalid")
    normalized_principal = (principal_id.strip() or None) if isinstance(principal_id, str) else None
    if normalized_principal and not _PRINCIPAL_PATTERN.fullmatch(normalized_principal):
        raise ValueError("realtime ticket principal is invalid")
    if access_class == "public" and normalized_principal is not None:
        raise ValueError("public realtime tickets cannot bind a principal")
    if access_class != "public" and normalized_principal is None:
        raise ValueError("protected realtime tickets require a principal")
    return cast(AccessClass, access_class), normalized_principal


def _epoch_seconds(value: int | None, *, field: str) -> int:
    if value is None:
        if field == "now":
            return int(time.time())
        raise ValueError(f"{field} is required")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be integer epoch seconds")
    return value


def _stored_epoch_seconds(value: Any, *, field: str) -> int:
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError(f"{field} must be integer epoch seconds")
        return int(value)
    return _epoch_seconds(value, field=field)


def _ticket_table() -> Any:
    table_name = os.environ.get(TICKET_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{TICKET_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)
