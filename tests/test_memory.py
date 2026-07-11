"""Database-backed tests for bi-temporal memory and provenance."""

import os
from datetime import timedelta
from time import sleep
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

    expected_tables = {
        "episodic_memories",
        "semantic_memories",
        "semantic_memory_embeddings",
        "memory_reads",
        "memory_operations",
        "mcp_audit_events",
    }
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
def test_semantic_vector_index_exists():
    from hindsight.db import connect

    with connect() as conn:
        rows = conn.execute("SHOW INDEXES FROM semantic_memory_embeddings").fetchall()

    index_names = {row[1] for row in rows}
    assert "semantic_memory_embeddings_namespace_embedding_idx" in index_names


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
def test_public_memory_api_remembers_recalls_and_invalidates(memory_store):
    from hindsight.memory import Provenance

    namespace = f"incident-{uuid4()}"
    memory = memory_store.remember(
        memory_kind="semantic",
        namespace=namespace,
        content="payment timeout recovered after retry fanout was throttled",
        provenance=Provenance(
            writer="agent.reflect",
            source_ref="incident:memory-api",
            justification="Capture the resolution as reusable memory",
        ),
    )

    recalled = memory_store.recall(
        namespace=namespace,
        query="retry fanout",
    )

    assert [row["id"] for row in recalled] == [memory["id"]]

    invalidated = memory_store.invalidate(
        memory_id=str(memory["id"]),
        actor="agent.rewind",
        reason="Superseded by later incident review",
    )

    assert invalidated is not None
    assert invalidated["invalidated_by"] == "agent.rewind"
    assert memory_store.recall(namespace=namespace, query="retry fanout") == []


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


@requires_db
def test_semantic_write_creates_embedding_row():
    from hindsight.db import connect
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    conn = connect()
    try:
        store = MemoryStore(conn, embedding_provider=DeterministicEmbeddingProvider())
        memory = store.write_semantic(
            namespace=f"incident-{uuid4()}",
            content="payment timeout in worker",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:vector-write",
                justification="Embedding write test",
            ),
        )

        row = conn.execute(
            """
                SELECT namespace, provider, model, dimensions
                FROM semantic_memory_embeddings
                WHERE memory_id = %s
            """,
            (memory["id"],),
        ).fetchone()

        assert row == (
            memory["namespace"],
            "deterministic",
            "stable-hash-v1",
            1024,
        )
    finally:
        conn.rollback()
        conn.close()


@requires_db
def test_bad_embedding_provider_does_not_leave_semantic_row():
    from hindsight.db import connect
    from hindsight.memory import MemoryStore, Provenance

    class BadProvider:
        provider_name = "bad"
        model_name = "wrong-dimensions"
        dimensions = 3

        def embed(self, text: str) -> list[float]:
            return [0.0, 0.0, 0.0]

    conn = connect()
    namespace = f"incident-{uuid4()}"
    try:
        store = MemoryStore(conn, embedding_provider=BadProvider())

        with pytest.raises(ValueError, match="semantic vector store expects 1024 dimensions"):
            store.write_semantic(
                namespace=namespace,
                content="payment timeout in worker",
                provenance=Provenance(
                    writer="agent.reflect",
                    source_ref="incident:bad-vector-write",
                    justification="Reject bad embedding provider before row insert",
                ),
            )
        conn.commit()

        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE namespace = %s",
            (namespace,),
        ).fetchone() == (0,)
    finally:
        conn.rollback()
        conn.close()


@requires_db
def test_current_semantic_rejects_blank_namespace(memory_store):
    from hindsight.memory import ProvenanceError

    with pytest.raises(ProvenanceError, match="namespace is required"):
        memory_store.current_semantic(namespace="")


@requires_db
def test_as_of_recall_does_not_commit_caller_transaction():
    from hindsight.db import connect
    from hindsight.memory import MemoryStore, Provenance

    conn = connect()
    namespace = f"incident-{uuid4()}"
    try:
        store = MemoryStore(conn)
        baseline = store.write_semantic(
            namespace=namespace,
            content="payment latency came from retry fanout",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:baseline",
                justification="Committed baseline before tentative write",
            ),
        )
        conn.commit()
        as_of = conn.execute("SELECT now()").fetchone()[0]
        conn.commit()
        sleep(0.05)

        tentative = store.write_semantic(
            namespace=namespace,
            content="payment latency came from certificate expiry",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:tentative",
                justification="Tentative write must remain rollbackable after AS OF recall",
            ),
        )

        recalled = store.recall(namespace=namespace, query="", as_of=as_of, limit=5)

        assert [row["id"] for row in recalled] == [baseline["id"]]
        conn.rollback()
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE id = %s",
            (tentative["id"],),
        ).fetchone() == (0,)
    finally:
        conn.rollback()
        conn.close()


@requires_db
def test_rewind_invalidates_poisoned_and_derived_semantic_memories():
    from hindsight.db import connect
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    conn = connect()
    try:
        store = MemoryStore(conn, embedding_provider=DeterministicEmbeddingProvider())
        namespace = f"incident-{uuid4()}"
        good = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="payment latency came from retry fanout to the processor",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:good",
                justification="Known-good resolution before poisoning",
            ),
        )
        sleep(0.05)
        rewind_target = conn.execute("SELECT now()").fetchone()[0] + timedelta(milliseconds=1)
        conn.commit()
        sleep(0.05)

        poisoned = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="payment latency came from certificate expiry",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:poisoned",
                justification="Bad hypothesis introduced by poisoned memory",
            ),
        )
        decision_id = f"decision-{uuid4()}"
        recalled = store.recall(
            namespace=namespace,
            query="certificate expiry",
            limit=1,
            decision_id=decision_id,
            reader="agent.triage",
            purpose="choose remediation from memory",
        )
        assert [row["id"] for row in recalled] == [poisoned["id"]]

        derived = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="rotate certificates to fix payment latency",
            provenance=Provenance(
                writer="agent.plan",
                source_ref=decision_id,
                justification="Derived from the poisoned decision context",
            ),
        )

        result = store.rewind(
            timestamp=rewind_target,
            namespace=namespace,
            actor="agent.rewind",
            reason="Poisoned memory led to the wrong remediation plan",
        )

        assert [row["id"] for row in result.restored_memories] == [good["id"]]
        assert {row["id"] for row in result.invalidated_memories} == {
            poisoned["id"],
            derived["id"],
        }
        assert result.operation["operation_type"] == "rewind"
        assert result.operation["namespace"] == namespace
        assert set(result.operation["invalidated_memory_ids"]) == {
            str(poisoned["id"]),
            str(derived["id"]),
        }
        assert store.audit_memory(memory_kind="semantic", memory_id=str(poisoned["id"]))[
            "t_invalid"
        ] is not None
        assert store.audit_memory(memory_kind="semantic", memory_id=str(derived["id"]))[
            "t_invalid"
        ] is not None
        assert [row["id"] for row in store.recall(namespace=namespace, query="payment")] == [
            good["id"]
        ]
    finally:
        conn.rollback()
        conn.close()


@requires_db
def test_rewind_does_not_commit_caller_transaction():
    from hindsight.db import connect
    from hindsight.memory import MemoryStore, Provenance

    conn = connect()
    namespace = f"incident-{uuid4()}"
    try:
        store = MemoryStore(conn)
        store.write_semantic(
            namespace=namespace,
            content="payment latency came from retry fanout",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:baseline",
                justification="Known-good baseline before rewind target",
            ),
        )
        conn.commit()
        rewind_target = conn.execute("SELECT now()").fetchone()[0]
        conn.commit()
        sleep(0.05)

        poisoned = store.write_semantic(
            namespace=namespace,
            content="payment latency came from certificate expiry",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:poisoned",
                justification="Committed bad memory to be invalidated inside caller transaction",
            ),
        )
        conn.commit()

        tentative = store.write_semantic(
            namespace=namespace,
            content="rotate certificates to fix payment latency",
            provenance=Provenance(
                writer="agent.plan",
                source_ref="decision:tentative",
                justification="Tentative derived memory must remain rollbackable after rewind",
            ),
        )
        result = store.rewind(
            timestamp=rewind_target,
            namespace=namespace,
            actor="agent.rewind",
            reason="Poisoned memory led to the wrong remediation plan",
        )
        operation_id = result.operation["id"]

        conn.rollback()

        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE id = %s",
            (tentative["id"],),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT t_invalid FROM semantic_memories WHERE id = %s",
            (poisoned["id"],),
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT count(*) FROM memory_operations WHERE id = %s",
            (operation_id,),
        ).fetchone() == (0,)
    finally:
        conn.rollback()
        conn.close()


@requires_db
def test_vector_recall_is_namespace_scoped_current_and_tracked():
    from hindsight.db import connect
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    conn = connect()
    try:
        store = MemoryStore(conn, embedding_provider=DeterministicEmbeddingProvider())
        namespace = f"incident-{uuid4()}"
        other_namespace = f"incident-{uuid4()}"
        decision_id = f"decision-{uuid4()}"
        target = store.write_semantic(
            namespace=namespace,
            content="payment timeout in worker",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:target",
                justification="Relevant prior incident",
            ),
        )
        stale = store.write_semantic(
            namespace=namespace,
            content="payment timeout stale hypothesis",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:stale",
                justification="Superseded memory",
            ),
        )
        store.write_semantic(
            namespace=other_namespace,
            content="payment timeout in worker",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:other-namespace",
                justification="Same content in another namespace",
            ),
        )
        store.invalidate(
            memory_kind="semantic",
            memory_id=str(stale["id"]),
            invalidated_by="agent.rewind",
            reason="Stale vector memory should not be recalled",
        )

        recalled = store.recall_semantic(
            namespace=namespace,
            query="payment timeout",
            limit=5,
            decision_id=decision_id,
            reader="agent.triage",
            purpose="retrieve similar memory",
        )

        assert [row["id"] for row in recalled] == [target["id"]]
        assert recalled[0]["distance"] is not None
        reads = store.reads_for_decision(decision_id=decision_id)
        assert [row["memory_id"] for row in reads] == [target["id"]]
    finally:
        conn.rollback()
        conn.close()
