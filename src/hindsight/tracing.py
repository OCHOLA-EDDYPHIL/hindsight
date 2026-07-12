"""OpenTelemetry helpers for safe Hindsight instrumentation."""

from __future__ import annotations

import atexit
import os
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span

TRACER_NAME = "hindsight"
SERVICE_NAME = "hindsight"

_CONFIGURED = False
_SENSITIVE_ATTRIBUTE_PARTS = (
    "content",
    "query",
    "prompt",
    "reason",
    "justification",
    "metadata",
    "db",
    "dsn",
    "url",
    "secret",
    "key",
    "token",
    "password",
    "source_ref",
)


def tracer() -> trace.Tracer:
    """Return the project tracer; it is no-op unless a provider is configured."""

    return trace.get_tracer(TRACER_NAME)


def configure_tracing_from_env(*, service_name: str = SERVICE_NAME) -> bool:
    """Configure OTLP tracing only when explicitly enabled for a demo/runtime."""

    global _CONFIGURED
    if _CONFIGURED:
        return True
    if not _otel_enabled():
        return False

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)
    _CONFIGURED = True
    return True


@contextmanager
def start_span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
    """Start a span and apply only safe, non-sensitive attributes."""

    with tracer().start_as_current_span(name) as span:
        set_span_attributes(span, attributes or {})
        yield span


def set_span_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    """Set span attributes after dropping sensitive names and unsupported values."""

    for key, value in safe_attributes(attributes).items():
        span.set_attribute(key, value)


def safe_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Return attributes that are safe for traces and supported by OpenTelemetry."""

    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None or _is_sensitive_key(key):
            continue
        normalized = _normalize_attribute_value(value)
        if normalized is not None:
            safe[key] = normalized
    return safe


def memory_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Extract stable memory IDs from result rows without touching memory content."""

    ids: list[str] = []
    for row in rows:
        memory_id = row.get("memory_id") or row.get("id")
        if memory_id is not None:
            ids.append(str(memory_id))
    return ids


def _otel_enabled() -> bool:
    enabled_flag = os.environ.get("HINDSIGHT_OTEL_ENABLED")
    if enabled_flag is not None:
        return _truthy(enabled_flag)
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _is_sensitive_key(key: str) -> bool:
    lower_key = key.lower()
    return any(part in lower_key for part in _SENSITIVE_ATTRIBUTE_PARTS)


def _normalize_attribute_value(value: Any) -> str | bool | int | float | list[str] | None:
    if isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        normalized = [str(item) for item in value if item is not None]
        return normalized if normalized else None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
