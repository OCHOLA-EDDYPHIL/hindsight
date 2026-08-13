"""Tests for the memory poisoning and rewind demo."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@requires_db
def test_browser_demo_reset_isolates_sessions_and_incidents():
    from hindsight.db import database_url
    from hindsight.demo_state import (
        ensure_poison_rewind_incident,
        record_poison_rewind_anchor,
        reset_poison_rewind_state,
    )
    from hindsight.db import connect

    first_fixture = uuid4()
    second_fixture = uuid4()
    first_incident = ensure_poison_rewind_incident(
        fixture_id=first_fixture,
        db_url=database_url(),
    )
    second_incident = ensure_poison_rewind_incident(
        fixture_id=second_fixture,
        db_url=database_url(),
    )
    first = reset_poison_rewind_state(
        namespace=f"browser-reset:{uuid4()}",
        session_id=first_fixture,
        incident_id=first_fixture,
        db_url=database_url(),
    )
    rewind_anchor = record_poison_rewind_anchor(
        namespace=first,
        db_url=database_url(),
    )
    second = reset_poison_rewind_state(
        namespace=f"browser-reset:{uuid4()}",
        session_id=second_fixture,
        incident_id=second_fixture,
        db_url=database_url(),
    )
    replacement = reset_poison_rewind_state(
        namespace=first,
        db_url=database_url(),
    )
    with connect(database_url()) as conn:
        statuses = dict(
            conn.execute(
                "SELECT namespace, status FROM demo_sessions WHERE namespace IN (%s, %s, %s)",
                (first, second, replacement),
            ).fetchall()
        )
        replay_identity = conn.execute(
            """
                SELECT id, incident_tenant_id, incident_id, rewind_anchor
                FROM demo_sessions
                WHERE namespace = %s
            """,
            (first,),
        ).fetchone()
        session_tenant_id = replay_identity[1]
        conn.execute("DELETE FROM incidents WHERE id = %s", (second_fixture,))
        detached_identity = conn.execute(
            """
                SELECT tenant_id, incident_tenant_id, incident_id, status
                FROM demo_sessions
                WHERE namespace = %s
            """,
            (second,),
        ).fetchone()
        conn.commit()

    assert statuses == {first: "archived", second: "active", replacement: "active"}
    assert replay_identity == (
        first_fixture,
        session_tenant_id,
        first_fixture,
        rewind_anchor,
    )
    assert detached_identity == (session_tenant_id, None, None, "active")
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
    from tests.fakes import DeterministicEmbeddingProvider
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
    assert poison["metadata"]["scenario_role"] == "compromised_guidance"
    assert poison["metadata"]["risk_class"] == "stale_operational_guidance"
    assert "role" not in poison["metadata"]
    assert poison["belief_id"] == seed["belief_id"]
    assert poison["previous_version_id"] == seed["id"]
    assert poison["version_number"] == seed["version_number"] + 1
    assert poison["transition_kind"] == "supersession"
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
        seed_audit = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(seed["id"]),
        )
        reflection_audit = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(rejected_reflection["id"]),
        )

    assert len(current) == 1
    reasserted = current[0]
    assert reasserted["belief_id"] == seed["belief_id"]
    assert reasserted["version_number"] == poison["version_number"] + 1
    assert reasserted["previous_version_id"] == poison["id"]
    assert reasserted["transition_kind"] == "rewind_reassertion"
    assert str(reasserted["id"]) in {
        str(value) for value in completed["restored_memory_ids"]
    }
    assert seed_audit is not None and seed_audit["t_invalid"] is not None
    assert poison_audit is not None and poison_audit["t_invalid"] is not None
    assert reflection_audit is not None and reflection_audit["t_invalid"] is not None
