"""Bi-temporal memory storage with provenance.

This module is the narrow write path for Hindsight's own memory rows. Memories
are invalidated, never deleted, so audit queries can still reconstruct what the
agent believed before a correction or rewind.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect, database_url
from hindsight.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingProvider,
    vector_literal,
)

MemoryKind = Literal["episodic", "semantic"]


class ProvenanceError(ValueError):
    """Raised when a memory write or read record lacks required provenance."""


@dataclass(frozen=True)
class Provenance:
    """Origin metadata required for every memory write."""

    writer: str
    source_ref: str
    justification: str

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("writer", self.writer),
                ("source_ref", self.source_ref),
                ("justification", self.justification),
            )
            if not value or not value.strip()
        ]
        if missing:
            raise ProvenanceError(f"Missing provenance field(s): {', '.join(missing)}")


@dataclass(frozen=True)
class RewindResult:
    """The audited result of restoring memory to an earlier belief state."""

    operation: dict[str, Any]
    restored_memories: list[dict[str, Any]]
    invalidated_memories: list[dict[str, Any]]


class MemoryStore:
    """Product-facing memory API backed by CockroachDB.

    Agent code should use ``remember``, ``recall``, ``invalidate``, and
    ``rewind`` instead of issuing raw SQL. Each memory row carries provenance,
    and corrections are explicit invalidations rather than silent deletes.
    """

    def __init__(
        self,
        conn: psycopg.Connection | None = None,
        url: str | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self._url = url or (database_url() if conn is None else None)
        self._conn = conn or connect(self._url)
        self._owns_connection = conn is None
        self._embedding_provider = embedding_provider

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    def remember(
        self,
        *,
        memory_kind: MemoryKind,
        content: str,
        provenance: Provenance,
        namespace: str | None = None,
        episode_id: str | None = None,
        role: str | None = None,
        metadata: dict[str, Any] | None = None,
        t_valid: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a new belief with provenance.

        Semantic memories require a ``namespace`` so later recalls and rewinds
        can be scoped to one incident or agent. Episodic memories require an
        ``episode_id`` and ``role`` so the conversation history remains
        reconstructable.
        """

        with self._conn.transaction():
            if memory_kind == "semantic":
                if not namespace or not namespace.strip():
                    raise ProvenanceError("namespace is required for semantic memory")
                return self.write_semantic(
                    namespace=namespace,
                    content=content,
                    provenance=provenance,
                    metadata=metadata,
                    t_valid=t_valid,
                )
            if memory_kind == "episodic":
                if not episode_id or not episode_id.strip():
                    raise ProvenanceError("episode_id is required for episodic memory")
                if not role or not role.strip():
                    raise ProvenanceError("role is required for episodic memory")
                return self.write_episodic(
                    episode_id=episode_id,
                    role=role,
                    content=content,
                    provenance=provenance,
                    metadata=metadata,
                    t_valid=t_valid,
                )
        raise ValueError(f"Unsupported memory kind: {memory_kind}")

    def recall(
        self,
        *,
        query: str,
        namespace: str,
        as_of: datetime | None = None,
        limit: int = 5,
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve semantic beliefs for an incident namespace.

        With no ``as_of`` timestamp, recall uses vector similarity when an
        embedding provider is configured. With ``as_of``, recall reconstructs
        the belief set visible at that point in CockroachDB time, then applies
        valid-time filters so rewinds can re-plan from a past state.
        """

        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not namespace or not namespace.strip():
            raise ProvenanceError("namespace is required")
        if as_of is None and self._embedding_provider is not None:
            return self.recall_semantic(
                namespace=namespace,
                query=query,
                limit=limit,
                decision_id=decision_id,
                reader=reader,
                purpose=purpose,
            )
        if as_of is not None:
            rows = self._semantic_beliefs_as_of(
                namespace=namespace,
                as_of=as_of,
                limit=limit,
                query=query,
            )
            with self._conn.transaction():
                self._record_retrieval(
                    rows,
                    memory_kind="semantic",
                    decision_id=decision_id,
                    reader=reader,
                    purpose=purpose,
                )
            return rows

        with self._conn.transaction():
            rows = self._fetch_all(
                """
                    SELECT *
                    FROM current_semantic_memories
                    WHERE namespace = %s
                        AND content ILIKE %s
                    ORDER BY t_valid DESC, written_at DESC
                    LIMIT %s
                """,
                (namespace, f"%{query}%", limit),
            )
            if not rows:
                rows = self._fetch_all(
                    """
                        SELECT *
                        FROM current_semantic_memories
                        WHERE namespace = %s
                        ORDER BY t_valid DESC, written_at DESC
                        LIMIT %s
                    """,
                    (namespace, limit),
                )
            self._record_retrieval(
                rows,
                memory_kind="semantic",
                decision_id=decision_id,
                reader=reader,
                purpose=purpose,
            )
            return rows

    def write_episodic(
        self,
        *,
        episode_id: str,
        role: str,
        content: str,
        provenance: Provenance,
        metadata: dict[str, Any] | None = None,
        t_valid: datetime | None = None,
    ) -> dict[str, Any]:
        """Write an episodic memory row and return the inserted row."""

        provenance.validate()
        query = """
            INSERT INTO episodic_memories (
                episode_id, role, content, metadata, t_valid,
                writer, source_ref, justification
            )
            VALUES (%s, %s, %s, %s, COALESCE(%s, now()), %s, %s, %s)
            RETURNING *
        """
        params = (
            episode_id,
            role,
            content,
            Jsonb(metadata or {}),
            t_valid,
            provenance.writer,
            provenance.source_ref,
            provenance.justification,
        )
        return self._fetch_one(query, params)

    def write_semantic(
        self,
        *,
        namespace: str,
        content: str,
        provenance: Provenance,
        metadata: dict[str, Any] | None = None,
        t_valid: datetime | None = None,
    ) -> dict[str, Any]:
        """Write a semantic memory row and return the inserted row."""

        provenance.validate()
        if not namespace or not namespace.strip():
            raise ProvenanceError("namespace is required")
        embedding = None
        if self._embedding_provider is not None:
            self._validate_embedding_provider_dimensions()
            embedding = self._embedding_provider.embed(content)
            self._validate_semantic_embedding(embedding)
        query = """
            INSERT INTO semantic_memories (
                namespace, content, metadata, t_valid,
                writer, source_ref, justification
            )
            VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s, %s)
            RETURNING *
        """
        params = (
            namespace,
            content,
            Jsonb(metadata or {}),
            t_valid,
            provenance.writer,
            provenance.source_ref,
            provenance.justification,
        )

        def write_row() -> dict[str, Any]:
            row = self._fetch_one(query, params)
            if self._embedding_provider is not None and embedding is not None:
                self._insert_semantic_embedding(
                    memory_id=str(row["id"]),
                    namespace=namespace,
                    embedding=embedding,
                )
            return row

        return self._in_savepoint(write_row)

    def invalidate(
        self,
        *,
        memory_id: str,
        reason: str,
        memory_kind: MemoryKind = "semantic",
        actor: str | None = None,
        invalidated_by: str | None = None,
        t_invalid: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Invalidate a memory by updating valid time; rows are not deleted."""

        invalidator = actor or invalidated_by
        if not invalidator or not invalidator.strip():
            raise ProvenanceError("actor is required")
        if not reason or not reason.strip():
            raise ProvenanceError("reason is required")

        with self._conn.transaction():
            return self._invalidate_one(
                memory_kind=memory_kind,
                memory_id=memory_id,
                invalidated_by=invalidator,
                reason=reason,
                t_invalid=t_invalid,
            )

    def rewind(
        self,
        *,
        timestamp: datetime,
        reason: str,
        actor: str,
        namespace: str | None = None,
    ) -> RewindResult:
        """Restore the active belief set to an earlier CockroachDB timestamp.

        Rewind is intentionally a state mutation, not only a historical query.
        It reconstructs the semantic belief set at ``timestamp``, invalidates
        current memories written later, follows one or more read-provenance hops
        to invalidate derived memories, records an auditable operation row, and
        returns the restored beliefs for immediate replanning.
        """

        if not actor or not actor.strip():
            raise ProvenanceError("actor is required")
        if not reason or not reason.strip():
            raise ProvenanceError("reason is required")

        restored = self._semantic_beliefs_as_of(
            namespace=namespace,
            as_of=timestamp,
            limit=None,
            query=None,
        )

        with self._conn.transaction():
            invalidated = self._semantic_rewind_candidates(
                timestamp=timestamp,
                namespace=namespace,
            )
            invalidated_by_id = {str(row["id"]): row for row in invalidated}
            pending_ids = set(invalidated_by_id)

            while pending_ids:
                derived = self._derived_semantic_memories(
                    memory_ids=sorted(pending_ids),
                    namespace=namespace,
                )
                next_ids = {
                    str(row["id"])
                    for row in derived
                    if str(row["id"]) not in invalidated_by_id
                }
                for row in derived:
                    invalidated_by_id.setdefault(str(row["id"]), row)
                pending_ids = next_ids

            invalidated_rows = []
            for memory_id in sorted(invalidated_by_id):
                memory = invalidated_by_id[memory_id]
                invalid_at = timestamp
                if memory["t_valid"] > timestamp:
                    invalid_at = memory["t_valid"]
                row = self._invalidate_one(
                    memory_kind="semantic",
                    memory_id=memory_id,
                    invalidated_by=actor,
                    reason=reason,
                    t_invalid=invalid_at,
                )
                if row is not None:
                    invalidated_rows.append(row)

            operation = self._record_memory_operation(
                operation_type="rewind",
                actor=actor,
                reason=reason,
                target_timestamp=timestamp,
                namespace=namespace,
                invalidated_memory_ids=[str(row["id"]) for row in invalidated_rows],
                restored_memory_ids=[str(row["id"]) for row in restored],
            )
            return RewindResult(
                operation=operation,
                restored_memories=restored,
                invalidated_memories=invalidated_rows,
            )

    def _invalidate_one(
        self,
        *,
        memory_kind: MemoryKind,
        memory_id: str,
        invalidated_by: str,
        reason: str,
        t_invalid: datetime | None = None,
    ) -> dict[str, Any] | None:
        table = self._table_for_kind(memory_kind)
        query = f"""
            UPDATE {table}
            SET
                t_invalid = COALESCE(%s, now()),
                invalidated_by = %s,
                invalidation_reason = %s,
                invalidated_at = now()
            WHERE id = %s AND t_invalid IS NULL
            RETURNING *
        """
        return self._fetch_optional(query, (t_invalid, invalidated_by, reason, memory_id))

    def current_episodic(
        self,
        *,
        episode_id: str | None = None,
        limit: int | None = None,
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-invalidated episodic memories, optionally tracking the read."""

        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        limit_clause = " LIMIT %s" if limit is not None else ""
        with self._conn.transaction():
            if episode_id:
                params: tuple[Any, ...] = (episode_id,) if limit is None else (episode_id, limit)
                rows = self._fetch_all(
                    f"""
                        SELECT *
                        FROM current_episodic_memories
                        WHERE episode_id = %s
                        ORDER BY t_valid DESC, written_at DESC
                        {limit_clause}
                    """,
                    params,
                )
            else:
                params = () if limit is None else (limit,)
                rows = self._fetch_all(
                    f"""
                        SELECT *
                        FROM current_episodic_memories
                        ORDER BY t_valid DESC, written_at DESC
                        {limit_clause}
                    """,
                    params,
                )
            self._record_retrieval(
                rows,
                memory_kind="episodic",
                decision_id=decision_id,
                reader=reader,
                purpose=purpose,
            )
            return rows

    def current_semantic(
        self,
        *,
        namespace: str | None = None,
        limit: int | None = None,
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-invalidated semantic memories, optionally tracking the read."""

        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        if namespace is not None and not namespace.strip():
            raise ProvenanceError("namespace is required")
        limit_clause = " LIMIT %s" if limit is not None else ""
        with self._conn.transaction():
            if namespace is not None:
                params: tuple[Any, ...] = (namespace,) if limit is None else (namespace, limit)
                rows = self._fetch_all(
                    f"""
                        SELECT *
                        FROM current_semantic_memories
                        WHERE namespace = %s
                        ORDER BY t_valid DESC, written_at DESC
                        {limit_clause}
                    """,
                    params,
                )
            else:
                params = () if limit is None else (limit,)
                rows = self._fetch_all(
                    f"""
                        SELECT *
                        FROM current_semantic_memories
                        ORDER BY t_valid DESC, written_at DESC
                        {limit_clause}
                    """,
                    params,
                )
            self._record_retrieval(
                rows,
                memory_kind="semantic",
                decision_id=decision_id,
                reader=reader,
                purpose=purpose,
            )
            return rows

    def recall_semantic(
        self,
        *,
        namespace: str,
        query: str,
        limit: int = 5,
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return nearest current semantic memories within one namespace."""

        if self._embedding_provider is None:
            raise RuntimeError("recall_semantic requires an embedding provider")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query_vector = vector_literal(
            self._embedding_provider.embed(query),
            dimensions=self._embedding_provider.dimensions,
        )
        with self._conn.transaction():
            rows = self._fetch_all(
                f"""
                    SELECT
                        m.*,
                        e.provider AS embedding_provider,
                        e.model AS embedding_model,
                        e.embedded_at,
                        e.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS}) AS distance
                    FROM current_semantic_memories AS m
                    JOIN semantic_memory_embeddings AS e
                        ON e.memory_id = m.id
                    WHERE m.namespace = %s
                    ORDER BY e.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})
                    LIMIT %s
                """,
                (query_vector, namespace, query_vector, limit),
            )
            self._record_retrieval(
                rows,
                memory_kind="semantic",
                decision_id=decision_id,
                reader=reader,
                purpose=purpose,
            )
            return rows

    def recall_similar_incidents(
        self,
        *,
        namespace: str,
        query: str,
        service_slug: str,
        limit: int = 5,
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find similar current memories and join them to incident facts.

        This is the demo showpiece query: one CockroachDB statement combines
        namespace-scoped vector similarity, explicit memory validity, and
        transactional filters over incidents, services, and runbooks.
        """

        if self._embedding_provider is None:
            raise RuntimeError("recall_similar_incidents requires an embedding provider")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not namespace or not namespace.strip():
            raise ProvenanceError("namespace is required")
        if not service_slug or not service_slug.strip():
            raise ProvenanceError("service_slug is required")

        query_vector = vector_literal(
            self._embedding_provider.embed(query),
            dimensions=self._embedding_provider.dimensions,
        )
        with self._conn.transaction():
            rows = self._fetch_all(
                f"""
                    SELECT
                        m.id,
                        m.id AS memory_id,
                        m.content AS memory_content,
                        e.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS}) AS distance,
                        i.slug AS incident_slug,
                        i.title AS incident_title,
                        i.severity,
                        s.slug AS service_slug,
                        s.name AS service_name,
                        r.slug AS runbook_slug,
                        r.title AS runbook_title
                    FROM current_semantic_memories AS m
                    JOIN semantic_memory_embeddings AS e
                        ON e.memory_id = m.id
                    JOIN incident_semantic_memories AS im
                        ON im.memory_id = m.id
                    JOIN incidents AS i
                        ON i.id = im.incident_id
                    JOIN incident_services AS isvc
                        ON isvc.incident_id = i.id
                    JOIN services AS s
                        ON s.id = isvc.service_id
                    LEFT JOIN (
                        SELECT
                            ir.incident_id,
                            r.service_id,
                            r.slug,
                            r.title
                        FROM incident_runbooks AS ir
                        JOIN runbooks AS r
                            ON r.id = ir.runbook_id
                    ) AS r
                        ON r.incident_id = i.id
                        AND (r.service_id = s.id OR r.service_id IS NULL)
                    WHERE m.namespace = %s
                        AND s.slug = %s
                    ORDER BY e.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})
                    LIMIT %s
                """,
                (query_vector, namespace, service_slug, query_vector, limit),
            )
            self._record_retrieval(
                rows,
                memory_kind="semantic",
                decision_id=decision_id,
                reader=reader,
                purpose=purpose,
            )
            return rows

    def audit_memory(self, *, memory_kind: MemoryKind, memory_id: str) -> dict[str, Any] | None:
        """Return a memory row whether it is current or invalidated."""

        table = self._table_for_kind(memory_kind)
        return self._fetch_optional(f"SELECT * FROM {table} WHERE id = %s", (memory_id,))

    def provenance_for_memory(
        self, *, memory_kind: MemoryKind, memory_id: str
    ) -> dict[str, Any] | None:
        """Return origin and invalidation metadata for a memory row."""

        table = self._table_for_kind(memory_kind)
        return self._fetch_optional(
            f"""
                SELECT
                    id, writer, source_ref, justification, written_at,
                    invalidated_by, invalidation_reason, invalidated_at
                FROM {table}
                WHERE id = %s
            """,
            (memory_id,),
        )

    def record_read(
        self,
        *,
        decision_id: str,
        memory_kind: MemoryKind,
        memory_id: str,
        reader: str,
        purpose: str,
    ) -> dict[str, Any]:
        """Record that a decision read a specific memory row."""

        self._table_for_kind(memory_kind)
        if not decision_id or not decision_id.strip():
            raise ProvenanceError("decision_id is required")
        if not reader or not reader.strip():
            raise ProvenanceError("reader is required")
        if not purpose or not purpose.strip():
            raise ProvenanceError("purpose is required")
        return self._fetch_one(
            """
                INSERT INTO memory_reads (
                    decision_id, memory_kind, memory_id, reader, purpose
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            """,
            (decision_id, memory_kind, memory_id, reader, purpose),
        )

    def reads_for_decision(self, *, decision_id: str) -> list[dict[str, Any]]:
        """Return memory reads attached to one agent or human decision."""

        return self._fetch_all(
            """
                SELECT *
                FROM memory_reads
                WHERE decision_id = %s
                ORDER BY read_at ASC
            """,
            (decision_id,),
        )

    def memories_for_decision(self, *, decision_id: str) -> list[dict[str, Any]]:
        """Return read records joined to their episodic or semantic memory rows."""

        return self._fetch_all(
            """
                SELECT
                    r.id AS read_id,
                    r.decision_id,
                    r.memory_kind,
                    r.memory_id,
                    r.reader,
                    r.purpose,
                    r.read_at,
                    e.content AS episodic_content,
                    e.writer AS episodic_writer,
                    e.source_ref AS episodic_source_ref,
                    s.content AS semantic_content,
                    s.writer AS semantic_writer,
                    s.source_ref AS semantic_source_ref
                FROM memory_reads AS r
                LEFT JOIN episodic_memories AS e
                    ON r.memory_kind = 'episodic' AND r.memory_id = e.id
                LEFT JOIN semantic_memories AS s
                    ON r.memory_kind = 'semantic' AND r.memory_id = s.id
                WHERE r.decision_id = %s
                ORDER BY r.read_at ASC
            """,
            (decision_id,),
        )

    def record_reads(
        self,
        *,
        decision_id: str,
        memory_kind: MemoryKind,
        memory_ids: Iterable[str],
        reader: str,
        purpose: str,
    ) -> list[dict[str, Any]]:
        """Record several reads for one decision and return the inserted rows."""

        return [
            self.record_read(
                decision_id=decision_id,
                memory_kind=memory_kind,
                memory_id=memory_id,
                reader=reader,
                purpose=purpose,
            )
            for memory_id in memory_ids
        ]

    def _semantic_beliefs_as_of(
        self,
        *,
        namespace: str | None,
        as_of: datetime,
        limit: int | None,
        query: str | None,
    ) -> list[dict[str, Any]]:
        where = [
            "t_valid <= %s",
            "(t_invalid IS NULL OR t_invalid > %s)",
        ]
        params: list[Any] = [as_of, as_of]
        if namespace is not None:
            where.append("namespace = %s")
            params.append(namespace)
        if query:
            where.append("content ILIKE %s")
            params.append(f"%{query}%")
        query_sql = f"""
            SELECT *, NULL::FLOAT8 AS distance
            FROM semantic_memories
            WHERE {" AND ".join(where)}
            ORDER BY t_valid DESC, written_at DESC
        """
        if limit is not None:
            query_sql += " LIMIT %s"
            params.append(limit)
        with connect(self._historical_read_url()) as read_conn:
            as_of_literal = sql.Literal(as_of.isoformat()).as_string(read_conn)
            with read_conn.transaction():
                read_conn.execute(f"SET TRANSACTION AS OF SYSTEM TIME {as_of_literal}")
                rows = self._fetch_all_on(read_conn, query_sql, tuple(params))
        if query and not rows:
            return self._semantic_beliefs_as_of(
                namespace=namespace,
                as_of=as_of,
                limit=limit,
                query=None,
            )
        return rows

    def _semantic_rewind_candidates(
        self,
        *,
        timestamp: datetime,
        namespace: str | None,
    ) -> list[dict[str, Any]]:
        where = ["written_at > %s"]
        params: list[Any] = [timestamp]
        if namespace is not None:
            where.append("namespace = %s")
            params.append(namespace)
        return self._fetch_all(
            f"""
                SELECT *
                FROM current_semantic_memories
                WHERE {" AND ".join(where)}
                ORDER BY written_at ASC
            """,
            tuple(params),
        )

    def _derived_semantic_memories(
        self,
        *,
        memory_ids: list[str],
        namespace: str | None,
    ) -> list[dict[str, Any]]:
        if not memory_ids:
            return []
        placeholders = ", ".join(["%s"] * len(memory_ids))
        params: list[Any] = [*memory_ids]
        namespace_filter = ""
        if namespace is not None:
            namespace_filter = "AND m.namespace = %s"
            params.append(namespace)
        return self._fetch_all(
            f"""
                SELECT DISTINCT m.*
                FROM current_semantic_memories AS m
                JOIN memory_reads AS r
                    ON m.source_ref = r.decision_id
                WHERE r.memory_kind = 'semantic'
                    AND r.memory_id IN ({placeholders})
                    {namespace_filter}
                ORDER BY m.written_at ASC
            """,
            tuple(params),
        )

    def _record_memory_operation(
        self,
        *,
        operation_type: str,
        actor: str,
        reason: str,
        target_timestamp: datetime,
        namespace: str | None,
        invalidated_memory_ids: list[str],
        restored_memory_ids: list[str],
    ) -> dict[str, Any]:
        return self._fetch_one(
            """
                INSERT INTO memory_operations (
                    operation_type, actor, reason, target_timestamp, namespace,
                    invalidated_memory_ids, restored_memory_ids
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """,
            (
                operation_type,
                actor,
                reason,
                target_timestamp,
                namespace,
                Jsonb(invalidated_memory_ids),
                Jsonb(restored_memory_ids),
            ),
        )

    def _insert_semantic_embedding(
        self,
        *,
        memory_id: str,
        namespace: str,
        embedding: list[float],
    ) -> dict[str, Any]:
        if self._embedding_provider is None:
            raise RuntimeError("embedding provider is not configured")
        self._validate_semantic_embedding(embedding)
        return self._fetch_one(
            f"""
                INSERT INTO semantic_memory_embeddings (
                    memory_id, namespace, embedding, provider, model, dimensions
                )
                VALUES (%s, %s, %s::VECTOR({EMBEDDING_DIMENSIONS}), %s, %s, %s)
                RETURNING memory_id, namespace, provider, model, dimensions, embedded_at
            """,
            (
                memory_id,
                namespace,
                vector_literal(embedding),
                self._embedding_provider.provider_name,
                self._embedding_provider.model_name,
                self._embedding_provider.dimensions,
            ),
        )

    def _record_retrieval(
        self,
        rows: list[dict[str, Any]],
        *,
        memory_kind: MemoryKind,
        decision_id: str | None,
        reader: str | None,
        purpose: str | None,
    ) -> None:
        if decision_id is None and reader is None and purpose is None:
            return
        if decision_id is None or reader is None or purpose is None:
            raise ProvenanceError(
                "decision_id, reader, and purpose are all required to track a retrieval"
            )
        self.record_reads(
            decision_id=decision_id,
            memory_kind=memory_kind,
            memory_ids=(str(row["id"]) for row in rows),
            reader=reader,
            purpose=purpose,
        )

    @staticmethod
    def _table_for_kind(memory_kind: MemoryKind) -> str:
        if memory_kind == "episodic":
            return "episodic_memories"
        if memory_kind == "semantic":
            return "semantic_memories"
        raise ValueError(f"Unsupported memory kind: {memory_kind}")

    def _fetch_one(self, query: str, params: tuple[Any, ...]) -> dict[str, Any]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("Expected one row, got none")
            return dict(row)

    def _fetch_optional(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def _fetch_all(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _fetch_all_on(
        conn: psycopg.Connection, query: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def _historical_read_url(self) -> str:
        if self._url is not None:
            return self._url
        return self._conn.info.dsn

    def _validate_embedding_provider_dimensions(self) -> None:
        if self._embedding_provider is None:
            raise RuntimeError("embedding provider is not configured")
        if self._embedding_provider.dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"semantic vector store expects {EMBEDDING_DIMENSIONS} dimensions, "
                f"got {self._embedding_provider.dimensions}"
            )

    def _validate_semantic_embedding(self, embedding: list[float]) -> None:
        self._validate_embedding_provider_dimensions()
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"expected {EMBEDDING_DIMENSIONS} dimensions, got {len(embedding)}")

    def _in_savepoint(self, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if self._conn.autocommit:
            return callback()

        savepoint = sql.Identifier(f"hindsight_memory_{uuid4().hex}").as_string(self._conn)
        self._conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = callback()
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result
