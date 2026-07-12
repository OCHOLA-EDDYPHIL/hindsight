"""Tests for the live memory dashboard helpers."""

from datetime import UTC, datetime
from threading import Event
from uuid import uuid4


def test_changefeed_row_to_event_filters_namespace_and_marks_invalidated():
    from hindsight.dashboard import changefeed_row_to_event

    memory_id = uuid4()
    row = {
        "table": "semantic_memories",
        "key": f'["{memory_id}"]',
        "value": {
            "id": str(memory_id),
            "namespace": "demo:payments",
            "content": "poisoned memory",
            "writer": "demo.poison",
            "source_ref": "demo:poison",
            "justification": "test",
            "metadata": {},
            "t_valid": "2026-07-12T14:00:00+00:00",
            "t_invalid": "2026-07-12T14:01:00+00:00",
            "written_at": "2026-07-12T14:00:00+00:00",
            "invalidated_at": "2026-07-12T14:02:00+00:00",
            "invalidated_by": "demo.operator",
            "invalidation_reason": "bad memory",
        },
        "updated": "2026-07-12T14:02:00+00:00",
    }

    event = changefeed_row_to_event(row, namespace="demo:payments")
    ignored = changefeed_row_to_event(row, namespace="demo:other")

    assert ignored is None
    assert event is not None
    assert event["event"] == "memory"
    assert event["memory"]["id"] == str(memory_id)
    assert event["memory"]["status"] == "invalidated"
    assert event["memory"]["invalidation_reason"] == "bad memory"


def test_changefeed_row_to_event_accepts_cockroach_after_and_resolved_payloads():
    from hindsight.dashboard import changefeed_row_to_event

    memory_id = uuid4()
    memory_event = changefeed_row_to_event(
        {
            "table": "semantic_memories",
            "key": f'["{memory_id}"]',
            "value": {
                "after": {
                    "id": str(memory_id),
                    "namespace": "demo:payments",
                    "content": "streamed memory",
                    "writer": "demo.seed",
                    "source_ref": "demo:seed",
                    "justification": "test",
                    "metadata": {},
                    "t_valid": "2026-07-12T14:00:00Z",
                    "t_invalid": None,
                    "written_at": "2026-07-12T14:00:00Z",
                    "invalidated_at": None,
                    "invalidated_by": None,
                    "invalidation_reason": None,
                },
                "updated": "1783870400683795863.0000000000",
            },
        },
        namespace="demo:payments",
    )
    resolved_event = changefeed_row_to_event(
        {
            "table": None,
            "key": None,
            "value": b'{"resolved":"1783870400683795863.0000000000"}',
        },
        namespace="demo:payments",
    )

    assert memory_event is not None
    assert memory_event["event"] == "memory"
    assert memory_event["updated"] == "1783870400683795863.0000000000"
    assert memory_event["memory"]["content"] == "streamed memory"
    assert resolved_event == {
        "event": "resolved",
        "type": "resolved",
        "namespace": "demo:payments",
        "resolved": "1783870400683795863.0000000000",
    }


def test_memory_snapshot_current_includes_memories_operations_and_timeline(monkeypatch):
    import hindsight.dashboard as dashboard

    memory_id = uuid4()
    operation_id = uuid4()
    monkeypatch.setattr(
        dashboard,
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
        dashboard,
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

    snapshot = dashboard.memory_snapshot(namespace="demo:payments")

    assert snapshot["mode"] == "current"
    assert snapshot["memories"][0]["content"] == "retry fanout"
    assert snapshot["memories"][0]["status"] == "current"
    assert snapshot["operations"][0]["operation_type"] == "rewind"
    assert "2026-07-12T14:00:00+00:00" in snapshot["timeline"]
    assert "2026-07-12T14:02:00+00:00" in snapshot["timeline"]


def test_memory_snapshot_as_of_uses_memory_store_recall(monkeypatch):
    import hindsight.dashboard as dashboard

    calls = []

    class FakeMemoryStore:
        def __init__(self, *, url):
            calls.append(("init", url))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def recall(self, **kwargs):
            calls.append(("recall", kwargs))
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
                    "t_invalid": None,
                    "written_at": datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
                    "invalidated_at": None,
                    "invalidated_by": None,
                    "invalidation_reason": None,
                }
            ]

    monkeypatch.setattr(dashboard, "MemoryStore", FakeMemoryStore)
    monkeypatch.setattr(dashboard, "_memory_operations", lambda **kwargs: [])

    snapshot = dashboard.memory_snapshot(
        namespace="demo:payments",
        as_of="2026-07-12T14:00:00+00:00",
        db_url="postgresql://db",
    )

    assert snapshot["mode"] == "as_of"
    assert snapshot["memories"][0]["content"] == "as-of memory"
    assert snapshot["operations"] == []
    assert calls[1][1]["namespace"] == "demo:payments"
    assert calls[1][1]["query"] == ""
    assert calls[1][1]["as_of"].isoformat() == "2026-07-12T14:00:00+00:00"


def test_dashboard_broker_shares_one_changefeed_and_caches_events():
    import hindsight.dashboard as dashboard

    memory_id = str(uuid4())
    started = Event()
    continue_feed = Event()
    calls = {"snapshots": 0, "changefeeds": 0}

    def snapshot_loader(**kwargs):
        calls["snapshots"] += 1
        return {
            "type": "snapshot",
            "mode": "current",
            "namespace": kwargs["namespace"],
            "as_of": None,
            "memories": [],
            "operations": [],
            "timeline": [],
            "generated_at": "2026-07-12T14:00:00+00:00",
        }

    def changefeed_loader(**kwargs):
        calls["changefeeds"] += 1
        assert kwargs["cursor"].tzinfo is not None
        started.set()
        continue_feed.wait(timeout=2)
        yield {
            "event": "memory",
            "type": "memory",
            "namespace": kwargs["namespace"],
            "memory": {
                "id": memory_id,
                "namespace": kwargs["namespace"],
                "content": "retry fanout",
                "writer": "demo.seed",
                "source_ref": "demo:seed",
                "justification": "test",
                "metadata": {},
                "t_valid": "2026-07-12T14:00:00+00:00",
                "t_invalid": None,
                "written_at": "2026-07-12T14:00:00+00:00",
                "invalidated_at": None,
                "invalidated_by": None,
                "invalidation_reason": None,
                "status": "current",
            },
        }

    broker = dashboard.DashboardBroker(
        namespace="demo:payments",
        snapshot_loader=snapshot_loader,
        changefeed_loader=changefeed_loader,
    )
    try:
        first = broker.subscribe()
        second = broker.subscribe()

        assert started.is_set()
        assert calls == {"snapshots": 1, "changefeeds": 1}
        assert first.snapshot["memories"] == []
        assert second.snapshot["memories"] == []

        continue_feed.set()
        first_event = first.events.get(timeout=2)
        second_event = second.events.get(timeout=2)

        assert first_event is not None
        assert second_event is not None
        assert first_event["event"] == "memory"
        assert second_event["memory"]["id"] == memory_id

        late = broker.subscribe()

        assert calls == {"snapshots": 1, "changefeeds": 1}
        assert late.snapshot["memories"][0]["id"] == memory_id
    finally:
        broker.close()


def test_changefeed_events_uses_autocommit_cursor_now_and_ready_callback(monkeypatch):
    import hindsight.dashboard as dashboard

    executed = []
    ready = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def stream(self, query, params):
            executed.append((query, params))
            yield {
                "resolved": "2026-07-12T14:00:00+00:00",
                "table": None,
                "value": None,
            }

    class FakeConnection:
        def __init__(self):
            self.autocommit = False

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def cursor(self, *, row_factory):
            assert row_factory is dashboard.dict_row
            return FakeCursor()

    connection = FakeConnection()
    stop_event = Event()

    def on_ready():
        ready.append(connection.autocommit)
        stop_event.set()

    monkeypatch.setattr(dashboard, "connect", lambda url: connection)

    events = list(
        dashboard.changefeed_events(
            namespace="demo:payments",
            db_url="postgresql://db",
            stop_event=stop_event,
            cursor=datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
            on_ready=on_ready,
        )
    )

    assert ready == [True]
    assert events == []
    assert "cursor = %s" in executed[0][0]
    assert executed[0][1] == ("2026-07-12T14:00:00+00:00",)


def test_memory_queries_fetch_recent_rows_then_return_display_order(monkeypatch):
    import hindsight.dashboard as dashboard

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
            assert row_factory is dashboard.dict_row
            return FakeCursor()

    monkeypatch.setattr(dashboard, "connect", lambda url: FakeConnection())

    dashboard._semantic_memories(namespace="demo:payments", db_url="postgresql://db", limit=100)
    dashboard._memory_operations(
        namespace="demo:payments",
        db_url="postgresql://db",
        limit=100,
        as_of=datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
    )

    memory_query, memory_params = executed[0]
    operation_query, operation_params = executed[1]

    assert "FROM (" in memory_query
    assert "COALESCE(invalidated_at, written_at, t_invalid, t_valid) DESC" in memory_query
    assert "ORDER BY t_valid ASC" in memory_query
    assert memory_params == ("demo:payments", 100)
    assert "created_at <= %s" in operation_query
    assert "ORDER BY created_at DESC" in operation_query
    assert "ORDER BY created_at ASC" in operation_query
    assert operation_params == ("demo:payments", datetime(2026, 7, 12, 14, 0, tzinfo=UTC), 100)


def test_dashboard_html_contains_sse_and_timeline_surface():
    from hindsight.dashboard import dashboard_html

    html = dashboard_html(default_namespace="demo:payments")

    assert "Memory Dashboard" in html
    assert "new EventSource" in html
    assert "/events?namespace=" in html
    assert 'id="timeline"' in html
    assert "Current Beliefs" in html
    assert "Rewinds" in html
    assert "AbortController" in html
    assert "setTimeout" in html
