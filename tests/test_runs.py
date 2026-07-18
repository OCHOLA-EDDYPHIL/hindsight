"""Durable agent-run schema and identity checks."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def test_get_run_retries_a_transient_serialization_failure(monkeypatch):
    import hindsight.runs as runs
    from psycopg.errors import SerializationFailure

    expected = {
        "id": "run-retried",
        "status": "awaiting_approval",
        "events": [
            {"sequence": 1, "phase": "queue"},
            {"sequence": 2, "phase": "triage"},
        ],
    }
    calls = 0

    def flaky_read(*, run_id, db_url):
        nonlocal calls
        calls += 1
        assert run_id == "run-retried"
        assert db_url == "postgresql://fixture"
        if calls == 1:
            raise SerializationFailure("restart transaction")
        return expected

    monkeypatch.setattr(runs, "_read_run", flaky_read)

    assert runs.get_run(
        run_id="run-retried",
        db_url="postgresql://fixture",
    ) == expected
    assert calls == 2


def test_get_run_bounds_retries_and_preserves_non_retryable_errors(monkeypatch):
    import hindsight.runs as runs
    from psycopg.errors import SerializationFailure

    serialization_calls = 0

    def always_serialized(**_kwargs):
        nonlocal serialization_calls
        serialization_calls += 1
        raise SerializationFailure("restart transaction")

    monkeypatch.setattr(runs, "_read_run", always_serialized)
    with pytest.raises(SerializationFailure):
        runs.get_run(run_id="run-exhausted")
    assert serialization_calls == runs.GET_RUN_READ_ATTEMPTS

    non_retryable_calls = 0

    def non_retryable(**_kwargs):
        nonlocal non_retryable_calls
        non_retryable_calls += 1
        raise RuntimeError("not retryable")

    monkeypatch.setattr(runs, "_read_run", non_retryable)
    with pytest.raises(RuntimeError, match="not retryable"):
        runs.get_run(run_id="run-failed")
    assert non_retryable_calls == 1


@requires_db
def test_agent_run_projection_is_idempotent_and_evented():
    from hindsight.runs import create_run, get_run

    suffix = uuid4().hex
    first, created = create_run(
        incident_slug=f"incident-{suffix}",
        namespace=f"namespace-{suffix}",
        user_input="checkout latency",
        idempotency_key=f"request-{suffix}",
    )
    second, duplicate_created = create_run(
        incident_slug=f"incident-{suffix}",
        namespace=f"namespace-{suffix}",
        user_input="checkout latency",
        idempotency_key=f"request-{suffix}",
    )

    assert created is True
    assert duplicate_created is False
    assert first["id"] == second["id"]
    assert first["decision_id"] == f"agent:{first['id']}:plan"
    assert get_run(run_id=first["id"])["events"][0]["status"] == "queued"
