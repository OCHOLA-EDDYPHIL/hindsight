"""Durable incident and agent-run projections for the product API."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.errors import SerializationFailure
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
GET_RUN_READ_ATTEMPTS = 3


class RunConflictError(RuntimeError):
    """Raised when a requested run transition is no longer valid."""


class RunNotFoundError(LookupError):
    """Raised when a run does not exist."""


class RunAttemptLeaseLostError(RuntimeError):
    """Raised when a worker no longer owns the current run attempt."""


class RunAttemptBusyError(RuntimeError):
    """Raised when a duplicate delivery encounters a live worker attempt."""


class RunAttemptsExhaustedError(RuntimeError):
    """Raised when a run command has used all durable worker attempts."""


@dataclass(frozen=True)
class RunAttemptClaim:
    outcome: Literal["claimed", "busy", "duplicate", "exhausted", "missing"]
    run: dict[str, Any] | None
    attempt_id: str | None


def create_incident(
    *,
    slug: str,
    title: str,
    severity: str,
    summary: str,
    service_slug: str | None = None,
    consolidation_policy: str = "managed",
    db_url: str | None = None,
) -> dict[str, Any]:
    """Create one open incident and optionally associate its service."""

    if consolidation_policy not in {"managed", "manual"}:
        raise ValueError("consolidation_policy must be managed or manual")

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        INSERT INTO incidents (
                            slug, title, severity, status, started_at, summary,
                            consolidation_policy
                        )
                        VALUES (%s, %s, %s, 'open', now(), %s, %s)
                        RETURNING *
                    """,
                    (slug, title, severity, summary, consolidation_policy),
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


def resolve_incident(
    *,
    slug: str,
    root_cause: str,
    action: str,
    observation: str,
    recovered: bool,
    actor: str,
    occurred_at: datetime | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Record an independently structured resolution transition and evidence event."""

    payload = {
        "schema_version": 1,
        "incident_slug": slug,
        "root_cause": root_cause,
        "action": action,
        "observation": observation,
        "recovered": recovered,
        "actor": actor,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM incidents WHERE slug = %s FOR UPDATE", (slug,))
                before = cur.fetchone()
                if before is None:
                    raise LookupError(slug)
                if before["status"] == "resolved":
                    cur.execute(
                        """
                            SELECT * FROM incident_events
                            WHERE incident_id = %s AND event_type = 'incident_resolved'
                            ORDER BY occurred_at DESC LIMIT 1
                        """,
                        (before["id"],),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        return _jsonable(
                            {"incident": dict(before), "event": dict(existing), "created": False}
                        )
                resolved_at = occurred_at or datetime.now(UTC)
                event_id = uuid4()
                cur.execute(
                    """
                        INSERT INTO incident_events (
                            id, incident_id, occurred_at, event_type, summary, metadata,
                            event_schema, payload_digest, structured_payload
                        )
                        VALUES (%s, %s, %s, 'incident_resolved', %s, %s,
                                'incident_resolution.v1', %s, %s)
                        RETURNING *
                    """,
                    (
                        event_id,
                        before["id"],
                        resolved_at,
                        observation,
                        Jsonb({"actor": actor}),
                        digest,
                        Jsonb(payload),
                    ),
                )
                event = dict(cur.fetchone())
                cur.execute(
                    """
                        UPDATE incidents
                        SET status = 'resolved', resolved_at = %s, root_cause = %s,
                            resolution_event_id = %s
                        WHERE id = %s
                        RETURNING *
                    """,
                    (resolved_at, root_cause, event_id, before["id"]),
                )
                incident = dict(cur.fetchone())
                return _jsonable({"incident": incident, "event": event, "created": True})


def list_incidents(
    *,
    limit: int = 30,
    before_started_at: str | None = None,
    before_id: str | None = None,
    db_url: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent incidents with their newest run state."""

    with connect(db_url, application_name="hindsight-api") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cursor_clause = ""
            params: list[Any] = []
            if before_started_at is not None or before_id is not None:
                if before_started_at is None or before_id is None:
                    raise ValueError("incident cursor requires timestamp and identifier")
                cursor_clause = "WHERE (i.started_at, i.id) < (%s, %s)"
                params.extend((before_started_at, before_id))
            params.append(limit)
            cur.execute(
                f"""
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
                    {cursor_clause}
                    ORDER BY i.started_at DESC, i.id DESC
                    LIMIT %s
                """,
                params,
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
    retrieval_policy: str = "semantic_strict",
    dispatch_available_at: datetime | None = None,
    db_url: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a queued run, returning ``(run, created)``."""

    if retrieval_policy not in {"semantic_strict", "semantic_then_keyword"}:
        raise ValueError(f"unsupported retrieval policy: {retrieval_policy}")
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
                        INSERT INTO memory_decisions (
                            id, actor, decision_kind, purpose, namespace, metadata
                        )
                        VALUES (%s, 'agent.run', 'agent_plan', %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        decision_id,
                        "Triage incident, retrieve evidence, and propose a safe action",
                        namespace,
                        Jsonb({"thread_id": resolved_thread_id}),
                    ),
                )
                cur.execute(
                    """
                        INSERT INTO agent_runs (
                            id, idempotency_key, thread_id, incident_id, incident_slug,
                            namespace, service_slug, user_input, status, decision_id,
                            retrieval_policy
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s)
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
                        retrieval_policy,
                    ),
                )
                run = dict(cur.fetchone())
                cur.execute(
                    "UPDATE memory_decisions SET run_id = %s WHERE id = %s",
                    (run_id, decision_id),
                )
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase="queue",
                    status="queued",
                    summary="Agent run queued",
                )
                _append_dispatch_with_cursor(
                    cur,
                    run_id=run_id,
                    command="start",
                    payload={"command": "start", "run_id": str(run_id)},
                    available_at=dispatch_available_at,
                )
        return _jsonable(run), True


def get_run(*, run_id: str | UUID, db_url: str | None = None) -> dict[str, Any] | None:
    """Return one run and its ordered phase events."""

    for attempt in range(GET_RUN_READ_ATTEMPTS):
        try:
            return _read_run(run_id=run_id, db_url=db_url)
        except SerializationFailure:
            if attempt + 1 == GET_RUN_READ_ATTEMPTS:
                raise
    raise AssertionError("unreachable run read retry state")


def _read_run(*, run_id: str | UUID, db_url: str | None) -> dict[str, Any] | None:
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


def claim_run_attempt(
    *,
    run_id: str | UUID,
    command: str,
    lease_ttl: timedelta,
    max_attempts: int,
    db_url: str | None = None,
) -> RunAttemptClaim:
    """Claim or recover one database-authoritative worker attempt."""

    if command not in {"start", "resume"}:
        raise ValueError(f"unsupported worker command: {command}")
    if lease_ttl <= timedelta(0):
        raise ValueError("lease_ttl must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    expected_status = "queued" if command == "start" else "resuming"
    initial_status = "triaging" if command == "start" else "reflecting"
    active_statuses = (
        {"triaging", "recalling", "planning"}
        if command == "start"
        else {"reflecting"}
    )
    with connect(db_url, application_name="hindsight-worker") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT now() AS current_time")
                current_time = cur.fetchone()["current_time"]
                cur.execute("SELECT * FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,))
                row = cur.fetchone()
                if row is None:
                    return RunAttemptClaim("missing", None, None)
                run = dict(row)
                same_command = run["worker_attempt_command"] == command
                count = int(run["worker_attempt_count"] or 0) if same_command else 0
                active = run["status"] in active_statuses and same_command
                lease_live = (
                    active
                    and run["worker_attempt_id"] is not None
                    and run["worker_attempt_lease_expires_at"] is not None
                    and run["worker_attempt_lease_expires_at"] > current_time
                )
                if lease_live:
                    return RunAttemptClaim("busy", _jsonable(run), None)

                reclaiming = active
                if run["status"] != expected_status and not reclaiming:
                    return RunAttemptClaim("duplicate", _jsonable(run), None)
                if count >= max_attempts:
                    return RunAttemptClaim("exhausted", _jsonable(run), None)

                attempt_id = uuid4()
                attempt_count = count + 1
                previous_status = run["status"]
                previous_attempt_id = run["worker_attempt_id"]
                cur.execute(
                    """
                        UPDATE agent_runs
                        SET status = %s,
                            started_at = COALESCE(started_at, now()),
                            updated_at = now(),
                            worker_attempt_id = %s,
                            worker_attempt_count = %s,
                            worker_attempt_command = %s,
                            worker_attempt_lease_expires_at = now() + %s
                        WHERE id = %s
                        RETURNING *
                    """,
                    (
                        initial_status,
                        attempt_id,
                        attempt_count,
                        command,
                        lease_ttl,
                        run_id,
                    ),
                )
                claimed = dict(cur.fetchone())
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase="recovery" if reclaiming else initial_status,
                    status=initial_status,
                    summary=(
                        "Agent run attempt reclaimed after lease expiry"
                        if reclaiming
                        else f"Agent run entered {initial_status.replace('_', ' ')}"
                    ),
                    metadata={
                        "attempt": attempt_count,
                        "attempt_id": str(attempt_id),
                        "command": command,
                        **(
                            {
                                "previous_status": previous_status,
                                "previous_attempt_id": str(previous_attempt_id),
                            }
                            if reclaiming
                            else {}
                        ),
                    },
                )
                return RunAttemptClaim("claimed", _jsonable(claimed), str(attempt_id))


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
    if status in {"triaging", "recalling", "planning", "reflecting", *TERMINAL_RUN_STATUSES}:
        raise ValueError(f"worker-owned run status requires an attempt token: {status}")
    assignments, values = _run_field_assignments(status=status, fields=fields)
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


def transition_run_attempt(
    *,
    run_id: str | UUID,
    attempt_id: str | UUID,
    status: str,
    phase: str,
    summary: str,
    metadata: dict[str, Any] | None = None,
    fields: dict[str, Any] | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Write progress only while the exact worker attempt owns a live lease."""

    if status not in {"triaging", "recalling", "planning", "reflecting"}:
        raise ValueError(f"unsupported active run status: {status}")
    expected_command = "resume" if status == "reflecting" else "start"
    assignments, values = _run_field_assignments(status=status, fields=fields)
    values.extend((run_id, attempt_id, expected_command))
    with connect(db_url, application_name="hindsight-worker") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                        UPDATE agent_runs SET {', '.join(assignments)}
                        WHERE id = %s
                            AND worker_attempt_id = %s
                            AND worker_attempt_command = %s
                            AND worker_attempt_lease_expires_at > now()
                        RETURNING *
                    """,
                    tuple(values),
                )
                row = cur.fetchone()
                if row is None:
                    raise RunAttemptLeaseLostError(
                        f"agent run attempt lease is no longer current: {run_id}"
                    )
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase=phase,
                    status=status,
                    summary=summary,
                    metadata=metadata,
                )
                return _jsonable(dict(row))


def finish_run_attempt(
    *,
    run_id: str | UUID,
    attempt_id: str | UUID,
    status: str,
    phase: str,
    summary: str,
    metadata: dict[str, Any] | None = None,
    fields: dict[str, Any] | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Finish the current start or resume attempt and release its lease."""

    if status not in {"awaiting_approval", "completed", "rejected"}:
        raise ValueError(f"unsupported attempt finish status: {status}")
    expected_command = "start" if status == "awaiting_approval" else "resume"
    assignments, values = _run_field_assignments(status=status, fields=fields)
    assignments.extend(
        ["worker_attempt_id = NULL", "worker_attempt_lease_expires_at = NULL"]
    )
    if status in TERMINAL_RUN_STATUSES:
        assignments.append("completed_at = now()")
    values.extend((run_id, attempt_id, expected_command))
    with connect(db_url, application_name="hindsight-worker") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                        UPDATE agent_runs SET {', '.join(assignments)}
                        WHERE id = %s
                            AND worker_attempt_id = %s
                            AND worker_attempt_command = %s
                            AND worker_attempt_lease_expires_at > now()
                        RETURNING *
                    """,
                    tuple(values),
                )
                row = cur.fetchone()
                if row is None:
                    raise RunAttemptLeaseLostError(
                        f"agent run attempt lease is no longer current: {run_id}"
                    )
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase=phase,
                    status=status,
                    summary=summary,
                    metadata=metadata,
                )
                return _jsonable(dict(row))


def record_run_attempt_failure(
    *,
    run_id: str | UUID,
    attempt_id: str | UUID,
    error_type: str,
    error_detail: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Record a retryable failure without releasing or extending its lease."""

    with connect(db_url, application_name="hindsight-worker") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        SELECT * FROM agent_runs
                        WHERE id = %s
                            AND worker_attempt_id = %s
                            AND worker_attempt_lease_expires_at > now()
                        FOR UPDATE
                    """,
                    (run_id, attempt_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise RunAttemptLeaseLostError(
                        f"agent run attempt lease is no longer current: {run_id}"
                    )
                run = dict(row)
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase="retry",
                    status=run["status"],
                    summary=f"Agent run attempt {run['worker_attempt_count']} failed",
                    metadata={
                        "attempt": int(run["worker_attempt_count"]),
                        "attempt_id": str(attempt_id),
                        "command": run["worker_attempt_command"],
                        "error_type": error_type,
                        "error_detail": error_detail[:500],
                    },
                )
                return _jsonable(run)


def finalize_exhausted_run(
    *,
    run_id: str | UUID,
    command: str,
    max_attempts: int,
    db_url: str | None = None,
) -> dict[str, Any] | None:
    """Atomically fail and seal a run after its final attempt expires."""

    if command not in {"start", "resume"}:
        raise ValueError(f"unsupported worker command: {command}")
    with connect(db_url, application_name="hindsight-worker") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT now() AS current_time")
                current_time = cur.fetchone()["current_time"]
                cur.execute("SELECT * FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                run = dict(row)
                if run["status"] in TERMINAL_RUN_STATUSES:
                    return _jsonable(run)
                if run["worker_attempt_command"] != command:
                    return _jsonable(run)
                lease_expiry = run["worker_attempt_lease_expires_at"]
                if lease_expiry is not None and lease_expiry > current_time:
                    raise RunAttemptBusyError(f"agent run attempt is still live: {run_id}")
                if int(run["worker_attempt_count"] or 0) < max_attempts:
                    raise RunAttemptsExhaustedError(
                        f"agent run has not exhausted its attempts: {run_id}"
                    )
                cur.execute(
                    """
                        UPDATE agent_runs
                        SET status = 'failed', failure_code = 'RunAttemptsExhausted',
                            failure_detail = %s, completed_at = now(), updated_at = now(),
                            worker_attempt_id = NULL,
                            worker_attempt_lease_expires_at = NULL
                        WHERE id = %s
                        RETURNING *
                    """,
                    (f"{command} exhausted {max_attempts} worker attempts", run_id),
                )
                failed = dict(cur.fetchone())
                _append_event_with_cursor(
                    cur,
                    run_id=run_id,
                    phase="failure",
                    status="failed",
                    summary="Agent run exhausted its worker attempts",
                    metadata={"attempts": max_attempts, "command": command},
                )
                cur.execute(
                    """
                        UPDATE memory_decisions
                        SET status = 'failed', sealed_at = COALESCE(sealed_at, now())
                        WHERE id = %s AND status = 'open'
                    """,
                    (failed["decision_id"],),
                )
                return _jsonable(failed)


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
                _append_dispatch_with_cursor(
                    cur,
                    run_id=run_id,
                    command="resume",
                    payload={"command": "resume", "run_id": str(run_id), "approved": approved},
                )
                return _jsonable(dict(row))


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


def _append_dispatch_with_cursor(
    cur: Any,
    *,
    run_id: str | UUID,
    command: str,
    payload: dict[str, Any],
    available_at: datetime | None = None,
) -> None:
    cur.execute(
        """
            INSERT INTO agent_run_dispatches (run_id, command, payload, available_at)
            VALUES (%s, %s, %s, COALESCE(%s, now()))
        """,
        (run_id, command, Jsonb(payload), available_at),
    )


def _run_field_assignments(
    *, status: str, fields: dict[str, Any] | None
) -> tuple[list[str], list[Any]]:
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
    return assignments, values


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
