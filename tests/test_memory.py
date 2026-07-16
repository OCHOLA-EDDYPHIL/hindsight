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
        mode="current_text",
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
    assert memory_store.recall(
        mode="current_text", namespace=namespace, query="retry fanout"
    ) == []


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
def test_owned_semantic_write_retries_serialization_with_one_prepared_embedding(monkeypatch):
    from psycopg.errors import SerializationFailure

    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    class CountingProvider(DeterministicEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.document_calls = 0

        def embed_document(self, text):
            self.document_calls += 1
            return super().embed_document(text)

    provider = CountingProvider()
    namespace = f"owned-serialization-retry-{uuid4()}"
    original = MemoryStore._insert_semantic_embedding
    insert_calls = 0

    def fail_once(self, **kwargs):
        nonlocal insert_calls
        insert_calls += 1
        if insert_calls == 1:
            raise SerializationFailure("restart transaction")
        return original(self, **kwargs)

    monkeypatch.setattr(MemoryStore, "_insert_semantic_embedding", fail_once)
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="retry fanout overloaded the processor",
            provenance=Provenance("pytest", "evidence:serialization", "retry owned write"),
        )

    with connect() as conn:
        persisted = conn.execute(
            """
                SELECT count(*), count(vector.memory_id), count(evidence.id)
                FROM semantic_memories AS memory
                LEFT JOIN semantic_memory_vectors AS vector ON vector.memory_id = memory.id
                LEFT JOIN memory_external_evidence AS evidence
                    ON evidence.semantic_memory_id = memory.id
                WHERE memory.namespace = %s
            """,
            (namespace,),
        ).fetchone()

    assert persisted == (1, 1, 1)
    assert memory["namespace"] == namespace
    assert insert_calls >= 2
    assert provider.document_calls == 1


@requires_db
def test_serialization_retry_exhaustion_is_owned_and_caller_transactions_propagate(
    monkeypatch,
):
    from psycopg.errors import SerializationFailure

    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import (
        MAX_OWNED_WRITE_TRANSACTION_ATTEMPTS,
        MemoryStore,
        Provenance,
    )

    namespaces = {
        "owned": f"owned-serialization-exhaustion-{uuid4()}",
        "caller": f"caller-serialization-propagation-{uuid4()}",
    }
    insert_calls = 0

    def always_restart(self, **kwargs):
        nonlocal insert_calls
        insert_calls += 1
        raise SerializationFailure("restart transaction")

    monkeypatch.setattr(MemoryStore, "_insert_semantic_embedding", always_restart)
    with MemoryStore(
        url=database_url(), embedding_provider=DeterministicEmbeddingProvider()
    ) as store:
        with pytest.raises(SerializationFailure):
            store.remember(
                memory_kind="semantic",
                namespace=namespaces["owned"],
                content="owned retry exhaustion",
                provenance=Provenance("pytest", "evidence:owned", "exhaust retries"),
            )
    assert insert_calls == MAX_OWNED_WRITE_TRANSACTION_ATTEMPTS

    with connect() as caller_connection:
        with MemoryStore(
            conn=caller_connection,
            embedding_provider=DeterministicEmbeddingProvider(),
        ) as store:
            with pytest.raises(SerializationFailure):
                store.remember(
                    memory_kind="semantic",
                    namespace=namespaces["caller"],
                    content="caller retry propagation",
                    provenance=Provenance(
                        "pytest", "evidence:caller", "preserve outer transaction fence"
                    ),
                )
        caller_connection.rollback()
    assert insert_calls == MAX_OWNED_WRITE_TRANSACTION_ATTEMPTS + 1

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE namespace = ANY(%s)",
            (list(namespaces.values()),),
        ).fetchone() == (0,)


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
def test_live_document_embedding_is_rejected_inside_caller_transaction():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    class CountingSemanticProvider(DeterministicEmbeddingProvider):
        provider_name = "test-semantic"
        model_name = "test-semantic-v1"
        capability = "semantic"
        encoder_revision = "test-semantic-v1"

        def __init__(self):
            super().__init__()
            self.document_calls = 0

        def embed_document(self, text: str) -> list[float]:
            self.document_calls += 1
            return super().embed_document(text)

    namespace = f"transaction-bound-embedding-{uuid4()}"
    provider = CountingSemanticProvider()
    with connect(database_url()) as conn:
        with conn.transaction():
            store = MemoryStore(conn=conn, embedding_provider=provider)
            with pytest.raises(
                RuntimeError,
                match="must be precomputed before opening a database transaction",
            ):
                store.remember(
                    memory_kind="semantic",
                    namespace=namespace,
                    content="a slow document embedding must not hold this transaction",
                    provenance=Provenance(
                        "pytest",
                        "evidence:transaction-bound-embedding",
                        "enforce external-call transaction boundary",
                    ),
                )

    assert provider.document_calls == 0
    with connect(database_url()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE namespace = %s",
            (namespace,),
        ).fetchone() == (0,)


@requires_db
def test_remember_accepts_precomputed_embedding_inside_caller_transaction():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider, embedding_profile
    from hindsight.memory import MemoryStore, Provenance

    provider = DeterministicEmbeddingProvider()
    content = "document embedding was prepared before the governed write"
    prepared_embedding = provider.embed_document(content)
    namespace = f"precomputed-embedding-{uuid4()}"
    with connect(database_url()) as conn:
        with conn.transaction():
            memory = MemoryStore(conn=conn, embedding_provider=provider).remember(
                memory_kind="semantic",
                namespace=namespace,
                content=content,
                provenance=Provenance(
                    "pytest",
                    "evidence:precomputed-embedding",
                    "commit a validated precomputed vector atomically",
                ),
                precomputed_embedding=prepared_embedding,
            )
            vector = conn.execute(
                "SELECT profile_id FROM semantic_memory_vectors WHERE memory_id = %s",
                (memory["id"],),
            ).fetchone()

    assert vector == (embedding_profile(provider).profile_id,)


def test_historical_read_url_uses_caller_connection_when_url_is_not_supplied():
    from hindsight.memory import MemoryStore

    class FakeInfo:
        dsn = "caller-supplied-dsn"

    class FakeConnection:
        info = FakeInfo()

    store = MemoryStore.__new__(MemoryStore)
    store._url = None
    store._conn = FakeConnection()

    assert store._historical_read_url() == "caller-supplied-dsn"


def test_historical_read_url_prefers_explicit_store_url():
    from hindsight.memory import MemoryStore

    class FakeInfo:
        dsn = "caller-supplied-dsn"

    class FakeConnection:
        info = FakeInfo()

    store = MemoryStore.__new__(MemoryStore)
    store._url = "explicit-database-url"
    store._conn = FakeConnection()

    assert store._historical_read_url() == "explicit-database-url"


@requires_db
def test_direct_semantic_write_supports_autocommit_connections():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    conn = connect(database_url())
    namespace = f"incident-{uuid4()}"
    conn.autocommit = True
    try:
        store = MemoryStore(
            conn,
            url=database_url(),
            embedding_provider=DeterministicEmbeddingProvider(),
        )
        memory = store.write_semantic(
            namespace=namespace,
            content="payment timeout in worker",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:autocommit-vector-write",
                justification="Direct autocommit semantic write remains supported",
            ),
        )

        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE id = %s",
            (memory["id"],),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM semantic_memory_embeddings WHERE memory_id = %s",
            (memory["id"],),
        ).fetchone() == (1,)
    finally:
        conn.execute("DELETE FROM semantic_memory_embeddings WHERE namespace = %s", (namespace,))
        conn.execute("DELETE FROM semantic_memories WHERE namespace = %s", (namespace,))
        conn.close()


@requires_db
def test_current_semantic_rejects_blank_namespace(memory_store):
    from hindsight.memory import ProvenanceError

    with pytest.raises(ProvenanceError, match="namespace is required"):
        memory_store.current_semantic(namespace="")


@requires_db
def test_as_of_recall_does_not_commit_caller_transaction():
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore, Provenance

    db_url = database_url()
    conn = connect(db_url)
    namespace = f"incident-{uuid4()}"
    try:
        store = MemoryStore(conn, url=db_url)
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

        recalled = store.recall(
            mode="as_of_list", namespace=namespace, query="", as_of=as_of, limit=5
        )

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
def test_historical_projection_hides_unknown_and_marks_future_invalidation_current():
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"historical-projection-{uuid4()}"
    with MemoryStore(url=database_url()) as store:
        unknown = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="future invalidation is not known yet",
            provenance=Provenance("pytest", "evidence:unknown", "historical projection"),
        )
    with connect() as conn:
        before_invalidation = conn.execute("SELECT now()").fetchone()[0]
    sleep(0.02)
    with MemoryStore(url=database_url()) as store:
        store.invalidate(
            memory_id=str(unknown["id"]),
            actor="pytest",
            reason="invalidate after historical snapshot",
        )
        scheduled = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="known invalidation takes effect later",
            provenance=Provenance("pytest", "evidence:scheduled", "scheduled invalidation"),
        )
        with connect() as conn:
            valid_at = conn.execute("SELECT now()").fetchone()[0]
        future_valid_time = valid_at + timedelta(hours=1)
        store.invalidate(
            memory_id=str(scheduled["id"]),
            actor="pytest",
            reason="scheduled invalidation",
            t_invalid=future_valid_time,
        )
    with connect() as conn:
        system_after_schedule = conn.execute("SELECT now()").fetchone()[0]

    with MemoryStore(url=database_url()) as store:
        old_rows = store.list_semantic_as_of(
            namespace=namespace,
            system_as_of=before_invalidation,
            valid_at=before_invalidation,
        )
        scheduled_rows = store.search_semantic_text_as_of(
            namespace=namespace,
            query="known invalidation",
            system_as_of=system_after_schedule,
            valid_at=valid_at,
        )

    old = next(row for row in old_rows if row["id"] == unknown["id"])
    assert old["snapshot_invalidated"] is False
    assert old["t_invalid"] is None
    assert old["invalidated_at"] is None
    assert old["invalidation_reason"] is None
    assert len(scheduled_rows) == 1
    assert scheduled_rows[0]["snapshot_invalidated"] is False
    assert scheduled_rows[0]["t_invalid"] == future_valid_time
    assert scheduled_rows[0]["invalidated_at"] is not None


@requires_db
def test_historical_semantic_reads_return_mutable_fields_from_mvcc_snapshot():
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"historical-mutable-fields-{uuid4()}"
    with MemoryStore(url=database_url()) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeout requires queue depth review",
            provenance=Provenance(
                "pytest",
                "evidence:historical-mutable-fields",
                "Verify historical mutable fields come from one MVCC snapshot",
            ),
        )
    with connect() as conn:
        system_as_of = conn.execute("SELECT now()").fetchone()[0]
    sleep(0.05)
    with connect() as conn:
        conn.execute(
            """
                UPDATE semantic_memories
                SET trust_status = 'review_required',
                    lineage_status = 'legacy_unverified'
                WHERE id = %s
            """,
            (memory["id"],),
        )

    with MemoryStore(url=database_url()) as store:
        valid_at = system_as_of + timedelta(minutes=5)
        listed = store.list_semantic_as_of(
            namespace=namespace,
            system_as_of=system_as_of,
            valid_at=valid_at,
        )
        searched = store.search_semantic_text_as_of(
            namespace=namespace,
            query="queue depth",
            system_as_of=system_as_of,
            valid_at=valid_at,
        )
        current = store.audit_memory(memory_kind="semantic", memory_id=str(memory["id"]))

    assert [(row["trust_status"], row["lineage_status"]) for row in listed] == [
        ("active", "complete")
    ]
    assert [(row["trust_status"], row["lineage_status"]) for row in searched] == [
        ("active", "complete")
    ]
    assert current is not None
    assert current["trust_status"] == "review_required"
    assert current["lineage_status"] == "legacy_unverified"


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


@requires_db
def test_vector_recall_commits_read_tracking_when_store_owns_connection():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"incident-{uuid4()}"
    decision_id = f"decision-{uuid4()}"
    with MemoryStore(
        url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    ) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="payment timeout in worker",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref="incident:owned-store-read-tracking",
                justification="Relevant prior incident",
            ),
        )

    with MemoryStore(
        url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    ) as store:
        recalled = store.recall_semantic(
            namespace=namespace,
            query="payment timeout",
            decision_id=decision_id,
            reader="agent.recall",
            purpose="retrieve memory for planning",
        )

    with MemoryStore(url=database_url()) as verifier:
        reads = verifier.reads_for_decision(decision_id=decision_id)
        verifier.invalidate(
            memory_id=str(memory["id"]),
            actor="pytest.cleanup",
            reason="Clean up committed read-tracking regression fixture",
        )

    assert [row["id"] for row in recalled] == [memory["id"]]
    assert [row["memory_id"] for row in reads] == [memory["id"]]
