"""Tests for Hindsight's MCP memory inspection server."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def test_create_mcp_server_registers_tools():
    from hindsight.mcp_server import create_mcp_server

    server = create_mcp_server()

    assert server.name == "hindsight-memory"
    for tool in server._tool_manager._tools.values():
        assert "actor" not in tool.parameters.get("properties", {})
        assert "purpose" not in tool.parameters.get("properties", {})


@requires_db
def test_mcp_decision_trace_shows_memory_provenance_in_one_view():
    from hindsight.db import connect, database_url
    from hindsight.mcp_server import inspect_decision_trace
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"mcp-decision-trace-{uuid4()}"
    decision_id = f"agent:{uuid4()}:plan"
    with MemoryStore(url=database_url()) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="rotate certificates to repair checkout latency",
            provenance=Provenance(
                writer="demo.poison",
                source_ref="demo:simulated-memory-poisoning",
                justification="Seed poisoned memory for trace inspection",
            ),
        )
        store.record_read(
            decision_id=decision_id,
            memory_kind="semantic",
            memory_id=str(memory["id"]),
            reader="agent.recall",
            purpose="retrieve semantic incident context",
        )

    result = inspect_decision_trace(
        decision_id=decision_id,
        actor="pytest.mcp",
        purpose="verify one-view decision trace",
        db_url=database_url(),
    )

    assert result["tool"] == "decision_trace"
    assert result["decision_id"] == decision_id
    assert result["count"] == 1
    traced = result["memories"][0]
    assert traced["read"]["reader"] == "agent.recall"
    assert traced["memory"]["id"] == str(memory["id"])
    assert traced["provenance"]["writer"] == "demo.poison"
    assert traced["provenance"]["source_ref"] == "demo:simulated-memory-poisoning"
    assert traced["status"] == "current"

    with connect(database_url()) as conn:
        audit = conn.execute(
            """
                SELECT tool_name, actor, result_count
                FROM mcp_audit_events
                WHERE id = %s
            """,
            (result["audit_event_id"],),
        ).fetchone()

    assert audit == ("decision_trace", "pytest.mcp", 1)


@requires_db
def test_mcp_current_beliefs_records_memory_and_tool_audit():
    from hindsight.db import connect, database_url
    from hindsight.mcp_server import inspect_current_beliefs
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"mcp-current-{uuid4()}"
    with MemoryStore(url=database_url()) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="checkout latency improved after retry fanout was throttled",
            provenance=Provenance(
                writer="pytest.mcp",
                source_ref="test:mcp-current",
                justification="Seed MCP current-beliefs fixture",
            ),
        )

    result = inspect_current_beliefs(
        namespace=namespace,
        actor="pytest.mcp",
        purpose="verify MCP current belief inspection",
        db_url=database_url(),
    )

    assert result["count"] == 1
    assert result["memories"][0]["id"] == str(memory["id"])

    with connect(database_url()) as conn:
        read = conn.execute(
            """
                SELECT reader, purpose
                FROM memory_reads
                WHERE decision_id = %s AND memory_id = %s
            """,
            (result["decision_id"], memory["id"]),
        ).fetchone()
        audit = conn.execute(
            """
                SELECT tool_name, actor, result_count
                FROM mcp_audit_events
                WHERE id = %s
            """,
            (result["audit_event_id"],),
        ).fetchone()

    assert read == ("pytest.mcp", "verify MCP current belief inspection")
    assert audit == ("current_beliefs", "pytest.mcp", 1)


@requires_db
def test_mcp_current_beliefs_rejects_blank_semantic_namespace():
    from hindsight.db import database_url
    from hindsight.mcp_server import inspect_current_beliefs

    with pytest.raises(ValueError, match="namespace is required"):
        inspect_current_beliefs(namespace="", db_url=database_url())
    with pytest.raises(ValueError, match="namespace is required"):
        inspect_current_beliefs(namespace=None, db_url=database_url())


def test_mcp_beliefs_as_of_passes_requested_url_to_memory_store(monkeypatch):
    import hindsight.mcp_server as mcp_server

    captured: dict[str, str | None] = {}

    class FakeConnection:
        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, *exc_info: object) -> None:
            pass

        def commit(self) -> None:
            pass

    class FakeMemoryStore:
        def __init__(self, *, conn: FakeConnection, url: str | None = None):
            captured["url"] = url

        def list_semantic_as_of(self, **kwargs: object) -> list[dict[str, object]]:
            return []

    monkeypatch.setattr(mcp_server, "connect", lambda url: FakeConnection())
    monkeypatch.setattr(mcp_server, "MemoryStore", FakeMemoryStore)
    monkeypatch.setattr(
        mcp_server,
        "_record_mcp_audit_event",
        lambda *args, **kwargs: {"id": uuid4()},
    )

    result = mcp_server.inspect_beliefs_as_of(
        namespace="incident-test",
        as_of="2026-07-12T00:00:00+00:00",
        db_url="postgresql://staging-db",
    )

    assert captured["url"] == "postgresql://staging-db"
    assert result["tool"] == "beliefs_as_of"


@requires_db
def test_mcp_provenance_chain_and_audit_log_are_visible():
    from hindsight.db import database_url
    from hindsight.mcp_server import inspect_mcp_audit_log, inspect_provenance_chain
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"mcp-provenance-{uuid4()}"
    with MemoryStore(url=database_url()) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="search errors recovered after rolling back the deploy candidate",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:search-rollback",
                justification="Capture prior incident resolution",
            ),
        )

    result = inspect_provenance_chain(
        memory_kind="semantic",
        memory_id=str(memory["id"]),
        actor="pytest.mcp",
        purpose="verify MCP provenance inspection",
        db_url=database_url(),
    )

    assert result["memory"]["id"] == str(memory["id"])
    assert result["provenance"]["writer"] == "agent.reflect"
    assert result["reads"]

    audit = inspect_mcp_audit_log(
        limit=5,
        actor="pytest.mcp",
        purpose="verify MCP audit log visibility",
        db_url=database_url(),
    )

    assert audit["count"] >= 1
    assert any(event["tool_name"] == "provenance_chain" for event in audit["events"])
    assert any(event["id"] == audit["audit_event_id"] for event in audit["events"])
