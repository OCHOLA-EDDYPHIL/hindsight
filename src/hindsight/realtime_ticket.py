"""Short-lived signed WebSocket tickets for server-bound tenants."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from hindsight.tenant import normalize_tenant_id

MAX_TICKET_TTL_SECONDS = 300


def issue_realtime_ticket(
    *, tenant_id: str, secret: str, ttl_seconds: int = 60, now: int | None = None
) -> str:
    if not 1 <= ttl_seconds <= MAX_TICKET_TTL_SECONDS:
        raise ValueError("realtime ticket lifetime is out of bounds")
    issued_at = now if now is not None else int(time.time())
    payload = json.dumps(
        {
            "tenant_id": normalize_tenant_id(tenant_id),
            "expires_at": issued_at + ttl_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_realtime_ticket(ticket: str, *, secret: str, now: int | None = None) -> str:
    encoded, separator, signature = ticket.partition(".")
    if not separator or not encoded or not signature:
        raise ValueError("realtime ticket is invalid")
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("realtime ticket is invalid")
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        tenant_id = normalize_tenant_id(payload["tenant_id"])
        expires_at = int(payload["expires_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("realtime ticket is invalid") from exc
    current_time = now if now is not None else int(time.time())
    if expires_at <= current_time:
        raise ValueError("realtime ticket has expired")
    return tenant_id
