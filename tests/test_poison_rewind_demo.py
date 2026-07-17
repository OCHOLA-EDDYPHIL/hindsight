"""Tests for the memory poisoning and rewind demo."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@requires_db
def test_poison_rewind_demo_runs_bad_trace_rewind_and_corrected_turn():
    from hindsight.db import database_url
    from hindsight.demo import (
        BAD_RECOMMENDATION,
        GOOD_RECOMMENDATION,
        REWIND_REASON,
        run_poison_rewind_demo,
    )
    from hindsight.memory import MemoryStore

    namespace = f"poison-rewind-test-{uuid4()}"

    result = run_poison_rewind_demo(db_url=database_url(), namespace=namespace)

    assert result.namespace.startswith(f"{namespace}:session:")
    assert result.clean_run.plan == GOOD_RECOMMENDATION
    assert result.bad_run.plan == BAD_RECOMMENDATION
    assert str(result.poison_memory["id"]) in result.bad_run.recalled_memory_ids
    assert result.diagnosis["decision_id"] == result.bad_run.decision_id
    assert any(
        item["memory"]["id"] == str(result.poison_memory["id"])
        and item["provenance"]["writer"] == "demo.poison"
        for item in result.diagnosis["memories"]
    )
    assert result.rewind.operation["operation_type"] == "rewind"
    assert result.rewind.operation["reason"] == REWIND_REASON

    invalidated_ids = {str(row["id"]) for row in result.rewind.invalidated_memories}
    assert str(result.poison_memory["id"]) in invalidated_ids
    assert result.bad_run.reflected_memory_id in invalidated_ids
    assert result.corrected_run.plan == GOOD_RECOMMENDATION
    assert str(result.poison_memory["id"]) not in result.corrected_run.recalled_memory_ids

    with MemoryStore(url=database_url()) as store:
        poison = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(result.poison_memory["id"]),
        )
        bad_reflection = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(result.bad_run.reflected_memory_id),
        )

    assert poison is not None
    assert poison["invalidation_reason"] == REWIND_REASON
    assert bad_reflection is not None
    assert bad_reflection["invalidation_reason"] == REWIND_REASON


@requires_db
def test_browser_demo_reset_isolates_sessions_and_incidents():
    from hindsight.db import database_url
    from hindsight.demo_state import (
        ensure_poison_rewind_incident,
        reset_poison_rewind_state,
    )
    from hindsight.db import connect

    first_fixture = uuid4()
    second_fixture = uuid4()
    first = reset_poison_rewind_state(
        namespace=f"browser-reset:{uuid4()}",
        session_id=first_fixture,
        db_url=database_url(),
    )
    second = reset_poison_rewind_state(
        namespace=f"browser-reset:{uuid4()}",
        session_id=second_fixture,
        db_url=database_url(),
    )
    replacement = reset_poison_rewind_state(
        namespace=first,
        db_url=database_url(),
    )
    first_incident = ensure_poison_rewind_incident(
        fixture_id=first_fixture,
        db_url=database_url(),
    )
    second_incident = ensure_poison_rewind_incident(
        fixture_id=second_fixture,
        db_url=database_url(),
    )

    with connect(database_url()) as conn:
        statuses = dict(
            conn.execute(
                "SELECT namespace, status FROM demo_sessions WHERE namespace IN (%s, %s, %s)",
                (first, second, replacement),
            ).fetchall()
        )

    assert statuses == {first: "archived", second: "active", replacement: "active"}
    assert first_incident["id"] != second_incident["id"]
    assert first_incident["slug"] != second_incident["slug"]
