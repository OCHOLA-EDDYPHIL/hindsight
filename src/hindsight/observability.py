"""Safe correlation fields for structured logs and trace propagation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from opentelemetry import trace

CORRELATION_KEYS = (
    "tenant_id",
    "run_id",
    "dispatch_id",
    "dispatch_attempt_id",
    "attempt_id",
)


def correlation_fields(values: Mapping[str, Any]) -> dict[str, str]:
    """Return bounded opaque identities plus the active trace and span identities."""

    fields: dict[str, str] = {}
    for key in CORRELATION_KEYS:
        value = str(values.get(key) or "").strip()
        if value:
            fields[key] = value[:128]
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        fields["trace_id"] = format(context.trace_id, "032x")
        fields["span_id"] = format(context.span_id, "016x")
    return fields


def structured_event(event: str, values: Mapping[str, Any]) -> str:
    """Encode a safe structured event without copying arbitrary request data."""

    payload = {"event": event, **correlation_fields(values)}
    for key in (
        "status",
        "command",
        "message_id",
        "lambda_request_id",
        "operation_id",
        "incident_id",
        "receive_count",
        "source_arn",
        "error_code",
        "error_detail",
    ):
        value = str(values.get(key) or "").strip()
        if value:
            payload[key] = value[:128]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
