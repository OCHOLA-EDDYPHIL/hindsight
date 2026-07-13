"""Durable agent-run schema and identity checks."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


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
