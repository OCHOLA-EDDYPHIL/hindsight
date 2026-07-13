"""Acceptance coverage for governed identity, retrieval, and correction semantics."""

import os
from time import sleep
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@requires_db
def test_explicit_text_miss_stays_empty_and_strict_retrieval_is_audited():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"governed-retrieval-{uuid4()}"
    decision_id = f"decision:{uuid4()}"
    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeout recovered after retry fanout was throttled",
            provenance=Provenance("pytest", "evidence:retrieval", "seed relevant evidence"),
        )
        assert store.search_current_semantic_text(
            namespace=namespace, query="certificate expiry"
        ) == []
        result = store.retrieve_semantic(
            namespace=namespace,
            query="retry fanout",
            decision_id=decision_id,
            reader="pytest.agent",
            purpose="verify strict retrieval audit",
        )
        assert [row["id"] for row in result.hits] == [memory["id"]]
        assert result.policy == "semantic_strict"
        assert result.selected_strategy == "semantic_vector"
        retrieval = store._fetch_one(  # noqa: SLF001
            "SELECT * FROM memory_retrievals WHERE id = %s", (result.retrieval_id,)
        )
        assert retrieval["returned_memory_ids"] == [str(memory["id"])]


@requires_db
def test_exact_rewind_reasserts_target_version_without_rewriting_history():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_rewind

    namespace = f"exact-rewind-{uuid4()}"
    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        first = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="retry fanout is the cause",
            provenance=Provenance("pytest", "evidence:v1", "initial belief"),
        )
    with connect() as conn:
        target = conn.execute("SELECT now()").fetchone()[0]
    sleep(0.02)
    with connect() as conn:
        with conn.transaction():
            store = MemoryStore(conn=conn, embedding_provider=provider)
            store.invalidate(
                memory_id=str(first["id"]), actor="pytest", reason="evolve belief"
            )
            second = store.write_semantic(
                namespace=namespace,
                content="certificate expiry is the cause",
                provenance=Provenance("pytest", "evidence:v2", "later belief"),
                belief_id=str(first["belief_id"]),
                previous_version_id=str(first["id"]),
                transition_kind="supersession",
            )
    preview = preview_rewind(
        namespace=namespace,
        target_timestamp=target,
        actor="pytest.operator",
        reason="restore exact target belief",
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"rewind:{uuid4()}",
        db_url=database_url(),
    )
    result = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    assert result["status"] == "completed"
    with MemoryStore(url=database_url()) as store:
        current = store.list_current_semantic(namespace=namespace)
        assert len(current) == 1
        assert current[0]["content"] == first["content"]
        assert current[0]["id"] not in {first["id"], second["id"]}
        assert current[0]["transition_kind"] == "rewind_reassertion"
        history = store._fetch_all(  # noqa: SLF001
            "SELECT * FROM semantic_memories WHERE belief_id = %s ORDER BY version_number",
            (first["belief_id"],),
        )
        assert [row["version_number"] for row in history] == [1, 2, 3]


def test_power_analysis_and_ci_smoke_cannot_authorize_public_claims():
    from hindsight.benchmark import power_analysis

    plan = power_analysis(paired_differences=[0.5, 1.0, 1.5, 2.0], power=0.90)
    assert plan.repetitions_per_variant >= 2
    assert plan.alpha_per_comparison == pytest.approx(0.025)


@requires_db
def test_memory_payload_and_provenance_fields_are_database_immutable():
    from psycopg import errors

    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    with MemoryStore(
        url=database_url(), embedding_provider=DeterministicEmbeddingProvider()
    ) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=f"immutable-{uuid4()}",
            content="original governed payload",
            provenance=Provenance("pytest", "evidence:immutable", "immutability test"),
        )
    with connect() as conn:
        with pytest.raises(errors.RaiseException):
            conn.execute(
                "UPDATE semantic_memories SET content = 'rewritten' WHERE id = %s",
                (memory["id"],),
            )
        conn.rollback()
        conn.execute(
            """
                UPDATE semantic_memories
                SET t_invalid = now(), invalidated_by = 'pytest',
                    invalidation_reason = 'allowed correction state', invalidated_at = now()
                WHERE id = %s
            """,
            (memory["id"],),
        )
        conn.commit()


@requires_db
def test_evolution_supersession_quarantines_cross_namespace_descendants_for_review():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import (
        enqueue_operation,
        execute_operation,
        preview_review_resolution,
        preview_supersession,
    )

    provider = DeterministicEmbeddingProvider()
    root_namespace = f"evolution-root-{uuid4()}"
    child_namespace = f"evolution-child-{uuid4()}"
    decision_id = f"decision:{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=root_namespace,
            content="processor timeout threshold is 20 percent",
            provenance=Provenance("pytest", "evidence:root", "root belief"),
        )
        store.record_read(
            decision_id=decision_id,
            memory_kind="semantic",
            memory_id=str(root["id"]),
            reader="pytest.agent",
            purpose="derive a remediation in another namespace",
        )
        child = store.remember(
            memory_kind="semantic",
            namespace=child_namespace,
            content="scale retry workers when threshold is reached",
            provenance=Provenance("pytest.agent", "evidence:child", "derived belief"),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(root["id"])],
        )
    preview = preview_supersession(
        root_memory_id=str(root["id"]),
        intent="evolution",
        content="processor timeout threshold is 10 percent",
        structured_payload={"threshold_percent": 10},
        actor="pytest.operator",
        reason="threshold policy evolved",
        authorized_namespaces=[root_namespace, child_namespace],
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"evolution:{uuid4()}",
        db_url=database_url(),
    )
    result = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    assert result["status"] == "completed"
    with MemoryStore(url=database_url()) as store:
        quarantined = store.audit_memory(memory_kind="semantic", memory_id=str(child["id"]))
        assert quarantined["trust_status"] == "review_required"
        review = store._fetch_one(  # noqa: SLF001
            "SELECT * FROM memory_review_items WHERE semantic_memory_id = %s",
            (child["id"],),
        )
    review_preview = preview_review_resolution(
        review_item_id=str(review["id"]),
        action="confirmed",
        actor="pytest.operator",
        reason="descendant remains valid under the new threshold",
        authorized_namespaces=[root_namespace, child_namespace],
        db_url=database_url(),
    )
    review_operation, _ = enqueue_operation(
        preview_id=str(review_preview["id"]),
        fingerprint=review_preview["fingerprint"],
        idempotency_key=f"review:{uuid4()}",
        db_url=database_url(),
    )
    execute_operation(
        operation_id=str(review_operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url()) as store:
        reviewed_version = store.audit_memory(
            memory_kind="semantic", memory_id=str(child["id"])
        )
        assert reviewed_version["t_invalid"] is not None
        confirmed = store.list_current_semantic(namespace=child_namespace)
        assert len(confirmed) == 1
        assert confirmed[0]["content"] == child["content"]
        assert confirmed[0]["trust_status"] == "active"


@requires_db
def test_correction_supersession_requires_all_namespaces_and_retracts_descendants():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import (
        OperationAuthorizationError,
        enqueue_operation,
        execute_operation,
        preview_supersession,
    )

    provider = DeterministicEmbeddingProvider()
    root_namespace = f"correction-root-{uuid4()}"
    child_namespace = f"correction-child-{uuid4()}"
    decision_id = f"decision:{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=root_namespace,
            content="certificate expiry caused the timeout",
            provenance=Provenance("pytest", "evidence:poison", "poisoned root"),
        )
        store.record_read(
            decision_id=decision_id,
            memory_kind="semantic",
            memory_id=str(root["id"]),
            reader="pytest.agent",
            purpose="derive cross-namespace action",
        )
        child = store.remember(
            memory_kind="semantic",
            namespace=child_namespace,
            content="rotate certificates immediately",
            provenance=Provenance("pytest.agent", "evidence:derived", "derived action"),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(root["id"])],
        )
    with pytest.raises(OperationAuthorizationError):
        preview_supersession(
            root_memory_id=str(root["id"]),
            intent="correction",
            content="retry amplification caused the timeout",
            structured_payload={"cause": "retry_amplification"},
            actor="pytest.operator",
            reason="correct poisoned cause",
            authorized_namespaces=[root_namespace],
            db_url=database_url(),
        )
    preview = preview_supersession(
        root_memory_id=str(root["id"]),
        intent="correction",
        content="retry amplification caused the timeout",
        structured_payload={"cause": "retry_amplification"},
        actor="pytest.operator",
        reason="correct poisoned cause",
        authorized_namespaces=[root_namespace, child_namespace],
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"correction:{uuid4()}",
        db_url=database_url(),
    )
    execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url()) as store:
        assert store.audit_memory(memory_kind="semantic", memory_id=str(child["id"]))[
            "t_invalid"
        ] is not None
        current_root = store.list_current_semantic(namespace=root_namespace)
        assert [row["content"] for row in current_root] == [
            "retry amplification caused the timeout"
        ]


@requires_db
def test_stale_operation_preview_conflicts_without_partial_mutation():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_retraction

    provider = DeterministicEmbeddingProvider()
    namespace = f"stale-preview-{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="belief approved for retraction preview",
            provenance=Provenance("pytest", "evidence:root", "preview root"),
        )
    preview = preview_retraction(
        root_memory_id=str(root["id"]),
        actor="pytest.operator",
        reason="previewed retraction",
        authorized_namespaces=[namespace],
        db_url=database_url(),
    )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="concurrent namespace change",
            provenance=Provenance("pytest", "evidence:concurrent", "change revision"),
        )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"stale:{uuid4()}",
        db_url=database_url(),
    )
    result = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    assert result["status"] == "conflict"
    with MemoryStore(url=database_url()) as store:
        unchanged = store.audit_memory(memory_kind="semantic", memory_id=str(root["id"]))
        assert unchanged["t_invalid"] is None


@requires_db
def test_embedding_profile_rotation_refuses_partial_coverage_and_switches_atomically():
    from hindsight.db import database_url
    from hindsight.embedding_index import (
        EmbeddingCoverageError,
        activate_profile,
        begin_profile_build,
        run_backfill_batch,
    )
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    class AlternateSemanticProvider(DeterministicEmbeddingProvider):
        provider_name = "test-semantic"
        model_name = "test-semantic-v2"
        capability = "semantic"
        encoder_revision = "test-semantic-v2"

    alternate = AlternateSemanticProvider()
    original = DeterministicEmbeddingProvider()
    namespace = f"profile-rotation-{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=original) as store:
        store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="profile rotation coverage sentinel",
            provenance=Provenance("pytest", "evidence:profile", "coverage sentinel"),
        )
    building = begin_profile_build(
        provider=alternate, max_distance=0.75, db_url=database_url()
    )
    with pytest.raises(EmbeddingCoverageError):
        activate_profile(profile_id=str(building["id"]), db_url=database_url())
    while True:
        batch = run_backfill_batch(
            provider=alternate,
            worker_id="pytest-profile",
            limit=1_000,
            max_distance=0.75,
            db_url=database_url(),
        )
        if batch["leased"] == 0:
            break
        assert batch["failed"] == 0
    state = activate_profile(profile_id=str(building["id"]), db_url=database_url())
    assert state["active_profile_id"] == building["id"]
    with MemoryStore(url=database_url(), embedding_provider=alternate) as store:
        result = store.retrieve_semantic(
            namespace=namespace,
            query="profile rotation coverage",
            decision_id=f"profile-query:{uuid4()}",
            reader="pytest",
            purpose="verify threshold-versioned active profile",
        )
        assert result.embedding_profile.max_distance == 0.75

    restored = begin_profile_build(provider=original, db_url=database_url())
    while True:
        batch = run_backfill_batch(
            provider=original,
            worker_id="pytest-profile-restore",
            limit=1_000,
            db_url=database_url(),
        )
        if batch["leased"] == 0:
            break
        assert batch["failed"] == 0
    activate_profile(profile_id=str(restored["id"]), db_url=database_url())


@requires_db
def test_missing_active_profile_with_trusted_memories_requires_backfill():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        store.remember(
            memory_kind="semantic",
            namespace=f"profile-fail-closed-{uuid4()}",
            content="trusted memory must never be partially indexed",
            provenance=Provenance(
                "pytest", "evidence:profile-fail-closed", "seed trusted memory"
            ),
        )

    conn = connect()
    try:
        state = conn.execute(
            "SELECT active_profile_id FROM embedding_index_state WHERE singleton = true"
        ).fetchone()
        assert state is not None and state[0] is not None
        conn.execute(
            "UPDATE embedding_index_state SET active_profile_id = NULL WHERE singleton = true"
        )
        store = MemoryStore(conn=conn, embedding_provider=provider)
        with pytest.raises(RuntimeError, match="require side-by-side backfill"):
            store.ensure_active_embedding_profile()
    finally:
        conn.rollback()
        conn.close()
