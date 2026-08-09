"""Durable agent-run schema and identity checks."""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


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

    assert (
        runs.get_run(
            run_id="run-retried",
            db_url="postgresql://fixture",
        )
        == expected
    )
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


def test_create_run_retries_a_concurrent_idempotency_race(monkeypatch):
    import hindsight.runs as runs
    from psycopg.errors import SerializationFailure

    expected = ({"id": "winning-run", "status": "queued"}, False)
    calls = []

    def race_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise SerializationFailure("unique-key race")
        return expected

    monkeypatch.setattr(runs, "_create_run_once", race_once)

    assert (
        runs.create_run(
            incident_slug="incident",
            namespace="namespace",
            user_input="checkout latency",
            idempotency_key="request-1",
        )
        == expected
    )
    assert len(calls) == 2
    assert calls[0]["request_fingerprint"] == calls[1]["request_fingerprint"]


@requires_db
def test_agent_run_projection_is_idempotent_and_evented():
    from hindsight.db import connect
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
    assert len(first["request_fingerprint"]) == 64
    assert get_run(run_id=first["id"])["events"][0]["status"] == "queued"
    with connect(application_name="hindsight-test") as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_runs WHERE idempotency_key = %s",
            (f"request-{suffix}",),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id = %s",
            (first["id"],),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM agent_run_dispatches WHERE run_id = %s",
            (first["id"],),
        ).fetchone() == (1,)


@requires_db
def test_idempotency_key_reuse_with_different_request_is_side_effect_free():
    from hindsight.db import connect
    from hindsight.runs import RunIdempotencyConflictError, create_run

    suffix = uuid4().hex
    key = f"conflicting-request-{suffix}"
    namespace = f"conflicting-namespace-{suffix}"
    first, created = create_run(
        incident_slug=f"incident-{suffix}",
        namespace=namespace,
        user_input="checkout latency",
        idempotency_key=key,
    )

    with pytest.raises(RunIdempotencyConflictError):
        create_run(
            incident_slug=f"incident-{suffix}",
            namespace=namespace,
            user_input="a different request body",
            idempotency_key=key,
        )

    assert created is True
    with connect(application_name="hindsight-test") as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_runs WHERE idempotency_key = %s",
            (key,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM memory_decisions WHERE namespace = %s AND actor = 'agent.run'",
            (namespace,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id = %s",
            (first["id"],),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM agent_run_dispatches WHERE run_id = %s",
            (first["id"],),
        ).fetchone() == (1,)


@requires_db
def test_idempotency_keys_are_scoped_to_the_bound_tenant():
    from hindsight.db import connect
    from hindsight.runs import create_run
    from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, PUBLIC_DEMO_TENANT_ID
    from hindsight.tenant import tenant_scope

    suffix = uuid4().hex
    key = f"tenant-request-{suffix}"
    request = {
        "incident_slug": f"tenant-incident-{suffix}",
        "namespace": f"tenant-namespace-{suffix}",
        "user_input": "checkout latency",
        "idempotency_key": key,
    }
    with tenant_scope(PUBLIC_DEMO_TENANT_ID):
        public_run, public_created = create_run(**request)
        with connect(application_name="hindsight-test") as conn:
            public_count = conn.execute(
                """
                    SELECT count(*) FROM agent_runs
                    WHERE tenant_id = current_hindsight_tenant_id()
                        AND idempotency_key = %s
                """,
                (key,),
            ).fetchone()
    with tenant_scope(ACCEPTANCE_TENANT_ID):
        acceptance_run, acceptance_created = create_run(**request)
        with connect(application_name="hindsight-test") as conn:
            acceptance_count = conn.execute(
                """
                    SELECT count(*) FROM agent_runs
                    WHERE tenant_id = current_hindsight_tenant_id()
                        AND idempotency_key = %s
                """,
                (key,),
            ).fetchone()

    assert public_created is True
    assert acceptance_created is True
    assert public_run["id"] != acceptance_run["id"]
    assert public_count == (1,)
    assert acceptance_count == (1,)


@requires_db
def test_legacy_keyed_run_is_validated_and_lazily_fingerprinted():
    from hindsight.db import connect
    from hindsight.runs import create_run

    suffix = uuid4().hex
    key = f"legacy-request-{suffix}"
    request = {
        "incident_slug": f"legacy-incident-{suffix}",
        "namespace": f"legacy-namespace-{suffix}",
        "user_input": "checkout latency",
        "idempotency_key": key,
    }
    first, _ = create_run(**request)
    with connect(application_name="hindsight-test") as conn:
        conn.execute(
            "UPDATE agent_runs SET request_fingerprint = NULL WHERE id = %s",
            (first["id"],),
        )
        conn.commit()

    retried, created = create_run(**request)

    assert created is False
    assert retried["id"] == first["id"]
    assert len(retried["request_fingerprint"]) == 64


@requires_db
def test_concurrent_identical_requests_converge_without_orphan_side_effects():
    from hindsight.db import connect
    from hindsight.runs import create_run
    from hindsight.tenant import tenant_scope

    tenant_id = "00000000-0000-0000-0000-000000000001"
    suffix = uuid4().hex
    key = f"concurrent-request-{suffix}"
    namespace = f"concurrent-namespace-{suffix}"
    request = {
        "incident_slug": f"concurrent-incident-{suffix}",
        "namespace": namespace,
        "user_input": "checkout latency",
        "idempotency_key": key,
    }
    barrier = Barrier(4)

    def create_concurrently():
        with tenant_scope(tenant_id):
            barrier.wait(timeout=5)
            return create_run(**request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: create_concurrently(), range(4)))

    run_ids = {run["id"] for run, _created in results}
    assert len(run_ids) == 1
    assert sum(created for _run, created in results) == 1
    run_id = run_ids.pop()
    with tenant_scope(tenant_id), connect(application_name="hindsight-test") as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_runs WHERE idempotency_key = %s",
            (key,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM memory_decisions WHERE namespace = %s AND actor = 'agent.run'",
            (namespace,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id = %s",
            (run_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM agent_run_dispatches WHERE run_id = %s",
            (run_id,),
        ).fetchone() == (1,)
