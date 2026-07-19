"""Transactional run-dispatch outbox behavior."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


class RecordingSqs:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[dict[str, object]] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("SQS unavailable")
        self.messages.append({"queue_url": QueueUrl, "body": json.loads(MessageBody)})
        return {"MessageId": f"message-{len(self.messages)}"}


def _database_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


@requires_db
def test_outbox_migration_backfills_queued_and_resuming_runs():
    database_name = f"hindsight_dispatch_upgrade_{uuid4().hex}"
    target_url = _database_url(database_name)
    with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        legacy_migrations = [
            path
            for path in sorted(MIGRATIONS.glob("[0-9]*.sql"))
            if path.name <= "0007_agent_runs.sql"
        ]
        queued_id = uuid4()
        resuming_id = uuid4()
        with psycopg.connect(target_url, autocommit=True) as conn:
            for path in legacy_migrations:
                with conn.transaction():
                    conn.execute(path.read_text())
            conn.execute(
                """
                    INSERT INTO agent_runs (
                        id, thread_id, incident_slug, namespace, user_input,
                        status, decision_id, action_approved
                    ) VALUES
                        (%s, 'queued-thread', 'queued-incident', 'dispatch-upgrade',
                         'queued input', 'queued', 'queued-decision', NULL),
                        (%s, 'resume-thread', 'resume-incident', 'dispatch-upgrade',
                         'resume input', 'resuming', 'resume-decision', false)
                """,
                (queued_id, resuming_id),
            )
            conn.execute((MIGRATIONS / "0017_agent_run_dispatch_outbox.sql").read_text())
            dispatches = conn.execute(
                """
                    SELECT run_id, command, payload, status
                    FROM agent_run_dispatches
                    ORDER BY command
                """
            ).fetchall()

        assert dispatches == [
            (
                resuming_id,
                "resume",
                {"approved": False, "command": "resume", "run_id": str(resuming_id)},
                "pending",
            ),
            (
                queued_id,
                "start",
                {"command": "start", "run_id": str(queued_id)},
                "pending",
            ),
        ]
    finally:
        with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} CASCADE").format(sql.Identifier(database_name)))


@requires_db
def test_run_and_approval_transitions_commit_their_dispatches():
    from hindsight.runs import create_run, prepare_approval, transition_run

    suffix = uuid4().hex
    run, created = create_run(
        incident_slug=f"dispatch-{suffix}",
        namespace=f"dispatch-{suffix}",
        user_input="checkout latency",
        idempotency_key=f"dispatch-request-{suffix}",
    )
    transition_run(
        run_id=run["id"],
        status="awaiting_approval",
        phase="plan",
        summary="Plan awaits approval",
    )
    prepared = prepare_approval(run_id=run["id"], approved=True)

    assert created is True
    assert prepared["status"] == "resuming"
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        dispatches = conn.execute(
            """
                SELECT command, payload, status
                FROM agent_run_dispatches
                WHERE run_id = %s
                ORDER BY created_at, command
            """,
            (run["id"],),
        ).fetchall()
        events = conn.execute(
            "SELECT phase, status FROM agent_run_events WHERE run_id = %s ORDER BY sequence",
            (run["id"],),
        ).fetchall()

    assert dispatches == [
        ("start", {"command": "start", "run_id": run["id"]}, "pending"),
        (
            "resume",
            {"approved": True, "command": "resume", "run_id": run["id"]},
            "pending",
        ),
    ]
    assert events == [
        ("queue", "queued"),
        ("plan", "awaiting_approval"),
        ("approval", "resuming"),
    ]


@requires_db
def test_run_creation_can_atomically_delay_dispatch_availability():
    from hindsight.runs import create_run

    available_at = datetime.now(UTC) + timedelta(minutes=5)
    run, _ = create_run(
        incident_slug=f"dispatch-delayed-{uuid4().hex}",
        namespace=f"dispatch-delayed-{uuid4().hex}",
        user_input="validate an atomic future dispatch",
        dispatch_available_at=available_at,
    )

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        persisted = conn.execute(
            "SELECT available_at FROM agent_run_dispatches WHERE run_id = %s",
            (run["id"],),
        ).fetchone()[0]

    assert persisted == available_at


@requires_db
def test_run_creation_rolls_back_when_dispatch_cannot_be_persisted(monkeypatch):
    from hindsight import runs

    idempotency_key = f"dispatch-rollback-{uuid4().hex}"
    monkeypatch.setattr(
        runs,
        "_append_dispatch_with_cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outbox insert failed")),
    )

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        runs.create_run(
            incident_slug=f"dispatch-{uuid4().hex}",
            namespace="dispatch-rollback",
            user_input="checkout latency",
            idempotency_key=idempotency_key,
        )

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_runs WHERE idempotency_key = %s",
            (idempotency_key,),
        ).fetchone() == (0,)


@requires_db
def test_approval_rolls_back_when_resume_dispatch_cannot_be_persisted(monkeypatch):
    from hindsight import runs

    run, _ = runs.create_run(
        incident_slug=f"dispatch-{uuid4().hex}",
        namespace="dispatch-approval-rollback",
        user_input="checkout latency",
    )
    runs.transition_run(
        run_id=run["id"],
        status="awaiting_approval",
        phase="plan",
        summary="Plan awaits approval",
    )
    monkeypatch.setattr(
        runs,
        "_append_dispatch_with_cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outbox insert failed")),
    )

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        runs.prepare_approval(run_id=run["id"], approved=True)

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        persisted = conn.execute(
            "SELECT status, action_approved FROM agent_runs WHERE id = %s",
            (run["id"],),
        ).fetchone()
        approval_events = conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id = %s AND phase = 'approval'",
            (run["id"],),
        ).fetchone()
        resume_dispatches = conn.execute(
            """
                SELECT count(*) FROM agent_run_dispatches
                WHERE run_id = %s AND command = 'resume'
            """,
            (run["id"],),
        ).fetchone()

    assert persisted == ("awaiting_approval", None)
    assert approval_events == (0,)
    assert resume_dispatches == (0,)


@requires_db
def test_queue_failure_leaves_pending_command_for_a_later_sweep(monkeypatch):
    from hindsight.run_dispatch import dispatch_run_commands
    from hindsight.runs import create_run

    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    run, _ = create_run(
        incident_slug=f"dispatch-{uuid4().hex}",
        namespace="dispatch-retry",
        user_input="checkout latency",
    )

    failed = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=RecordingSqs(fail=True),
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        pending = conn.execute(
            """
                SELECT status, attempt_count, lease_owner, lease_expires_at,
                       transport_message_id, last_error
                FROM agent_run_dispatches
                WHERE run_id = %s AND command = 'start'
            """,
            (run["id"],),
        ).fetchone()
    succeeding_client = RecordingSqs()
    retried = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=succeeding_client,
    )

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        persisted = conn.execute(
            """
                SELECT status, attempt_count, transport_message_id, last_error
                FROM agent_run_dispatches
                WHERE run_id = %s AND command = 'start'
            """,
            (run["id"],),
        ).fetchone()

    assert failed == {"leased": 1, "dispatched": 0, "failed": 1, "lease_lost": 0}
    assert pending[:5] == ("pending", 1, None, None, None)
    assert pending[5]
    assert retried["dispatched"] == 1
    assert succeeding_client.messages
    assert persisted[0:3] == ("sent", 2, "message-1")
    assert persisted[3] is None


@requires_db
def test_expired_dispatch_lease_is_reclaimed_and_duplicate_delivery_is_phase_safe(monkeypatch):
    import hindsight.run_dispatch as run_dispatch
    from hindsight.runs import claim_run_attempt, create_run, get_run

    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    run, _ = create_run(
        incident_slug=f"dispatch-{uuid4().hex}",
        namespace="dispatch-expired-lease",
        user_input="checkout latency",
    )
    client = RecordingSqs()
    leased = run_dispatch._lease_run_dispatches(
        db_url=None,
        run_id=run["id"],
        command="start",
        limit=1,
    )
    run_dispatch.enqueue_run(dict(leased[0]["payload"]), client=client)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """
                UPDATE agent_run_dispatches
                SET lease_expires_at = now() - INTERVAL '1 second'
                WHERE id = %s
            """,
            (leased[0]["id"],),
        )
        conn.commit()

    swept = run_dispatch.dispatch_run_commands(limit=100, client=client)
    first_claim = claim_run_attempt(
        run_id=run["id"],
        command="start",
        lease_ttl=timedelta(minutes=5),
        max_attempts=3,
    )
    duplicate_claim = claim_run_attempt(
        run_id=run["id"],
        command="start",
        lease_ttl=timedelta(minutes=5),
        max_attempts=3,
    )

    assert swept["dispatched"] >= 1
    assert [message["body"] for message in client.messages].count(
        {
            **dict(leased[0]["payload"]),
            "tenant_id": str(leased[0]["tenant_id"]),
        }
    ) == 2
    assert first_claim.outcome == "claimed"
    assert duplicate_claim.outcome == "busy"
    assert [
        event["status"] for event in get_run(run_id=run["id"])["events"]
    ].count("triaging") == 1
