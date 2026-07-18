"""Evidence-verified resolved-incident lesson consolidation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from hindsight.db import connect, database_url
from hindsight.embedding_index import lock_embedding_index_write_fence
from hindsight.embeddings import EmbeddingProvider, embedding_provider_from_env
from hindsight.memory import MemoryStore, Provenance
from hindsight.reasoning import (
    DeterministicReasoningProvider,
    ReasoningProvider,
    ReasoningRequest,
    reasoning_provider_from_env,
)
from hindsight.runtime import runtime_settings
from hindsight.security import safe_error_detail

CONSOLIDATION_WRITER = "consolidation.worker"
LESSON_SCHEMA = "procedural_lesson.v1"
TERMINAL_JOB_STATUSES = {"completed", "not_eligible", "failed"}
MAX_CONSOLIDATION_ATTEMPTS = 3
ELIGIBLE_SOURCE_RELATIONSHIPS = {"summary", "resolution", "root_cause"}
ELIGIBLE_SOURCE_LINEAGE_STATUSES = {"complete", "legacy_unverified"}
SOURCE_EVIDENCE_CHANGED_REASON = "source evidence changed during synthesis"
LESSON_RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "title": {"type": "string", "minLength": 1},
        "claims": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "situation",
                            "diagnostic_check",
                            "safe_action",
                            "avoidance",
                        ],
                    },
                    "text": {"type": "string", "minLength": 1},
                    "citations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "evidence_id": {"type": "string", "minLength": 1},
                                "quote": {"type": "string", "minLength": 1},
                            },
                            "required": ["evidence_id", "quote"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "text", "citations"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["schema_version", "title", "claims"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ConsolidationResult:
    """Durable result of one consolidation job."""

    incident: dict[str, Any] | None
    namespace: str | None
    memory: dict[str, Any] | None
    created: bool
    reason: str | None = None
    source_memory_ids: list[str] | None = None
    job_id: str | None = None


class LessonValidationError(ValueError):
    """Raised when model output does not cite eligible evidence exactly."""


class ConsolidationLeaseLostError(RuntimeError):
    """Raised when an attempt no longer owns a live consolidation lease."""


class ConsolidationLeaseBusyError(RuntimeError):
    """Raised when another attempt still owns a live consolidation lease."""


class ConsolidationEvidenceChangedError(RuntimeError):
    """Raised when selected source evidence is no longer eligible or linked."""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint: accept real resolved transitions and process their jobs."""

    settings = runtime_settings()
    reasoning = reasoning_provider_from_env(settings.provider_env)
    embeddings = embedding_provider_from_env(settings.provider_env)
    results = handle_incident_changefeed_event(
        event,
        db_url=settings.database_url,
        reasoning_provider=reasoning,
        embedding_provider=embeddings,
    )
    return {"processed": len(results), "results": [_jsonable_result(item) for item in results]}


def handle_incident_changefeed_event(
    event: dict[str, Any] | list[dict[str, Any]],
    *,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> list[ConsolidationResult]:
    """Create one idempotent job only for an actual transition to resolved."""

    results = []
    for row in _event_rows(event):
        transition = _resolved_transition(row)
        if transition is None:
            continue
        after, source_event_id = transition
        job = enqueue_consolidation_job(
            incident_id=str(after["id"]),
            source_event_id=source_event_id,
            db_url=db_url,
        )
        results.append(
            process_consolidation_job(
                job_id=str(job["id"]),
                db_url=db_url,
                reasoning_provider=reasoning_provider,
                embedding_provider=embedding_provider,
            )
        )
    return results


def enqueue_consolidation_job(
    *, incident_id: str, source_event_id: str, db_url: str | None = None
) -> dict[str, Any]:
    """Persist or return the unique job for one resolution event."""

    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        INSERT INTO consolidation_jobs (incident_id, source_event_id)
                        VALUES (%s, %s)
                        ON CONFLICT (incident_id, source_event_id)
                        DO UPDATE SET updated_at = consolidation_jobs.updated_at
                        RETURNING *
                    """,
                    (incident_id, source_event_id),
                )
                return dict(cur.fetchone())


def consolidate_resolved_incident(
    *,
    incident_id: str | None = None,
    incident_slug: str | None = None,
    namespace: str | None = None,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> ConsolidationResult:
    """Compatibility entrypoint backed by a durable consolidation job."""

    if not incident_id and not incident_slug:
        raise ValueError("incident_id or incident_slug is required")
    resolved_url = db_url or database_url()
    with connect(resolved_url, application_name="hindsight-consolidation") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if incident_id:
                cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
            else:
                cur.execute("SELECT * FROM incidents WHERE slug = %s", (incident_slug,))
            incident = cur.fetchone()
            if incident is None:
                return ConsolidationResult(None, namespace, None, False, "incident not found")
            if incident["status"] != "resolved":
                return ConsolidationResult(
                    dict(incident), namespace, None, False, "incident is not resolved"
                )
            cur.execute(
                """
                    SELECT * FROM incident_events
                    WHERE incident_id = %s AND event_type = 'incident_resolved'
                    ORDER BY occurred_at DESC LIMIT 1
                """,
                (incident["id"],),
            )
            event = cur.fetchone()
            if event is None or event["event_schema"] != "incident_resolution.v1":
                return ConsolidationResult(
                    dict(incident), namespace, None, False, "structured resolution evidence missing"
                )
    job = enqueue_consolidation_job(
        incident_id=str(incident["id"]),
        source_event_id=str(event["id"]),
        db_url=resolved_url,
    )
    return process_consolidation_job(
        job_id=str(job["id"]),
        namespace=namespace,
        db_url=resolved_url,
        reasoning_provider=reasoning_provider,
        embedding_provider=embedding_provider,
    )


def process_consolidation_job(
    *,
    job_id: str,
    namespace: str | None = None,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> ConsolidationResult:
    """Lease, synthesize, verify, and publish exactly one logical lesson."""

    resolved_url = db_url or database_url()
    job = _claim_job(job_id=job_id, db_url=resolved_url)
    if job["status"] in TERMINAL_JOB_STATUSES:
        return _result_for_job(job=job, db_url=resolved_url)
    lease_owner = str(job["lease_owner"])
    provider = reasoning_provider or DeterministicReasoningProvider()
    embeddings = embedding_provider or embedding_provider_from_env()
    decision_id = f"consolidation:{job_id}"
    try:
        context = _load_context(
            incident_id=str(job["incident_id"]),
            source_event_id=str(job["source_event_id"]),
            namespace=namespace,
            db_url=resolved_url,
        )
        if context["reason"]:
            reason = context["reason"]
            if _decision_has_ineligible_source_read(
                decision_id=job.get("decision_id"),
                incident_id=str(job["incident_id"]),
                db_url=resolved_url,
            ):
                reason = SOURCE_EVIDENCE_CHANGED_REASON
            return _finish_without_lesson(
                job_id=job_id,
                lease_owner=lease_owner,
                status="not_eligible",
                reason=reason,
                db_url=resolved_url,
            )
        source_memories = context["source_memories"]
        evidence = _evidence_catalog(context)
        with connect(resolved_url, application_name="hindsight-consolidation") as conn:
            with conn.transaction():
                _lock_current_lease(
                    conn,
                    job_id=job_id,
                    lease_owner=lease_owner,
                )
                store = MemoryStore(conn=conn)
                store.open_decision(
                    decision_id=decision_id,
                    actor=CONSOLIDATION_WRITER,
                    decision_kind="lesson_synthesis",
                    purpose="Synthesize a reusable lesson from verified incident evidence",
                    namespace=context["namespace"],
                    metadata={"job_id": job_id},
                )
                existing_read_ids = {
                    str(row["memory_id"])
                    for row in store.reads_for_decision(decision_id=decision_id)
                }
                _validate_read_evidence(
                    conn,
                    context=context,
                    decision_id=decision_id,
                )
                for memory in source_memories:
                    memory_id = str(memory["id"])
                    if memory_id in existing_read_ids:
                        continue
                    store.record_read(
                        decision_id=decision_id,
                        memory_kind="semantic",
                        memory_id=memory_id,
                        reader=CONSOLIDATION_WRITER,
                        purpose="Eligible source evidence for lesson synthesis",
                    )
                linked = conn.execute(
                    """
                        UPDATE consolidation_jobs
                        SET decision_id = %s, updated_at = now()
                        WHERE id = %s AND (decision_id IS NULL OR decision_id = %s)
                            AND status = 'leased' AND lease_owner = %s
                            AND lease_expires_at > now()
                        RETURNING id
                    """,
                    (
                        decision_id,
                        job_id,
                        decision_id,
                        lease_owner,
                    ),
                ).fetchone()
                if linked is None:
                    raise ConsolidationLeaseLostError(
                        f"consolidation lease is no longer current: {job_id}"
                    )
        lesson = _generate_lesson(provider=provider, context=context, evidence=evidence)
        _validate_lesson(lesson=lesson, evidence=evidence)
        content = _render_lesson(lesson)
        prepared_embedding = embeddings.embed_document(content)
        parent_ids = sorted(
            {
                citation["evidence_id"].removeprefix("memory:")
                for claim in lesson["claims"]
                for citation in claim["citations"]
                if citation["evidence_id"].startswith("memory:")
            }
        )
        with connect(resolved_url, application_name="hindsight-consolidation") as conn:
            with conn.transaction():
                lock_embedding_index_write_fence(conn)
                _lock_current_lease(
                    conn,
                    job_id=job_id,
                    lease_owner=lease_owner,
                )
                store = MemoryStore(conn=conn, embedding_provider=embeddings)
                _lock_publication_evidence(
                    conn,
                    context=context,
                    decision_id=decision_id,
                )
                existing = _existing_lesson(
                    conn,
                    incident_id=context["incident"]["id"],
                    namespace=context["namespace"],
                )
                if existing is not None:
                    store.seal_decision(decision_id=decision_id)
                    _complete_job(
                        conn,
                        job_id=job_id,
                        lease_owner=lease_owner,
                        memory=existing,
                    )
                    return ConsolidationResult(
                        context["incident"],
                        context["namespace"],
                        existing,
                        False,
                        "lesson already exists",
                        [str(row["id"]) for row in source_memories],
                        job_id,
                    )
                memory = store.write_semantic(
                    namespace=context["namespace"],
                    content=content,
                    provenance=Provenance(
                        writer=CONSOLIDATION_WRITER,
                        source_ref=f"incident_event:{context['resolution_event']['id']}",
                        justification="Publish evidence-verified procedural lesson",
                    ),
                    metadata={
                        "role": "consolidated-lesson",
                        "source_incident_id": str(context["incident"]["id"]),
                        "source_incident_slug": context["incident"]["slug"],
                        "source_memory_ids": [str(row["id"]) for row in source_memories],
                    },
                    content_schema=LESSON_SCHEMA,
                    structured_payload=lesson,
                    producer_decision_id=decision_id,
                    parent_memory_ids=parent_ids,
                    precomputed_embedding=prepared_embedding,
                )
                conn.execute(
                    """
                        INSERT INTO incident_semantic_memories (
                            incident_id, memory_id, relationship
                        ) VALUES (%s, %s, 'lesson')
                        ON CONFLICT (incident_id, memory_id) DO NOTHING
                    """,
                    (context["incident"]["id"], memory["id"]),
                )
                conn.execute(
                    """
                        INSERT INTO incident_semantic_beliefs (
                            incident_id, belief_id, relationship
                        ) VALUES (%s, %s, 'lesson')
                        ON CONFLICT (incident_id, belief_id)
                        DO UPDATE SET relationship = 'lesson'
                    """,
                    (context["incident"]["id"], memory["belief_id"]),
                )
                _complete_job(
                    conn,
                    job_id=job_id,
                    lease_owner=lease_owner,
                    memory=memory,
                )
        return ConsolidationResult(
            context["incident"],
            context["namespace"],
            memory,
            True,
            None,
            [str(row["id"]) for row in source_memories],
            job_id,
        )
    except ConsolidationEvidenceChangedError:
        return _finish_without_lesson(
            job_id=job_id,
            lease_owner=lease_owner,
            status="not_eligible",
            reason=SOURCE_EVIDENCE_CHANGED_REASON,
            db_url=resolved_url,
        )
    except LessonValidationError as exc:
        return _fail_job_and_decision(
            job_id=job_id,
            lease_owner=lease_owner,
            reason=f"invalid_lesson:{exc}",
            db_url=resolved_url,
        )
    except ConsolidationLeaseLostError:
        raise
    except Exception as exc:
        _retry_or_fail_job(
            job_id=job_id,
            lease_owner=lease_owner,
            exc=exc,
            db_url=resolved_url,
        )
        raise


def incident_closed_changefeed_event(
    incident: dict[str, Any], *, before_status: str = "open", source_event_id: str | None = None
) -> dict[str, Any]:
    """Build the full-envelope transition shape used by local mechanism demos."""

    return {
        "table": "incidents",
        "key": json.dumps([str(incident["id"])]),
        "source_event_id": source_event_id,
        "value": {
            "before": {**_jsonable(incident), "status": before_status},
            "after": _jsonable(incident),
        },
    }


def _claim_job(*, job_id: str, db_url: str) -> dict[str, Any]:
    lease_owner = f"{CONSOLIDATION_WRITER}:{uuid4()}"
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM consolidation_jobs WHERE id = %s FOR UPDATE", (job_id,))
                row = cur.fetchone()
                if row is None:
                    raise LookupError(job_id)
                if row["status"] in TERMINAL_JOB_STATUSES:
                    return dict(row)
                cur.execute("SELECT now() AS current_time")
                current_time = cur.fetchone()["current_time"]
                if (
                    row["status"] == "leased"
                    and row["lease_expires_at"] is not None
                    and row["lease_expires_at"] > current_time
                ):
                    raise ConsolidationLeaseBusyError(
                        "consolidation job already has an active lease"
                    )
                if row["attempt_count"] >= MAX_CONSOLIDATION_ATTEMPTS:
                    cur.execute(
                        """
                            UPDATE consolidation_jobs
                            SET status = 'leased', lease_owner = %s,
                                lease_expires_at = now() + INTERVAL '2 minutes',
                                updated_at = now()
                            WHERE id = %s
                            RETURNING *
                        """,
                        (lease_owner, job_id),
                    )
                    claimed = cur.fetchone()
                    _fail_open_decision(conn, decision_id=claimed["decision_id"])
                    cur.execute(
                        """
                            UPDATE consolidation_jobs
                            SET status = 'failed', lease_owner = NULL,
                                lease_expires_at = NULL,
                                error_code = 'RetryLimitExceeded',
                                error_detail = 'maximum consolidation attempts exhausted',
                                completed_at = now(), updated_at = now()
                            WHERE id = %s AND status = 'leased' AND lease_owner = %s
                                AND lease_expires_at > now()
                            RETURNING *
                        """,
                        (job_id, lease_owner),
                    )
                    terminal = cur.fetchone()
                    if terminal is None:
                        raise ConsolidationLeaseLostError(
                            f"consolidation lease is no longer current: {job_id}"
                        )
                    return dict(terminal)
                cur.execute(
                    """
                        UPDATE consolidation_jobs
                        SET status = 'leased', attempt_count = attempt_count + 1,
                            lease_owner = %s,
                            lease_expires_at = now() + INTERVAL '2 minutes',
                            updated_at = now()
                        WHERE id = %s
                        RETURNING *
                    """,
                    (lease_owner, job_id),
                )
                return dict(cur.fetchone())


def _lock_current_lease(conn: Any, *, job_id: str, lease_owner: str) -> dict[str, Any]:
    """Lock and return a job only when this attempt still owns its live lease."""

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT * FROM consolidation_jobs
                WHERE id = %s AND status = 'leased' AND lease_owner = %s
                    AND lease_expires_at > now()
                FOR UPDATE
            """,
            (job_id, lease_owner),
        )
        row = cur.fetchone()
    if row is None:
        raise ConsolidationLeaseLostError(f"consolidation lease is no longer current: {job_id}")
    return dict(row)


def _load_context(
    *, incident_id: str, source_event_id: str, namespace: str | None, db_url: str
) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
            incident = cur.fetchone()
            cur.execute("SELECT * FROM incident_events WHERE id = %s", (source_event_id,))
            event = cur.fetchone()
            if incident is None or event is None:
                return {"reason": "incident or resolution event not found"}
            if incident["status"] != "resolved":
                return {"incident": dict(incident), "reason": "incident is not resolved"}
            if (
                event["incident_id"] != incident["id"]
                or incident["resolution_event_id"] != event["id"]
                or event["event_type"] != "incident_resolved"
                or event["event_schema"] != "incident_resolution.v1"
            ):
                return {"incident": dict(incident), "reason": "structured resolution evidence missing"}
            cur.execute(
                """
                    SELECT memory.*, link.relationship AS incident_relationship
                    FROM incident_semantic_memories AS link
                    JOIN semantic_memories AS memory ON memory.id = link.memory_id
                    WHERE link.incident_id = %s
                        AND link.relationship IN ('summary', 'resolution', 'root_cause')
                        AND memory.lineage_status IN ('complete', 'legacy_unverified')
                        AND memory.trust_status = 'active'
                        AND memory.t_invalid IS NULL
                    ORDER BY memory.written_at, memory.id
                """,
                (incident["id"],),
            )
            memories = [dict(row) for row in cur.fetchall()]
            target_namespace = namespace or (
                str(memories[0]["namespace"]) if memories else None
            )
            if not memories:
                return {
                    "incident": dict(incident),
                    "namespace": target_namespace,
                    "reason": "no eligible semantic source evidence",
                }
            cur.execute(
                """
                    SELECT service.* FROM incident_services AS link
                    JOIN services AS service ON service.id = link.service_id
                    WHERE link.incident_id = %s ORDER BY service.slug LIMIT 1
                """,
                (incident["id"],),
            )
            service = cur.fetchone()
            return {
                "reason": None,
                "incident": dict(incident),
                "resolution_event": dict(event),
                "service": dict(service) if service else None,
                "source_memories": memories,
                "namespace": target_namespace,
            }


def _validate_read_evidence(
    conn: Any,
    *,
    context: dict[str, Any],
    decision_id: str,
) -> None:
    """Validate selected evidence under row locks before recording decision reads."""

    _lock_and_validate_evidence(
        conn,
        context=context,
        decision_id=decision_id,
        require_all_reads=False,
    )


def _lock_publication_evidence(
    conn: Any,
    *,
    context: dict[str, Any],
    decision_id: str,
) -> None:
    """Lock namespaces and revalidate the exact evidence set before publication."""

    namespaces = sorted(
        {
            str(context["namespace"]),
            *(str(memory["namespace"]) for memory in context["source_memories"]),
        }
    )
    for namespace in namespaces:
        conn.execute(
            """
                INSERT INTO memory_namespaces (namespace)
                VALUES (%s)
                ON CONFLICT (namespace) DO NOTHING
            """,
            (namespace,),
        )
    locked_namespaces = conn.execute(
        """
            SELECT namespace FROM memory_namespaces
            WHERE namespace = ANY(%s)
            ORDER BY namespace
            FOR UPDATE
        """,
        (namespaces,),
    ).fetchall()
    if {str(row[0]) for row in locked_namespaces} != set(namespaces):
        raise ConsolidationEvidenceChangedError(SOURCE_EVIDENCE_CHANGED_REASON)
    _lock_and_validate_evidence(
        conn,
        context=context,
        decision_id=decision_id,
        require_all_reads=True,
    )


def _lock_and_validate_evidence(
    conn: Any,
    *,
    context: dict[str, Any],
    decision_id: str,
    require_all_reads: bool,
) -> None:
    expected_incident = context["incident"]
    expected_event = context["resolution_event"]
    expected_sources = {
        str(memory["id"]): str(memory["incident_relationship"])
        for memory in context["source_memories"]
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM incidents WHERE id = %s FOR UPDATE",
            (expected_incident["id"],),
        )
        incident = cur.fetchone()
        cur.execute("SELECT * FROM incident_events WHERE id = %s", (expected_event["id"],))
        event = cur.fetchone()
        cur.execute(
            """
                SELECT memory_id, relationship
                FROM incident_semantic_memories
                WHERE incident_id = %s
                ORDER BY memory_id
            """,
            (expected_incident["id"],),
        )
        links = [dict(row) for row in cur.fetchall()]
        linked_ids = [str(row["memory_id"]) for row in links]
        memories: list[dict[str, Any]] = []
        if linked_ids:
            cur.execute(
                """
                    SELECT * FROM semantic_memories
                    WHERE id = ANY(%s)
                    ORDER BY id
                    FOR UPDATE
                """,
                (linked_ids,),
            )
            memories = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
                SELECT memory_kind, memory_id
                FROM memory_reads
                WHERE decision_id = %s
                ORDER BY read_at, id
            """,
            (decision_id,),
        )
        decision_reads = [dict(row) for row in cur.fetchall()]

    if not _incident_matches_snapshot(incident, expected_incident):
        raise ConsolidationEvidenceChangedError(SOURCE_EVIDENCE_CHANGED_REASON)
    if not _event_matches_snapshot(
        event,
        expected_event,
        incident_id=expected_incident["id"],
        resolution_event_id=expected_incident["resolution_event_id"],
    ):
        raise ConsolidationEvidenceChangedError(SOURCE_EVIDENCE_CHANGED_REASON)

    memory_by_id = {str(memory["id"]): memory for memory in memories}
    current_sources = {
        str(link["memory_id"]): str(link["relationship"])
        for link in links
        if (memory := memory_by_id.get(str(link["memory_id"]))) is not None
        and _source_is_eligible(memory=memory, relationship=str(link["relationship"]))
    }
    if current_sources != expected_sources:
        raise ConsolidationEvidenceChangedError(SOURCE_EVIDENCE_CHANGED_REASON)

    read_ids: set[str] = set()
    for read in decision_reads:
        if read["memory_kind"] != "semantic":
            raise ConsolidationEvidenceChangedError(SOURCE_EVIDENCE_CHANGED_REASON)
        read_ids.add(str(read["memory_id"]))
    expected_ids = set(expected_sources)
    if not read_ids.issubset(expected_ids):
        raise ConsolidationEvidenceChangedError(SOURCE_EVIDENCE_CHANGED_REASON)
    if require_all_reads and read_ids != expected_ids:
        raise ConsolidationEvidenceChangedError(SOURCE_EVIDENCE_CHANGED_REASON)


def _source_is_eligible(*, memory: dict[str, Any], relationship: str) -> bool:
    return (
        relationship in ELIGIBLE_SOURCE_RELATIONSHIPS
        and memory["lineage_status"] in ELIGIBLE_SOURCE_LINEAGE_STATUSES
        and memory["trust_status"] == "active"
        and memory["t_invalid"] is None
    )


def _incident_matches_snapshot(
    incident: dict[str, Any] | None,
    expected: dict[str, Any],
) -> bool:
    if incident is None or incident["status"] != "resolved":
        return False
    fields = (
        "id",
        "slug",
        "title",
        "severity",
        "status",
        "resolved_at",
        "summary",
        "root_cause",
        "resolution_event_id",
    )
    return all(incident.get(field) == expected.get(field) for field in fields)


def _event_matches_snapshot(
    event: dict[str, Any] | None,
    expected: dict[str, Any],
    *,
    incident_id: UUID,
    resolution_event_id: UUID,
) -> bool:
    if event is None:
        return False
    fields = (
        "id",
        "incident_id",
        "event_type",
        "event_schema",
        "payload_digest",
        "structured_payload",
    )
    return (
        event["incident_id"] == incident_id
        and event["id"] == resolution_event_id
        and event["event_type"] == "incident_resolved"
        and event["event_schema"] == "incident_resolution.v1"
        and all(event.get(field) == expected.get(field) for field in fields)
    )


def _decision_has_ineligible_source_read(
    *,
    decision_id: str | None,
    incident_id: str,
    db_url: str,
) -> bool:
    if decision_id is None:
        return False
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        row = conn.execute(
            """
                SELECT 1
                FROM memory_reads AS read
                LEFT JOIN semantic_memories AS memory
                    ON read.memory_kind = 'semantic' AND memory.id = read.semantic_memory_id
                LEFT JOIN incident_semantic_memories AS link
                    ON link.incident_id = %s AND link.memory_id = memory.id
                WHERE read.decision_id = %s
                    AND (
                        read.memory_kind != 'semantic'
                        OR memory.id IS NULL
                        OR memory.t_invalid IS NOT NULL
                        OR memory.trust_status != 'active'
                        OR memory.lineage_status NOT IN ('complete', 'legacy_unverified')
                        OR link.memory_id IS NULL
                        OR link.relationship NOT IN ('summary', 'resolution', 'root_cause')
                    )
                LIMIT 1
            """,
            (incident_id, decision_id),
        ).fetchone()
    return row is not None


def _evidence_catalog(context: dict[str, Any]) -> dict[str, str]:
    catalog = {
        f"event:{context['resolution_event']['id']}": json.dumps(
            context["resolution_event"]["structured_payload"], sort_keys=True
        )
    }
    for memory in context["source_memories"]:
        catalog[f"memory:{memory['id']}"] = str(memory["content"])
    return catalog


def _generate_lesson(
    *, provider: ReasoningProvider, context: dict[str, Any], evidence: dict[str, str]
) -> dict[str, Any]:
    if provider.provider_name == "deterministic":
        return _fixture_lesson(context=context, evidence=evidence)
    response = provider.generate(
        ReasoningRequest(
            system=(
                "Return only JSON for procedural_lesson.v1. Every claim must have one or more "
                "citations. Each citation requires evidence_id and an exact verbatim quote from "
                "that evidence. Do not infer facts that are not quoted."
            ),
            prompt=json.dumps(
                {
                    "incident": _jsonable(context["incident"]),
                    "service": _jsonable(context["service"]),
                    "evidence": evidence,
                    "required_shape": {
                        "schema_version": 1,
                        "title": "string",
                        "claims": [
                            {
                                "kind": "situation|diagnostic_check|safe_action|avoidance",
                                "text": "string",
                                "citations": [
                                    {"evidence_id": "memory:uuid or event:uuid", "quote": "exact"}
                                ],
                            }
                        ],
                    },
                },
                sort_keys=True,
                default=str,
            ),
            temperature=0.0,
            max_output_tokens=2048,
            routing_key=f"consolidation:{context['incident']['id']}",
            response_json_schema=LESSON_RESPONSE_JSON_SCHEMA,
            thinking_budget=0,
        )
    )
    return _parse_lesson_response(response.text)


def _parse_lesson_response(text: str) -> dict[str, Any]:
    """Accept bare JSON or one optional JSON fence, but no surrounding prose."""

    candidate = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LessonValidationError("model output is not JSON") from exc
    if not isinstance(value, dict):
        raise LessonValidationError("lesson must be a JSON object")
    return value


def _fixture_lesson(*, context: dict[str, Any], evidence: dict[str, str]) -> dict[str, Any]:
    event_id = f"event:{context['resolution_event']['id']}"
    event_payload = context["resolution_event"]["structured_payload"]
    memory_id = next(key for key in evidence if key.startswith("memory:"))
    return {
        "schema_version": 1,
        "title": f"Lesson from {context['incident']['slug']}",
        "claims": [
            {
                "kind": "situation",
                "text": str(context["incident"].get("root_cause") or "Resolved incident pattern"),
                "citations": [{"evidence_id": memory_id, "quote": evidence[memory_id]}],
            },
            {
                "kind": "safe_action",
                "text": str(event_payload["action"]),
                "citations": [{"evidence_id": event_id, "quote": str(event_payload["action"])}],
            },
            {
                "kind": "diagnostic_check",
                "text": str(event_payload["observation"]),
                "citations": [
                    {"evidence_id": event_id, "quote": str(event_payload["observation"])}
                ],
            },
        ],
    }


def _validate_lesson(*, lesson: dict[str, Any], evidence: dict[str, str]) -> None:
    if lesson.get("schema_version") != 1:
        raise LessonValidationError("schema_version must be 1")
    if not isinstance(lesson.get("title"), str) or not lesson["title"].strip():
        raise LessonValidationError("title is required")
    claims = lesson.get("claims")
    if not isinstance(claims, list) or not claims:
        raise LessonValidationError("at least one claim is required")
    allowed_kinds = {"situation", "diagnostic_check", "safe_action", "avoidance"}
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or claim.get("kind") not in allowed_kinds:
            raise LessonValidationError(f"claim {index} has invalid kind")
        if not isinstance(claim.get("text"), str) or not claim["text"].strip():
            raise LessonValidationError(f"claim {index} text is required")
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations:
            raise LessonValidationError(f"claim {index} has no citations")
        for citation in citations:
            if not isinstance(citation, dict):
                raise LessonValidationError(f"claim {index} citation is not an object")
            evidence_id = citation.get("evidence_id")
            quote = citation.get("quote")
            if evidence_id not in evidence:
                raise LessonValidationError(f"claim {index} cites ineligible evidence")
            if not isinstance(quote, str) or not quote.strip() or quote not in evidence[evidence_id]:
                raise LessonValidationError(f"claim {index} quote is not exact evidence")


def _render_lesson(lesson: dict[str, Any]) -> str:
    lines = [lesson["title"]]
    labels = {
        "situation": "Situation",
        "diagnostic_check": "Diagnostic check",
        "safe_action": "Safe action",
        "avoidance": "Avoid",
    }
    lines.extend(f"{labels[claim['kind']]}: {claim['text']}" for claim in lesson["claims"])
    return "\n".join(lines)


def _existing_lesson(
    conn: Any, *, incident_id: UUID, namespace: str
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT memory.*
                FROM incident_semantic_beliefs AS link
                JOIN semantic_memories AS memory ON memory.belief_id = link.belief_id
                WHERE link.incident_id = %s AND link.relationship = 'lesson'
                    AND memory.namespace = %s AND memory.t_invalid IS NULL
                    AND memory.trust_status = 'active'
                    AND memory.writer = %s
                ORDER BY memory.version_number DESC LIMIT 1
            """,
            (incident_id, namespace, CONSOLIDATION_WRITER),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _complete_job(conn: Any, *, job_id: str, lease_owner: str, memory: dict[str, Any]) -> None:
    row = conn.execute(
        """
            UPDATE consolidation_jobs
            SET status = 'completed', lesson_belief_id = %s, lesson_memory_id = %s,
                reason = NULL, error_code = NULL, error_detail = NULL,
                completed_at = now(), updated_at = now(), lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = %s AND status = 'leased' AND lease_owner = %s
                AND lease_expires_at > now()
            RETURNING id
        """,
        (
            memory["belief_id"],
            memory["id"],
            job_id,
            lease_owner,
        ),
    ).fetchone()
    if row is None:
        raise ConsolidationLeaseLostError(f"consolidation lease is no longer current: {job_id}")


def _finish_without_lesson(
    *, job_id: str, lease_owner: str, status: str, reason: str, db_url: str
) -> ConsolidationResult:
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.transaction():
            job = _lock_current_lease(
                conn,
                job_id=job_id,
                lease_owner=lease_owner,
            )
            _fail_open_decision(conn, decision_id=job.get("decision_id"))
            row = conn.execute(
                """
                    UPDATE consolidation_jobs
                    SET status = %s, reason = %s, completed_at = now(),
                        updated_at = now(), lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = %s AND status = 'leased' AND lease_owner = %s
                        AND lease_expires_at > now()
                    RETURNING id
                """,
                (
                    status,
                    reason[:1000],
                    job_id,
                    lease_owner,
                ),
            ).fetchone()
            if row is None:
                raise ConsolidationLeaseLostError(
                    f"consolidation lease is no longer current: {job_id}"
                )
    job = _get_job(job_id=job_id, db_url=db_url)
    return _result_for_job(job=job, db_url=db_url)


def _retry_or_fail_job(
    *,
    job_id: str,
    lease_owner: str,
    exc: Exception,
    db_url: str,
) -> None:
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.transaction():
            job = _lock_current_lease(
                conn,
                job_id=job_id,
                lease_owner=lease_owner,
            )
            row = conn.execute(
                """
                    UPDATE consolidation_jobs
                    SET status = CASE WHEN attempt_count < %s THEN 'retrying' ELSE 'failed' END,
                        error_code = %s, error_detail = %s,
                        completed_at = CASE WHEN attempt_count < %s THEN NULL ELSE now() END,
                        updated_at = now(), lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = %s AND status = 'leased' AND lease_owner = %s
                        AND lease_expires_at > now()
                    RETURNING status
                """,
                (
                    MAX_CONSOLIDATION_ATTEMPTS,
                    type(exc).__name__,
                    safe_error_detail(exc, max_chars=1000),
                    MAX_CONSOLIDATION_ATTEMPTS,
                    job_id,
                    lease_owner,
                ),
            ).fetchone()
            if row is None:
                raise ConsolidationLeaseLostError(
                    f"consolidation lease is no longer current: {job_id}"
                )
            if row[0] == "failed":
                _fail_open_decision(
                    conn,
                    decision_id=job.get("decision_id"),
                )


def _fail_job_and_decision(
    *,
    job_id: str,
    lease_owner: str,
    reason: str,
    db_url: str,
) -> ConsolidationResult:
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.transaction():
            job = _lock_current_lease(
                conn,
                job_id=job_id,
                lease_owner=lease_owner,
            )
            _fail_open_decision(
                conn,
                decision_id=job.get("decision_id"),
            )
            row = conn.execute(
                """
                    UPDATE consolidation_jobs
                    SET status = 'failed', reason = %s, completed_at = now(),
                        updated_at = now(), lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = %s AND status = 'leased' AND lease_owner = %s
                        AND lease_expires_at > now()
                    RETURNING id
                """,
                (reason[:1000], job_id, lease_owner),
            ).fetchone()
            if row is None:
                raise ConsolidationLeaseLostError(
                    f"consolidation lease is no longer current: {job_id}"
                )
    return _result_for_job(job=_get_job(job_id=job_id, db_url=db_url), db_url=db_url)


def _fail_open_decision(conn: Any, *, decision_id: str | None) -> None:
    if decision_id is None:
        return
    conn.execute(
        """
            UPDATE memory_decisions
            SET status = 'failed', sealed_at = COALESCE(sealed_at, now())
            WHERE id = %s AND status = 'open'
        """,
        (decision_id,),
    )


def _get_job(*, job_id: str, db_url: str) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM consolidation_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            if row is None:
                raise LookupError(job_id)
            return dict(row)


def _result_for_job(*, job: dict[str, Any], db_url: str) -> ConsolidationResult:
    with connect(db_url, application_name="hindsight-consolidation") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM incidents WHERE id = %s", (job["incident_id"],))
            incident = cur.fetchone()
            memory = None
            if job.get("lesson_memory_id"):
                cur.execute(
                    "SELECT * FROM semantic_memories WHERE id = %s", (job["lesson_memory_id"],)
                )
                memory = cur.fetchone()
    return ConsolidationResult(
        dict(incident) if incident else None,
        str(memory["namespace"]) if memory else None,
        dict(memory) if memory else None,
        False,
        job.get("reason")
        or job.get("error_code")
        or ("lesson already exists" if memory is not None else None),
        None,
        str(job["id"]),
    )


def _event_rows(event: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(event, list):
        return event
    records = event.get("Records") or event.get("records")
    if isinstance(records, list):
        rows = []
        for record in records:
            body = record.get("body") if isinstance(record, dict) else None
            rows.append(json.loads(body) if isinstance(body, str) else record)
        return rows
    return [event]


def _resolved_transition(row: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    if row.get("table") not in {None, "incidents"}:
        return None
    value = _decode_json(row.get("value", row))
    if not isinstance(value, dict):
        return None
    before = value.get("before")
    after = value.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if before.get("status") == "resolved" or after.get("status") != "resolved":
        return None
    source_event_id = row.get("source_event_id") or after.get("resolution_event_id")
    if not source_event_id:
        return None
    return after, str(source_event_id)


def _decode_json(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def _jsonable_result(result: ConsolidationResult) -> dict[str, Any]:
    return {
        "incident": _jsonable(result.incident),
        "namespace": result.namespace,
        "memory": _jsonable(result.memory),
        "created": result.created,
        "reason": result.reason,
        "source_memory_ids": result.source_memory_ids or [],
        "job_id": result.job_id,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def evidence_digest(value: Any) -> str:
    """Return the stable digest used for fixture and external evidence checks."""

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
