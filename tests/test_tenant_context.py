from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.pq import TransactionStatus

from hindsight import db
from hindsight.tenant import current_tenant_id, tenant_scope


@dataclass
class _Info:
    transaction_status: TransactionStatus = TransactionStatus.IDLE


class _RawCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None, **kwargs):
        self.connection.calls.append((str(query), params))
        self.connection.info.transaction_status = TransactionStatus.INTRANS
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.info.transaction_status = TransactionStatus.INTRANS
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.connection.info.transaction_status = TransactionStatus.IDLE
        return False


class _RawConnection:
    def __init__(self):
        self.info = _Info()
        self.autocommit = False
        self.calls = []
        self.closed = False

    def execute(self, query, params=None, **kwargs):
        self.calls.append((str(query), params))
        self.info.transaction_status = TransactionStatus.INTRANS
        return _RawCursor(self)

    def cursor(self, *args, **kwargs):
        return _RawCursor(self)

    def transaction(self, *args, **kwargs):
        return _Transaction(self)

    def commit(self):
        self.info.transaction_status = TransactionStatus.IDLE

    def rollback(self):
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.info.transaction_status = TransactionStatus.IDLE
        return False


def test_tenant_scope_is_canonical_nested_and_restored():
    first = uuid4()
    second = uuid4()
    assert current_tenant_id() is None
    with tenant_scope(first) as bound:
        assert bound == str(first)
        assert current_tenant_id(required=True) == str(first)
        with tenant_scope(second):
            assert current_tenant_id() == str(second)
        assert current_tenant_id() == str(first)
    assert current_tenant_id() is None


def test_tenant_connection_binds_once_per_transaction_and_rebinds_when_idle():
    raw = _RawConnection()
    first = uuid4()
    second = uuid4()
    connection = db.TenantConnection(raw, tenant_id=str(first))

    connection.execute("SELECT 1")
    connection.execute("SELECT 2")
    assert raw.calls[0] == (
        "SELECT set_config(%s, %s, true)",
        (db.TENANT_SETTING, str(first)),
    )
    assert [call[0] for call in raw.calls].count("SELECT set_config(%s, %s, true)") == 1

    connection.commit()
    connection.bind_tenant(str(second))
    with connection.cursor() as cursor:
        cursor.execute("SELECT 3")
    assert raw.calls[-2] == (
        "SELECT set_config(%s, %s, true)",
        (db.TENANT_SETTING, str(second)),
    )


def test_tenant_connection_refuses_rebind_in_transaction_and_autocommit():
    raw = _RawConnection()
    connection = db.TenantConnection(raw, tenant_id=str(uuid4()))
    connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="only while the connection is idle"):
        connection.bind_tenant(str(uuid4()))
    with pytest.raises(RuntimeError, match="cannot use autocommit"):
        connection.autocommit = True


def test_connect_requires_context_when_runtime_guard_is_enabled(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_REQUIRE_TENANT_CONTEXT", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="tenant context is required"):
        db.connect("postgresql://unused/unused")


def test_connect_wraps_context_and_rejects_explicit_override(monkeypatch):
    raw = _RawConnection()
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: raw)
    first = uuid4()
    second = uuid4()
    with tenant_scope(first):
        connection = db.connect("postgresql://unused/unused")
        assert isinstance(connection, db.TenantConnection)
        assert connection.tenant_id == str(first)
        with pytest.raises(RuntimeError, match="differs from the bound tenant"):
            db.connect("postgresql://unused/unused", tenant_id=str(second))
