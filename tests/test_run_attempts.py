"""Durable fencing and recovery for asynchronous agent-run attempts."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
ROOT = Path(__file__).resolve().parents[1]
LEASE_TTL = timedelta(minutes=5)
APPROVAL_RECOMMENDATION_ID = "recommendation:test"
APPROVAL_SELECTION_FINGERPRINT = "selection:test"
APPROVAL_METADATA = {
    "action_trace": {
        "recommendation": {"id": APPROVAL_RECOMMENDATION_ID},
        "selection": {"fingerprint": APPROVAL_SELECTION_FINGERPRINT},
    }
}
ACTION_ID = "remediation_action:test"
ACTION_OBSERVATION_FINGERPRINT = "observation:test"
ACTION_PREVIEW_ID = "preview:test"
ACTION_PREVIEW_FINGERPRINT = "preview-fingerprint:test"
ACTION_APPROVAL_ACTOR = "product:operator:test"
ACTION_APPROVAL_METADATA = {
    "action_trace": {
        "mode": "governed_memory_remediation",
        "selection": {"fingerprint": APPROVAL_SELECTION_FINGERPRINT},
        "observation_fingerprint": ACTION_OBSERVATION_FINGERPRINT,
        "remediation_action": {"id": ACTION_ID},
        "preview": {
            "id": ACTION_PREVIEW_ID,
            "fingerprint": ACTION_PREVIEW_FINGERPRINT,
            "effect_count": 2,
            "effects": {
                "close_memory_ids": ["memory:unsafe"],
                "review_resolutions": [
                    {
                        "id": "review:unsafe",
                        "semantic_memory_id": "memory:unsafe",
                        "status": "superseded",
                    }
                ],
            },
        },
    }
}


@pytest.fixture
def agent_writer_database_url() -> str:
    database_url = os.environ["DATABASE_URL"]
    login_role = f"hindsight_agent_writer_test_{uuid4().hex}"
    password = f"AgentWriter-{uuid4().hex}"
    sslmode = parse_qs(urlsplit(database_url).query).get("sslmode", [None])[0]
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute((ROOT / "infra/db/roles.sql").read_text())
        if sslmode == "disable":
            admin.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN NOBYPASSRLS").format(sql.Identifier(login_role))
            )
            restricted_url = make_conninfo(database_url, user=login_role)
        else:
            admin.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s NOBYPASSRLS").format(
                    sql.Identifier(login_role)
                ),
                (password,),
            )
            restricted_url = make_conninfo(
                database_url,
                user=login_role,
                password=password,
            )
        admin.execute(
            sql.SQL("GRANT hindsight_agent_writer TO {}").format(sql.Identifier(login_role))
        )
    with psycopg.connect(restricted_url) as restricted:
        assert restricted.execute(
            "SELECT current_user, pg_has_role(current_user, 'hindsight_agent_writer', 'member')"
        ).fetchone() == (login_role, True)
    try:
        yield restricted_url
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login_role)))


def test_delivery_identity_validation_fails_before_database_access():
    from hindsight.runs import claim_run_attempt

    common = {
        "run_id": str(uuid4()),
        "command": "start",
        "command_generation": 0,
        "lease_ttl": LEASE_TTL,
        "max_attempts": 3,
    }
    with pytest.raises(ValueError, match="complete dispatch delivery identity"):
        claim_run_attempt(**common, dispatch_id=uuid4())
    with pytest.raises(ValueError, match="positive integer"):
        claim_run_attempt(
            **common,
            dispatch_id=uuid4(),
            dispatch_attempt_id=uuid4(),
            dispatch_sequence=0,
            worker_message_id="message-1",
        )
    with pytest.raises(ValueError, match="must not be blank"):
        claim_run_attempt(
            **common,
            dispatch_id=uuid4(),
            dispatch_attempt_id=uuid4(),
            dispatch_sequence=1,
            worker_message_id=" ",
        )


def _expire_attempt(run_id: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """
                UPDATE agent_runs
                SET worker_attempt_lease_expires_at = now() - INTERVAL '1 second'
                WHERE id = %s
            """,
            (run_id,),
        )


@requires_db
def test_expired_attempt_is_reclaimed_and_stale_token_is_fenced():
    from hindsight.runs import (
        RunAttemptLeaseLostError,
        claim_run_attempt,
        create_run,
        get_run,
        transition_run_attempt,
    )

    run, _ = create_run(
        incident_slug=f"attempt-{uuid4().hex}",
        namespace="attempt-fencing",
        user_input="checkout latency",
    )
    first = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    busy = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    _expire_attempt(run["id"])
    replacement = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )

    assert first.outcome == "claimed"
    assert busy.outcome == "busy"
    assert replacement.outcome == "claimed"
    assert replacement.attempt_id != first.attempt_id
    assert replacement.run["worker_attempt_count"] == 2
    with pytest.raises(RunAttemptLeaseLostError):
        transition_run_attempt(
            run_id=run["id"],
            attempt_id=first.attempt_id,
            command="start",
            status="recalling",
            phase="recall",
            summary="stale recall",
        )
    transitioned = transition_run_attempt(
        run_id=run["id"],
        attempt_id=replacement.attempt_id,
        command="start",
        status="recalling",
        phase="recall",
        summary="current recall",
    )

    assert transitioned["status"] == "recalling"
    events = get_run(run_id=run["id"])["events"]
    assert [event["phase"] for event in events].count("recovery") == 1


@requires_db
def test_resume_replanning_is_lease_fenced_and_reclaimable():
    from hindsight.runs import (
        RunAttemptLeaseLostError,
        claim_run_attempt,
        create_run,
        finish_run_attempt,
        prepare_approval,
        transition_run_attempt,
    )

    run, _ = create_run(
        incident_slug=f"attempt-replan-{uuid4().hex}",
        namespace="attempt-replan",
        user_input="checkout latency",
    )
    start = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    finish_run_attempt(
        run_id=run["id"],
        attempt_id=start.attempt_id,
        command="start",
        status="awaiting_approval",
        phase="approval",
        summary="Plan is ready for operator review",
        metadata=APPROVAL_METADATA,
    )
    prepare_approval(
        run_id=run["id"],
        approved=True,
        recommendation_id=APPROVAL_RECOMMENDATION_ID,
        selection_fingerprint=APPROVAL_SELECTION_FINGERPRINT,
    )
    resume = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=1,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    replanning = transition_run_attempt(
        run_id=run["id"],
        attempt_id=resume.attempt_id,
        command="resume",
        status="planning",
        phase="plan",
        summary="Recommendation is being replanned",
    )
    _expire_attempt(run["id"])
    replacement = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=1,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )

    assert replanning["status"] == "planning"
    assert replacement.outcome == "claimed"
    assert replacement.run["worker_attempt_count"] == 2
    with pytest.raises(RunAttemptLeaseLostError):
        transition_run_attempt(
            run_id=run["id"],
            attempt_id=resume.attempt_id,
            command="resume",
            status="planning",
            phase="diagnostic",
            summary="Stale diagnostic",
        )


@requires_db
def test_external_call_budgets_are_preserved_across_attempt_recovery():
    from hindsight.runs import (
        RunBudgetExceededError,
        claim_run_attempt,
        create_run,
        reserve_run_budget,
    )

    run, _ = create_run(
        incident_slug=f"attempt-budget-{uuid4().hex}",
        namespace="attempt-budget",
        user_input="checkout latency",
    )
    first = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    assert [
        reserve_run_budget(
            run_id=run["id"],
            attempt_id=first.attempt_id,
            command="start",
            budget="model",
        )
        for _ in range(2)
    ] == [1, 2]
    assert (
        reserve_run_budget(
            run_id=run["id"],
            attempt_id=first.attempt_id,
            command="start",
            budget="cloudwatch",
        )
        == 1
    )

    _expire_attempt(run["id"])
    replacement = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    assert replacement.run["model_call_count"] == 2
    assert replacement.run["cloudwatch_call_count"] == 1
    assert [
        reserve_run_budget(
            run_id=run["id"],
            attempt_id=replacement.attempt_id,
            command="start",
            budget="model",
        )
        for _ in range(2)
    ] == [3, 4]
    assert [
        reserve_run_budget(
            run_id=run["id"],
            attempt_id=replacement.attempt_id,
            command="start",
            budget="cloudwatch",
        )
        for _ in range(2)
    ] == [2, 3]
    with pytest.raises(RunBudgetExceededError):
        reserve_run_budget(
            run_id=run["id"],
            attempt_id=replacement.attempt_id,
            command="start",
            budget="model",
        )
    with pytest.raises(RunBudgetExceededError):
        reserve_run_budget(
            run_id=run["id"],
            attempt_id=replacement.attempt_id,
            command="start",
            budget="cloudwatch",
        )
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with pytest.raises(psycopg.Error, match="advance monotonically"):
            conn.execute(
                "UPDATE agent_runs SET model_call_count = 0 WHERE id = %s",
                (run["id"],),
            )


@requires_db
def test_finishing_start_releases_lease_and_resume_uses_a_fresh_attempt_count():
    from hindsight.runs import (
        claim_run_attempt,
        create_run,
        finish_run_attempt,
        prepare_approval,
    )

    run, _ = create_run(
        incident_slug=f"attempt-finish-{uuid4().hex}",
        namespace="attempt-finish",
        user_input="checkout latency",
    )
    start = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    waiting = finish_run_attempt(
        run_id=run["id"],
        attempt_id=start.attempt_id,
        status="awaiting_approval",
        phase="approval",
        summary="Plan is ready for operator review",
        metadata=APPROVAL_METADATA,
    )
    stable_duplicate = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    prepare_approval(
        run_id=run["id"],
        approved=True,
        recommendation_id=APPROVAL_RECOMMENDATION_ID,
        selection_fingerprint=APPROVAL_SELECTION_FINGERPRINT,
    )
    resume = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=1,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    completed = finish_run_attempt(
        run_id=run["id"],
        attempt_id=resume.attempt_id,
        status="completed",
        phase="completion",
        summary="Agent run completed",
    )

    assert waiting["worker_attempt_id"] is None
    assert waiting["worker_attempt_count"] == 1
    assert stable_duplicate.outcome == "duplicate"
    assert resume.run["worker_attempt_command"] == "resume"
    assert resume.run["worker_attempt_count"] == 1
    assert completed["worker_attempt_id"] is None
    assert completed["status"] == "completed"
    terminal_duplicate = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=1,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    assert terminal_duplicate.outcome == "duplicate"


@requires_db
@pytest.mark.parametrize(
    ("terminal_status", "approved"),
    [("completed", True), ("rejected", False)],
)
def test_terminal_remediation_seals_decision_without_semantic_reflection(terminal_status, approved):
    from hindsight.runs import (
        claim_run_attempt,
        create_run,
        finish_run_attempt,
        get_run,
        prepare_approval,
    )

    suffix = uuid4().hex
    run, _ = create_run(
        incident_slug=f"remediation-seal-{suffix}",
        namespace=f"remediation-seal-{suffix}",
        user_input="unsafe recalled guidance",
    )
    start = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    finish_run_attempt(
        run_id=run["id"],
        attempt_id=start.attempt_id,
        command="start",
        status="awaiting_approval",
        phase="approval",
        summary="Governed-memory retraction awaits approval",
        metadata=ACTION_APPROVAL_METADATA,
    )
    prepare_approval(
        run_id=run["id"],
        approved=approved,
        recommendation_id=None,
        selection_fingerprint=APPROVAL_SELECTION_FINGERPRINT,
        remediation_action_id=ACTION_ID,
        observation_fingerprint=ACTION_OBSERVATION_FINGERPRINT,
        preview_id=ACTION_PREVIEW_ID,
        preview_fingerprint=ACTION_PREVIEW_FINGERPRINT,
        approval_actor=ACTION_APPROVAL_ACTOR,
    )
    resume = claim_run_attempt(
        run_id=run["id"],
        command="resume",
        command_generation=1,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    finish_run_attempt(
        run_id=run["id"],
        attempt_id=resume.attempt_id,
        command="resume",
        status=terminal_status,
        phase="completion",
        summary="Governed-memory remediation reached a terminal disposition",
        metadata=ACTION_APPROVAL_METADATA,
    )

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        decision = conn.execute(
            "SELECT status, sealed_at IS NOT NULL FROM memory_decisions WHERE id = %s",
            (run["decision_id"],),
        ).fetchone()
        reflection_count = conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE producer_decision_id = %s",
            (run["decision_id"],),
        ).fetchone()[0]

    assert decision == ("sealed", True)
    assert reflection_count == 0
    persisted = get_run(run_id=run["id"])
    assert persisted is not None
    assert persisted["action_trace"]["preview"]["effects"] == {
        "close_memory_ids": ["memory:unsafe"],
        "review_resolutions": [
            {
                "id": "review:unsafe",
                "semantic_memory_id": "memory:unsafe",
                "status": "superseded",
            }
        ],
    }


@requires_db
def test_exhausted_source_finalization_is_atomic_and_idempotent():
    from hindsight.runs import claim_run_attempt, create_run, finalize_exhausted_run, get_run

    run, _ = create_run(
        incident_slug=f"attempt-exhausted-{uuid4().hex}",
        namespace="attempt-exhausted",
        user_input="checkout latency",
    )
    for expected_count in range(1, 4):
        claimed = claim_run_attempt(
            run_id=run["id"],
            command="start",
            command_generation=0,
            lease_ttl=LEASE_TTL,
            max_attempts=3,
        )
        assert claimed.outcome == "claimed"
        assert claimed.run["worker_attempt_count"] == expected_count
        _expire_attempt(run["id"])

    exhausted = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
    )
    assert exhausted.outcome == "exhausted"
    failed = finalize_exhausted_run(run_id=run["id"], command="start", max_attempts=3)
    repeated = finalize_exhausted_run(run_id=run["id"], command="start", max_attempts=3)

    assert failed["status"] == "failed"
    assert repeated["status"] == "failed"
    persisted = get_run(run_id=run["id"])
    assert [event["status"] for event in persisted["events"]].count("failed") == 1
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        decision = conn.execute(
            "SELECT status, sealed_at IS NOT NULL FROM memory_decisions WHERE id = %s",
            (run["decision_id"],),
        ).fetchone()
    assert decision == ("failed", True)


@requires_db
def test_exact_owner_can_finalize_the_live_final_attempt():
    from hindsight.runs import claim_run_attempt, create_run, finalize_exhausted_run, get_run

    run, _ = create_run(
        incident_slug=f"attempt-final-owner-{uuid4().hex}",
        namespace="attempt-final-owner",
        user_input="checkout latency",
    )
    final = None
    for attempt_number in range(1, 4):
        claim = claim_run_attempt(
            run_id=run["id"],
            command="start",
            command_generation=0,
            lease_ttl=LEASE_TTL,
            max_attempts=3,
        )
        assert claim.outcome == "claimed"
        if attempt_number < 3:
            _expire_attempt(run["id"])
        else:
            final = claim
    failed = finalize_exhausted_run(
        run_id=run["id"],
        command="start",
        max_attempts=3,
        attempt_id=final.attempt_id,
    )
    repeated = finalize_exhausted_run(
        run_id=run["id"],
        command="start",
        max_attempts=3,
        attempt_id=final.attempt_id,
    )

    assert failed["status"] == "failed"
    assert repeated["status"] == "failed"
    persisted = get_run(run_id=run["id"])
    assert [event["status"] for event in persisted["events"]].count("failed") == 1


@requires_db
def test_agent_writer_redrive_creates_one_fresh_run_for_an_exhausted_source(
    agent_writer_database_url,
):
    from hindsight.quarantine import persist_quarantine_record
    from hindsight.quarantine_redrive import redrive_quarantined_run
    from hindsight.runs import claim_run_attempt, create_run, finalize_exhausted_run, get_run
    from tests.quarantine_fakes import InMemoryQuarantineTable

    run, _ = create_run(
        incident_slug=f"redrive-source-{uuid4().hex}",
        namespace="attempt-redrive",
        user_input="checkout latency",
    )
    for _ in range(3):
        claim = claim_run_attempt(
            run_id=run["id"],
            command="start",
            command_generation=0,
            lease_ttl=LEASE_TTL,
            max_attempts=3,
        )
        assert claim.outcome == "claimed"
        _expire_attempt(run["id"])
    finalized = finalize_exhausted_run(
        run_id=run["id"],
        command="start",
        max_attempts=3,
    )
    assert finalized["status"] == "failed"

    table = InMemoryQuarantineTable()
    record = persist_quarantine_record(
        table=table,
        source_arn="arn:aws:sqs:us-east-1:123456789012:hindsight-runs",
        source_message_id=str(uuid4()),
        raw_body='{"command":"start"}',
        reason_code="run_attempts_exhausted",
        work_kind="run",
        command="start",
        receive_count=4,
        tenant_id="00000000-0000-0000-0000-000000000001",
        run_id=str(run["id"]),
        command_generation=0,
    ).item
    confirmation = f"redrive:{record['quarantine_id']}:{record['raw_body_sha256']}"
    arguments = {
        "table": table,
        "quarantine_id": record["quarantine_id"],
        "raw_body_sha256": record["raw_body_sha256"],
        "confirmation": confirmation,
        "repository_owner": "owner",
        "actor": "owner",
        "triggering_actor": "owner",
        "db_url": agent_writer_database_url,
    }

    first = redrive_quarantined_run(**arguments)
    repeated = redrive_quarantined_run(**arguments)

    assert first["created"] is True
    assert repeated == {**first, "created": False}
    assert first["run_id"] != str(run["id"])
    redriven = get_run(run_id=first["run_id"])
    assert redriven["status"] == "queued"
    assert redriven["user_input"] == finalized["user_input"]
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        effect_count = conn.execute(
            "SELECT count(*) FROM agent_runs WHERE idempotency_key = %s",
            (f"quarantine-redrive:{first['redrive_effect_id']}",),
        ).fetchone()[0]
    assert effect_count == 1


@requires_db
def test_delivery_acknowledgement_commits_with_first_run_effects_and_wins_send_race():
    import hindsight.run_dispatch as run_dispatch
    from hindsight.runs import claim_run_attempt, create_run, get_run

    run, _ = create_run(
        incident_slug=f"attempt-delivery-{uuid4().hex}",
        namespace="attempt-delivery",
        user_input="checkout latency",
    )
    delivery = run_dispatch._lease_run_dispatches(
        db_url=None,
        run_id=run["id"],
        command="start",
        limit=1,
    )[0]

    claimed = claim_run_attempt(
        run_id=run["id"],
        command="start",
        command_generation=0,
        lease_ttl=LEASE_TTL,
        max_attempts=3,
        dispatch_id=delivery["id"],
        dispatch_attempt_id=delivery["dispatch_attempt_id"],
        dispatch_sequence=delivery["dispatch_sequence"],
        worker_message_id="worker-message-1",
    )

    assert claimed.outcome == "claimed"
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        before_send_completion = conn.execute(
            """
                SELECT status, acknowledged_attempt_id, acknowledged_at IS NOT NULL,
                       transport_message_id
                FROM agent_run_dispatches
                WHERE id = %s
            """,
            (delivery["id"],),
        ).fetchone()
        attempt = conn.execute(
            """
                SELECT sequence, worker_message_id, acknowledged_at IS NOT NULL,
                       transport_message_id
                FROM agent_run_dispatch_attempts
                WHERE id = %s
            """,
            (delivery["dispatch_attempt_id"],),
        ).fetchone()

    assert before_send_completion == (
        "acknowledged",
        delivery["dispatch_attempt_id"],
        True,
        None,
    )
    assert attempt == (delivery["dispatch_sequence"], "worker-message-1", True, None)

    completed = run_dispatch._complete_run_dispatch(
        dispatch_id=delivery["id"],
        dispatch_attempt_id=delivery["dispatch_attempt_id"],
        lease_owner=delivery["lease_owner"],
        message_id="transport-message-1",
        db_url=None,
    )

    assert completed is True
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        after_send_completion = conn.execute(
            """
                SELECT status, acknowledged_attempt_id, transport_message_id,
                       dispatched_at IS NOT NULL
                FROM agent_run_dispatches
                WHERE id = %s
            """,
            (delivery["id"],),
        ).fetchone()
    assert after_send_completion == (
        "acknowledged",
        delivery["dispatch_attempt_id"],
        "transport-message-1",
        True,
    )
    triage_event = get_run(run_id=run["id"])["events"][-1]
    assert triage_event["metadata"]["dispatch_attempt_id"] == str(delivery["dispatch_attempt_id"])
    assert triage_event["metadata"]["dispatch_sequence"] == delivery["dispatch_sequence"]


@requires_db
def test_run_effects_and_delivery_acknowledgement_roll_back_together(monkeypatch):
    import hindsight.run_dispatch as run_dispatch
    import hindsight.runs as runs

    run, _ = runs.create_run(
        incident_slug=f"attempt-delivery-rollback-{uuid4().hex}",
        namespace="attempt-delivery-rollback",
        user_input="checkout latency",
    )
    delivery = run_dispatch._lease_run_dispatches(
        db_url=None,
        run_id=run["id"],
        command="start",
        limit=1,
    )[0]

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(runs, "_append_event_with_cursor", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        runs.claim_run_attempt(
            run_id=run["id"],
            command="start",
            command_generation=0,
            lease_ttl=LEASE_TTL,
            max_attempts=3,
            dispatch_id=delivery["id"],
            dispatch_attempt_id=delivery["dispatch_attempt_id"],
            dispatch_sequence=delivery["dispatch_sequence"],
            worker_message_id="worker-message-rollback",
        )

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        persisted_run = conn.execute(
            """
                SELECT status, worker_attempt_id
                FROM agent_runs
                WHERE id = %s
            """,
            (run["id"],),
        ).fetchone()
        persisted_dispatch = conn.execute(
            """
                SELECT status, acknowledged_attempt_id
                FROM agent_run_dispatches
                WHERE id = %s
            """,
            (delivery["id"],),
        ).fetchone()
        persisted_attempt = conn.execute(
            """
                SELECT worker_message_id, acknowledged_at
                FROM agent_run_dispatch_attempts
                WHERE id = %s
            """,
            (delivery["dispatch_attempt_id"],),
        ).fetchone()
        triage_events = conn.execute(
            """
                SELECT count(*) FROM agent_run_events
                WHERE run_id = %s AND phase = 'triaging'
            """,
            (run["id"],),
        ).fetchone()

    assert persisted_run == ("queued", None)
    assert persisted_dispatch == ("leased", None)
    assert persisted_attempt == (None, None)
    assert triage_events == (0,)
