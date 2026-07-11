"""Database-backed tests for bi-temporal memory and provenance."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@pytest.fixture
def memory_store():
    from hindsight.db import connect
    from hindsight.memory import MemoryStore

    conn = connect()
    try:
        with MemoryStore(conn) as store:
            yield store
    finally:
        conn.rollback()
        conn.close()


@requires_db
def test_memory_schema_objects_exist():
    from hindsight.db import connect

    expected_tables = {"episodic_memories", "semantic_memories", "memory_reads"}
    expected_views = {"current_episodic_memories", "current_semantic_memories"}

    with connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                        AND table_type = 'BASE TABLE'
                """
            )
        }
        views = {
            row[0]
            for row in conn.execute(
                """
                    SELECT table_name
                    FROM information_schema.views
                    WHERE table_schema = 'public'
                """
            )
        }

    assert expected_tables <= tables
    assert expected_views <= views


@requires_db
def test_writes_without_provenance_are_rejected(memory_store):
    from hindsight.memory import Provenance, ProvenanceError

    with pytest.raises(ProvenanceError):
        memory_store.write_episodic(
            episode_id=f"episode-{uuid4()}",
            role="assistant",
            content="missing provenance",
            provenance=Provenance(writer="", source_ref="turn-1", justification="test"),
        )


@requires_db
def test_invalidation_updates_row_and_current_view_excludes_it(memory_store):
    from hindsight.memory import Provenance

    episode_id = f"episode-{uuid4()}"
    memory = memory_store.write_episodic(
        episode_id=episode_id,
        role="assistant",
        content="the cache is healthy",
        provenance=Provenance(
            writer="agent.triage",
            source_ref="conversation:turn-1",
            justification="Initial diagnosis from telemetry",
        ),
    )

    assert [row["id"] for row in memory_store.current_episodic(episode_id=episode_id)] == [
        memory["id"]
    ]

    invalidated = memory_store.invalidate(
        memory_kind="episodic",
        memory_id=str(memory["id"]),
        invalidated_by="agent.rewind",
        reason="Telemetry contradicted this memory",
    )

    assert invalidated is not None
    assert invalidated["t_invalid"] is not None
    assert memory_store.current_episodic(episode_id=episode_id) == []

    audit_row = memory_store.audit_memory(memory_kind="episodic", memory_id=str(memory["id"]))
    assert audit_row is not None
    assert audit_row["content"] == "the cache is healthy"
    assert audit_row["invalidation_reason"] == "Telemetry contradicted this memory"


@requires_db
def test_semantic_memory_retrieval_respects_validity(memory_store):
    from hindsight.memory import Provenance

    namespace = f"incident-{uuid4()}"
    current = memory_store.write_semantic(
        namespace=namespace,
        content="payment latency was caused by a downstream timeout",
        metadata={"service": "payments"},
        provenance=Provenance(
            writer="agent.reflect",
            source_ref="incident:123",
            justification="Resolution summary",
        ),
    )
    stale = memory_store.write_semantic(
        namespace=namespace,
        content="payment latency was caused by certificate expiry",
        provenance=Provenance(
            writer="agent.reflect",
            source_ref="incident:123:old",
            justification="Superseded hypothesis",
        ),
    )
    memory_store.invalidate(
        memory_kind="semantic",
        memory_id=str(stale["id"]),
        invalidated_by="agent.reflect",
        reason="Final incident review found a different cause",
    )

    rows = memory_store.current_semantic(namespace=namespace)
    assert [row["id"] for row in rows] == [current["id"]]
    assert memory_store.audit_memory(memory_kind="semantic", memory_id=str(stale["id"]))[
        "t_invalid"
    ] is not None


@requires_db
def test_provenance_and_decision_read_tracking(memory_store):
    from hindsight.memory import Provenance

    decision_id = f"decision-{uuid4()}"
    memory = memory_store.write_semantic(
        namespace=f"incident-{uuid4()}",
        content="restart the worker after clearing the stuck lease",
        provenance=Provenance(
            writer="human.sre",
            source_ref="runbook:worker-restart",
            justification="Runbook step used in prior incident",
        ),
    )

    provenance = memory_store.provenance_for_memory(
        memory_kind="semantic", memory_id=str(memory["id"])
    )
    assert provenance is not None
    assert provenance["writer"] == "human.sre"
    assert provenance["source_ref"] == "runbook:worker-restart"
    assert provenance["justification"] == "Runbook step used in prior incident"

    rows = memory_store.current_semantic(
        namespace=memory["namespace"],
        decision_id=decision_id,
        reader="agent.triage",
        purpose="choose remediation plan",
    )
    assert [row["id"] for row in rows] == [memory["id"]]

    reads = memory_store.reads_for_decision(decision_id=decision_id)
    assert [row["memory_id"] for row in reads] == [memory["id"]]

    joined = memory_store.memories_for_decision(decision_id=decision_id)
    assert joined[0]["semantic_content"] == "restart the worker after clearing the stuck lease"
    assert joined[0]["semantic_writer"] == "human.sre"
