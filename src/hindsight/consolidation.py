"""Resolved-incident consolidation into reusable semantic lessons."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from hindsight.db import connect, database_url
from hindsight.embeddings import DeterministicEmbeddingProvider
from hindsight.memory import MemoryStore, Provenance

CONSOLIDATION_WRITER = "consolidation.worker"


@dataclass(frozen=True)
class ConsolidationResult:
    """Result of consolidating one resolved incident."""

    incident: dict[str, Any] | None
    namespace: str | None
    memory: dict[str, Any] | None
    created: bool
    reason: str | None = None
    source_memory_ids: list[str] | None = None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda-compatible entrypoint for incident changefeed events."""

    results = handle_incident_changefeed_event(event)
    return {
        "processed": len(results),
        "results": [_jsonable_result(result) for result in results],
    }


def handle_incident_changefeed_event(
    event: dict[str, Any] | list[dict[str, Any]],
    *,
    db_url: str | None = None,
) -> list[ConsolidationResult]:
    """Consolidate resolved incidents from CockroachDB changefeed-shaped events."""

    results: list[ConsolidationResult] = []
    for row in _event_rows(event):
        incident = _incident_after(row)
        if incident is None:
            continue
        status = str(incident.get("status") or "").lower()
        if status != "resolved":
            continue
        incident_id = incident.get("id")
        incident_slug = incident.get("slug")
        results.append(
            consolidate_resolved_incident(
                incident_id=str(incident_id) if incident_id is not None else None,
                incident_slug=str(incident_slug) if incident_slug is not None else None,
                db_url=db_url,
            )
        )
    return results


def consolidate_resolved_incident(
    *,
    incident_id: str | None = None,
    incident_slug: str | None = None,
    namespace: str | None = None,
    db_url: str | None = None,
) -> ConsolidationResult:
    """Distill one resolved incident into an idempotent semantic lesson memory."""

    if not incident_id and not incident_slug:
        raise ValueError("incident_id or incident_slug is required")
    resolved_db_url = db_url or database_url()
    with connect(resolved_db_url) as conn:
        with conn.transaction():
            incident = _incident(conn, incident_id=incident_id, incident_slug=incident_slug)
            if incident is None:
                return ConsolidationResult(
                    incident=None,
                    namespace=namespace,
                    memory=None,
                    created=False,
                    reason="incident not found",
                )
            if incident["status"] != "resolved":
                return ConsolidationResult(
                    incident=incident,
                    namespace=namespace,
                    memory=None,
                    created=False,
                    reason="incident is not resolved",
                )
            target_namespace = namespace or _incident_namespace(conn, incident_id=incident["id"])
            if not target_namespace:
                return ConsolidationResult(
                    incident=incident,
                    namespace=None,
                    memory=None,
                    created=False,
                    reason="no linked memory namespace",
                )
            existing = _existing_lesson(
                conn,
                incident_id=incident["id"],
                namespace=target_namespace,
            )
            if existing is not None:
                return ConsolidationResult(
                    incident=incident,
                    namespace=target_namespace,
                    memory=existing,
                    created=False,
                    reason="lesson already exists",
                    source_memory_ids=_source_memory_ids(conn, incident_id=incident["id"]),
                )

            service = _incident_service(conn, incident_id=incident["id"])
            resolution_event = _latest_resolution_event(conn, incident_id=incident["id"])
            source_memories = _source_memories(conn, incident_id=incident["id"])
            content = _lesson_content(
                incident=incident,
                service=service,
                resolution_event=resolution_event,
                source_memories=source_memories,
            )
            memory = MemoryStore(
                conn=conn,
                embedding_provider=DeterministicEmbeddingProvider(),
            ).remember(
                memory_kind="semantic",
                namespace=target_namespace,
                content=content,
                provenance=Provenance(
                    writer=CONSOLIDATION_WRITER,
                    source_ref=f"incident:{incident['slug']}:resolved",
                    justification="Distill resolved incident into reusable cross-episode lesson",
                ),
                metadata={
                    "demo": "cross-episode-learning",
                    "role": "consolidated-lesson",
                    "source_incident_id": str(incident["id"]),
                    "source_incident_slug": incident["slug"],
                    "service_slug": service["slug"] if service else None,
                    "source_memory_ids": [str(row["id"]) for row in source_memories],
                },
            )
            conn.execute(
                """
                    INSERT INTO incident_semantic_memories (
                        incident_id, memory_id, relationship
                    )
                    VALUES (%s, %s, 'lesson')
                    ON CONFLICT (incident_id, memory_id) DO UPDATE SET
                        relationship = excluded.relationship
                """,
                (incident["id"], memory["id"]),
            )
        return ConsolidationResult(
            incident=incident,
            namespace=target_namespace,
            memory=memory,
            created=True,
            source_memory_ids=[str(row["id"]) for row in source_memories],
        )


def incident_closed_changefeed_event(incident: dict[str, Any]) -> dict[str, Any]:
    """Return the sinkless changefeed row shape used by the local demo."""

    return {
        "table": "incidents",
        "key": json.dumps([str(incident["id"])]),
        "value": {"after": _jsonable(incident)},
    }


def _event_rows(event: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(event, list):
        return event
    records = event.get("Records") or event.get("records")
    if isinstance(records, list):
        rows = []
        for record in records:
            body = record.get("body") if isinstance(record, dict) else None
            if isinstance(body, str):
                rows.append(json.loads(body))
            elif isinstance(record, dict):
                rows.append(record)
        return rows
    return [event]


def _incident_after(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("table") not in {None, "incidents"}:
        return None
    value = _decode_json(row.get("value", row))
    if not isinstance(value, dict):
        return None
    after = value.get("after") if isinstance(value.get("after"), dict) else value
    if not isinstance(after, dict):
        return None
    if "status" not in after or "slug" not in after:
        return None
    return after


def _incident(conn: Any, *, incident_id: str | None, incident_slug: str | None) -> dict[str, Any] | None:
    query = """
        SELECT *
        FROM incidents
        WHERE id = %s
    """
    params = (incident_id,)
    if not incident_id:
        query = """
            SELECT *
            FROM incidents
            WHERE slug = %s
        """
        params = (incident_slug,)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return dict(row) if row else None


def _incident_namespace(conn: Any, *, incident_id: UUID) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
                SELECT s.namespace
                FROM incident_semantic_memories AS im
                JOIN semantic_memories AS s
                    ON s.id = im.memory_id
                WHERE im.incident_id = %s
                ORDER BY s.written_at ASC
                LIMIT 1
            """,
            (incident_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _existing_lesson(conn: Any, *, incident_id: UUID, namespace: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT s.*
                FROM incident_semantic_memories AS im
                JOIN semantic_memories AS s
                    ON s.id = im.memory_id
                WHERE im.incident_id = %s
                    AND im.relationship = 'lesson'
                    AND s.namespace = %s
                    AND s.writer = %s
                ORDER BY s.written_at ASC
                LIMIT 1
            """,
            (incident_id, namespace, CONSOLIDATION_WRITER),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _incident_service(conn: Any, *, incident_id: UUID) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT s.*
                FROM incident_services AS isvc
                JOIN services AS s
                    ON s.id = isvc.service_id
                WHERE isvc.incident_id = %s
                ORDER BY s.slug ASC
                LIMIT 1
            """,
            (incident_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _latest_resolution_event(conn: Any, *, incident_id: UUID) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT *
                FROM incident_events
                WHERE incident_id = %s
                    AND event_type = 'incident_resolved'
                ORDER BY occurred_at DESC
                LIMIT 1
            """,
            (incident_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _source_memories(conn: Any, *, incident_id: UUID) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT s.*
                FROM incident_semantic_memories AS im
                JOIN semantic_memories AS s
                    ON s.id = im.memory_id
                WHERE im.incident_id = %s
                    AND im.relationship IN ('summary', 'resolution', 'root_cause')
                ORDER BY s.written_at ASC
            """,
            (incident_id,),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def _source_memory_ids(conn: Any, *, incident_id: UUID) -> list[str]:
    return [str(row["id"]) for row in _source_memories(conn, incident_id=incident_id)]


def _lesson_content(
    *,
    incident: dict[str, Any],
    service: dict[str, Any] | None,
    resolution_event: dict[str, Any] | None,
    source_memories: list[dict[str, Any]],
) -> str:
    service_slug = service["slug"] if service else "affected service"
    resolution = resolution_event["summary"] if resolution_event else "resolution was recorded"
    source_excerpt = " ".join(str(row.get("content") or "") for row in source_memories)
    root_cause = incident.get("root_cause") or "root cause was identified during the episode"
    return (
        f"Consolidated lesson from incident {incident['slug']} for {service_slug}. "
        f"Root cause: {_sentence(root_cause)} What worked: {_sentence(resolution)} "
        "Repeat guidance: check processor timeout rate and queue depth first, throttle retry "
        "fanout, and avoid scaling workers until downstream processor health is understood. "
        f"Source episode evidence: {source_excerpt[:800]}"
    )


def _sentence(value: Any) -> str:
    return str(value).strip().rstrip(".") + "."


def _decode_json(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _jsonable_result(result: ConsolidationResult) -> dict[str, Any]:
    return {
        "incident": _jsonable(result.incident),
        "namespace": result.namespace,
        "memory": _jsonable(result.memory),
        "created": result.created,
        "reason": result.reason,
        "source_memory_ids": result.source_memory_ids or [],
    }


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
    return value
