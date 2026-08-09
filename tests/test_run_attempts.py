"""Durable fencing and recovery for asynchronous agent-run attempts."""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
LEASE_TTL = timedelta(minutes=5)
APPROVAL_RECOMMENDATION_ID = "recommendation:test"
APPROVAL_SELECTION_FINGERPRINT = "selection:test"
APPROVAL_METADATA = {
    "action_trace": {
        "recommendation": {"id": APPROVAL_RECOMMENDATION_ID},
        "selection": {"fingerprint": APPROVAL_SELECTION_FINGERPRINT},
    }
}


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
def test_exhausted_dlq_finalization_is_atomic_and_idempotent():
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
