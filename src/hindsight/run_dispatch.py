"""Durable database outbox dispatch for asynchronous agent-run commands."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from hindsight.db import connect
from hindsight.observability import structured_event
from hindsight.queueing import enqueue_run
from hindsight.security import safe_error_detail

RUN_DISPATCH_LEASE_TTL = timedelta(seconds=30)
RUN_DISPATCH_ACK_TTL = timedelta(minutes=5)
RUN_DISPATCH_BATCH_LIMIT = 25
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


def dispatch_run_commands(
    *,
    db_url: str | None = None,
    run_id: str | UUID | None = None,
    command: str | None = None,
    limit: int = RUN_DISPATCH_BATCH_LIMIT,
    client: Any | None = None,
) -> dict[str, int]:
    """Lease and deliver durable run commands without holding a database transaction."""

    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if command is not None and command not in {"start", "resume"}:
        raise ValueError(f"unsupported run dispatch command: {command}")

    dispatches = _lease_run_dispatches(
        db_url=db_url,
        run_id=run_id,
        command=command,
        limit=limit,
    )
    dispatched = 0
    failed = 0
    lease_lost = 0
    for dispatch in dispatches:
        try:
            payload = {
                **dict(dispatch["payload"]),
                "tenant_id": str(dispatch["tenant_id"]),
                "dispatch_id": str(dispatch["id"]),
                "dispatch_attempt_id": str(dispatch["dispatch_attempt_id"]),
                "dispatch_sequence": int(dispatch["dispatch_sequence"]),
            }
            message_id = enqueue_run(payload, client=client)
        except Exception as exc:
            _release_run_dispatch(
                dispatch_id=dispatch["id"],
                dispatch_attempt_id=dispatch["dispatch_attempt_id"],
                lease_owner=dispatch["lease_owner"],
                error_detail=safe_error_detail(exc, max_chars=1000),
                db_url=db_url,
            )
            failed += 1
            continue
        if _complete_run_dispatch(
            dispatch_id=dispatch["id"],
            dispatch_attempt_id=dispatch["dispatch_attempt_id"],
            lease_owner=dispatch["lease_owner"],
            message_id=message_id,
            db_url=db_url,
        ):
            LOGGER.info(
                structured_event(
                    "run_dispatch",
                    {
                        **payload,
                        "status": "sent",
                        "message_id": message_id,
                    },
                )
            )
            dispatched += 1
        else:
            lease_lost += 1
    return {
        "leased": len(dispatches),
        "dispatched": dispatched,
        "failed": failed,
        "lease_lost": lease_lost,
    }


def _lease_run_dispatches(
    *,
    db_url: str | None,
    run_id: str | UUID | None,
    command: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    values: list[Any] = []
    if run_id is not None:
        filters.append("run_id = %s")
        values.append(run_id)
    if command is not None:
        filters.append("command = %s")
        values.append(command)
    filter_sql = f" AND {' AND '.join(filters)}" if filters else ""
    values.append(limit)

    with connect(db_url, application_name="hindsight-run-dispatcher") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                        SELECT dispatch.*
                        FROM agent_run_dispatches AS dispatch
                        WHERE (
                            (dispatch.status = 'pending' AND dispatch.available_at <= now())
                            OR (
                                dispatch.status = 'leased'
                                AND dispatch.lease_expires_at <= now()
                            )
                            OR (dispatch.status = 'sent' AND dispatch.available_at <= now())
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM agent_runs AS run
                            WHERE run.id = dispatch.run_id
                                AND run.command_generation = dispatch.command_generation
                                AND (
                                    (dispatch.command = 'start' AND run.status = 'queued')
                                    OR (
                                        dispatch.command = 'resume'
                                        AND run.status = 'resuming'
                                    )
                                )
                        )
                        {filter_sql}
                        ORDER BY available_at, created_at, id
                        LIMIT %s
                        FOR UPDATE
                    """,
                    tuple(values),
                )
                rows = cur.fetchall()
                leased: list[dict[str, Any]] = []
                for row in rows:
                    lease_owner = uuid4()
                    dispatch_attempt_id = uuid4()
                    dispatch_sequence = int(row["attempt_count"] or 0) + 1
                    cur.execute(
                        """
                            UPDATE agent_run_dispatches
                            SET status = 'leased', lease_owner = %s,
                                lease_expires_at = now() + %s,
                                attempt_count = attempt_count + 1,
                                transport_message_id = NULL,
                                dispatched_at = NULL,
                                acknowledged_attempt_id = NULL,
                                acknowledged_at = NULL,
                                updated_at = now()
                            WHERE id = %s
                            RETURNING *
                        """,
                        (lease_owner, RUN_DISPATCH_LEASE_TTL, row["id"]),
                    )
                    dispatch = dict(cur.fetchone())
                    cur.execute(
                        """
                            INSERT INTO agent_run_dispatch_attempts (
                                id, dispatch_id, sequence, lease_owner, lease_expires_at
                            )
                            VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            dispatch_attempt_id,
                            dispatch["id"],
                            dispatch_sequence,
                            lease_owner,
                            dispatch["lease_expires_at"],
                        ),
                    )
                    dispatch["dispatch_attempt_id"] = dispatch_attempt_id
                    dispatch["dispatch_sequence"] = dispatch_sequence
                    leased.append(dispatch)
                return leased


def _complete_run_dispatch(
    *,
    dispatch_id: str | UUID,
    dispatch_attempt_id: str | UUID,
    lease_owner: str | UUID,
    message_id: str,
    db_url: str | None,
) -> bool:
    with connect(db_url, application_name="hindsight-run-dispatcher") as conn:
        with conn.transaction():
            attempt = conn.execute(
                """
                    UPDATE agent_run_dispatch_attempts
                    SET transport_message_id = COALESCE(transport_message_id, %s),
                        sent_at = COALESCE(sent_at, now()),
                        updated_at = now()
                    WHERE id = %s AND dispatch_id = %s AND lease_owner = %s
                        AND (
                            transport_message_id IS NULL
                            OR transport_message_id = %s
                        )
                    RETURNING id
                """,
                (
                    message_id,
                    dispatch_attempt_id,
                    dispatch_id,
                    lease_owner,
                    message_id,
                ),
            ).fetchone()
            if attempt is None:
                return False
            row = conn.execute(
                """
                    UPDATE agent_run_dispatches
                    SET status = CASE
                            WHEN status = 'acknowledged' THEN 'acknowledged'
                            ELSE 'sent'
                        END,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        transport_message_id = %s,
                        dispatched_at = now(),
                        available_at = now() + %s,
                        updated_at = now(),
                        last_error = NULL
                    WHERE id = %s
                        AND (
                            (status = 'leased' AND lease_owner = %s)
                            OR (
                                status = 'acknowledged'
                                AND acknowledged_attempt_id = %s
                            )
                        )
                    RETURNING id
                """,
                (
                    message_id,
                    RUN_DISPATCH_ACK_TTL,
                    dispatch_id,
                    lease_owner,
                    dispatch_attempt_id,
                ),
            ).fetchone()
            return row is not None


def _release_run_dispatch(
    *,
    dispatch_id: str | UUID,
    dispatch_attempt_id: str | UUID,
    lease_owner: str | UUID,
    error_detail: str,
    db_url: str | None,
) -> bool:
    with connect(db_url, application_name="hindsight-run-dispatcher") as conn:
        with conn.transaction():
            row = conn.execute(
                """
                    UPDATE agent_run_dispatches
                    SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,
                        transport_message_id = NULL, dispatched_at = NULL,
                        acknowledged_attempt_id = NULL, acknowledged_at = NULL,
                        available_at = now(), last_error = %s, updated_at = now()
                    WHERE id = %s AND status = 'leased' AND lease_owner = %s
                        AND EXISTS (
                            SELECT 1
                            FROM agent_run_dispatch_attempts AS attempt
                            WHERE attempt.id = %s
                                AND attempt.dispatch_id = agent_run_dispatches.id
                                AND attempt.lease_owner = %s
                        )
                    RETURNING id
                """,
                (
                    error_detail,
                    dispatch_id,
                    lease_owner,
                    dispatch_attempt_id,
                    lease_owner,
                ),
            ).fetchone()
            return row is not None
