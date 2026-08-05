"""Tests for current and historical governed-memory snapshots."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def test_memory_snapshot_current_includes_memories_operations_and_timeline(monkeypatch):
    import hindsight.snapshots as snapshots

    memory_id = uuid4()
    operation_id = uuid4()
    monkeypatch.setattr(
        snapshots,
        "_semantic_memories",
        lambda **kwargs: [
            {
                "id": memory_id,
                "namespace": "demo:payments",
                "content": "retry fanout",
                "writer": "demo.seed",
                "source_ref": "demo:seed",
                "justification": "test",
                "metadata": {},
                "t_valid": datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
                "t_invalid": None,
                "written_at": datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
                "invalidated_at": None,
                "invalidated_by": None,
                "invalidation_reason": None,
            }
        ],
    )
    monkeypatch.setattr(
        snapshots,
        "_memory_operations",
        lambda **kwargs: [
            {
                "id": operation_id,
                "operation_type": "rewind",
                "actor": "demo.operator",
                "reason": "bad memory",
                "namespace": "demo:payments",
                "target_timestamp": datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
                "invalidated_memory_ids": [str(memory_id)],
                "restored_memory_ids": [],
                "created_at": datetime(2026, 7, 12, 14, 2, tzinfo=UTC),
            }
        ],
    )

    snapshot = snapshots.memory_snapshot(namespace="demo:payments")

    assert snapshot["mode"] == "current"
    assert snapshot["memories"][0]["content"] == "retry fanout"
    assert snapshot["memories"][0]["status"] == "current"
    assert snapshot["operations"][0]["operation_type"] == "rewind"
    assert "2026-07-12T14:00:00+00:00" in snapshot["timeline"]
    assert "2026-07-12T14:02:00+00:00" in snapshot["timeline"]


def test_memory_snapshot_as_of_uses_explicit_historical_listing(monkeypatch):
    import hindsight.snapshots as snapshots

    calls = []

    class FakeMemoryStore:
        def __init__(self, *, url):
            calls.append(("init", url))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def list_semantic_as_of(self, **kwargs):
            calls.append(("list_semantic_as_of", kwargs))
            return [
                {
                    "id": uuid4(),
                    "namespace": "demo:payments",
                    "content": "as-of memory",
                    "writer": "demo.seed",
                    "source_ref": "demo:seed",
                    "justification": "test",
                    "metadata": {},
                    "t_valid": datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
                    "t_invalid": datetime(2026, 7, 12, 15, 0, tzinfo=UTC),
                    "written_at": datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
                    "invalidated_at": datetime(2026, 7, 12, 14, 5, tzinfo=UTC),
                    "invalidated_by": "demo.operator",
                    "invalidation_reason": "scheduled correction",
                    "snapshot_invalidated": False,
                }
            ]

    monkeypatch.setattr(snapshots, "MemoryStore", FakeMemoryStore)
    monkeypatch.setattr(snapshots, "_memory_operations", lambda **kwargs: [])

    snapshot = snapshots.memory_snapshot(
        namespace="demo:payments",
        as_of="2026-07-12T14:00:00+00:00",
        db_url="postgresql://db",
    )

    assert snapshot["mode"] == "as_of"
    assert snapshot["memories"][0]["content"] == "as-of memory"
    assert snapshot["memories"][0]["status"] == "current"
    assert "2026-07-12T15:00:00+00:00" not in snapshot["timeline"]
    assert snapshot["operations"] == []
    assert calls[1][1]["namespace"] == "demo:payments"
    assert calls[1][1]["system_as_of"].isoformat() == "2026-07-12T14:00:00+00:00"
    assert calls[1][1]["valid_at"].isoformat() == "2026-07-12T14:00:00+00:00"


def test_memory_queries_fetch_recent_rows_then_return_display_order(monkeypatch):
    import hindsight.snapshots as snapshots

    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def execute(self, query, params):
            executed.append((query, params))

        def fetchall(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def cursor(self, *, row_factory):
            assert row_factory is snapshots.dict_row
            return FakeCursor()

    monkeypatch.setattr(snapshots, "connect", lambda url: FakeConnection())

    snapshots._semantic_memories(
        namespace="demo:payments", db_url="postgresql://db", limit=100
    )
    snapshots._memory_operations(
        namespace="demo:payments",
        db_url="postgresql://db",
        limit=100,
    )

    memory_query, memory_params = executed[0]
    operation_query, operation_params = executed[1]

    assert "FROM (" in memory_query
    assert "COALESCE(invalidated_at, written_at, t_invalid, t_valid) DESC" in memory_query
    assert "ORDER BY t_valid ASC" in memory_query
    assert memory_params == ("demo:payments", 100)
    assert "ORDER BY created_at DESC" in operation_query
    assert "ORDER BY created_at ASC" in operation_query
    assert operation_params == ("demo:payments", 100)


def test_historical_operation_queries_set_mvcc_cutoff_before_snapshot_reads(monkeypatch):
    import hindsight.snapshots as snapshots

    operation_id = uuid4()
    executed = []
    transaction_events = []

    class FakeCursor:
        last_query = ""

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def execute(self, query, params):
            assert connection.in_transaction
            self.last_query = query
            executed.append((query, params))

        def fetchall(self):
            if "FROM memory_operation_effects" in self.last_query:
                return [{"operation_id": operation_id, "sequence": 1}]
            return [{"id": operation_id, "status": "queued"}]

    class FakeTransaction:
        def __enter__(self):
            assert not connection.in_transaction
            connection.in_transaction = True
            transaction_events.append("enter")

        def __exit__(self, *exc_info):
            transaction_events.append("exit")
            connection.in_transaction = False

    class FakeConnection:
        in_transaction = False

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def transaction(self):
            return FakeTransaction()

        def execute(self, query):
            assert self.in_transaction
            executed.append((query.as_string(), None))

        def cursor(self, *, row_factory):
            assert row_factory is snapshots.dict_row
            return FakeCursor()

    connection = FakeConnection()
    monkeypatch.setattr(snapshots, "connect", lambda url: connection)

    rows = snapshots._memory_operations(
        namespace="demo:payments",
        db_url="postgresql://db",
        limit=100,
        as_of=datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
    )

    assert transaction_events == ["enter", "exit"]
    assert executed[0] == (
        "SET TRANSACTION AS OF SYSTEM TIME '2026-07-12T14:00:00+00:00'",
        None,
    )
    assert "FROM memory_operations" in executed[1][0]
    assert executed[1][1] == ("demo:payments", 100)
    assert "FROM memory_operation_effects" in executed[2][0]
    assert executed[2][1] == (operation_id,)
    assert rows[0]["status"] == "queued"
    assert rows[0]["effects"] == [{"operation_id": operation_id, "sequence": 1}]


@requires_db
def test_historical_operation_snapshot_reads_row_and_effects_at_same_mvcc_cutoff():
    from psycopg.types.json import Jsonb

    from hindsight.db import connect, database_url
    from hindsight.snapshots import _memory_operations

    namespace = f"test:snapshot-history:{uuid4()}"
    operation_id = uuid4()
    invalidated_memory_id = uuid4()
    with connect() as conn:
        try:
            with conn.transaction():
                before_enqueue = conn.execute("SELECT now()").fetchone()[0]
            with conn.transaction():
                conn.execute(
                    """
                        INSERT INTO memory_operations (
                            id, operation_type, actor, reason, namespace, status,
                            expected_revisions, request_payload, attempt_count
                        )
                        VALUES (%s, 'rewind', 'test.snapshot', 'historical snapshot test',
                                %s, 'queued', '{}'::JSONB, '{}'::JSONB, 0)
                    """,
                    (operation_id, namespace),
                )
            with conn.transaction():
                cutoff = conn.execute("SELECT now()").fetchone()[0]
            with conn.transaction():
                conn.execute(
                    """
                        UPDATE memory_operations
                        SET status = 'completed', invalidated_memory_ids = %s,
                            completed_at = now()
                        WHERE id = %s
                    """,
                    (Jsonb([str(invalidated_memory_id)]), operation_id),
                )
                conn.execute(
                    """
                        INSERT INTO memory_operation_effects (
                            operation_id, sequence, effect_type, namespace
                        )
                        VALUES (%s, 1, 'unchanged', %s)
                    """,
                    (operation_id, namespace),
                )

            historical = _memory_operations(
                namespace=namespace,
                db_url=database_url(),
                limit=100,
                as_of=cutoff,
            )
            before = _memory_operations(
                namespace=namespace,
                db_url=database_url(),
                limit=100,
                as_of=before_enqueue,
            )
            current = _memory_operations(
                namespace=namespace,
                db_url=database_url(),
                limit=100,
            )

            assert before == []
            assert len(historical) == 1
            assert historical[0]["status"] == "queued"
            assert historical[0]["invalidated_memory_ids"] == []
            assert historical[0]["failure_code"] is None
            assert historical[0]["effects"] == []
            assert len(current) == 1
            assert current[0]["status"] == "completed"
            assert current[0]["invalidated_memory_ids"] == [str(invalidated_memory_id)]
            assert len(current[0]["effects"]) == 1
        finally:
            with conn.transaction():
                conn.execute("DELETE FROM memory_operations WHERE id = %s", (operation_id,))
