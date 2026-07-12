"""MCP server for read-only Hindsight memory inspection."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect, database_url
from hindsight.memory import MemoryKind, MemoryStore

MCP_READER = "mcp.memory_inspector"
MCP_ACTOR_ENV = "HINDSIGHT_MCP_ACTOR"
CURRENT_BELIEFS_PURPOSE = "Inspect current Hindsight memory through MCP"
BELIEFS_AS_OF_PURPOSE = "Inspect Hindsight belief state at a timestamp through MCP"
PROVENANCE_CHAIN_PURPOSE = "Inspect Hindsight memory provenance through MCP"
DECISION_TRACE_PURPOSE = "Inspect one decision's memory provenance through MCP"
MCP_AUDIT_LOG_PURPOSE = "Inspect recent Hindsight MCP audit events"
MAX_LIMIT = 100


def create_mcp_server(*, db_url: str | None = None) -> FastMCP:
    """Create the Hindsight memory-inspection MCP server."""

    server = FastMCP(
        "hindsight-memory",
        instructions=(
            "Read-only inspection tools for Hindsight memory, provenance, "
            "as-of belief state, and MCP audit events."
        ),
    )

    @server.tool()
    def current_beliefs(
        namespace: str,
        memory_kind: Literal["semantic", "episodic"] = "semantic",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return current, non-invalidated memories and audit the MCP read."""

        return inspect_current_beliefs(
            namespace=namespace,
            memory_kind=memory_kind,
            limit=limit,
            actor=_mcp_actor(),
            purpose=CURRENT_BELIEFS_PURPOSE,
            db_url=db_url,
        )

    @server.tool()
    def beliefs_as_of(
        namespace: str,
        as_of: str,
        query: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return semantic memories visible at an ISO-8601 timestamp."""

        return inspect_beliefs_as_of(
            namespace=namespace,
            as_of=as_of,
            query=query,
            limit=limit,
            actor=_mcp_actor(),
            purpose=BELIEFS_AS_OF_PURPOSE,
            db_url=db_url,
        )

    @server.tool()
    def provenance_chain(
        memory_id: str,
        memory_kind: Literal["semantic", "episodic"] = "semantic",
    ) -> dict[str, Any]:
        """Return a memory row, origin metadata, and decisions that read it."""

        return inspect_provenance_chain(
            memory_id=memory_id,
            memory_kind=memory_kind,
            actor=_mcp_actor(),
            purpose=PROVENANCE_CHAIN_PURPOSE,
            db_url=db_url,
        )

    @server.tool()
    def decision_trace(
        decision_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return every memory read by one decision with provenance."""

        return inspect_decision_trace(
            decision_id=decision_id,
            limit=limit,
            actor=_mcp_actor(),
            purpose=DECISION_TRACE_PURPOSE,
            db_url=db_url,
        )

    @server.tool()
    def mcp_audit_log(
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return recent MCP audit events, including this audit-log read."""

        return inspect_mcp_audit_log(
            limit=limit,
            actor=_mcp_actor(),
            purpose=MCP_AUDIT_LOG_PURPOSE,
            db_url=db_url,
        )

    return server


def inspect_current_beliefs(
    *,
    namespace: str | None = None,
    memory_kind: MemoryKind = "semantic",
    limit: int = 10,
    actor: str = MCP_READER,
    purpose: str = "Inspect current Hindsight memory through MCP",
    db_url: str | None = None,
) -> dict[str, Any]:
    """Return current memories and record both memory-read and MCP audit rows."""

    limit = _validated_limit(limit)
    if not namespace or not namespace.strip():
        raise ValueError("namespace is required")
    decision_id = _decision_id("current-beliefs")
    with connect(db_url) as conn:
        store = MemoryStore(conn=conn)
        if memory_kind == "semantic":
            rows = store.current_semantic(
                namespace=namespace,
                limit=limit,
                decision_id=decision_id,
                reader=actor,
                purpose=purpose,
            )
        elif memory_kind == "episodic":
            rows = store.current_episodic(
                episode_id=namespace,
                limit=limit,
                decision_id=decision_id,
                reader=actor,
                purpose=purpose,
            )
        else:
            raise ValueError(f"Unsupported memory kind: {memory_kind}")
        audit_event = _record_mcp_audit_event(
            conn,
            tool_name="current_beliefs",
            actor=actor,
            purpose=purpose,
            arguments={
                "namespace": namespace,
                "memory_kind": memory_kind,
                "limit": limit,
                "decision_id": decision_id,
            },
            result_count=len(rows),
        )
        conn.commit()
    return {
        "tool": "current_beliefs",
        "decision_id": decision_id,
        "audit_event_id": str(audit_event["id"]),
        "count": len(rows),
        "memories": _jsonable(rows),
    }


def inspect_beliefs_as_of(
    *,
    namespace: str,
    as_of: str,
    query: str | None = None,
    limit: int = 10,
    actor: str = MCP_READER,
    purpose: str = "Inspect Hindsight belief state at a timestamp through MCP",
    db_url: str | None = None,
) -> dict[str, Any]:
    """Return semantic memories valid at a timestamp and audit the read."""

    if not namespace or not namespace.strip():
        raise ValueError("namespace is required")
    limit = _validated_limit(limit)
    timestamp = _parse_timestamp(as_of)
    decision_id = _decision_id("beliefs-as-of")
    resolved_db_url = db_url or database_url()
    with connect(resolved_db_url) as conn:
        store = MemoryStore(conn=conn, url=resolved_db_url)
        rows = store.recall(
            namespace=namespace,
            query=query or "",
            as_of=timestamp,
            limit=limit,
            decision_id=decision_id,
            reader=actor,
            purpose=purpose,
        )
        audit_event = _record_mcp_audit_event(
            conn,
            tool_name="beliefs_as_of",
            actor=actor,
            purpose=purpose,
            arguments={
                "namespace": namespace,
                "as_of": timestamp.isoformat(),
                "query": query,
                "limit": limit,
                "decision_id": decision_id,
            },
            result_count=len(rows),
        )
        conn.commit()
    return {
        "tool": "beliefs_as_of",
        "decision_id": decision_id,
        "audit_event_id": str(audit_event["id"]),
        "namespace": namespace,
        "as_of": timestamp.isoformat(),
        "count": len(rows),
        "memories": _jsonable(rows),
    }


def inspect_provenance_chain(
    *,
    memory_id: str,
    memory_kind: MemoryKind = "semantic",
    actor: str = MCP_READER,
    purpose: str = "Inspect Hindsight memory provenance through MCP",
    db_url: str | None = None,
) -> dict[str, Any]:
    """Return memory provenance and downstream read records."""

    decision_id = _decision_id("provenance-chain")
    with connect(db_url) as conn:
        store = MemoryStore(conn=conn)
        memory = store.audit_memory(memory_kind=memory_kind, memory_id=memory_id)
        if memory is None:
            raise ValueError(f"{memory_kind} memory not found: {memory_id}")
        provenance = store.provenance_for_memory(memory_kind=memory_kind, memory_id=memory_id)
        store.record_read(
            decision_id=decision_id,
            memory_kind=memory_kind,
            memory_id=memory_id,
            reader=actor,
            purpose=purpose,
        )
        downstream_reads = _memory_reads(
            conn,
            memory_kind=memory_kind,
            memory_id=memory_id,
        )
        audit_event = _record_mcp_audit_event(
            conn,
            tool_name="provenance_chain",
            actor=actor,
            purpose=purpose,
            arguments={
                "memory_id": memory_id,
                "memory_kind": memory_kind,
                "decision_id": decision_id,
            },
            result_count=1,
        )
        conn.commit()
    return {
        "tool": "provenance_chain",
        "decision_id": decision_id,
        "audit_event_id": str(audit_event["id"]),
        "memory": _jsonable(memory),
        "provenance": _jsonable(provenance),
        "reads": _jsonable(downstream_reads),
    }


def inspect_decision_trace(
    *,
    decision_id: str,
    limit: int = 20,
    actor: str = MCP_READER,
    purpose: str = "Inspect one decision's memory provenance through MCP",
    db_url: str | None = None,
) -> dict[str, Any]:
    """Return a decision-to-memory provenance chain in one audited view."""

    if not decision_id or not decision_id.strip():
        raise ValueError("decision_id is required")
    limit = _validated_limit(limit)
    inspection_decision_id = _decision_id("decision-trace")
    with connect(db_url) as conn:
        store = MemoryStore(conn=conn)
        reads = store.reads_for_decision(decision_id=decision_id)[:limit]
        memories = []
        for read in reads:
            memory_kind = read["memory_kind"]
            memory_id = str(read["memory_id"])
            memory = store.audit_memory(memory_kind=memory_kind, memory_id=memory_id)
            provenance = store.provenance_for_memory(
                memory_kind=memory_kind,
                memory_id=memory_id,
            )
            store.record_read(
                decision_id=inspection_decision_id,
                memory_kind=memory_kind,
                memory_id=memory_id,
                reader=actor,
                purpose=purpose,
            )
            memories.append(
                {
                    "read": read,
                    "memory": memory,
                    "provenance": provenance,
                    "status": "invalidated"
                    if provenance and provenance.get("invalidated_at") is not None
                    else "current",
                }
            )
        audit_event = _record_mcp_audit_event(
            conn,
            tool_name="decision_trace",
            actor=actor,
            purpose=purpose,
            arguments={
                "decision_id": decision_id,
                "limit": limit,
                "inspection_decision_id": inspection_decision_id,
            },
            result_count=len(memories),
        )
        conn.commit()
    return {
        "tool": "decision_trace",
        "decision_id": decision_id,
        "inspection_decision_id": inspection_decision_id,
        "audit_event_id": str(audit_event["id"]),
        "count": len(memories),
        "memories": _jsonable(memories),
    }


def inspect_mcp_audit_log(
    *,
    limit: int = 20,
    actor: str = MCP_READER,
    purpose: str = "Inspect recent Hindsight MCP audit events",
    db_url: str | None = None,
) -> dict[str, Any]:
    """Return recent MCP audit events and record this audit-log read."""

    limit = _validated_limit(limit)
    with connect(db_url) as conn:
        audit_event = _record_mcp_audit_event(
            conn,
            tool_name="mcp_audit_log",
            actor=actor,
            purpose=purpose,
            arguments={"limit": limit},
            result_count=0,
        )
        rows = _mcp_audit_events(conn, limit=limit)
        _update_mcp_audit_result_count(conn, audit_event_id=str(audit_event["id"]), count=len(rows))
        for row in rows:
            if str(row["id"]) == str(audit_event["id"]):
                row["result_count"] = len(rows)
        conn.commit()
    return {
        "tool": "mcp_audit_log",
        "audit_event_id": str(audit_event["id"]),
        "count": len(rows),
        "events": _jsonable(rows),
    }


def run_stdio_server() -> None:
    """Run the MCP server over stdio."""

    create_mcp_server().run("stdio")


def _record_mcp_audit_event(
    conn: Any,
    *,
    tool_name: str,
    actor: str,
    purpose: str,
    arguments: dict[str, Any],
    result_count: int,
) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                INSERT INTO mcp_audit_events (
                    tool_name, actor, purpose, arguments, result_count
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            """,
            (tool_name, actor, purpose, Jsonb(arguments), result_count),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("Expected MCP audit event row")
    return dict(row)


def _update_mcp_audit_result_count(conn: Any, *, audit_event_id: str, count: int) -> None:
    conn.execute(
        """
            UPDATE mcp_audit_events
            SET result_count = %s
            WHERE id = %s
        """,
        (count, audit_event_id),
    )


def _memory_reads(conn: Any, *, memory_kind: MemoryKind, memory_id: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT *
                FROM memory_reads
                WHERE memory_kind = %s AND memory_id = %s
                ORDER BY read_at ASC
            """,
            (memory_kind, memory_id),
        )
        return [dict(row) for row in cur.fetchall()]


def _mcp_audit_events(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT *
                FROM mcp_audit_events
                ORDER BY created_at DESC
                LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def _parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp


def _validated_limit(value: int) -> int:
    if value < 1:
        raise ValueError("limit must be at least 1")
    if value > MAX_LIMIT:
        raise ValueError(f"limit must be at most {MAX_LIMIT}")
    return value


def _mcp_actor() -> str:
    return os.environ.get(MCP_ACTOR_ENV, "mcp.local_client")


def _decision_id(prefix: str) -> str:
    return f"mcp:{prefix}:{uuid4()}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value
