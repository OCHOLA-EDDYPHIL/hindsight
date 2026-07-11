"""Bi-temporal memory storage with provenance.

This module is the narrow write path for Hindsight's own memory rows. Memories
are invalidated, never deleted, so audit queries can still reconstruct what the
agent believed before a correction or rewind.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect
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


class MemoryStore:
    """Small SQL wrapper for valid-time memory writes, reads, and audit trails."""

    def __init__(
        self,
        conn: psycopg.Connection | None = None,
        url: str | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self._conn = conn or connect(url)
        self._owns_connection = conn is None
        self._embedding_provider = embedding_provider

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

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

        embedding = None
        if self._embedding_provider is not None:
            embedding = self._embedding_provider.embed(content)
        provenance.validate()
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
        row = self._fetch_one(query, params)
        if self._embedding_provider is not None and embedding is not None:
            self._insert_semantic_embedding(
                memory_id=str(row["id"]),
                namespace=namespace,
                embedding=embedding,
            )
        return row

    def invalidate(
        self,
        *,
        memory_kind: MemoryKind,
        memory_id: str,
        invalidated_by: str,
        reason: str,
        t_invalid: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Invalidate a memory by updating valid time; rows are not deleted."""

        if not invalidated_by or not invalidated_by.strip():
            raise ProvenanceError("invalidated_by is required")
        if not reason or not reason.strip():
            raise ProvenanceError("reason is required")

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
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-invalidated episodic memories, optionally tracking the read."""

        if episode_id:
            rows = self._fetch_all(
                """
                    SELECT *
                    FROM current_episodic_memories
                    WHERE episode_id = %s
                    ORDER BY t_valid DESC, written_at DESC
                """,
                (episode_id,),
            )
        else:
            rows = self._fetch_all(
                """
                    SELECT *
                    FROM current_episodic_memories
                    ORDER BY t_valid DESC, written_at DESC
                """,
                (),
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
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-invalidated semantic memories, optionally tracking the read."""

        if namespace:
            rows = self._fetch_all(
                """
                    SELECT *
                    FROM current_semantic_memories
                    WHERE namespace = %s
                    ORDER BY t_valid DESC, written_at DESC
                """,
                (namespace,),
            )
        else:
            rows = self._fetch_all(
                """
                    SELECT *
                    FROM current_semantic_memories
                    ORDER BY t_valid DESC, written_at DESC
                """,
                (),
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

    def _insert_semantic_embedding(
        self,
        *,
        memory_id: str,
        namespace: str,
        embedding: list[float],
    ) -> dict[str, Any]:
        if self._embedding_provider is None:
            raise RuntimeError("embedding provider is not configured")
        if self._embedding_provider.dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"semantic vector store expects {EMBEDDING_DIMENSIONS} dimensions, "
                f"got {self._embedding_provider.dimensions}"
            )
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
