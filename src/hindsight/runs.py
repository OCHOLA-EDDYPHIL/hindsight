"""Durable incident and agent-run projections for the product API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect

TERMINAL_RUN_STATUSES = frozenset({"completed", "rejected", "failed"})
RUN_STATUSES = frozenset(
    {
        "queued",
        "triaging",
        "recalling",
        "planning",
        "awaiting_approval",
        "resuming",
        "reflecting",
        *TERMINAL_RUN_STATUSES,
    }
)


class RunConflictError(RuntimeError):
    """Raised when a requested run transition is no longer valid."""


class RunNotFoundError(LookupError):
    """Raised when a run does not exist."""


def create_incident(
    *,
    slug: str,
    title: str,
    severity: str,
    summary: str,
    service_slug: str | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Create one open incident and optionally associate its service."""

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        INSERT INTO incidents (
                            slug, title, severity, status, started_at, summary
                        )
                        VALUES (%s, %s, %s, 'open', now(), %s)
                        RETURNING *
                    """,
                    (slug, title, severity, summary),
                )
                incident = dict(cur.fetchone())
                if service_slug:
                    cur.execute("SELECT id FROM services WHERE slug = %s", (service_slug,))
                    service = cur.fetchone()
                    if service is not None:
                        cur.execute(
                            """
                                INSERT INTO incident_services (incident_id, service_id, impact)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (incident_id, service_id) DO NOTHING
                            """,
                            (incident["id"], service["id"], summary),
                        )
        return _jsonable(incident)


def list_incidents(*, limit: int = 30, db_url: str | None = None) -> list[dict[str, Any]]:
    """Return recent incidents with their newest run state."""

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT
                        i.*,
                        s.slug AS service_slug,
                        r.id AS latest_run_id,
                        r.status AS latest_run_status,
                        r.decision_id AS latest_decision_id,
                        r.updated_at AS latest_run_updated_at
                    FROM incidents AS i
                    LEFT JOIN LATERAL (
                        SELECT service.slug
                        FROM incident_services AS link
                        JOIN services AS service ON service.id = link.service_id
                        WHERE link.incident_id = i.id
                        ORDER BY service.slug
                        LIMIT 1
                    ) AS s ON true
                    LEFT JOIN LATERAL (
                        SELECT id, status, decision_id, updated_at
                        FROM agent_runs
                        WHERE incident_id = i.id OR incident_slug = i.slug
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS r ON true
                    ORDER BY i.started_at DESC, i.created_at DESC
                    LIMIT %s
                """,
                (limit,),
            )
            return _jsonable([dict(row) for row in cur.fetchall()])


def get_incident(*, slug: str, db_url: str | None = None) -> dict[str, Any] | None:
    """Return an incident and its recent runs."""

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT i.*, s.slug AS service_slug
                    FROM incidents AS i
                    LEFT JOIN LATERAL (
                        SELECT service.slug
                        FROM incident_services AS link
                        JOIN services AS service ON service.id = link.service_id
                        WHERE link.incident_id = i.id
                        ORDER BY service.slug
                        LIMIT 1
                    ) AS s ON true
                    WHERE i.slug = %s
                """,
                (slug,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            incident = dict(row)
            cur.execute(
                """
                    SELECT *
                    FROM agent_runs
                    WHERE incident_id = %s OR incident_slug = %s
                    ORDER BY created_at DESC
                    LIMIT 30
                """,
                (incident["id"], slug),
            )
            incident["runs"] = [dict(item) for item in cur.fetchall()]
            return _jsonable(incident)


def create_run(
    *,
    incident_slug: str,
    namespace: str,
    user_input: str,
    service_slug: str | None = None,
    thread_id: str | None = None,
    idempotency_key: str | None = None,
    db_url: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a queued run, returning ``(run, created)``."""

    run_id = uuid4()
    resolved_thread_id = thread_id or f"{incident_slug}:{run_id}"
    decision_id = f"agent:{run_id}:plan"
    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                if idempotency_key:
                    cur.execute(
                        "SELECT * FROM agent_runs WHERE idempotency_key = %s",
                        (idempotency_key,),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        return _jsonable(dict(existing)), False
                cur.execute("SELECT id FROM incidents WHERE slug = %s", (incident_slug,))
                incident = cur.fetchone()
                cur.execute(
                    """
                        INSERT INTO agent_runs (
                            id, idempotency_key, thread_id, incident_id, incident_slug,
                            namespace, service_slug, user_input, status, decision_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s)
                        RETURNING *
                    """,
                    (
                        run_id,
                        idempotency_key,
                        resolved_thread_id,
                        incident["id"] if incident else None,
                        incident_slug,
                        namespace,
                        service_slug,
                        user_input,
                        decision_id,
                    ),
                )
                run = dict(cur.fetchone())
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase="queue",
                    status="queued",
                    summary="Agent run queued",
                )
        return _jsonable(run), True


def get_run(*, run_id: str | UUID, db_url: str | None = None) -> dict[str, Any] | None:
    """Return one run and its ordered phase events."""

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM agent_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
            if row is None:
                return None
            run = dict(row)
            cur.execute(
                "SELECT * FROM agent_run_events WHERE run_id = %s ORDER BY sequence",
                (run_id,),
            )
            run["events"] = [dict(item) for item in cur.fetchall()]
            return _jsonable(run)


def claim_run(
    *,
    run_id: str | UUID,
    expected_status: str,
    next_status: str,
    db_url: str | None = None,
) -> dict[str, Any] | None:
    """Claim a queued/resuming run once; duplicate deliveries return ``None``."""

    _validate_status(next_status)
    with connect(db_url, application_name="hindsight-worker") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        UPDATE agent_runs
                        SET status = %s,
                            started_at = COALESCE(started_at, now()),
                            updated_at = now()
                        WHERE id = %s AND status = %s
                        RETURNING *
                    """,
                    (next_status, run_id, expected_status),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase=next_status,
                    status=next_status,
                    summary=f"Agent run entered {next_status.replace('_', ' ')}",
                )
                return _jsonable(dict(row))


def transition_run(
    *,
    run_id: str | UUID,
    status: str,
    phase: str,
    summary: str,
    metadata: dict[str, Any] | None = None,
    fields: dict[str, Any] | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Update one run and append its event in the same transaction."""

    _validate_status(status)
    allowed_fields = {
        "plan",
        "proposed_action",
        "action_approved",
        "provider",
        "model",
        "usage",
        "reflected_memory_id",
        "failure_code",
        "failure_detail",
    }
    supplied = fields or {}
    unknown = set(supplied) - allowed_fields
    if unknown:
        raise ValueError(f"unsupported run fields: {', '.join(sorted(unknown))}")
    assignments = ["status = %s", "updated_at = now()"]
    values: list[Any] = [status]
    for name, value in supplied.items():
        assignments.append(f"{name} = %s")
        values.append(Jsonb(value) if name == "usage" else value)
    if status in TERMINAL_RUN_STATUSES:
        assignments.append("completed_at = now()")
    values.append(run_id)

    with connect(db_url, application_name="hindsight-worker") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"UPDATE agent_runs SET {', '.join(assignments)} WHERE id = %s RETURNING *",
                    tuple(values),
                )
                row = cur.fetchone()
                if row is None:
                    raise RunNotFoundError(str(run_id))
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase=phase,
                    status=status,
                    summary=summary,
                    metadata=metadata,
                )
                return _jsonable(dict(row))


def prepare_approval(
    *,
    run_id: str | UUID,
    approved: bool,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Move an interrupted run into the resuming queue exactly once."""

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        UPDATE agent_runs
                        SET status = 'resuming', action_approved = %s, updated_at = now()
                        WHERE id = %s AND status = 'awaiting_approval'
                        RETURNING *
                    """,
                    (approved, run_id),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT status FROM agent_runs WHERE id = %s", (run_id,))
                    current = cur.fetchone()
                    if current is None:
                        raise RunNotFoundError(str(run_id))
                    raise RunConflictError(
                        f"run {run_id} cannot be approved from {current['status']}"
                    )
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase="approval",
                    status="resuming",
                    summary="Operator approved the proposed action"
                    if approved
                    else "Operator rejected the proposed action",
                    metadata={"approved": approved},
                )
                return _jsonable(dict(row))


def fail_run(
    *,
    run_id: str | UUID,
    failure_code: str,
    failure_detail: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Put a non-terminal run into its explicit failed state."""

    existing = get_run(run_id=run_id, db_url=db_url)
    if existing is None:
        raise RunNotFoundError(str(run_id))
    if existing["status"] in TERMINAL_RUN_STATUSES:
        return existing
    return transition_run(
        run_id=run_id,
        status="failed",
        phase="failure",
        summary="Agent run failed",
        fields={
            "failure_code": failure_code,
            "failure_detail": failure_detail[:500],
        },
        db_url=db_url,
    )


def _append_event_with_cursor(
    cur: Any,
    *,
    run_id: str | UUID,
    phase: str,
    status: str,
    summary: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    cur.execute("SELECT id FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,))
    cur.execute(
        "SELECT COALESCE(max(sequence), 0) + 1 AS sequence FROM agent_run_events WHERE run_id = %s",
        (run_id,),
    )
    sequence = cur.fetchone()["sequence"]
    cur.execute(
        """
            INSERT INTO agent_run_events (
                run_id, sequence, phase, status, summary, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (run_id, sequence, phase, status, summary, Jsonb(metadata or {})),
    )


def _validate_status(status: str) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"unsupported run status: {status}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return timestamp.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value
