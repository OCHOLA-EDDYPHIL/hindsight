"""Bi-temporal memory storage with provenance.

This module is the narrow write path for Hindsight's own memory rows. Memories
are invalidated, never deleted, so audit queries can still reconstruct what the
agent believed before a correction or rewind.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.errors import SerializationFailure
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect, database_url
from hindsight.embedding_index import lock_embedding_index_write_fence
from hindsight.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingProfile,
    EmbeddingProvider,
    embedding_profile,
    vector_literal,
)
from hindsight.prompt_safety import (
    PROMPT_SAFETY_METADATA_KEYS,
    assess_prompt_safety,
)
from hindsight.tracing import memory_ids, set_span_attributes, start_span

MemoryKind = Literal["episodic", "semantic"]
RetrievalPolicy = Literal["semantic_strict", "semantic_then_keyword"]
TrustStatus = Literal["active", "review_required"]
OperatorDisposition = Literal["approved", "rejected", "unreviewed"]
SafetyStatus = Literal["safe", "unsafe", "unassessed"]
ContradictionStatus = Literal["supported", "contradicted", "unassessed"]
UsageInstruction = Literal["positive_guidance", "audit_only"]
MAX_OWNED_WRITE_TRANSACTION_ATTEMPTS = 3
RecallMode = Literal[
    "semantic_strict",
    "semantic_then_keyword",
    "current_text",
    "as_of_text",
    "as_of_list",
]


class ProvenanceError(ValueError):
    """Raised when a memory write or read record lacks required provenance."""


class MemorySelectionChangedError(RuntimeError):
    """Raised when approval-bound memory is no longer the current selection."""


@dataclass(frozen=True)
class MemoryGovernance:
    """Typed governance projected into immutable semantic-memory metadata."""

    operator_disposition: OperatorDisposition
    safety_status: SafetyStatus
    contradiction_status: ContradictionStatus
    usage_instruction: UsageInstruction

    def __post_init__(self) -> None:
        allowed = {
            "operator_disposition": {"approved", "rejected", "unreviewed"},
            "safety_status": {"safe", "unsafe", "unassessed"},
            "contradiction_status": {"supported", "contradicted", "unassessed"},
            "usage_instruction": {"positive_guidance", "audit_only"},
        }
        for name, values in allowed.items():
            if getattr(self, name) not in values:
                raise ProvenanceError(f"unsupported memory governance field: {name}")

    def metadata(self) -> dict[str, str]:
        return {
            "operator_disposition": self.operator_disposition,
            "safety_status": self.safety_status,
            "contradiction_status": self.contradiction_status,
            "usage_instruction": self.usage_instruction,
        }


APPROVED_POSITIVE_GUIDANCE = MemoryGovernance(
    operator_disposition="approved",
    safety_status="safe",
    contradiction_status="supported",
    usage_instruction="positive_guidance",
)


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


@dataclass(frozen=True)
class ReadContext:
    """Identity attached to an auditable retrieval."""

    decision_id: str
    reader: str
    purpose: str


@dataclass(frozen=True)
class RetrievalAttempt:
    strategy: str
    outcome: Literal["selected", "empty", "error", "skipped"]
    result_count: int
    error_code: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class RetrievalResult:
    retrieval_id: str
    decision_id: str
    policy: str
    policy_version: int
    status: Literal["succeeded", "empty", "degraded", "failed"]
    selected_strategy: str | None
    fallback_reason: str | None
    embedding_profile: EmbeddingProfile | None
    attempts: tuple[RetrievalAttempt, ...]
    hits: tuple[dict[str, Any], ...]


class MemoryStore:
    """Product-facing memory API backed by CockroachDB.

    Agent code should use ``remember`` and explicit retrieval methods instead
    of issuing raw SQL. Governed corrections run through
    :mod:`hindsight.operations`; each memory row carries provenance, and
    corrections create auditable versions or invalidations rather than deletes.
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

    def namespace_revision(self, *, namespace: str) -> int:
        """Return the current revision for a semantic-memory namespace."""

        if not namespace or not namespace.strip():
            raise ProvenanceError("namespace is required")
        with self._conn.transaction():
            row = self._fetch_optional(
                "SELECT revision FROM memory_namespaces WHERE namespace = %s",
                (namespace,),
            )
            return int(row["revision"]) if row is not None else 0

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
        content_schema: str = "memory.v1",
        structured_payload: dict[str, Any] | None = None,
        producer_decision_id: str | None = None,
        parent_memory_ids: Iterable[str] | None = None,
        precomputed_embedding: list[float] | None = None,
        trust_status: TrustStatus = "active",
        governance: MemoryGovernance | None = None,
    ) -> dict[str, Any]:
        """Persist a new belief with provenance.

        Semantic memories require a ``namespace`` so later recalls and rewinds
        can be scoped to one incident or agent. Episodic memories require an
        ``episode_id`` and ``role`` so the conversation history remains
        reconstructable.
        """

        if memory_kind != "semantic" and (
            precomputed_embedding is not None or governance is not None or trust_status != "active"
        ):
            raise ValueError(
                "semantic trust, governance, and precomputed embeddings require semantic memory"
            )

        prepared_embedding = None
        if memory_kind == "semantic":
            provenance.validate()
            if not namespace or not namespace.strip():
                raise ProvenanceError("namespace is required for semantic memory")
            if self._embedding_provider is not None or precomputed_embedding is not None:
                prepared_embedding, _ = self._prepare_semantic_embedding(
                    content=content,
                    precomputed_embedding=precomputed_embedding,
                )

        with start_span(
            "hindsight.memory.remember",
            {
                "hindsight.memory.operation": "remember",
                "hindsight.memory.kind": memory_kind,
                "hindsight.memory.namespace": namespace,
                "hindsight.memory.episode_id": episode_id,
                "hindsight.memory.role": role,
                "hindsight.provenance.writer": provenance.writer,
            },
        ) as span:
            attempts = MAX_OWNED_WRITE_TRANSACTION_ATTEMPTS if self._owns_connection else 1
            for attempt in range(1, attempts + 1):
                try:
                    with self._conn.transaction():
                        if memory_kind == "semantic":
                            if not namespace or not namespace.strip():
                                raise ProvenanceError("namespace is required for semantic memory")
                            memory = self.write_semantic(
                                namespace=namespace,
                                content=content,
                                provenance=provenance,
                                metadata=metadata,
                                t_valid=t_valid,
                                content_schema=content_schema,
                                structured_payload=structured_payload,
                                producer_decision_id=producer_decision_id,
                                parent_memory_ids=parent_memory_ids,
                                precomputed_embedding=prepared_embedding,
                                trust_status=trust_status,
                                governance=governance,
                            )
                            set_span_attributes(
                                span, {"hindsight.memory.id": str(memory["id"])}
                            )
                            return memory
                        if memory_kind == "episodic":
                            if not episode_id or not episode_id.strip():
                                raise ProvenanceError("episode_id is required for episodic memory")
                            if not role or not role.strip():
                                raise ProvenanceError("role is required for episodic memory")
                            memory = self.write_episodic(
                                episode_id=episode_id,
                                role=role,
                                content=content,
                                provenance=provenance,
                                metadata=metadata,
                                t_valid=t_valid,
                                content_schema=content_schema,
                                structured_payload=structured_payload,
                                producer_decision_id=producer_decision_id,
                                parent_memory_ids=parent_memory_ids,
                            )
                            set_span_attributes(
                                span, {"hindsight.memory.id": str(memory["id"])}
                            )
                            return memory
                        raise ValueError(f"Unsupported memory kind: {memory_kind}")
                except SerializationFailure:
                    if not self._owns_connection or attempt == attempts:
                        raise
                    self._reconnect_owned_connection()
            raise RuntimeError("owned memory write retry loop exited without a result")

    def _reconnect_owned_connection(self) -> None:
        if not self._owns_connection or self._url is None:
            raise RuntimeError("cannot reconnect a caller-owned memory connection")
        self._conn.close()
        self._conn = connect(self._url)

    def recall(
        self,
        *,
        mode: RecallMode,
        query: str,
        namespace: str,
        as_of: datetime | None = None,
        limit: int = 5,
        decision_id: str | None = None,
        reader: str | None = None,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper requiring an explicit retrieval mode."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not namespace or not namespace.strip():
            raise ProvenanceError("namespace is required")
        if mode in {"as_of_text", "as_of_list"} and as_of is None:
            raise ValueError(f"as_of is required for {mode}")
        if mode not in {"as_of_text", "as_of_list"} and as_of is not None:
            raise ValueError(f"as_of is not supported for {mode}")
        if mode == "as_of_list" and query.strip():
            raise ValueError("query must be empty for as_of_list")
        with start_span(
            "hindsight.memory.recall",
            {
                "hindsight.memory.operation": "recall",
                "hindsight.memory.kind": "semantic",
                "hindsight.memory.namespace": namespace,
                "hindsight.memory.limit": limit,
                "hindsight.memory.as_of": as_of,
                "hindsight.memory.decision_id": decision_id,
                "hindsight.memory.reader": reader,
                "hindsight.memory.recall_mode": mode,
            },
        ) as span:
            if mode in {"semantic_strict", "semantic_then_keyword"}:
                context = _optional_read_context(
                    decision_id=decision_id,
                    reader=reader,
                    purpose=purpose,
                )
                if context is None:
                    raise ProvenanceError(
                        "decision_id, reader, and purpose are required for semantic retrieval"
                    )
                rows = list(
                    self.retrieve_semantic(
                        namespace=namespace,
                        query=query,
                        decision_id=context.decision_id,
                        reader=context.reader,
                        purpose=context.purpose,
                        policy=mode,
                        limit=limit,
                    ).hits
                )
            else:
                context = _optional_read_context(
                    decision_id=decision_id,
                    reader=reader,
                    purpose=purpose,
                )
                if mode == "current_text":
                    rows = self.search_current_semantic_text(
                        namespace=namespace,
                        query=query,
                        limit=limit,
                        read_context=context,
                    )
                elif mode == "as_of_text":
                    rows = self.search_semantic_text_as_of(
                        namespace=namespace,
                        query=query,
                        system_as_of=as_of,
                        valid_at=as_of,
                        limit=limit,
                        read_context=context,
                    )
                elif mode == "as_of_list":
                    rows = self.list_semantic_as_of(
                        namespace=namespace,
                        system_as_of=as_of,
                        valid_at=as_of,
                        limit=limit,
                        read_context=context,
                    )
                else:
                    raise ValueError(f"unsupported recall mode: {mode}")
            set_span_attributes(
                span,
                {
                    "hindsight.memory.count": len(rows),
                    "hindsight.memory.ids": memory_ids(rows),
                },
            )
            return rows

    def list_current_semantic(
        self,
        *,
        namespace: str,
        limit: int = 100,
        read_context: ReadContext | None = None,
        include_review_required: bool = True,
    ) -> list[dict[str, Any]]:
        """List current semantic versions explicitly in recency order."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        trust_filter = "" if include_review_required else "AND trust_status = 'active'"
        rows = self._fetch_all(
            f"""
                SELECT *
                FROM current_semantic_memories
                WHERE namespace = %s
                    {trust_filter}
                ORDER BY t_valid DESC, written_at DESC
                LIMIT %s
            """,
            (namespace, limit),
        )
        self._record_with_context(rows, memory_kind="semantic", context=read_context)
        return rows

    def list_semantic_as_of(
        self,
        *,
        namespace: str,
        system_as_of: datetime,
        valid_at: datetime | None = None,
        limit: int = 100,
        read_context: ReadContext | None = None,
    ) -> list[dict[str, Any]]:
        """Reconstruct durable semantic state on independent system/valid-time axes."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        resolved_valid_at = valid_at or system_as_of
        rows = self._fetch_all_as_of(
            system_as_of=system_as_of,
            query="""
                SELECT *, NULL::FLOAT8 AS distance
                FROM semantic_memories
                WHERE namespace = %s
                    AND t_valid <= %s
                    AND (t_invalid IS NULL OR t_invalid > %s)
                ORDER BY t_valid DESC, written_at DESC
                LIMIT %s
            """,
            params=(
                namespace,
                resolved_valid_at,
                resolved_valid_at,
                limit,
            ),
        )
        rows = _project_historical_rows(rows, valid_at=resolved_valid_at)
        self._record_with_context(rows, memory_kind="semantic", context=read_context)
        return rows

    def search_current_semantic_text(
        self,
        *,
        namespace: str,
        query: str,
        limit: int = 5,
        read_context: ReadContext | None = None,
        positive_guidance_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search current trusted semantic content without fallback."""

        _require_query(query)
        rows = self._fetch_all(
            f"""
                SELECT memory.*, NULL::FLOAT8 AS distance
                FROM current_semantic_memories AS memory
                WHERE memory.namespace = %s
                    {_semantic_eligibility_sql("memory", positive_guidance_only)}
                    AND memory.content ILIKE %s ESCAPE '\\'
                ORDER BY memory.t_valid DESC, memory.written_at DESC
                LIMIT %s
            """,
            (namespace, f"%{_escape_like(query)}%", limit),
        )
        self._record_with_context(rows, memory_kind="semantic", context=read_context)
        return rows

    def search_semantic_text_as_of(
        self,
        *,
        namespace: str,
        query: str,
        system_as_of: datetime,
        valid_at: datetime | None = None,
        limit: int = 5,
        read_context: ReadContext | None = None,
    ) -> list[dict[str, Any]]:
        """Search durable historical content without unfiltered fallback."""

        _require_query(query)
        resolved_valid_at = valid_at or system_as_of
        rows = self._fetch_all_as_of(
            system_as_of=system_as_of,
            query="""
                SELECT *, NULL::FLOAT8 AS distance
                FROM semantic_memories
                WHERE namespace = %s
                    AND t_valid <= %s
                    AND (t_invalid IS NULL OR t_invalid > %s)
                    AND content ILIKE %s ESCAPE '\\'
                ORDER BY t_valid DESC, written_at DESC
                LIMIT %s
            """,
            params=(
                namespace,
                resolved_valid_at,
                resolved_valid_at,
                f"%{_escape_like(query)}%",
                limit,
            ),
        )
        rows = _project_historical_rows(rows, valid_at=resolved_valid_at)
        self._record_with_context(rows, memory_kind="semantic", context=read_context)
        return rows

    def search_semantic_vector(
        self,
        *,
        namespace: str,
        query_vector: list[float],
        profile_id: str,
        limit: int = 5,
        service_slug: str | None = None,
        read_context: ReadContext | None = None,
        positive_guidance_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search one exact vector profile and return no unrelated fallback rows."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        if len(query_vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"expected {EMBEDDING_DIMENSIONS} dimensions")
        profile = self._fetch_optional(
            "SELECT * FROM embedding_profiles WHERE id = %s", (profile_id,)
        )
        if profile is None:
            raise RuntimeError(f"embedding profile not found: {profile_id}")
        if profile["status"] != "active":
            raise RuntimeError(f"embedding profile is not active: {profile_id}")
        service_join = ""
        service_filter = ""
        params: list[Any] = [
            vector_literal(query_vector),
            namespace,
            namespace,
            profile_id,
        ]
        if service_slug:
            service_join = """
                JOIN incident_semantic_memories AS link
                    ON link.tenant_id = memory.tenant_id
                    AND link.memory_id = memory.id
                JOIN incident_services AS incident_service
                    ON incident_service.tenant_id = link.tenant_id
                    AND incident_service.incident_id = link.incident_id
                JOIN services AS service
                    ON service.tenant_id = incident_service.tenant_id
                    AND service.id = incident_service.service_id
            """
            service_filter = "AND service.slug = %s"
            params.append(service_slug)
        max_distance = profile.get("max_distance")
        distance_filter = ""
        if max_distance is not None:
            distance_filter = (
                f"AND (vector.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})) <= %s"
            )
            params.extend([vector_literal(query_vector), max_distance])
        params.append(limit)
        rows = self._fetch_all(
            f"""
                SELECT
                    memory.*,
                    vector.profile_id AS embedding_profile_id,
                    profile.provider AS embedding_provider,
                    profile.model AS embedding_model,
                    vector.embedded_at,
                    vector.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS}) AS distance
                FROM current_semantic_memories AS memory
                JOIN semantic_memory_vectors AS vector
                    ON vector.tenant_id = memory.tenant_id
                    AND vector.memory_id = memory.id
                JOIN embedding_profiles AS profile ON profile.id = vector.profile_id
                {service_join}
                WHERE memory.namespace = %s
                    {_semantic_eligibility_sql("memory", positive_guidance_only)}
                    AND vector.tenant_id = current_hindsight_tenant_id()
                    AND vector.namespace = %s
                    AND vector.profile_id = %s
                    {service_filter}
                    {distance_filter}
                ORDER BY distance
                LIMIT %s
            """,
            tuple(params),
        )
        self._record_with_context(rows, memory_kind="semantic", context=read_context)
        return rows

    def retrieve_semantic(
        self,
        *,
        namespace: str,
        query: str,
        decision_id: str,
        reader: str,
        purpose: str,
        policy: RetrievalPolicy = "semantic_strict",
        limit: int = 5,
        service_slug: str | None = None,
        positive_guidance_only: bool = False,
    ) -> RetrievalResult:
        """Execute and audit an explicit semantic retrieval policy.

        ``semantic_strict`` never falls back. ``semantic_then_keyword`` is an
        operator-visible degraded policy and records both attempts. A miss is
        an empty result, never an unrelated recency list.
        """

        _require_query(query)
        if policy not in {"semantic_strict", "semantic_then_keyword"}:
            raise ValueError(f"unsupported retrieval policy: {policy}")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not namespace or not namespace.strip():
            raise ProvenanceError("namespace is required")
        if self._embedding_provider is None:
            raise RuntimeError("semantic retrieval requires an embedding provider")

        retrieval_id = str(uuid4())
        attempts: list[RetrievalAttempt] = []
        profile: EmbeddingProfile | None = None
        rows: list[dict[str, Any]] = []
        selected_strategy: str | None = None
        fallback_reason: str | None = None
        failure: Exception | None = None
        try:
            profile = self.ensure_active_embedding_profile()
            configured_profile = embedding_profile(
                self._embedding_provider, max_distance=profile.max_distance
            )
            if profile.profile_id != configured_profile.profile_id:
                raise RuntimeError(
                    "configured embedding provider does not match the database-active profile: "
                    f"{configured_profile.profile_id} != {profile.profile_id}"
                )
            query_vector = self._embedding_provider.embed_query(query)
            self._validate_semantic_embedding(query_vector)
            with self._conn.transaction():
                rows = self.search_semantic_vector(
                    namespace=namespace,
                    query_vector=query_vector,
                    profile_id=profile.profile_id,
                    limit=limit,
                    service_slug=service_slug,
                    positive_guidance_only=positive_guidance_only,
                )
            attempts.append(
                RetrievalAttempt(
                    strategy="semantic_vector",
                    outcome="selected" if rows else "empty",
                    result_count=len(rows),
                )
            )
            if rows:
                selected_strategy = "semantic_vector"
        except Exception as exc:
            failure = exc
            attempts.append(
                RetrievalAttempt(
                    strategy="semantic_vector",
                    outcome="error",
                    result_count=0,
                    error_code=type(exc).__name__,
                )
            )

        if policy == "semantic_then_keyword" and not rows:
            fallback_reason = (
                "semantic_vector_error" if failure is not None else "semantic_vector_empty"
            )
            try:
                with self._conn.transaction():
                    rows = self.search_current_semantic_text(
                        namespace=namespace,
                        query=query,
                        limit=limit,
                        positive_guidance_only=positive_guidance_only,
                    )
                attempts.append(
                    RetrievalAttempt(
                        strategy="keyword",
                        outcome="selected" if rows else "empty",
                        result_count=len(rows),
                    )
                )
                if rows:
                    selected_strategy = "keyword"
                failure = None
            except Exception as exc:
                failure = exc
                attempts.append(
                    RetrievalAttempt(
                        strategy="keyword",
                        outcome="error",
                        result_count=0,
                        error_code=type(exc).__name__,
                    )
                )

        if failure is not None:
            status: Literal["succeeded", "empty", "degraded", "failed"] = "failed"
        elif selected_strategy == "keyword":
            status = "degraded"
        elif rows:
            status = "succeeded"
        else:
            status = "empty"

        with self._conn.transaction():
            self._ensure_decision(
                decision_id=decision_id,
                actor=reader,
                decision_kind="memory_retrieval",
                purpose=purpose,
                namespace=namespace,
            )
            self._conn.execute(
                """
                    INSERT INTO memory_retrievals (
                        id, decision_id, namespace, reader, purpose, policy,
                        policy_version, query_sha256, requested_limit, status,
                        selected_strategy, fallback_reason, embedding_profile_id, attempts,
                        returned_memory_ids, error_code, completed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    retrieval_id,
                    decision_id,
                    namespace,
                    reader,
                    purpose,
                    policy,
                    hashlib.sha256(query.encode("utf-8")).hexdigest(),
                    limit,
                    status,
                    selected_strategy,
                    fallback_reason,
                    profile.profile_id if profile is not None else None,
                    Jsonb([attempt.__dict__ for attempt in attempts]),
                    Jsonb([str(row["id"]) for row in rows]),
                    type(failure).__name__ if failure is not None else None,
                ),
            )
            for rank, row in enumerate(rows, start=1):
                self.record_read(
                    decision_id=decision_id,
                    memory_kind="semantic",
                    memory_id=str(row["id"]),
                    reader=reader,
                    purpose=purpose,
                    retrieval_id=retrieval_id,
                    rank=rank,
                    distance=row.get("distance"),
                )

        if self._owns_connection:
            self._conn.commit()

        result = RetrievalResult(
            retrieval_id=retrieval_id,
            decision_id=decision_id,
            policy=policy,
            policy_version=1,
            status=status,
            selected_strategy=selected_strategy,
            fallback_reason=fallback_reason,
            embedding_profile=profile,
            attempts=tuple(attempts),
            hits=tuple(rows),
        )
        if failure is not None:
            raise RuntimeError(
                f"retrieval {retrieval_id} failed under {policy}: {failure}"
            ) from failure
        return result

    def active_embedding_profile(self) -> EmbeddingProfile:
        """Return the single profile currently authorized for retrieval/writes."""

        started_idle = self._conn.info.transaction_status == TransactionStatus.IDLE
        try:
            row = self._fetch_optional(
                """
                    SELECT profile.*
                    FROM embedding_index_state AS state
                    JOIN embedding_profiles AS profile ON profile.id = state.active_profile_id
                    WHERE state.singleton = true AND profile.status = 'active'
                """,
                (),
            )
        finally:
            if (
                self._owns_connection
                and started_idle
                and self._conn.info.transaction_status != TransactionStatus.IDLE
            ):
                self._conn.rollback()
        if row is None:
            raise RuntimeError("no active embedding profile is configured")
        if row["capability"] == "lexical_hash" and _hosted_runtime():
            raise RuntimeError("hosted retrieval cannot use a lexical-hash embedding profile")
        return EmbeddingProfile(
            profile_id=str(row["id"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            dimensions=int(row["dimensions"]),
            capability=str(row["capability"]),
            encoder_revision=str(row["encoder_revision"]),
            configuration=dict(row.get("configuration") or {}),
            max_distance=row.get("max_distance"),
        )

    def ensure_active_embedding_profile(self) -> EmbeddingProfile:
        """Bootstrap an empty index, or return the active retrieval profile.

        Automatic activation is safe only before any trusted semantic memory
        exists. Once memories exist, a missing active profile must go through
        the side-by-side backfill workflow so retrieval can never observe a
        partially indexed belief set.
        """

        if self._embedding_provider is None:
            raise RuntimeError("semantic retrieval requires an embedding provider")
        self._validate_embedding_provider_dimensions()
        configured = embedding_profile(self._embedding_provider)
        with self._conn.transaction():
            lock_embedding_index_write_fence(self._conn)
            state = self._fetch_one(
                "SELECT * FROM embedding_index_state WHERE singleton = true FOR UPDATE",
                (),
            )
            if state.get("active_profile_id") is not None:
                return self.active_embedding_profile()

            current = self._fetch_one(
                """
                    SELECT count(*) AS current_count
                    FROM current_semantic_memories
                    WHERE trust_status = 'active'
                """,
                (),
            )
            if int(current["current_count"]) != 0:
                raise RuntimeError(
                    "no active embedding profile is configured; current trusted memories "
                    "require side-by-side backfill before activation"
                )
            building_profile_id = state.get("building_profile_id")
            if building_profile_id not in {None, configured.profile_id}:
                raise RuntimeError(
                    "a different embedding profile build is already in progress: "
                    f"{building_profile_id}"
                )
            if configured.capability == "lexical_hash" and _hosted_runtime():
                raise RuntimeError(
                    "hosted semantic memory cannot activate a lexical-hash profile"
                )
            self._conn.execute(
                """
                    INSERT INTO embedding_profiles (
                        id, provider, model, dimensions, capability,
                        encoder_revision, configuration, max_distance, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'building')
                    ON CONFLICT (id) DO NOTHING
                """,
                (
                    configured.profile_id,
                    configured.provider,
                    configured.model,
                    configured.dimensions,
                    configured.capability,
                    configured.encoder_revision,
                    Jsonb(dict(configured.configuration)),
                    configured.max_distance,
                ),
            )
            self._conn.execute(
                """
                    UPDATE embedding_profiles
                    SET status = 'active', activated_at = COALESCE(activated_at, now()),
                        retired_at = NULL
                    WHERE id = %s
                """,
                (configured.profile_id,),
            )
            self._conn.execute(
                """
                    UPDATE embedding_index_state
                    SET active_profile_id = %s, building_profile_id = NULL,
                        generation = generation + 1, updated_at = now()
                    WHERE singleton = true
                """,
                (configured.profile_id,),
            )
        return configured

    def write_episodic(
        self,
        *,
        episode_id: str,
        role: str,
        content: str,
        provenance: Provenance,
        metadata: dict[str, Any] | None = None,
        t_valid: datetime | None = None,
        content_schema: str = "episodic.v1",
        structured_payload: dict[str, Any] | None = None,
        producer_decision_id: str | None = None,
        parent_memory_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Write an episodic memory row and return the inserted row."""

        with start_span(
            "hindsight.memory.write_episodic",
            {
                "hindsight.memory.operation": "write",
                "hindsight.memory.kind": "episodic",
                "hindsight.memory.episode_id": episode_id,
                "hindsight.memory.role": role,
                "hindsight.provenance.writer": provenance.writer,
            },
        ) as span:
            provenance.validate()
            memory_id = uuid4()
            producer_id = producer_decision_id or f"memory:write:{memory_id}"
            payload = structured_payload or {"content": content, **(metadata or {})}
            digest = _payload_digest(content=content, payload=payload, metadata=metadata or {})
            self._ensure_decision(
                decision_id=producer_id,
                actor=provenance.writer,
                decision_kind="episodic_write",
                purpose=provenance.justification,
                namespace=episode_id,
            )
            classified_reads = self._prepare_output_reads(
                producer_decision_id=producer_id,
                parent_memory_ids=parent_memory_ids,
            )
            query = """
                INSERT INTO episodic_memories (
                    id, episode_id, role, content, metadata, t_valid,
                    writer, source_ref, justification, producer_decision_id,
                    content_schema, structured_payload, payload_digest,
                    lineage_status, trust_status
                )
                VALUES (
                    %s, %s, %s, %s, %s, COALESCE(%s, now()), %s, %s, %s,
                    %s, %s, %s, %s, 'complete', 'active'
                )
                RETURNING *
            """
            params = (
                memory_id,
                episode_id,
                role,
                content,
                Jsonb(metadata or {}),
                t_valid,
                provenance.writer,
                provenance.source_ref,
                provenance.justification,
                producer_id,
                content_schema,
                Jsonb(payload),
                digest,
            )
            memory = self._fetch_one(query, params)
            self._insert_external_evidence(
                memory_kind="episodic",
                memory_id=str(memory_id),
                provenance=provenance,
                observed_at=t_valid,
            )
            self._insert_output_lineage(
                memory_kind="episodic",
                memory_id=str(memory_id),
                producer_decision_id=producer_id,
                classified_reads=classified_reads,
            )
            self._seal_decision(producer_id)
            set_span_attributes(span, {"hindsight.memory.id": str(memory["id"])})
            return self.audit_memory(memory_kind="episodic", memory_id=str(memory_id)) or memory

    def write_semantic(
        self,
        *,
        namespace: str,
        content: str,
        provenance: Provenance,
        metadata: dict[str, Any] | None = None,
        t_valid: datetime | None = None,
        content_schema: str = "semantic.v1",
        structured_payload: dict[str, Any] | None = None,
        producer_decision_id: str | None = None,
        parent_memory_ids: Iterable[str] | None = None,
        belief_id: str | None = None,
        previous_version_id: str | None = None,
        transition_kind: Literal["assertion", "supersession", "rewind_reassertion"] = "assertion",
        trust_status: TrustStatus = "active",
        governance: MemoryGovernance | None = None,
        created_by_operation_id: str | None = None,
        precomputed_embedding: list[float] | None = None,
        expected_namespace_revision: int | None = None,
        require_current_parents: bool = False,
    ) -> dict[str, Any]:
        """Write a semantic memory row and return the inserted row."""

        with start_span(
            "hindsight.memory.write_semantic",
            {
                "hindsight.memory.operation": "write",
                "hindsight.memory.kind": "semantic",
                "hindsight.memory.namespace": namespace,
                "hindsight.provenance.writer": provenance.writer,
            },
        ) as span:
            provenance.validate()
            if not namespace or not namespace.strip():
                raise ProvenanceError("namespace is required")
            if trust_status not in {"active", "review_required"}:
                raise ProvenanceError(f"unsupported semantic trust status: {trust_status}")
            if expected_namespace_revision is not None and (
                isinstance(expected_namespace_revision, bool)
                or not isinstance(expected_namespace_revision, int)
                or expected_namespace_revision < 0
            ):
                raise ValueError("expected_namespace_revision must be a non-negative integer")
            resolved_parent_memory_ids = tuple(str(value) for value in (parent_memory_ids or ()))
            caller_metadata = {
                key: value
                for key, value in (metadata or {}).items()
                if key not in PROMPT_SAFETY_METADATA_KEYS
            }
            resolved_metadata = _governed_metadata(caller_metadata, governance)
            prompt_safety = assess_prompt_safety(
                content=content,
                metadata=resolved_metadata,
                structured_payload=structured_payload,
                provenance={
                    "writer": provenance.writer,
                    "source_ref": provenance.source_ref,
                    "justification": provenance.justification,
                },
            )
            resolved_metadata.update(prompt_safety.metadata())
            resolved_trust_status = trust_status
            if prompt_safety.status == "suspected":
                resolved_trust_status = "review_required"
                resolved_metadata["usage_instruction"] = "audit_only"
            memory_id = uuid4()
            resolved_belief_id = belief_id or str(uuid4())
            producer_id = producer_decision_id or f"memory:write:{memory_id}"
            payload = structured_payload or {"content": content, **resolved_metadata}
            digest = _payload_digest(
                content=content,
                payload=payload,
                metadata=resolved_metadata,
            )
            embedding, profile = self._prepare_semantic_embedding(
                content=content,
                precomputed_embedding=precomputed_embedding,
            )
            lock_embedding_index_write_fence(self._conn)
            self._ensure_decision(
                decision_id=producer_id,
                actor=provenance.writer,
                decision_kind="semantic_write",
                purpose=provenance.justification,
                namespace=namespace,
            )
            classified_reads = self._prepare_output_reads(
                producer_decision_id=producer_id,
                parent_memory_ids=resolved_parent_memory_ids,
                parent_edge_type="reasserted_from"
                if transition_kind == "rewind_reassertion"
                else "derived",
            )
            revision_namespaces = self._lock_output_namespaces(
                namespace=namespace,
                classified_reads=classified_reads,
            )
            self._validate_locked_memory_selection(
                namespace=namespace,
                expected_namespace_revision=expected_namespace_revision,
                parent_memory_ids=resolved_parent_memory_ids,
                require_current_parents=require_current_parents,
            )
            query = """
                INSERT INTO semantic_memories (
                    id, belief_id, version_number, previous_version_id,
                    namespace, content, metadata, t_valid,
                    writer, source_ref, justification, producer_decision_id,
                    transition_kind, content_schema, structured_payload,
                    payload_digest, lineage_status, trust_status,
                    created_by_operation_id, prompt_safety_status,
                    prompt_safety_scanner_version, prompt_safety_reason_codes
                )
                VALUES (
                    %s, %s,
                    COALESCE((SELECT max(version_number) + 1 FROM semantic_memories WHERE belief_id = %s), 1),
                    %s, %s, %s, %s, COALESCE(%s, now()), %s, %s, %s,
                    %s, %s, %s, %s, %s, 'complete', %s, %s, %s, %s, %s
                )
                RETURNING *
            """
            params = (
                memory_id,
                resolved_belief_id,
                resolved_belief_id,
                previous_version_id,
                namespace,
                content,
                Jsonb(resolved_metadata),
                t_valid,
                provenance.writer,
                provenance.source_ref,
                provenance.justification,
                producer_id,
                transition_kind,
                content_schema,
                Jsonb(payload),
                digest,
                resolved_trust_status,
                created_by_operation_id,
                prompt_safety.status,
                prompt_safety.scanner_version,
                Jsonb(list(prompt_safety.reason_codes)),
            )

            def write_row() -> dict[str, Any]:
                self._conn.execute(
                    """
                        INSERT INTO semantic_beliefs (id, namespace)
                        VALUES (%s, %s)
                        ON CONFLICT (id) DO NOTHING
                    """,
                    (resolved_belief_id, namespace),
                )
                row = self._fetch_one(query, params)
                if self._embedding_provider is not None and embedding is not None:
                    self._insert_semantic_embedding(
                        memory_id=str(row["id"]),
                        namespace=namespace,
                        embedding=embedding,
                        profile=profile,
                        content_digest=digest,
                    )
                self._enqueue_building_profile_task(memory_id=str(row["id"]))
                self._insert_external_evidence(
                    memory_kind="semantic",
                    memory_id=str(memory_id),
                    provenance=provenance,
                    observed_at=t_valid,
                )
                self._insert_output_lineage(
                    memory_kind="semantic",
                    memory_id=str(memory_id),
                    producer_decision_id=producer_id,
                    classified_reads=classified_reads,
                )
                self._seal_decision(producer_id)
                self._conn.execute(
                    """
                        UPDATE memory_namespaces
                        SET revision = revision + 1, updated_at = now()
                        WHERE namespace = ANY(%s)
                    """,
                    (revision_namespaces,),
                )
                return row

            memory = self._in_savepoint(write_row)
            set_span_attributes(span, {"hindsight.memory.id": str(memory["id"])})
            return self.audit_memory(memory_kind="semantic", memory_id=str(memory_id)) or memory

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

        with start_span(
            "hindsight.memory.invalidate",
            {
                "hindsight.memory.operation": "invalidate",
                "hindsight.memory.kind": memory_kind,
                "hindsight.memory.id": memory_id,
                "hindsight.memory.actor": invalidator,
            },
        ) as span:
            with self._conn.transaction():
                row = self._invalidate_one(
                    memory_kind=memory_kind,
                    memory_id=memory_id,
                    invalidated_by=invalidator,
                    reason=reason,
                    t_invalid=t_invalid,
                )
            set_span_attributes(span, {"hindsight.memory.count": 1 if row is not None else 0})
            return row

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
        row = self._fetch_optional(query, (t_invalid, invalidated_by, reason, memory_id))
        if row is not None and memory_kind == "semantic":
            self._conn.execute(
                """
                    UPDATE memory_namespaces
                    SET revision = revision + 1, updated_at = now()
                    WHERE namespace = %s
                """,
                (row["namespace"],),
            )
        return row

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
        with start_span(
            "hindsight.memory.current_episodic",
            {
                "hindsight.memory.operation": "current",
                "hindsight.memory.kind": "episodic",
                "hindsight.memory.episode_id": episode_id,
                "hindsight.memory.limit": limit,
                "hindsight.memory.decision_id": decision_id,
                "hindsight.memory.reader": reader,
            },
        ) as span:
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
                set_span_attributes(
                    span,
                    {
                        "hindsight.memory.count": len(rows),
                        "hindsight.memory.ids": memory_ids(rows),
                    },
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
        with start_span(
            "hindsight.memory.current_semantic",
            {
                "hindsight.memory.operation": "current",
                "hindsight.memory.kind": "semantic",
                "hindsight.memory.namespace": namespace,
                "hindsight.memory.limit": limit,
                "hindsight.memory.decision_id": decision_id,
                "hindsight.memory.reader": reader,
            },
        ) as span:
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
                set_span_attributes(
                    span,
                    {
                        "hindsight.memory.count": len(rows),
                        "hindsight.memory.ids": memory_ids(rows),
                    },
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
        """Compatibility wrapper for strict active-profile vector retrieval."""

        if self._embedding_provider is None:
            raise RuntimeError("recall_semantic requires an embedding provider")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        context = _optional_read_context(
            decision_id=decision_id,
            reader=reader,
            purpose=purpose,
        )
        if context is not None:
            return list(
                self.retrieve_semantic(
                    namespace=namespace,
                    query=query,
                    decision_id=context.decision_id,
                    reader=context.reader,
                    purpose=context.purpose,
                    policy="semantic_strict",
                    limit=limit,
                ).hits
            )
        profile = self.ensure_active_embedding_profile()
        configured_profile = embedding_profile(
            self._embedding_provider, max_distance=profile.max_distance
        )
        if configured_profile.profile_id != profile.profile_id:
            raise RuntimeError("configured embedding provider does not match active profile")
        vector = self._embedding_provider.embed_query(query)
        self._validate_semantic_embedding(vector)
        return self.search_semantic_vector(
            namespace=namespace,
            query_vector=vector,
            profile_id=profile.profile_id,
            limit=limit,
        )

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
        positive_guidance_only: bool = False,
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

        with start_span(
            "hindsight.memory.recall_similar_incidents",
            {
                "hindsight.memory.operation": "recall",
                "hindsight.memory.kind": "semantic",
                "hindsight.memory.namespace": namespace,
                "hindsight.memory.service_slug": service_slug,
                "hindsight.memory.limit": limit,
                "hindsight.memory.decision_id": decision_id,
                "hindsight.memory.reader": reader,
                "hindsight.memory.recall_mode": "similar_incidents",
            },
        ) as span:
            profile = self.ensure_active_embedding_profile()
            configured_profile = embedding_profile(
                self._embedding_provider, max_distance=profile.max_distance
            )
            if configured_profile.profile_id != profile.profile_id:
                raise RuntimeError("configured embedding provider does not match active profile")
            query_vector = vector_literal(
                self._embedding_provider.embed_query(query),
                dimensions=self._embedding_provider.dimensions,
            )
            with self._conn.transaction():
                rows = self._fetch_all(
                    f"""
                        SELECT
                            m.id,
                            m.id AS memory_id,
                            m.content AS memory_content,
                            vector.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS}) AS distance,
                            i.slug AS incident_slug,
                            i.title AS incident_title,
                            i.severity,
                            s.slug AS service_slug,
                            s.name AS service_name,
                            r.slug AS runbook_slug,
                            r.title AS runbook_title
                        FROM current_semantic_memories AS m
                        JOIN semantic_memory_vectors AS vector
                            ON vector.tenant_id = m.tenant_id
                            AND vector.memory_id = m.id
                        JOIN incident_semantic_memories AS im
                            ON im.tenant_id = m.tenant_id
                            AND im.memory_id = m.id
                        JOIN incidents AS i
                            ON i.tenant_id = im.tenant_id
                            AND i.id = im.incident_id
                        JOIN incident_services AS isvc
                            ON isvc.tenant_id = i.tenant_id
                            AND isvc.incident_id = i.id
                        JOIN services AS s
                            ON s.tenant_id = isvc.tenant_id
                            AND s.id = isvc.service_id
                        LEFT JOIN (
                            SELECT
                                ir.tenant_id,
                                ir.incident_id,
                                r.service_id,
                                r.slug,
                                r.title
                            FROM incident_runbooks AS ir
                            JOIN runbooks AS r
                                ON r.tenant_id = ir.tenant_id
                                AND r.id = ir.runbook_id
                        ) AS r
                            ON r.tenant_id = i.tenant_id
                            AND r.incident_id = i.id
                            AND (r.service_id = s.id OR r.service_id IS NULL)
                        WHERE m.namespace = %s
                            AND s.slug = %s
                            {_semantic_eligibility_sql("m", positive_guidance_only)}
                            AND vector.tenant_id = current_hindsight_tenant_id()
                            AND vector.namespace = %s
                            AND vector.profile_id = %s
                        ORDER BY vector.embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})
                        LIMIT %s
                    """,
                    (
                        query_vector,
                        namespace,
                        service_slug,
                        namespace,
                        profile.profile_id,
                        query_vector,
                        limit,
                    ),
                )
                self._record_retrieval(
                    rows,
                    memory_kind="semantic",
                    decision_id=decision_id,
                    reader=reader,
                    purpose=purpose,
                )
                set_span_attributes(
                    span,
                    {
                        "hindsight.memory.count": len(rows),
                        "hindsight.memory.ids": memory_ids(rows),
                    },
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

    def open_decision(
        self,
        *,
        decision_id: str,
        actor: str,
        decision_kind: str,
        purpose: str,
        namespace: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or return one durable open decision identity."""

        self._ensure_decision(
            decision_id=decision_id,
            actor=actor,
            decision_kind=decision_kind,
            purpose=purpose,
            namespace=namespace,
            run_id=run_id,
            metadata=metadata,
        )
        row = self._fetch_optional("SELECT * FROM memory_decisions WHERE id = %s", (decision_id,))
        if row is None:
            raise RuntimeError(f"decision was not created: {decision_id}")
        return row

    def seal_decision(self, *, decision_id: str, failed: bool = False) -> dict[str, Any]:
        """Seal a decision so later reads or outputs cannot alter its evidence set."""

        self._seal_decision(decision_id, failed=failed)
        row = self._fetch_optional("SELECT * FROM memory_decisions WHERE id = %s", (decision_id,))
        if row is None:
            raise RuntimeError(f"decision not found: {decision_id}")
        return row

    def record_agent_reflection(
        self,
        *,
        decision_id: str,
        run_id: str,
        thread_id: str,
        incident_id: str,
        namespace: str,
        service_slug: str | None,
        plan: str,
        proposed_action: str,
        action_approved: bool,
        memory: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the typed reflection projection linked to its immutable version."""

        if memory.get("content_schema") != "agent_reflection.v1":
            raise ProvenanceError("reflection memory must use agent_reflection.v1")
        run = self._fetch_optional("SELECT id FROM agent_runs WHERE id = %s", (run_id,))
        return self._fetch_one(
            """
                INSERT INTO agent_reflections (
                    decision_id, run_id, thread_id, incident_id, namespace,
                    service_slug, plan, proposed_action, action_approved,
                    semantic_memory_id, belief_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """,
            (
                decision_id,
                run["id"] if run is not None else None,
                thread_id,
                incident_id,
                namespace,
                service_slug,
                plan,
                proposed_action,
                action_approved,
                memory["id"],
                memory["belief_id"],
            ),
        )

    def remember_agent_reflection(
        self,
        *,
        decision_id: str,
        run_id: str,
        thread_id: str,
        incident_id: str,
        namespace: str,
        service_slug: str | None,
        plan: str,
        proposed_action: str,
        action_approved: bool,
        content: str,
        metadata: dict[str, Any],
        structured_payload: dict[str, Any],
        provenance: Provenance,
        parent_memory_ids: Iterable[str],
        guidance_eligible: bool | None = None,
        expected_namespace_revision: int | None = None,
        require_current_parents: bool = False,
    ) -> dict[str, Any]:
        """Atomically validate selection and persist a reflection projection."""

        embedding, _ = self._prepare_semantic_embedding(content=content)
        eligible = action_approved if guidance_eligible is None else guidance_eligible
        if eligible and not action_approved:
            raise ProvenanceError("unapproved reflection cannot become positive guidance")
        governance = MemoryGovernance(
            operator_disposition=str(
                metadata.get("operator_disposition")
                or ("approved" if eligible else "rejected")
            ),  # type: ignore[arg-type]
            safety_status=str(
                metadata.get("safety_status") or ("safe" if eligible else "unassessed")
            ),  # type: ignore[arg-type]
            contradiction_status=str(
                metadata.get("contradiction_status")
                or ("supported" if eligible else "unassessed")
            ),  # type: ignore[arg-type]
            usage_instruction=str(
                metadata.get("usage_instruction")
                or ("positive_guidance" if eligible else "audit_only")
            ),  # type: ignore[arg-type]
        )
        with self._conn.transaction():
            memory = self.write_semantic(
                namespace=namespace,
                content=content,
                provenance=provenance,
                metadata=metadata,
                content_schema="agent_reflection.v1",
                structured_payload=structured_payload,
                producer_decision_id=decision_id,
                parent_memory_ids=parent_memory_ids,
                precomputed_embedding=embedding,
                trust_status="active" if eligible else "review_required",
                governance=governance,
                expected_namespace_revision=expected_namespace_revision,
                require_current_parents=require_current_parents,
            )
            self.record_agent_reflection(
                decision_id=decision_id,
                run_id=run_id,
                thread_id=thread_id,
                incident_id=incident_id,
                namespace=namespace,
                service_slug=service_slug,
                plan=plan,
                proposed_action=proposed_action,
                action_approved=action_approved,
                memory=memory,
            )
            return memory

    def record_read(
        self,
        *,
        decision_id: str,
        memory_kind: MemoryKind,
        memory_id: str,
        reader: str,
        purpose: str,
        retrieval_id: str | None = None,
        rank: int | None = None,
        distance: float | None = None,
    ) -> dict[str, Any]:
        """Record that a decision read a specific memory row."""

        self._table_for_kind(memory_kind)
        if not decision_id or not decision_id.strip():
            raise ProvenanceError("decision_id is required")
        if not reader or not reader.strip():
            raise ProvenanceError("reader is required")
        if not purpose or not purpose.strip():
            raise ProvenanceError("purpose is required")
        self._ensure_decision(
            decision_id=decision_id,
            actor=reader,
            decision_kind="memory_read",
            purpose=purpose,
        )
        decision = self._fetch_optional(
            "SELECT status FROM memory_decisions WHERE id = %s", (decision_id,)
        )
        if decision is None or decision["status"] != "open":
            raise ProvenanceError(f"decision is not open: {decision_id}")

        with start_span(
            "hindsight.memory.record_read",
            {
                "hindsight.memory.operation": "record_read",
                "hindsight.memory.kind": memory_kind,
                "hindsight.memory.id": memory_id,
                "hindsight.memory.decision_id": decision_id,
                "hindsight.memory.reader": reader,
            },
        ) as span:

            def write_read() -> dict[str, Any]:
                return self._fetch_one(
                    """
                        INSERT INTO memory_reads (
                            decision_id, memory_kind, memory_id, reader, purpose,
                            semantic_memory_id, episodic_memory_id,
                            retrieval_id, rank, distance
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                    """,
                    (
                        decision_id,
                        memory_kind,
                        memory_id,
                        reader,
                        purpose,
                        memory_id if memory_kind == "semantic" else None,
                        memory_id if memory_kind == "episodic" else None,
                        retrieval_id,
                        rank,
                        distance,
                    ),
                )

            if self._owns_connection and not self._conn.autocommit:
                with self._conn.transaction():
                    row = write_read()
            else:
                row = write_read()
            set_span_attributes(span, {"hindsight.memory.read_id": str(row["id"])})
            if self._owns_connection and getattr(self._conn, "_num_transactions", 0) == 0:
                self._conn.commit()
            return row

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
        return self._fetch_all_as_of(
            system_as_of=as_of,
            query=query_sql,
            params=tuple(params),
        )

    def _ensure_decision(
        self,
        *,
        decision_id: str,
        actor: str,
        decision_kind: str,
        purpose: str,
        namespace: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not decision_id or not decision_id.strip():
            raise ProvenanceError("decision_id is required")
        self._conn.execute(
            """
                INSERT INTO memory_decisions (
                    id, actor, decision_kind, purpose, run_id, namespace, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """,
            (
                decision_id,
                actor,
                decision_kind,
                purpose,
                run_id,
                namespace,
                Jsonb(metadata or {}),
            ),
        )
        decision = self._fetch_optional(
            "SELECT status FROM memory_decisions WHERE id = %s FOR UPDATE",
            (decision_id,),
        )
        if decision is None or decision["status"] != "open":
            raise ProvenanceError(f"decision is not open: {decision_id}")

    def _seal_decision(self, decision_id: str, *, failed: bool = False) -> None:
        status = "failed" if failed else "sealed"
        row = self._conn.execute(
            """
                UPDATE memory_decisions
                SET status = %s, sealed_at = COALESCE(sealed_at, now())
                WHERE id = %s AND status = 'open'
                RETURNING id
            """,
            (status, decision_id),
        ).fetchone()
        if row is None:
            raise ProvenanceError(f"decision is not open: {decision_id}")

    def _insert_external_evidence(
        self,
        *,
        memory_kind: MemoryKind,
        memory_id: str,
        provenance: Provenance,
        observed_at: datetime | None,
    ) -> None:
        evidence_payload = {
            "source_ref": provenance.source_ref,
            "justification": provenance.justification,
            "writer": provenance.writer,
        }
        digest = hashlib.sha256(
            json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._conn.execute(
            """
                INSERT INTO memory_external_evidence (
                    semantic_memory_id, episodic_memory_id, evidence_kind,
                    evidence_ref, evidence_digest, observed_at, actor, metadata
                )
                VALUES (%s, %s, 'external', %s, %s, %s, %s, %s)
            """,
            (
                memory_id if memory_kind == "semantic" else None,
                memory_id if memory_kind == "episodic" else None,
                provenance.source_ref,
                digest,
                observed_at or datetime.now(UTC),
                provenance.writer,
                Jsonb({"justification": provenance.justification}),
            ),
        )

    def _prepare_output_reads(
        self,
        *,
        producer_decision_id: str,
        parent_memory_ids: Iterable[str] | None,
        parent_edge_type: Literal["derived", "reasserted_from"] = "derived",
    ) -> list[tuple[dict[str, Any], str]]:
        parents = {str(value) for value in (parent_memory_ids or [])}
        reads = self._fetch_all(
            "SELECT * FROM memory_reads WHERE decision_id = %s ORDER BY read_at, id",
            (producer_decision_id,),
        )
        read_ids = {str(row["memory_id"]) for row in reads}
        missing = parents - read_ids
        if missing:
            raise ProvenanceError(
                "derived parent was not read by producer decision: " + ", ".join(sorted(missing))
            )
        return [
            (
                read,
                parent_edge_type if str(read["memory_id"]) in parents else "context",
            )
            for read in reads
        ]

    def _insert_output_lineage(
        self,
        *,
        memory_kind: MemoryKind,
        memory_id: str,
        producer_decision_id: str,
        classified_reads: list[tuple[dict[str, Any], str]],
    ) -> None:
        justifications = {
            "derived": "Explicitly declared derivation parent",
            "context": "Read as context but not declared causal",
            "reasserted_from": "Reassert exact target logical belief",
        }
        for read, edge_type in classified_reads:
            self._conn.execute(
                """
                    INSERT INTO memory_lineage_edges (
                        child_semantic_memory_id, child_episodic_memory_id,
                        parent_read_id, producer_decision_id, edge_type, justification
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    memory_id if memory_kind == "semantic" else None,
                    memory_id if memory_kind == "episodic" else None,
                    read["id"],
                    producer_decision_id,
                    edge_type,
                    justifications[edge_type],
                ),
            )

    def _lock_output_namespaces(
        self,
        *,
        namespace: str,
        classified_reads: list[tuple[dict[str, Any], str]],
    ) -> list[str]:
        self._conn.execute(
            """
                INSERT INTO memory_namespaces (namespace)
                VALUES (%s)
                ON CONFLICT (namespace) DO NOTHING
            """,
            (namespace,),
        )
        causal_parent_ids = [
            str(read["semantic_memory_id"])
            for read, edge_type in classified_reads
            if edge_type in {"derived", "reasserted_from"}
            and read.get("semantic_memory_id") is not None
        ]
        namespaces = {namespace}
        if causal_parent_ids:
            rows = self._fetch_all(
                "SELECT DISTINCT namespace FROM semantic_memories WHERE id = ANY(%s)",
                (causal_parent_ids,),
            )
            namespaces.update(str(row["namespace"]) for row in rows)
        ordered = sorted(namespaces)
        self._fetch_all(
            """
                SELECT namespace FROM memory_namespaces
                WHERE namespace = ANY(%s)
                ORDER BY namespace
                FOR UPDATE
            """,
            (ordered,),
        )
        return ordered

    def _validate_locked_memory_selection(
        self,
        *,
        namespace: str,
        expected_namespace_revision: int | None,
        parent_memory_ids: Iterable[str],
        require_current_parents: bool,
    ) -> None:
        if expected_namespace_revision is not None:
            namespace_state = self._fetch_optional(
                """
                    SELECT revision FROM memory_namespaces
                    WHERE namespace = %s
                    FOR UPDATE
                """,
                (namespace,),
            )
            if (
                namespace_state is None
                or int(namespace_state["revision"]) != expected_namespace_revision
            ):
                raise MemorySelectionChangedError("memory selection changed before reflection")

        if not require_current_parents and expected_namespace_revision is None:
            return
        required_ids = sorted({str(value) for value in parent_memory_ids})
        if not required_ids:
            return
        rows = self._fetch_all(
            """
                SELECT id FROM semantic_memories
                WHERE id = ANY(%s) AND t_invalid IS NULL
                ORDER BY id
            """,
            (required_ids,),
        )
        current_ids = {str(row["id"]) for row in rows}
        if current_ids != set(required_ids):
            raise MemorySelectionChangedError("memory selection changed before reflection")

    def _insert_semantic_embedding(
        self,
        *,
        memory_id: str,
        namespace: str,
        embedding: list[float],
        profile: EmbeddingProfile | None = None,
        content_digest: str | None = None,
    ) -> dict[str, Any]:
        if self._embedding_provider is None:
            raise RuntimeError("embedding provider is not configured")
        self._validate_semantic_embedding(embedding)
        state = self._fetch_one(
            "SELECT * FROM embedding_index_state WHERE singleton = true",
            (),
        )
        active_profile_id = state.get("active_profile_id")
        building_profile_id = state.get("building_profile_id")
        configured_profile = profile or embedding_profile(self._embedding_provider)
        if active_profile_id is not None:
            active = self._fetch_one(
                "SELECT * FROM embedding_profiles WHERE id = %s",
                (active_profile_id,),
            )
            resolved_profile = embedding_profile(
                self._embedding_provider,
                configuration=dict(active.get("configuration") or {}),
                max_distance=active.get("max_distance"),
            )
            if resolved_profile.profile_id != active_profile_id:
                raise RuntimeError(
                    "embedding provider does not match active profile: "
                    f"{resolved_profile.profile_id} != {active_profile_id}"
                )
        else:
            resolved_profile = configured_profile
            if building_profile_id not in {None, resolved_profile.profile_id}:
                raise RuntimeError(
                    "a different embedding profile build is already in progress: "
                    f"{building_profile_id}"
                )
            if resolved_profile.capability == "lexical_hash" and _hosted_runtime():
                raise RuntimeError("hosted semantic memory cannot activate a lexical-hash profile")
            self._conn.execute(
                """
                    INSERT INTO embedding_profiles (
                        id, provider, model, dimensions, capability,
                        encoder_revision, configuration, max_distance, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'building')
                    ON CONFLICT (id) DO NOTHING
                """,
                (
                    resolved_profile.profile_id,
                    resolved_profile.provider,
                    resolved_profile.model,
                    resolved_profile.dimensions,
                    resolved_profile.capability,
                    resolved_profile.encoder_revision,
                    Jsonb(dict(resolved_profile.configuration)),
                    resolved_profile.max_distance,
                ),
            )
        legacy_row = self._fetch_one(
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
        self._conn.execute(
            f"""
                INSERT INTO semantic_memory_vectors (
                    memory_id, profile_id, namespace, content_digest, embedding
                )
                VALUES (%s, %s, %s, %s, %s::VECTOR({EMBEDDING_DIMENSIONS}))
            """,
            (
                memory_id,
                resolved_profile.profile_id,
                namespace,
                content_digest or f"memory:{memory_id}",
                vector_literal(embedding),
            ),
        )
        if active_profile_id is None:
            missing = self._fetch_one(
                """
                    SELECT count(*) AS missing
                    FROM current_semantic_memories AS memory
                    LEFT JOIN semantic_memory_vectors AS vector
                        ON vector.memory_id = memory.id AND vector.profile_id = %s
                    WHERE memory.trust_status = 'active' AND vector.memory_id IS NULL
                """,
                (resolved_profile.profile_id,),
            )
            if int(missing["missing"]) != 0:
                raise RuntimeError(
                    "embedding profile cannot activate until all current memories are backfilled"
                )
            self._conn.execute(
                """
                    UPDATE embedding_profiles
                    SET status = 'active', activated_at = COALESCE(activated_at, now())
                    WHERE id = %s
                """,
                (resolved_profile.profile_id,),
            )
            self._conn.execute(
                """
                    UPDATE embedding_index_state
                    SET active_profile_id = %s, building_profile_id = NULL,
                        generation = generation + 1, updated_at = now()
                    WHERE singleton = true
                """,
                (resolved_profile.profile_id,),
            )
        return legacy_row

    def _enqueue_building_profile_task(self, *, memory_id: str) -> None:
        state = self._fetch_one(
            "SELECT active_profile_id, building_profile_id "
            "FROM embedding_index_state WHERE singleton = true",
            (),
        )
        building_profile_id = state.get("building_profile_id")
        if building_profile_id in {None, state.get("active_profile_id")}:
            return
        self._conn.execute(
            """
                INSERT INTO embedding_backfill_tasks (memory_id, profile_id)
                VALUES (%s, %s)
                ON CONFLICT (memory_id, profile_id) DO NOTHING
            """,
            (memory_id, building_profile_id),
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

    def _record_with_context(
        self,
        rows: list[dict[str, Any]],
        *,
        memory_kind: MemoryKind,
        context: ReadContext | None,
    ) -> None:
        if context is None:
            return
        self._record_retrieval(
            rows,
            memory_kind=memory_kind,
            decision_id=context.decision_id,
            reader=context.reader,
            purpose=context.purpose,
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

    def _fetch_all_as_of(
        self,
        *,
        system_as_of: datetime,
        query: str,
        params: tuple[Any, ...],
    ) -> list[dict[str, Any]]:
        """Read one exact CockroachDB MVCC snapshot on a separate connection."""

        with connect(self._historical_read_url()) as read_conn:
            as_of_literal = sql.Literal(system_as_of.isoformat()).as_string(read_conn)
            with read_conn.transaction():
                read_conn.execute(f"SET TRANSACTION AS OF SYSTEM TIME {as_of_literal}")
                return self._fetch_all_on(read_conn, query, params)

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

    def _prepare_semantic_embedding(
        self,
        *,
        content: str,
        precomputed_embedding: list[float] | None = None,
    ) -> tuple[list[float] | None, EmbeddingProfile | None]:
        if self._embedding_provider is None:
            if precomputed_embedding is not None:
                raise RuntimeError("precomputed embedding requires an embedding provider")
            return None, None
        self._validate_embedding_provider_dimensions()
        if (
            precomputed_embedding is None
            and getattr(self._embedding_provider, "capability", None) == "semantic"
            and self._conn.info.transaction_status != TransactionStatus.IDLE
        ):
            raise RuntimeError(
                "semantic document embedding must be precomputed before opening "
                "a database transaction"
            )
        embedding = (
            precomputed_embedding
            if precomputed_embedding is not None
            else self._embedding_provider.embed_document(content)
        )
        self._validate_semantic_embedding(embedding)
        return embedding, embedding_profile(self._embedding_provider)

    def _in_savepoint(self, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if self._conn.autocommit:
            return callback()

        savepoint = sql.Identifier(f"hindsight_memory_{uuid4().hex}").as_string(self._conn)
        self._conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = callback()
        except SerializationFailure:
            raise
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result


def _payload_digest(
    *,
    content: str,
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    value = {
        "content": content,
        "payload": payload,
        "metadata": metadata,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _governed_metadata(
    metadata: dict[str, Any] | None,
    governance: MemoryGovernance | None,
) -> dict[str, Any]:
    resolved = dict(metadata or {})
    if governance is None:
        return resolved
    for key, value in governance.metadata().items():
        existing = resolved.get(key)
        if existing is not None and existing != value:
            raise ProvenanceError(f"semantic metadata conflicts with governance field: {key}")
        resolved[key] = value
    return resolved


def positive_guidance_eligible(memory: dict[str, Any]) -> bool:
    """Return the same fail-closed eligibility used by semantic retrieval SQL."""

    if (
        memory.get("t_invalid") is not None
        or memory.get("trust_status") != "active"
        or memory.get("prompt_safety_status") != "clear"
    ):
        return False
    metadata = memory.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if any(
        metadata.get(key) != value for key, value in APPROVED_POSITIVE_GUIDANCE.metadata().items()
    ):
        return False
    if memory.get("content_schema") == "agent_reflection.v1":
        payload = memory.get("structured_payload")
        return (
            isinstance(payload, dict)
            and payload.get("action_approved") is True
            and payload.get("guidance_eligible") is True
        )
    return True


def _semantic_eligibility_sql(alias: str, positive_guidance_only: bool) -> str:
    if alias not in {"memory", "m"}:
        raise ValueError("unsupported semantic-memory SQL alias")
    prefix = f"{alias}."
    if not positive_guidance_only:
        return (
            f"AND {prefix}trust_status = 'active' "
            f"AND {prefix}prompt_safety_status = 'clear'"
        )
    return f"""
        AND {prefix}trust_status = 'active'
        AND {prefix}prompt_safety_status = 'clear'
        AND {prefix}metadata->>'operator_disposition' = 'approved'
        AND {prefix}metadata->>'safety_status' = 'safe'
        AND {prefix}metadata->>'contradiction_status' = 'supported'
        AND {prefix}metadata->>'usage_instruction' = 'positive_guidance'
        AND (
            {prefix}content_schema != 'agent_reflection.v1'
            OR (
                {prefix}structured_payload->'action_approved' = 'true'::JSONB
                AND {prefix}structured_payload->'guidance_eligible' = 'true'::JSONB
            )
        )
    """


def _project_historical_rows(
    rows: list[dict[str, Any]], *, valid_at: datetime
) -> list[dict[str, Any]]:
    projected = []
    for source in rows:
        row = dict(source)
        t_invalid = row.get("t_invalid")
        row["snapshot_invalidated"] = bool(t_invalid is not None and t_invalid <= valid_at)
        projected.append(row)
    return projected


def _hosted_runtime() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _require_query(query: str) -> None:
    if not query or not query.strip():
        raise ValueError("query is required")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _optional_read_context(
    *,
    decision_id: str | None,
    reader: str | None,
    purpose: str | None,
) -> ReadContext | None:
    values = (decision_id, reader, purpose)
    if all(value is None for value in values):
        return None
    if any(value is None or not value.strip() for value in values):
        raise ProvenanceError(
            "decision_id, reader, and purpose are all required to track a retrieval"
        )
    return ReadContext(decision_id=decision_id, reader=reader, purpose=purpose)
