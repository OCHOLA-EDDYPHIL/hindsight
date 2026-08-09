"""Durable database outbox dispatch for asynchronous agent-run commands."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from hindsight.db import connect
from hindsight.queueing import enqueue_run
from hindsight.security import safe_error_detail

RUN_DISPATCH_LEASE_TTL = timedelta(seconds=30)
RUN_DISPATCH_BATCH_LIMIT = 25


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
            }
            message_id = enqueue_run(payload, client=client)
        except Exception as exc:
            _release_run_dispatch(
                dispatch_id=dispatch["id"],
                lease_owner=dispatch["lease_owner"],
                error_detail=safe_error_detail(exc, max_chars=1000),
                db_url=db_url,
            )
            failed += 1
            continue
        if _complete_run_dispatch(
            dispatch_id=dispatch["id"],
            lease_owner=dispatch["lease_owner"],
            message_id=message_id,
            db_url=db_url,
        ):
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

    lease_owner = uuid4()
    with connect(db_url, application_name="hindsight-run-dispatcher") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                        SELECT *
                        FROM agent_run_dispatches
                        WHERE (
                            (status = 'pending' AND available_at <= now())
                            OR (status = 'leased' AND lease_expires_at <= now())
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM agent_runs AS run
                            WHERE run.id = agent_run_dispatches.run_id
                                AND run.command_generation =
                                    agent_run_dispatches.command_generation
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
                    cur.execute(
                        """
                            UPDATE agent_run_dispatches
                            SET status = 'leased', lease_owner = %s,
                                lease_expires_at = now() + %s,
                                attempt_count = attempt_count + 1,
                                updated_at = now()
                            WHERE id = %s
                            RETURNING *
                        """,
                        (lease_owner, RUN_DISPATCH_LEASE_TTL, row["id"]),
                    )
                    leased.append(dict(cur.fetchone()))
                return leased


def _complete_run_dispatch(
    *,
    dispatch_id: str | UUID,
    lease_owner: str | UUID,
    message_id: str,
    db_url: str | None,
) -> bool:
    with connect(db_url, application_name="hindsight-run-dispatcher") as conn:
        with conn.transaction():
            row = conn.execute(
                """
                    UPDATE agent_run_dispatches
                    SET status = 'sent', lease_owner = NULL, lease_expires_at = NULL,
                        transport_message_id = %s, dispatched_at = now(), updated_at = now(),
                        last_error = NULL
                    WHERE id = %s AND status = 'leased' AND lease_owner = %s
                        AND lease_expires_at > now()
                    RETURNING id
                """,
                (message_id, dispatch_id, lease_owner),
            ).fetchone()
            return row is not None


def _release_run_dispatch(
    *,
    dispatch_id: str | UUID,
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
                        available_at = now(), last_error = %s, updated_at = now()
                    WHERE id = %s AND status = 'leased' AND lease_owner = %s
                    RETURNING id
                """,
                (error_detail, dispatch_id, lease_owner),
            ).fetchone()
            return row is not None
