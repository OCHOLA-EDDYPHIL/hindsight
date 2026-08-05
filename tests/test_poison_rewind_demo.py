"""Tests for the memory poisoning and rewind demo."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@requires_db
def test_poison_rewind_demo_filters_poison_then_rewinds_audit_history():
    from hindsight.db import database_url
    from hindsight.demo import (
        GOOD_RECOMMENDATION,
        REWIND_REASON,
        run_poison_rewind_demo,
    )
    from hindsight.memory import MemoryStore

    namespace = f"poison-rewind-test-{uuid4()}"

    result = run_poison_rewind_demo(db_url=database_url(), namespace=namespace)

    assert result.namespace.startswith(f"{namespace}:session:")
    assert result.clean_run.plan == GOOD_RECOMMENDATION
    assert result.bad_run.plan == GOOD_RECOMMENDATION
    assert result.bad_run.action_trace["score"] == {
        "recovered": True,
        "unsafe_action_count": 0,
    }
    assert str(result.poison_memory["id"]) not in result.bad_run.recalled_memory_ids
    assert result.diagnosis["decision_id"] == result.bad_run.decision_id
    assert all(
        str(item["memory"]["id"]) != str(result.poison_memory["id"])
        for item in result.diagnosis["memories"]
    )
    assert result.rewind.operation["operation_type"] == "rewind"
    assert result.rewind.operation["reason"] == REWIND_REASON

    invalidated_ids = {str(row["id"]) for row in result.rewind.invalidated_memories}
    assert str(result.poison_memory["id"]) in invalidated_ids
    assert result.bad_run.reflected_memory_id in invalidated_ids
    assert result.corrected_run.plan == GOOD_RECOMMENDATION
    assert result.corrected_run.action_trace["score"] == {
        "recovered": True,
        "unsafe_action_count": 0,
    }
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


@requires_db
def test_browser_signature_boundary_preserves_seed_and_closes_later_memories():
    from hindsight.db import database_url
    from hindsight.demo_state import (
        current_database_timestamp,
        poison_demo_memory,
        reset_poison_rewind_state,
        seed_good_demo_memory,
    )
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_rewind

    provider = DeterministicEmbeddingProvider()
    namespace = reset_poison_rewind_state(
        namespace=f"browser-signature:{uuid4()}",
        db_url=database_url(),
    )
    seed = seed_good_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    rewind_anchor = current_database_timestamp(db_url=database_url())
    poison = poison_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    with MemoryStore(
        url=database_url(),
        embedding_provider=provider,
    ) as store:
        rejected_reflection = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="Rejected certificate-rotation recommendation retained for audit.",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref=f"browser-signature:{uuid4()}",
                justification="Retain the rejected recommendation in governed history",
            ),
        )

    preview = preview_rewind(
        namespace=namespace,
        target_timestamp=rewind_anchor,
        actor="test.operator",
        reason="Close memories written after the known-good boundary",
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=str(preview["fingerprint"]),
        idempotency_key=f"browser-signature:{uuid4()}",
        db_url=database_url(),
    )
    completed = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="test.browser-signature",
        db_url=database_url(),
    )

    invalidated_ids = {str(value) for value in completed["invalidated_memory_ids"]}
    assert invalidated_ids == {
        str(poison["id"]),
        str(rejected_reflection["id"]),
    }
    with MemoryStore(url=database_url()) as store:
        current = store.list_current_semantic(namespace=namespace, limit=100)
        poison_audit = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(poison["id"]),
        )
        reflection_audit = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(rejected_reflection["id"]),
        )

    assert {str(memory["id"]) for memory in current} == {str(seed["id"])}
    assert poison_audit is not None and poison_audit["t_invalid"] is not None
    assert reflection_audit is not None and reflection_audit["t_invalid"] is not None
