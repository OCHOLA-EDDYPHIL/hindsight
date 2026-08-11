"""Regression coverage for durable governed-operation attempts and fencing."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def test_operation_source_reads_close_before_document_embedding(monkeypatch):
    import hindsight.operations as operations

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return None

        def execute(self, _query, _params):
            return None

        def fetchall(self):
            return [{"id": "memory-1", "content": "source memory"}]

    class Connection:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            self.closed = True

        def cursor(self, **_kwargs):
            return Cursor()

    connection = Connection()

    class Provider:
        def embed_document(self, _text):
            assert connection.closed
            return [0.0]

    monkeypatch.setattr(operations, "connect", lambda *_args, **_kwargs: connection)
    prepared = operations._precompute_embeddings(  # noqa: SLF001
        preview={
            "effect_payload": {
                "reassertions": [{"source_memory_id": "memory-1"}],
            },
            "request_payload": {},
        },
        provider=Provider(),
        db_url="postgresql://unused",
    )

    assert prepared == {"memory-1": [0.0]}


def test_enqueue_operation_bounds_serialization_retries(monkeypatch):
    import hindsight.operations as operations
    from psycopg.errors import SerializationFailure

    expected = ({"id": "winning-operation"}, False)
    attempts = 0
    actors = []

    def retry_once(**kwargs):
        nonlocal attempts
        attempts += 1
        actors.append(kwargs["actor"])
        if attempts == 1:
            raise SerializationFailure("unique-key race")
        return expected

    monkeypatch.setattr(operations, "_enqueue_operation_once", retry_once)
    assert (
        operations.enqueue_operation(
            preview_id="preview-1",
            fingerprint="fingerprint-1",
            idempotency_key="request-1",
            actor="  pytest.approver  ",
            db_url="postgresql://unused",
        )
        == expected
    )
    assert attempts == 2
    assert actors == ["pytest.approver", "pytest.approver"]

    attempts = 0

    def always_retry(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise SerializationFailure("restart transaction")

    monkeypatch.setattr(operations, "_enqueue_operation_once", always_retry)
    with pytest.raises(SerializationFailure, match="restart transaction"):
        operations.enqueue_operation(
            preview_id="preview-1",
            fingerprint="fingerprint-1",
            idempotency_key="request-1",
            db_url="postgresql://unused",
        )
    assert attempts == operations.MAX_OPERATION_TRANSACTION_ATTEMPTS

    attempts = 0

    def not_retryable(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("not retryable")

    monkeypatch.setattr(operations, "_enqueue_operation_once", not_retryable)
    with pytest.raises(RuntimeError, match="not retryable"):
        operations.enqueue_operation(
            preview_id="preview-1",
            fingerprint="fingerprint-1",
            idempotency_key="request-1",
            db_url="postgresql://unused",
        )
    assert attempts == 1


def _enqueue_supersession():
    from hindsight.db import database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, preview_supersession

    provider = DeterministicEmbeddingProvider()
    namespace = f"operation-retry-{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="certificate expiry caused the timeout",
            provenance=Provenance("pytest", "evidence:root", "seed governed operation"),
        )
    preview = preview_supersession(
        root_memory_id=str(root["id"]),
        intent="correction",
        content="retry amplification caused the timeout",
        structured_payload={"cause": "retry_amplification"},
        actor="pytest.operator",
        reason="correct the incident cause",
        authorized_namespaces=[namespace],
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"operation-retry:{uuid4()}",
        db_url=database_url(),
    )
    return provider, root, operation


def _expire_lease(operation_id: str) -> None:
    from hindsight.db import connect

    with connect() as conn:
        conn.execute(
            """
                UPDATE memory_operations
                SET lease_expires_at = now() - INTERVAL '1 second'
                WHERE id = %s
            """,
            (operation_id,),
        )


@requires_db
def test_precompute_failures_persist_three_attempts_and_reach_failed():
    from hindsight.db import database_url
    from hindsight.memory import MemoryStore
    from hindsight.operations import execute_operation, get_operation

    class FailingEmbeddingProvider:
        provider_name = "test"
        model_name = "precompute-failure-v1"

        def __init__(self):
            self.calls = 0

        def embed_document(self, _text):
            self.calls += 1
            raise RuntimeError("temporary embedding outage")

        def embed_query(self, _text):
            raise AssertionError("query embeddings are not expected")

    _provider, root, operation = _enqueue_supersession()
    failing = FailingEmbeddingProvider()
    expected_statuses = ["retrying", "retrying", "failed"]

    for attempt, expected_status in enumerate(expected_statuses, start=1):
        with pytest.raises(RuntimeError, match="temporary embedding outage"):
            execute_operation(
                operation_id=str(operation["id"]),
                embedding_provider=failing,
                worker_id="pytest-operation-worker",
                db_url=database_url(),
            )
        persisted = get_operation(
            operation_id=str(operation["id"]), db_url=database_url()
        )
        assert persisted["status"] == expected_status
        assert persisted["attempt_count"] == attempt
        assert persisted["lease_owner"] is None
        assert persisted["lease_expires_at"] is None
        assert persisted["failure_code"] == "RuntimeError"

    terminal = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=failing,
        worker_id="pytest-operation-worker",
        db_url=database_url(),
    )
    assert terminal["status"] == "failed"
    assert terminal["attempt_count"] == 3
    assert terminal["completed_at"] is not None
    assert failing.calls == 3
    assert [event["status"] for event in terminal["events"]] == [
        "queued",
        "leased",
        "retrying",
        "leased",
        "retrying",
        "leased",
        "failed",
    ]
    assert [
        event["metadata"]["attempt"]
        for event in terminal["events"]
        if event["status"] == "leased"
    ] == [1, 2, 3]
    assert [
        event["metadata"]["attempt"]
        for event in terminal["events"]
        if event["status"] in {"retrying", "failed"}
    ] == [1, 2, 3]
    assert terminal["effects"] == []
    with MemoryStore(url=database_url()) as store:
        assert store.audit_memory(
            memory_kind="semantic", memory_id=str(root["id"])
        )["t_invalid"] is None


@requires_db
def test_provider_factory_failure_is_recorded_after_durable_claim():
    from hindsight.db import database_url
    from hindsight.operations import execute_operation, get_operation

    _provider, _root, operation = _enqueue_supersession()

    def unavailable_provider():
        raise RuntimeError("embedding provider configuration unavailable")

    with pytest.raises(RuntimeError, match="configuration unavailable"):
        execute_operation(
            operation_id=str(operation["id"]),
            embedding_provider_factory=unavailable_provider,
            worker_id="pytest-provider-factory",
            db_url=database_url(),
        )

    persisted = get_operation(operation_id=str(operation["id"]), db_url=database_url())
    assert persisted["status"] == "retrying"
    assert persisted["attempt_count"] == 1
    assert persisted["failure_code"] == "RuntimeError"
    assert [event["status"] for event in persisted["events"]] == [
        "queued",
        "leased",
        "retrying",
    ]


@requires_db
def test_application_rollback_keeps_attempt_and_retry_can_complete(monkeypatch):
    import hindsight.operations as operations
    from hindsight.db import database_url
    from hindsight.memory import MemoryStore

    provider, root, operation = _enqueue_supersession()
    original_apply = operations._apply_supersession

    def fail_application(**_kwargs):
        raise RuntimeError("temporary database-side failure")

    monkeypatch.setattr(operations, "_apply_supersession", fail_application)
    with pytest.raises(RuntimeError, match="database-side failure"):
        operations.execute_operation(
            operation_id=str(operation["id"]),
            embedding_provider=provider,
            worker_id="pytest-operation-worker",
            db_url=database_url(),
        )

    retrying = operations.get_operation(
        operation_id=str(operation["id"]), db_url=database_url()
    )
    assert retrying["status"] == "retrying"
    assert retrying["attempt_count"] == 1
    assert retrying["effects"] == []
    with MemoryStore(url=database_url()) as store:
        assert store.audit_memory(
            memory_kind="semantic", memory_id=str(root["id"])
        )["t_invalid"] is None

    monkeypatch.setattr(operations, "_apply_supersession", original_apply)
    completed = operations.execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest-operation-worker",
        db_url=database_url(),
    )
    assert completed["status"] == "completed"
    assert completed["attempt_count"] == 2
    assert completed["lease_owner"] is None
    assert completed["lease_expires_at"] is None
    assert completed["failure_code"] is None
    assert completed["failure_detail"] is None
    assert [event["status"] for event in completed["events"]] == [
        "queued",
        "leased",
        "retrying",
        "leased",
        "completed",
    ]


@requires_db
def test_serialization_failure_retries_whole_transaction_without_duplicate_rows(
    monkeypatch,
):
    import hindsight.operations as operations
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore

    provider, root, operation = _enqueue_supersession()
    operation_id = str(operation["id"])
    original_embed = provider.embed_document
    original_insert = MemoryStore._insert_semantic_embedding
    embedding_calls = 0
    insertion_attempts = 0

    def count_embedding(text):
        nonlocal embedding_calls
        embedding_calls += 1
        return original_embed(text)

    def force_first_retry(store, **kwargs):
        nonlocal insertion_attempts
        insertion_attempts += 1
        if insertion_attempts == 1:
            store._conn.execute(  # noqa: SLF001 - inject a real Cockroach retry
                "SELECT crdb_internal.force_retry('1h'::INTERVAL)"
            ).fetchone()
        return original_insert(store, **kwargs)

    monkeypatch.setattr(provider, "embed_document", count_embedding)
    monkeypatch.setattr(MemoryStore, "_insert_semantic_embedding", force_first_retry)

    completed = operations.execute_operation(
        operation_id=operation_id,
        embedding_provider=provider,
        worker_id="pytest-serialization-retry",
        db_url=database_url(),
    )

    assert completed["status"] == "completed"
    assert completed["attempt_count"] == 1
    assert embedding_calls == 1
    assert insertion_attempts == 2
    assert [event["status"] for event in completed["events"]] == [
        "queued",
        "leased",
        "completed",
    ]
    assert [effect["effect_type"] for effect in completed["effects"]] == [
        "closed",
        "created",
    ]
    created_id = str(completed["effects"][1]["result_memory_id"])
    decision_id = f"operation:{operation_id}:supersede"
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE created_by_operation_id = %s",
            (operation_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM semantic_memory_embeddings WHERE memory_id = %s",
            (created_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM semantic_memory_vectors WHERE memory_id = %s",
            (created_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM memory_decisions WHERE id = %s", (decision_id,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM memory_reads WHERE decision_id = %s", (decision_id,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM memory_lineage_edges WHERE child_semantic_memory_id = %s",
            (created_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM memory_operation_effects WHERE operation_id = %s",
            (operation_id,),
        ).fetchone() == (2,)
        assert conn.execute(
            """
                SELECT count(*) FROM memory_operation_events
                WHERE operation_id = %s AND status = 'completed'
            """,
            (operation_id,),
        ).fetchone() == (1,)
    with MemoryStore(url=database_url()) as store:
        assert store.audit_memory(
            memory_kind="semantic", memory_id=str(root["id"])
        )["t_invalid"] is not None


@requires_db
def test_exhausted_transaction_retries_enter_existing_operation_retry(monkeypatch):
    import hindsight.operations as operations
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore
    from psycopg import errors

    provider, root, operation = _enqueue_supersession()
    operation_id = str(operation["id"])
    original_embed = provider.embed_document
    original_insert = MemoryStore._insert_semantic_embedding
    embedding_calls = 0
    insertion_attempts = 0

    def count_embedding(text):
        nonlocal embedding_calls
        embedding_calls += 1
        return original_embed(text)

    def force_every_retry(store, **_kwargs):
        nonlocal insertion_attempts
        insertion_attempts += 1
        store._conn.execute(  # noqa: SLF001 - inject a real Cockroach retry
            "SELECT crdb_internal.force_retry('1h'::INTERVAL)"
        ).fetchone()

    monkeypatch.setattr(provider, "embed_document", count_embedding)
    monkeypatch.setattr(MemoryStore, "_insert_semantic_embedding", force_every_retry)

    with pytest.raises(errors.SerializationFailure):
        operations.execute_operation(
            operation_id=operation_id,
            embedding_provider=provider,
            worker_id="pytest-serialization-exhaustion",
            db_url=database_url(),
        )

    retrying = operations.get_operation(operation_id=operation_id, db_url=database_url())
    assert retrying["status"] == "retrying"
    assert retrying["attempt_count"] == 1
    assert retrying["failure_code"] == "SerializationFailure"
    assert retrying["effects"] == []
    assert embedding_calls == 1
    assert insertion_attempts == operations.MAX_OPERATION_TRANSACTION_ATTEMPTS
    assert [event["status"] for event in retrying["events"]] == [
        "queued",
        "leased",
        "retrying",
    ]
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE created_by_operation_id = %s",
            (operation_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM memory_operation_effects WHERE operation_id = %s",
            (operation_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM memory_operation_events WHERE operation_id = %s",
            (operation_id,),
        ).fetchone() == (3,)
    with MemoryStore(url=database_url()) as store:
        assert store.audit_memory(
            memory_kind="semantic", memory_id=str(root["id"])
        )["t_invalid"] is None

    monkeypatch.setattr(MemoryStore, "_insert_semantic_embedding", original_insert)
    completed = operations.execute_operation(
        operation_id=operation_id,
        embedding_provider=provider,
        worker_id="pytest-serialization-exhaustion",
        db_url=database_url(),
    )
    assert completed["status"] == "completed"
    assert completed["attempt_count"] == 2
    assert embedding_calls == 2


@requires_db
def test_same_worker_cannot_reenter_and_stale_lease_token_cannot_transition():
    import hindsight.operations as operations
    from hindsight.db import database_url

    _provider, _root, operation = _enqueue_supersession()
    operation_id = str(operation["id"])
    first, first_token = operations._claim_operation(
        operation_id=operation_id,
        worker_id="same-worker",
        db_url=database_url(),
    )
    assert first["attempt_count"] == 1
    assert first_token is not None

    active, duplicate_token = operations._claim_operation(
        operation_id=operation_id,
        worker_id="same-worker",
        db_url=database_url(),
    )
    assert duplicate_token is None
    assert active["attempt_count"] == 1
    assert active["lease_owner"] == first_token

    _expire_lease(operation_id)
    operations._mark_retry(
        operation_id=operation_id,
        lease_token=first_token,
        exc=RuntimeError("slow precompute failed after lease expiry"),
        db_url=database_url(),
    )
    retrying = operations.get_operation(
        operation_id=operation_id, db_url=database_url()
    )
    assert retrying["status"] == "retrying"
    assert retrying["attempt_count"] == 1

    replacement, replacement_token = operations._claim_operation(
        operation_id=operation_id,
        worker_id="same-worker",
        db_url=database_url(),
    )
    assert replacement["attempt_count"] == 2
    assert replacement_token is not None
    assert replacement_token != first_token

    with pytest.raises(operations._OperationLeaseLostError):
        operations._mark_retry(
            operation_id=operation_id,
            lease_token=first_token,
            exc=RuntimeError("stale attempt failed"),
            db_url=database_url(),
        )
    current = operations.get_operation(operation_id=operation_id, db_url=database_url())
    assert current["status"] == "leased"
    assert current["attempt_count"] == 2
    assert current["lease_owner"] == replacement_token

    operations._mark_retry(
        operation_id=operation_id,
        lease_token=replacement_token,
        exc=RuntimeError("replacement cleanup"),
        db_url=database_url(),
    )


@requires_db
def test_expired_third_attempt_is_reaped_without_creating_attempt_four():
    import hindsight.operations as operations
    from hindsight.db import database_url

    _provider, _root, operation = _enqueue_supersession()
    operation_id = str(operation["id"])
    tokens = []
    for _attempt in range(operations.MAX_OPERATION_ATTEMPTS):
        claimed, token = operations._claim_operation(
            operation_id=operation_id,
            worker_id="crashing-worker",
            db_url=database_url(),
        )
        tokens.append(token)
        assert claimed["attempt_count"] == len(tokens)
        _expire_lease(operation_id)

    assert operations.reap_exhausted_operations(db_url=database_url()) == {"failed": 1}
    terminal = operations.get_operation(operation_id=operation_id, db_url=database_url())
    terminal_token = terminal["lease_owner"]
    assert terminal_token is None
    assert terminal["status"] == "failed"
    assert terminal["attempt_count"] == 3
    assert terminal["failure_code"] == "OperationAttemptExpired"
    assert terminal["lease_owner"] is None
    assert terminal["lease_expires_at"] is None
    assert len(set(tokens)) == 3
    persisted = operations.get_operation(
        operation_id=operation_id, db_url=database_url()
    )
    assert [event["status"] for event in persisted["events"]] == [
        "queued",
        "leased",
        "leased",
        "leased",
        "failed",
    ]
    assert persisted["events"][-1]["metadata"] == {
        "attempt": 3,
        "failure_code": "OperationAttemptExpired",
    }


@requires_db
def test_stale_preview_conflict_is_fenced_and_audited():
    from hindsight.db import database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import (
        enqueue_operation,
        execute_operation,
        preview_retraction,
    )

    provider = DeterministicEmbeddingProvider()
    namespace = f"operation-conflict-{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="previewed root",
            provenance=Provenance("pytest", "evidence:root", "root"),
        )
    preview = preview_retraction(
        root_memory_id=str(root["id"]),
        actor="pytest.operator",
        reason="retract previewed root",
        authorized_namespaces=[namespace],
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"operation-conflict:{uuid4()}",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="concurrent namespace write",
            provenance=Provenance("pytest", "evidence:late", "invalidate preview"),
        )

    result = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest-conflict-worker",
        db_url=database_url(),
    )
    assert result["status"] == "conflict"
    assert result["attempt_count"] == 1
    assert result["lease_owner"] is None
    assert result["lease_expires_at"] is None
    assert [event["status"] for event in result["events"]] == [
        "queued",
        "leased",
        "conflict",
    ]
    assert result["events"][-1]["metadata"]["attempt"] == 1
    assert result["events"][-1]["metadata"]["failure_code"] == "stale_preview"
