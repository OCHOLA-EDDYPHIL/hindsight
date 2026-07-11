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
