"""Tests for the live memory dashboard helpers."""

from datetime import UTC, datetime
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
    assert calls[1][1]["namespace"] == "demo:payments"
    assert calls[1][1]["query"] == ""
    assert calls[1][1]["as_of"].isoformat() == "2026-07-12T14:00:00+00:00"


def test_dashboard_html_contains_sse_and_timeline_surface():
    from hindsight.dashboard import dashboard_html

    html = dashboard_html(default_namespace="demo:payments")

    assert "Memory Dashboard" in html
    assert "new EventSource" in html
    assert "/events?namespace=" in html
    assert 'id="timeline"' in html
    assert "Current Beliefs" in html
    assert "Rewinds" in html
