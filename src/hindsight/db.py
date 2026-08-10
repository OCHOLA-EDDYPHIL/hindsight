"""Database connection helpers.

All of Hindsight's state lives in CockroachDB. Every component gets its
connection through here so the URL, and later the role separation, is
configured in exactly one place.
"""

import os
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
import psycopg
from dotenv import load_dotenv
from psycopg.pq import TransactionStatus

from hindsight.tenant import normalize_tenant_id, resolved_tenant_id

load_dotenv()

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_APPLICATION_NAME = "hindsight"
TENANT_SETTING = "hindsight.tenant_id"


class TenantCursor:
    """Cursor proxy that binds transaction-local tenant context before SQL."""

    def __init__(self, cursor: Any, connection: "TenantConnection"):
        self._cursor = cursor
        self._connection = connection

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        self._connection._ensure_tenant_context()
        return self._cursor.execute(query, params, **kwargs)

    def executemany(self, query: Any, params_seq: Any, **kwargs: Any) -> Any:
        self._connection._ensure_tenant_context()
        return self._cursor.executemany(query, params_seq, **kwargs)

    def __enter__(self) -> "TenantCursor":
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        return self._cursor.__exit__(exc_type, exc, traceback)

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class TenantConnection:
    """Connection proxy that never carries a tenant across transaction boundaries."""

    _INTERNAL_ATTRIBUTES = {"_connection", "_tenant_id", "_tenant_bound"}

    def __init__(self, connection: psycopg.Connection, *, tenant_id: str):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_tenant_id", normalize_tenant_id(tenant_id))
        object.__setattr__(self, "_tenant_bound", False)
        if connection.autocommit:
            raise RuntimeError("tenant-bound connections cannot use autocommit")

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def bind_tenant(self, tenant_id: str) -> None:
        """Rebind an idle connection before returning it to another tenant."""

        if self._connection.info.transaction_status != TransactionStatus.IDLE:
            raise RuntimeError("tenant can be rebound only while the connection is idle")
        object.__setattr__(self, "_tenant_id", normalize_tenant_id(tenant_id))
        object.__setattr__(self, "_tenant_bound", False)

    def _ensure_tenant_context(self) -> None:
        if self._connection.autocommit:
            raise RuntimeError("tenant-bound connections cannot use autocommit")
        if self._connection.info.transaction_status == TransactionStatus.IDLE:
            object.__setattr__(self, "_tenant_bound", False)
        if self._tenant_bound:
            return
        self._connection.execute(
            "SELECT set_config(%s, %s, true)",
            (TENANT_SETTING, self._tenant_id),
        )
        object.__setattr__(self, "_tenant_bound", True)

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        self._ensure_tenant_context()
        return self._connection.execute(query, params, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> TenantCursor:
        return TenantCursor(self._connection.cursor(*args, **kwargs), self)

    @contextmanager
    def transaction(self, *args: Any, **kwargs: Any):
        try:
            with self._connection.transaction(*args, **kwargs):
                self._ensure_tenant_context()
                yield self
        finally:
            if self._connection.info.transaction_status == TransactionStatus.IDLE:
                object.__setattr__(self, "_tenant_bound", False)

    def commit(self) -> None:
        self._connection.commit()
        object.__setattr__(self, "_tenant_bound", False)

    def rollback(self) -> None:
        self._connection.rollback()
        object.__setattr__(self, "_tenant_bound", False)

    def __enter__(self) -> "TenantConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        try:
            return self._connection.__exit__(exc_type, exc, traceback)
        finally:
            object.__setattr__(self, "_tenant_bound", False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._INTERNAL_ATTRIBUTES:
            object.__setattr__(self, name, value)
            return
        if name == "autocommit" and value:
            raise RuntimeError("tenant-bound connections cannot use autocommit")
        setattr(self._connection, name, value)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and adjust it, "
            "or export DATABASE_URL directly."
        )
    return database_url_with_tls_roots(url)


def database_url_with_tls_roots(url: str) -> str:
    """Use the bundled public CA when a verified URL omits an explicit root."""

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("sslmode") in {"verify-ca", "verify-full"} and not query.get("sslrootcert"):
        query["sslrootcert"] = certifi.where()
    return urlunsplit(parts._replace(query=urlencode(query)))


def connect(
    url: str | None = None,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name: str = DEFAULT_APPLICATION_NAME,
    tenant_id: str | None = None,
) -> psycopg.Connection | TenantConnection:
    """Open a CockroachDB connection with bounded startup behavior."""

    resolved = resolved_tenant_id(tenant_id)
    if resolved is None and _tenant_context_required():
        raise RuntimeError("tenant context is required for this database connection")
    from opentelemetry import trace

    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("db.system", "cockroachdb")
        span.set_attribute("db.operation.name", "connect")
        span.set_attribute("hindsight.db.application", application_name)
        if resolved is not None:
            span.set_attribute("hindsight.tenant_id", resolved)
    connection = psycopg.connect(
        database_url_with_tls_roots(url) if url is not None else database_url(),
        connect_timeout=connect_timeout,
        application_name=application_name,
    )
    if resolved is None:
        return connection
    return TenantConnection(connection, tenant_id=resolved)


def _tenant_context_required() -> bool:
    return os.environ.get("HINDSIGHT_REQUIRE_TENANT_CONTEXT", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
