"""Current and historical governed-memory snapshots for the product API."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.rows import dict_row

from hindsight.db import connect, database_url
from hindsight.memory import MemoryStore

MAX_SNAPSHOT_ROWS = 100


def memory_snapshot(
    *,
    namespace: str,
    as_of: str | datetime | None = None,
    db_url: str | None = None,
    limit: int = MAX_SNAPSHOT_ROWS,
) -> dict[str, Any]:
    """Return current or historical memory state for the product cockpit."""

    if not namespace or not namespace.strip():
        raise ValueError("namespace is required")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    timestamp = _parse_timestamp(as_of) if as_of else None
    if timestamp is not None:
        with MemoryStore(url=db_url or database_url()) as store:
            memories = store.list_semantic_as_of(
                namespace=namespace,
                system_as_of=timestamp,
                valid_at=timestamp,
                limit=limit,
            )
        operations = _memory_operations(
            namespace=namespace,
            db_url=db_url,
            limit=limit,
            as_of=timestamp,
        )
        return {
            "type": "snapshot",
            "mode": "as_of",
            "namespace": namespace,
            "as_of": timestamp.isoformat(),
            "memories": [_normalize_memory(row) for row in memories],
            "operations": [_normalize_operation(row) for row in operations],
            "timeline": _timeline(memories, operations, cutoff=timestamp),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    memories = _semantic_memories(namespace=namespace, db_url=db_url, limit=limit)
    operations = _memory_operations(namespace=namespace, db_url=db_url, limit=limit)
    return {
        "type": "snapshot",
        "mode": "current",
        "namespace": namespace,
        "as_of": None,
        "memories": [_normalize_memory(row) for row in memories],
        "operations": [_normalize_operation(row) for row in operations],
        "timeline": _timeline(memories, operations),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _semantic_memories(
    *, namespace: str, db_url: str | None, limit: int
) -> list[dict[str, Any]]:
    with connect(db_url or database_url()) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT *
                    FROM (
                        SELECT *
                        FROM semantic_memories
                        WHERE namespace = %s
                        ORDER BY
                            COALESCE(invalidated_at, written_at, t_invalid, t_valid) DESC,
                            written_at DESC,
                            id DESC
                        LIMIT %s
                    ) AS recent_memories
                    ORDER BY t_valid ASC, written_at ASC, id ASC
                """,
                (namespace, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def _memory_operations(
    *,
    namespace: str,
    db_url: str | None,
    limit: int,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    with connect(db_url or database_url()) as conn:
        if as_of is not None:
            as_of_statement = sql.SQL("SET TRANSACTION AS OF SYSTEM TIME {}").format(
                sql.Literal(as_of.isoformat())
            )
            with conn.transaction():
                conn.execute(as_of_statement)
                return _memory_operation_rows(conn, namespace=namespace, limit=limit)
        return _memory_operation_rows(conn, namespace=namespace, limit=limit)


def _memory_operation_rows(
    conn: Any,
    *,
    namespace: str,
    limit: int,
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT *
                FROM (
                    SELECT *
                    FROM memory_operations
                    WHERE namespace = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                ) AS recent_operations
                ORDER BY created_at ASC, id ASC
            """,
            (namespace, limit),
        )
        operations = [dict(row) for row in cur.fetchall()]
        for operation in operations:
            cur.execute(
                """
                    SELECT * FROM memory_operation_effects
                    WHERE operation_id = %s
                    ORDER BY sequence
                """,
                (operation["id"],),
            )
            operation["effects"] = [dict(row) for row in cur.fetchall()]
        return operations


def _normalize_memory(row: dict[str, Any]) -> dict[str, Any]:
    invalidated = row.get(
        "snapshot_invalidated",
        row.get("t_invalid") is not None or row.get("invalidated_at") is not None,
    )
    return {
        "id": str(row.get("id")),
        "namespace": row.get("namespace"),
        "content": row.get("content"),
        "writer": row.get("writer"),
        "source_ref": row.get("source_ref"),
        "justification": row.get("justification"),
        "metadata": row.get("metadata") or {},
        "belief_id": str(row.get("belief_id")) if row.get("belief_id") else None,
        "version_number": row.get("version_number"),
        "content_schema": row.get("content_schema"),
        "lineage_status": row.get("lineage_status"),
        "trust_status": row.get("trust_status"),
        "t_valid": _jsonable(row.get("t_valid")),
        "t_invalid": _jsonable(row.get("t_invalid")),
        "written_at": _jsonable(row.get("written_at")),
        "invalidated_at": _jsonable(row.get("invalidated_at")),
        "invalidated_by": row.get("invalidated_by"),
        "invalidation_reason": row.get("invalidation_reason"),
        "status": "invalidated" if invalidated else "current",
    }


def _normalize_operation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "operation_type": row.get("operation_type"),
        "actor": row.get("actor"),
        "reason": row.get("reason"),
        "namespace": row.get("namespace"),
        "target_timestamp": _jsonable(row.get("target_timestamp")),
        "invalidated_memory_ids": row.get("invalidated_memory_ids") or [],
        "restored_memory_ids": row.get("restored_memory_ids") or [],
        "status": row.get("status"),
        "failure_code": row.get("failure_code"),
        "failure_detail": row.get("failure_detail"),
        "effects": [_jsonable(item) for item in row.get("effects") or []],
        "created_at": _jsonable(row.get("created_at")),
    }


def _timeline(
    memories: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    *,
    cutoff: datetime | None = None,
) -> list[str]:
    values = set()
    for row in memories:
        for key in ("t_valid", "written_at", "t_invalid", "invalidated_at"):
            if row.get(key) is not None:
                value = row[key]
                if cutoff is None or not isinstance(value, datetime) or value <= cutoff:
                    values.add(_jsonable(value))
    for row in operations:
        for key in ("target_timestamp", "created_at"):
            if row.get(key) is not None:
                value = row[key]
                if cutoff is None or not isinstance(value, datetime) or value <= cutoff:
                    values.add(_jsonable(value))
    return sorted(str(value) for value in values if value is not None)


def _parse_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


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
