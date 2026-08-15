"""Transactional run-dispatch outbox behavior."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
APPROVAL_RECOMMENDATION_ID = "recommendation:test"
APPROVAL_SELECTION_FINGERPRINT = "selection:test"
APPROVAL_METADATA = {
    "action_trace": {
        "recommendation": {"id": APPROVAL_RECOMMENDATION_ID},
        "selection": {"fingerprint": APPROVAL_SELECTION_FINGERPRINT},
    }
}


class RecordingSqs:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.attempted_messages: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict[str, str]:
        message = {"queue_url": QueueUrl, "body": json.loads(MessageBody)}
        self.attempted_messages.append(message)
        if self.fail:
            raise RuntimeError("SQS unavailable")
        self.messages.append(message)
        return {"MessageId": f"message-{len(self.messages)}"}


def _assert_delivery_envelope(
    body: object,
    *,
    run: dict[str, object],
    sequence: int,
    dispatch_id: str | None = None,
) -> dict[str, object]:
    assert isinstance(body, dict)
    assert body["run_id"] == run["id"]
    assert body["tenant_id"] == run["tenant_id"]
    assert UUID(str(body["dispatch_id"]))
    assert UUID(str(body["dispatch_attempt_id"]))
    assert body["dispatch_sequence"] == sequence
    assert type(body["dispatch_sequence"]) is int
    assert body["dispatch_sequence"] > 0
    if dispatch_id is not None:
        assert body["dispatch_id"] == dispatch_id
    return body


def _database_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


def test_complete_run_dispatch_retries_one_serialization_failure(monkeypatch):
    import hindsight.run_dispatch as run_dispatch

    completion = {
        "dispatch_id": "dispatch-1",
        "dispatch_attempt_id": "attempt-1",
        "lease_owner": "lease-1",
        "message_id": "message-1",
        "db_url": "postgresql://unused",
    }
    calls = []

    def complete_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise psycopg.errors.SerializationFailure("restart transaction")
        return True

    monkeypatch.setattr(run_dispatch, "_complete_run_dispatch_once", complete_once)

    assert run_dispatch._complete_run_dispatch(**completion) is True
    assert calls == [completion, completion]


def test_complete_run_dispatch_bounds_serialization_retries(monkeypatch):
    import hindsight.run_dispatch as run_dispatch

    calls = 0

    def complete_once(**_kwargs):
        nonlocal calls
        calls += 1
        raise psycopg.errors.SerializationFailure("restart transaction")

    monkeypatch.setattr(run_dispatch, "_complete_run_dispatch_once", complete_once)

    with pytest.raises(psycopg.errors.SerializationFailure, match="restart transaction"):
        run_dispatch._complete_run_dispatch(
            dispatch_id="dispatch-1",
            dispatch_attempt_id="attempt-1",
            lease_owner="lease-1",
            message_id="message-1",
            db_url="postgresql://unused",
        )
    assert calls == run_dispatch.RUN_DISPATCH_TRANSACTION_ATTEMPTS


def test_complete_run_dispatch_does_not_retry_non_serialization_error(monkeypatch):
    import hindsight.run_dispatch as run_dispatch

    calls = 0

    def complete_once(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("completion failed")

    monkeypatch.setattr(run_dispatch, "_complete_run_dispatch_once", complete_once)

    with pytest.raises(RuntimeError, match="completion failed"):
        run_dispatch._complete_run_dispatch(
            dispatch_id="dispatch-1",
            dispatch_attempt_id="attempt-1",
            lease_owner="lease-1",
            message_id="message-1",
            db_url="postgresql://unused",
        )
    assert calls == 1


@requires_db
@pytest.mark.migration_acceptance
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
        metadata=APPROVAL_METADATA,
    )
    prepared = prepare_approval(
        run_id=run["id"],
        approved=True,
        recommendation_id=APPROVAL_RECOMMENDATION_ID,
        selection_fingerprint=APPROVAL_SELECTION_FINGERPRINT,
    )

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
        (
            "start",
            {"command": "start", "command_generation": 0, "run_id": run["id"]},
            "pending",
        ),
        (
            "resume",
            {
                "approved": True,
                "command": "resume",
                "command_generation": 1,
                "recommendation_id": APPROVAL_RECOMMENDATION_ID,
                "run_id": run["id"],
                "selection_fingerprint": APPROVAL_SELECTION_FINGERPRINT,
            },
            "pending",
        ),
    ]
    assert events == [
        ("queue", "queued"),
        ("plan", "awaiting_approval"),
        ("approval", "resuming"),
    ]


@requires_db
def test_replanned_run_can_commit_a_second_approval_dispatch(monkeypatch):
    from hindsight.run_dispatch import dispatch_run_commands
    from hindsight.runs import (
        claim_run_attempt,
        create_run,
        finish_run_attempt,
        prepare_approval,
        transition_run,
    )

    suffix = uuid4().hex
    run, _ = create_run(
        incident_slug=f"dispatch-replan-{suffix}",
        namespace=f"dispatch-replan-{suffix}",
        user_input="checkout latency",
    )
    transition_run(
        run_id=run["id"],
        status="awaiting_approval",
        phase="plan",
        summary="First recommendation awaits approval",
        metadata=APPROVAL_METADATA,
    )
    prepare_approval(
        run_id=run["id"],
        approved=True,
        recommendation_id=APPROVAL_RECOMMENDATION_ID,
        selection_fingerprint=APPROVAL_SELECTION_FINGERPRINT,
    )

    claimed = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=1,
        lease_ttl=timedelta(minutes=1),
        max_attempts=3,
    )
    assert claimed.outcome == "claimed"
    assert claimed.attempt_id is not None

    next_recommendation_id = "recommendation:replanned"
    next_selection_fingerprint = "selection:replanned"
    finish_run_attempt(
        run_id=run["id"],
        attempt_id=claimed.attempt_id,
        command="resume",
        status="awaiting_approval",
        phase="plan",
        summary="Changed memory requires a new approval",
        metadata={
            "action_trace": {
                "recommendation": {"id": next_recommendation_id},
                "selection": {"fingerprint": next_selection_fingerprint},
            }
        },
    )
    prepared = prepare_approval(
        run_id=run["id"],
        approved=True,
        recommendation_id=next_recommendation_id,
        selection_fingerprint=next_selection_fingerprint,
    )

    assert prepared["status"] == "resuming"
    assert prepared["command_generation"] == 2
    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    client = RecordingSqs()
    dispatched = dispatch_run_commands(
        run_id=run["id"],
        command="resume",
        limit=1,
        client=client,
    )
    assert dispatched["dispatched"] == 1
    assert client.messages[0]["body"]["command_generation"] == 2
    stale = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=1,
        lease_ttl=timedelta(minutes=1),
        max_attempts=3,
    )
    current = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=2,
        lease_ttl=timedelta(minutes=1),
        max_attempts=3,
    )
    assert stale.outcome == "duplicate"
    assert current.outcome == "claimed"
    assert current.run["worker_attempt_count"] == 1
    assert current.run["worker_attempt_generation"] == 2
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        resume_payloads = conn.execute(
            """
                SELECT payload
                FROM agent_run_dispatches
                WHERE run_id = %s AND command = 'resume'
                ORDER BY created_at, id
            """,
            (run["id"],),
        ).fetchall()

    assert resume_payloads == [
        (
            {
                "approved": True,
                "command": "resume",
                "command_generation": 1,
                "recommendation_id": APPROVAL_RECOMMENDATION_ID,
                "run_id": run["id"],
                "selection_fingerprint": APPROVAL_SELECTION_FINGERPRINT,
            },
        ),
        (
            {
                "approved": True,
                "command": "resume",
                "command_generation": 2,
                "recommendation_id": next_recommendation_id,
                "run_id": run["id"],
                "selection_fingerprint": next_selection_fingerprint,
            },
        ),
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
        metadata=APPROVAL_METADATA,
    )
    monkeypatch.setattr(
        runs,
        "_append_dispatch_with_cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outbox insert failed")),
    )

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        runs.prepare_approval(
            run_id=run["id"],
            approved=True,
            recommendation_id=APPROVAL_RECOMMENDATION_ID,
            selection_fingerprint=APPROVAL_SELECTION_FINGERPRINT,
        )

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
def test_enqueue_body_contains_persisted_delivery_identity(monkeypatch):
    from hindsight.run_dispatch import dispatch_run_commands
    from hindsight.runs import create_run

    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    run, _ = create_run(
        incident_slug=f"dispatch-envelope-{uuid4().hex}",
        namespace="dispatch-envelope",
        user_input="checkout latency",
    )
    client = RecordingSqs()

    result = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )

    assert result == {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
    assert len(client.attempted_messages) == 1
    body = _assert_delivery_envelope(
        client.attempted_messages[0]["body"],
        run=run,
        sequence=1,
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        persisted = conn.execute(
            """
                SELECT
                    dispatch.id,
                    dispatch.tenant_id,
                    dispatch.status,
                    attempt.id,
                    attempt.sequence,
                    attempt.transport_message_id,
                    attempt.sent_at IS NOT NULL
                FROM agent_run_dispatches AS dispatch
                JOIN agent_run_dispatch_attempts AS attempt
                    ON attempt.tenant_id = dispatch.tenant_id
                    AND attempt.dispatch_id = dispatch.id
                WHERE dispatch.run_id = %s AND dispatch.command = 'start'
            """,
            (run["id"],),
        ).fetchone()

    assert persisted == (
        UUID(str(body["dispatch_id"])),
        UUID(str(body["tenant_id"])),
        "sent",
        UUID(str(body["dispatch_attempt_id"])),
        1,
        "message-1",
        True,
    )


@requires_db
def test_sent_unacknowledged_dispatch_retries_with_a_new_attempt(monkeypatch):
    from hindsight.run_dispatch import dispatch_run_commands
    from hindsight.runs import create_run

    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    run, _ = create_run(
        incident_slug=f"dispatch-unacknowledged-{uuid4().hex}",
        namespace="dispatch-unacknowledged",
        user_input="checkout latency",
    )
    client = RecordingSqs()

    first = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )
    not_due = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )
    first_body = _assert_delivery_envelope(
        client.messages[0]["body"],
        run=run,
        sequence=1,
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """
                UPDATE agent_run_dispatches
                SET available_at = now() - INTERVAL '1 second'
                WHERE id = %s
            """,
            (first_body["dispatch_id"],),
        )
        conn.commit()

    retried = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )
    second_body = _assert_delivery_envelope(
        client.messages[1]["body"],
        run=run,
        sequence=2,
        dispatch_id=str(first_body["dispatch_id"]),
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        persisted = conn.execute(
            """
                SELECT status, attempt_count, transport_message_id
                FROM agent_run_dispatches
                WHERE id = %s
            """,
            (first_body["dispatch_id"],),
        ).fetchone()
        attempts = conn.execute(
            """
                SELECT id, sequence, transport_message_id, sent_at IS NOT NULL
                FROM agent_run_dispatch_attempts
                WHERE dispatch_id = %s
                ORDER BY sequence
            """,
            (first_body["dispatch_id"],),
        ).fetchall()

    assert first == {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
    assert not_due == {"leased": 0, "dispatched": 0, "failed": 0, "lease_lost": 0}
    assert retried == {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
    assert second_body["dispatch_attempt_id"] != first_body["dispatch_attempt_id"]
    assert persisted == ("sent", 2, "message-2")
    assert attempts == [
        (UUID(str(first_body["dispatch_attempt_id"])), 1, "message-1", True),
        (UUID(str(second_body["dispatch_attempt_id"])), 2, "message-2", True),
    ]


@requires_db
def test_acknowledged_dispatch_is_never_resent(monkeypatch):
    from hindsight.run_dispatch import dispatch_run_commands
    from hindsight.runs import claim_run_attempt, create_run

    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    run, _ = create_run(
        incident_slug=f"dispatch-acknowledged-{uuid4().hex}",
        namespace="dispatch-acknowledged",
        user_input="checkout latency",
    )
    client = RecordingSqs()
    sent = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )
    body = _assert_delivery_envelope(client.messages[0]["body"], run=run, sequence=1)

    claimed = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=timedelta(minutes=5),
        max_attempts=3,
        dispatch_id=str(body["dispatch_id"]),
        dispatch_attempt_id=str(body["dispatch_attempt_id"]),
        dispatch_sequence=1,
        worker_message_id="worker-message-1",
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        acknowledged = conn.execute(
            """
                SELECT
                    dispatch.status,
                    dispatch.acknowledged_attempt_id,
                    dispatch.acknowledged_at IS NOT NULL,
                    attempt.worker_message_id,
                    attempt.acknowledged_at IS NOT NULL
                FROM agent_run_dispatches AS dispatch
                JOIN agent_run_dispatch_attempts AS attempt
                    ON attempt.id = dispatch.acknowledged_attempt_id
                WHERE dispatch.id = %s
            """,
            (body["dispatch_id"],),
        ).fetchone()
        conn.execute(
            """
                UPDATE agent_runs
                SET status = 'queued', worker_attempt_id = NULL,
                    worker_attempt_lease_expires_at = NULL
                WHERE id = %s
            """,
            (run["id"],),
        )
        conn.execute(
            """
                UPDATE agent_run_dispatches
                SET available_at = now() - INTERVAL '1 second'
                WHERE id = %s
            """,
            (body["dispatch_id"],),
        )
        conn.commit()

    swept = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )

    assert sent == {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
    assert claimed.outcome == "claimed"
    assert acknowledged == (
        "acknowledged",
        UUID(str(body["dispatch_attempt_id"])),
        True,
        "worker-message-1",
        True,
    )
    assert swept == {"leased": 0, "dispatched": 0, "failed": 0, "lease_lost": 0}
    assert len(client.attempted_messages) == 1


@requires_db
def test_pending_dispatch_recovers_expired_attempt_but_not_live_lease(monkeypatch):
    from hindsight.run_dispatch import dispatch_run_commands
    from hindsight.runs import claim_run_attempt, create_run, get_run

    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    run, _ = create_run(
        incident_slug=f"dispatch-expired-attempt-{uuid4().hex}",
        namespace="dispatch-expired-attempt",
        user_input="checkout latency",
    )
    first = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=timedelta(minutes=5),
        max_attempts=3,
    )
    client = RecordingSqs()

    live = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """
                UPDATE agent_runs
                SET worker_attempt_lease_expires_at = now() - INTERVAL '1 second'
                WHERE id = %s
            """,
            (run["id"],),
        )
        conn.commit()

    expired = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )
    body = _assert_delivery_envelope(client.messages[0]["body"], run=run, sequence=1)
    recovered = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=timedelta(minutes=5),
        max_attempts=3,
        dispatch_id=str(body["dispatch_id"]),
        dispatch_attempt_id=str(body["dispatch_attempt_id"]),
        dispatch_sequence=1,
        worker_message_id="worker-message-1",
    )
    persisted = get_run(run_id=run["id"])

    assert first.outcome == "claimed"
    assert live == {"leased": 0, "dispatched": 0, "failed": 0, "lease_lost": 0}
    assert expired == {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
    assert recovered.outcome == "claimed"
    assert persisted["worker_attempt_count"] == 2
    assert any(event["phase"] == "recovery" for event in persisted["events"])


@requires_db
def test_queue_failure_leaves_unsent_attempt_and_retry_advances_sequence(monkeypatch):
    from hindsight.run_dispatch import dispatch_run_commands
    from hindsight.runs import create_run

    monkeypatch.setenv("HINDSIGHT_RUN_QUEUE_URL", "https://sqs.example/run-queue")
    run, _ = create_run(
        incident_slug=f"dispatch-{uuid4().hex}",
        namespace="dispatch-retry",
        user_input="checkout latency",
    )

    failing_client = RecordingSqs(fail=True)
    failed = dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=failing_client,
    )
    failed_body = _assert_delivery_envelope(
        failing_client.attempted_messages[0]["body"],
        run=run,
        sequence=1,
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
    retried_body = _assert_delivery_envelope(
        succeeding_client.attempted_messages[0]["body"],
        run=run,
        sequence=2,
        dispatch_id=str(failed_body["dispatch_id"]),
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
        attempts = conn.execute(
            """
                SELECT id, sequence, transport_message_id, sent_at IS NOT NULL
                FROM agent_run_dispatch_attempts
                WHERE dispatch_id = %s
                ORDER BY sequence
            """,
            (failed_body["dispatch_id"],),
        ).fetchall()

    assert failed == {"leased": 1, "dispatched": 0, "failed": 1, "lease_lost": 0}
    assert pending[:5] == ("pending", 1, None, None, None)
    assert pending[5]
    assert retried["dispatched"] == 1
    assert succeeding_client.messages
    assert retried_body["dispatch_attempt_id"] != failed_body["dispatch_attempt_id"]
    assert persisted[0:3] == ("sent", 2, "message-1")
    assert persisted[3] is None
    assert attempts == [
        (UUID(str(failed_body["dispatch_attempt_id"])), 1, None, False),
        (UUID(str(retried_body["dispatch_attempt_id"])), 2, "message-1", True),
    ]


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
    first_payload = {
        **dict(leased[0]["payload"]),
        "tenant_id": str(leased[0]["tenant_id"]),
        "dispatch_id": str(leased[0]["id"]),
        "dispatch_attempt_id": str(leased[0]["dispatch_attempt_id"]),
        "dispatch_sequence": int(leased[0]["dispatch_sequence"]),
    }
    run_dispatch.enqueue_run(first_payload, client=client)
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

    swept = run_dispatch.dispatch_run_commands(
        run_id=run["id"],
        command="start",
        limit=1,
        client=client,
    )
    first_body = _assert_delivery_envelope(client.messages[0]["body"], run=run, sequence=1)
    second_body = _assert_delivery_envelope(
        client.messages[1]["body"],
        run=run,
        sequence=2,
        dispatch_id=str(first_body["dispatch_id"]),
    )
    first_claim = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=timedelta(minutes=5),
        max_attempts=3,
        dispatch_id=str(second_body["dispatch_id"]),
        dispatch_attempt_id=str(second_body["dispatch_attempt_id"]),
        dispatch_sequence=2,
        worker_message_id="worker-message-2",
    )
    duplicate_claim = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=timedelta(minutes=5),
        max_attempts=3,
    )

    assert swept == {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
    assert first_body["dispatch_attempt_id"] != second_body["dispatch_attempt_id"]
    assert first_claim.outcome == "claimed"
    assert duplicate_claim.outcome == "busy"
    assert [event["status"] for event in get_run(run_id=run["id"])["events"]].count("triaging") == 1
