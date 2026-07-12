"""Tests for the M4 signature poison, diagnose, and rewind demo."""

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

    assert result.namespace == namespace
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
