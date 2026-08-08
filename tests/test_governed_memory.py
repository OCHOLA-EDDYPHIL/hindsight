"""Acceptance coverage for governed identity, retrieval, and correction semantics."""

import os
from time import sleep
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@requires_db
def test_positive_guidance_retrieval_filters_governance_before_vector_ranking():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import (
        APPROVED_POSITIVE_GUIDANCE,
        MemoryGovernance,
        MemoryStore,
        Provenance,
    )

    namespace = f"positive-guidance-boundary-{uuid4()}"
    provenance = Provenance("pytest", "evidence:governance", "exercise retrieval boundary")
    audit_governance = {
        "rejected": MemoryGovernance("rejected", "safe", "supported", "audit_only"),
        "unsafe": MemoryGovernance("approved", "unsafe", "supported", "audit_only"),
        "contradicted": MemoryGovernance(
            "approved", "safe", "contradicted", "audit_only"
        ),
    }
    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        approved = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="retry fanout guidance",
            provenance=provenance,
            governance=APPROVED_POSITIVE_GUIDANCE,
        )
        excluded = [
            store.remember(
                memory_kind="semantic",
                namespace=namespace,
                content=f"retry fanout guidance {label}",
                provenance=provenance,
                governance=governance,
            )
            for label, governance in audit_governance.items()
        ]
        excluded.append(
            store.remember(
                memory_kind="semantic",
                namespace=namespace,
                content="retry fanout guidance review required",
                provenance=provenance,
                governance=APPROVED_POSITIVE_GUIDANCE,
                trust_status="review_required",
            )
        )
        invalidated = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="retry fanout guidance invalidated",
            provenance=provenance,
            governance=APPROVED_POSITIVE_GUIDANCE,
        )
        store.invalidate(
            memory_kind="semantic",
            memory_id=str(invalidated["id"]),
            actor="pytest",
            reason="exercise invalidation boundary",
        )
        excluded.append(invalidated)

        result = store.retrieve_semantic(
            namespace=namespace,
            query="retry fanout guidance",
            decision_id=f"decision:{uuid4()}",
            reader="pytest.agent",
            purpose="verify positive-guidance governance filtering",
            positive_guidance_only=True,
        )

        assert [str(row["id"]) for row in result.hits] == [str(approved["id"])]
        assert all(
            store.audit_memory(memory_kind="semantic", memory_id=str(row["id"])) is not None
            for row in excluded
        )


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
        assert result.fallback_reason is None
        retrieval = store._fetch_one(  # noqa: SLF001
            "SELECT * FROM memory_retrievals WHERE id = %s", (result.retrieval_id,)
        )
        assert retrieval["returned_memory_ids"] == [str(memory["id"])]
        assert retrieval["fallback_reason"] is None


@requires_db
@pytest.mark.parametrize(
    ("vector_failure", "query", "expected_strategy", "expected_reason"),
    [
        (False, "retry fanout", "keyword", "semantic_vector_empty"),
        (True, "retry fanout", "keyword", "semantic_vector_error"),
        (False, "certificate expiry", None, "semantic_vector_empty"),
        (True, "certificate expiry", None, "semantic_vector_error"),
    ],
)
def test_degraded_retrieval_returns_and_persists_its_fallback_reason(
    monkeypatch, vector_failure, query, expected_strategy, expected_reason
):
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"retrieval-fallback-reason-{uuid4()}"
    decision_id = f"retrieval-fallback:{uuid4()}"
    with MemoryStore(
        url=database_url(), embedding_provider=DeterministicEmbeddingProvider()
    ) as store:
        memory = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeout recovered after retry fanout was throttled",
            provenance=Provenance(
                "pytest", "evidence:fallback", "seed explicit fallback test"
            ),
        )

        def vector_attempt(**_kwargs):
            if vector_failure:
                raise RuntimeError("simulated vector unavailability")
            return []

        monkeypatch.setattr(store, "search_semantic_vector", vector_attempt)
        result = store.retrieve_semantic(
            namespace=namespace,
            query=query,
            decision_id=decision_id,
            reader="pytest.agent",
            purpose="verify explicit fallback metadata",
            policy="semantic_then_keyword",
            limit=1,
        )
        store.seal_decision(decision_id=decision_id)
        audit = store._fetch_one(  # noqa: SLF001 - persisted retrieval contract
            "SELECT * FROM memory_retrievals WHERE id = %s", (result.retrieval_id,)
        )

    expected_hits = [memory["id"]] if expected_strategy == "keyword" else []
    assert [row["id"] for row in result.hits] == expected_hits
    assert result.selected_strategy == expected_strategy
    assert result.fallback_reason == expected_reason
    assert audit["selected_strategy"] == expected_strategy
    assert audit["fallback_reason"] == expected_reason
    assert audit["returned_memory_ids"] == [str(value) for value in expected_hits]


@requires_db
def test_strict_semantic_miss_returns_no_fallback_or_unrelated_rows(monkeypatch):
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"strict-empty-retrieval-{uuid4()}"
    decision_id = f"strict-empty:{uuid4()}"
    with MemoryStore(
        url=database_url(), embedding_provider=DeterministicEmbeddingProvider()
    ) as store:
        store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="an unrelated recent operational note",
            provenance=Provenance(
                "pytest", "evidence:unrelated", "seed unrelated current memory"
            ),
        )
        monkeypatch.setattr(store, "search_semantic_vector", lambda **_kwargs: [])
        result = store.retrieve_semantic(
            namespace=namespace,
            query="certificate expiry",
            decision_id=decision_id,
            reader="pytest.agent",
            purpose="verify strict empty retrieval",
            policy="semantic_strict",
            limit=5,
        )
        store.seal_decision(decision_id=decision_id)

    assert result.status == "empty"
    assert result.hits == ()
    assert result.selected_strategy is None
    assert result.fallback_reason is None


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


@requires_db
def test_exact_rewind_reasserts_a_target_version_invalidated_after_the_target():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_rewind

    namespace = f"exact-rewind-invalidated-target-{uuid4()}"
    actor = "pytest.operator"
    reason = "Restore the belief that was active at the approved target"
    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        source = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="retry amplification caused the processor backlog",
            provenance=Provenance("pytest", "evidence:target", "initial target belief"),
            content_schema="incident_root_cause.v1",
            structured_payload={"cause": "retry_amplification"},
        )
    with connect() as conn:
        target = conn.execute("SELECT now()").fetchone()[0]
    sleep(0.02)
    with MemoryStore(url=database_url()) as store:
        store.invalidate(
            memory_id=str(source["id"]),
            actor="pytest.correction",
            reason="Temporarily withdraw the target belief",
        )
        invalidated_source = store.audit_memory(
            memory_kind="semantic", memory_id=str(source["id"])
        )
    assert invalidated_source is not None
    assert invalidated_source["t_invalid"] is not None

    preview = preview_rewind(
        namespace=namespace,
        target_timestamp=target,
        actor=actor,
        reason=reason,
        db_url=database_url(),
    )
    assert preview["effect_payload"]["close_memory_ids"] == []
    assert preview["effect_payload"]["reassertions"] == [
        {
            "source_memory_id": str(source["id"]),
            "belief_id": str(source["belief_id"]),
            "previous_version_id": str(source["id"]),
        }
    ]
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"rewind-invalidated-target:{uuid4()}",
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
        history = store._fetch_all(  # noqa: SLF001 - exact version audit
            "SELECT * FROM semantic_memories WHERE belief_id = %s ORDER BY version_number",
            (source["belief_id"],),
        )
    assert len(current) == 1
    reasserted = current[0]
    assert str(reasserted["id"]) != str(source["id"])
    assert reasserted["belief_id"] == source["belief_id"]
    assert reasserted["version_number"] == 2
    assert reasserted["previous_version_id"] == source["id"]
    assert reasserted["transition_kind"] == "rewind_reassertion"
    assert str(reasserted["created_by_operation_id"]) == str(operation["id"])
    assert reasserted["content"] == source["content"]
    assert reasserted["content_schema"] == source["content_schema"]
    assert reasserted["structured_payload"] == source["structured_payload"]
    assert reasserted["source_ref"] == f"memory:{source['id']}"
    assert reasserted["writer"] == actor
    assert reasserted["justification"] == reason
    assert [row["version_number"] for row in history] == [1, 2]
    historical = history[0]
    for field in (
        "id",
        "belief_id",
        "version_number",
        "previous_version_id",
        "namespace",
        "content",
        "metadata",
        "t_valid",
        "t_invalid",
        "writer",
        "source_ref",
        "justification",
        "written_at",
        "producer_decision_id",
        "transition_kind",
        "content_schema",
        "structured_payload",
        "payload_digest",
    ):
        assert historical[field] == invalidated_source[field]

    decision_id = f"operation:{operation['id']}:reassert:{source['id']}"
    with connect() as conn:
        decision = conn.execute(
            "SELECT status, actor, decision_kind, purpose, namespace "
            "FROM memory_decisions WHERE id = %s",
            (decision_id,),
        ).fetchone()
        read = conn.execute(
            "SELECT id, decision_id, semantic_memory_id, reader, purpose "
            "FROM memory_reads WHERE decision_id = %s",
            (decision_id,),
        ).fetchone()
        edge = conn.execute(
            "SELECT child_semantic_memory_id, parent_read_id, producer_decision_id, "
            "edge_type, justification FROM memory_lineage_edges "
            "WHERE child_semantic_memory_id = %s",
            (reasserted["id"],),
        ).fetchone()
        effect = conn.execute(
            "SELECT effect_type, source_memory_id, result_memory_id, belief_id "
            "FROM memory_operation_effects WHERE operation_id = %s",
            (operation["id"],),
        ).fetchone()
        operation_row = conn.execute(
            "SELECT status, invalidated_memory_ids, restored_memory_ids "
            "FROM memory_operations WHERE id = %s",
            (operation["id"],),
        ).fetchone()

    assert decision == ("sealed", actor, "rewind_reassertion", reason, namespace)
    assert read[1:] == (
        decision_id,
        source["id"],
        actor,
        "Reassert exact target logical belief",
    )
    assert edge == (
        reasserted["id"],
        read[0],
        decision_id,
        "reasserted_from",
        "Reassert exact target logical belief",
    )
    assert effect == (
        "reasserted",
        source["id"],
        reasserted["id"],
        source["belief_id"],
    )
    assert operation_row == ("completed", [], [str(reasserted["id"])])


def test_memory_store_has_no_direct_rewind_mutation_surface():
    from hindsight.memory import MemoryStore

    assert not hasattr(MemoryStore, "rewind")
    assert not hasattr(MemoryStore, "preview_rewind")


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
def test_terminal_producer_decisions_reject_all_new_outputs():
    from psycopg import errors

    from hindsight.db import connect
    from hindsight.memory import MemoryStore, Provenance, ProvenanceError

    with connect() as conn:
        for status in ("sealed", "failed"):
            decision_id = f"terminal-producer:{status}:{uuid4()}"
            with conn.transaction():
                store = MemoryStore(conn=conn)
                store.open_decision(
                    decision_id=decision_id,
                    actor="pytest",
                    decision_kind="test_output",
                    purpose="verify terminal producer rejection",
                )
                store.seal_decision(
                    decision_id=decision_id, failed=status == "failed"
                )
            store = MemoryStore(conn=conn)
            with pytest.raises(ProvenanceError, match="decision is not open"):
                store.remember(
                    memory_kind="semantic",
                    namespace=f"terminal-{uuid4()}",
                    content="must not be written",
                    provenance=Provenance(
                        "pytest", "evidence:terminal", "terminal decision test"
                    ),
                    producer_decision_id=decision_id,
                )
            with pytest.raises(ProvenanceError, match="decision is not open"):
                store.remember(
                    memory_kind="episodic",
                    episode_id=f"terminal-{uuid4()}",
                    role="assistant",
                    content="must not be written",
                    provenance=Provenance(
                        "pytest", "evidence:terminal", "terminal decision test"
                    ),
                    producer_decision_id=decision_id,
                )
            assert conn.execute(
                "SELECT count(*) FROM semantic_memories WHERE producer_decision_id = %s",
                (decision_id,),
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT count(*) FROM episodic_memories WHERE producer_decision_id = %s",
                (decision_id,),
            ).fetchone() == (0,)
            with pytest.raises(errors.RaiseException, match="must be open"):
                conn.execute(
                    """
                        INSERT INTO episodic_memories (
                            episode_id, role, content, writer, source_ref, justification,
                            producer_decision_id, content_schema, structured_payload,
                            payload_digest, lineage_status, trust_status
                        ) VALUES (%s, 'assistant', 'direct SQL output', 'pytest',
                                  'evidence:direct', 'trigger test', %s,
                                  'episodic.v1', '{}'::JSONB, 'digest', 'complete', 'active')
                    """,
                    (f"terminal-direct-{uuid4()}", decision_id),
                )
            conn.rollback()


@requires_db
@pytest.mark.parametrize("memory_kind", ["semantic", "episodic"])
def test_lineage_edge_producer_must_own_the_child_memory(memory_kind):
    from psycopg import errors

    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    namespace = f"lineage-child-owner-{memory_kind}-{uuid4()}"
    producer_decision = f"lineage-producer:{uuid4()}"
    mismatched_decision = f"lineage-mismatch:{uuid4()}"
    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        parent = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeouts amplified retry pressure",
            provenance=Provenance("pytest", "evidence:parent", "lineage parent"),
        )
        store.record_read(
            decision_id=producer_decision,
            memory_kind="semantic",
            memory_id=str(parent["id"]),
            reader="pytest.producer",
            purpose="produce the governed child",
        )
        child_args = {
            "memory_kind": memory_kind,
            "content": "reduce retry fanout before adding capacity",
            "provenance": Provenance(
                "pytest.producer", "evidence:child", "derived child"
            ),
            "producer_decision_id": producer_decision,
            "parent_memory_ids": [str(parent["id"])],
        }
        if memory_kind == "semantic":
            child_args["namespace"] = namespace
        else:
            child_args.update({"episode_id": namespace, "role": "assistant"})
        child = store.remember(**child_args)
        mismatched_read = store.record_read(
            decision_id=mismatched_decision,
            memory_kind="semantic",
            memory_id=str(parent["id"]),
            reader="pytest.mismatch",
            purpose="attempt a mismatched lineage edge",
        )

    child_column = (
        "child_semantic_memory_id"
        if memory_kind == "semantic"
        else "child_episodic_memory_id"
    )
    constraint = f"memory_lineage_{memory_kind}_child_producer_fk"
    with connect() as conn:
        original_count = conn.execute(
            f"SELECT count(*) FROM memory_lineage_edges WHERE {child_column} = %s",
            (child["id"],),
        ).fetchone()[0]
        with pytest.raises(errors.ForeignKeyViolation, match=constraint):
            conn.execute(
                f"""
                    INSERT INTO memory_lineage_edges (
                        {child_column}, parent_read_id, producer_decision_id,
                        edge_type, justification
                    ) VALUES (%s, %s, %s, 'context', 'mismatched producer test')
                """,
                (child["id"], mismatched_read["id"], mismatched_decision),
            )
        conn.rollback()
        assert conn.execute(
            f"SELECT count(*) FROM memory_lineage_edges WHERE {child_column} = %s",
            (child["id"],),
        ).fetchone() == (original_count,)


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
def test_review_retraction_resolves_every_closed_descendant_item():
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
    namespaces = [f"review-closure-{index}-{uuid4()}" for index in range(3)]
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=namespaces[0],
            content="root threshold",
            provenance=Provenance("pytest", "evidence:root", "root"),
        )
        parent = root
        descendants = []
        for index in range(1, 3):
            decision_id = f"review-chain:{uuid4()}"
            store.record_read(
                decision_id=decision_id,
                memory_kind="semantic",
                memory_id=str(parent["id"]),
                reader="pytest",
                purpose="derive review chain",
            )
            parent = store.remember(
                memory_kind="semantic",
                namespace=namespaces[index],
                content=f"descendant {index}",
                provenance=Provenance("pytest", f"evidence:{index}", "descendant"),
                producer_decision_id=decision_id,
                parent_memory_ids=[str(parent["id"])],
            )
            descendants.append(parent)
    evolution = preview_supersession(
        root_memory_id=str(root["id"]),
        intent="evolution",
        content="evolved root threshold",
        structured_payload={"threshold": "evolved"},
        actor="pytest.operator",
        reason="evolve root",
        authorized_namespaces=namespaces,
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(evolution["id"]),
        fingerprint=evolution["fingerprint"],
        idempotency_key=f"review-evolution:{uuid4()}",
        db_url=database_url(),
    )
    execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url()) as store:
        items = store._fetch_all(  # noqa: SLF001
            """
                SELECT * FROM memory_review_items
                WHERE semantic_memory_id = ANY(%s) ORDER BY semantic_memory_id
            """,
            ([str(row["id"]) for row in descendants],),
        )
    selected = next(
        row for row in items if str(row["semantic_memory_id"]) == str(descendants[0]["id"])
    )
    preview = preview_review_resolution(
        review_item_id=str(selected["id"]),
        action="retracted",
        actor="pytest.operator",
        reason="retract invalid descendant chain",
        authorized_namespaces=namespaces,
        db_url=database_url(),
    )
    retraction, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"review-retraction:{uuid4()}",
        db_url=database_url(),
    )
    result = execute_operation(
        operation_id=str(retraction["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    assert result["status"] == "completed"
    with MemoryStore(url=database_url()) as store:
        resolved = store._fetch_all(  # noqa: SLF001
            "SELECT * FROM memory_review_items WHERE id = ANY(%s) ORDER BY id",
            ([str(row["id"]) for row in items],),
        )
        assert {row["status"] for row in resolved} == {"retracted"}
        assert {str(row["resolution_operation_id"]) for row in resolved} == {
            str(retraction["id"])
        }
        for descendant in descendants:
            assert store.audit_memory(
                memory_kind="semantic", memory_id=str(descendant["id"])
            )["t_invalid"] is not None


@requires_db
def test_review_retraction_rejects_an_already_closed_reviewed_memory():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import (
        OperationConflictError,
        enqueue_operation,
        execute_operation,
        preview_review_resolution,
        preview_supersession,
    )

    provider = DeterministicEmbeddingProvider()
    namespaces = [f"stale-review-{index}-{uuid4()}" for index in range(2)]
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=namespaces[0],
            content="root threshold",
            provenance=Provenance("pytest", "evidence:root", "root"),
        )
    decision_id = f"stale-review-child:{uuid4()}"
    with MemoryStore(url=database_url()) as store:
        store.record_read(
            decision_id=decision_id,
            memory_kind="semantic",
            memory_id=str(root["id"]),
            reader="pytest",
            purpose="derive reviewed child",
        )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        child = store.remember(
            memory_kind="semantic",
            namespace=namespaces[1],
            content="derived threshold",
            provenance=Provenance("pytest", "evidence:child", "child"),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(root["id"])],
        )
    evolution = preview_supersession(
        root_memory_id=str(root["id"]),
        intent="evolution",
        content="evolved root threshold",
        structured_payload={"threshold": "evolved"},
        actor="pytest.operator",
        reason="evolve root",
        authorized_namespaces=namespaces,
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(evolution["id"]),
        fingerprint=evolution["fingerprint"],
        idempotency_key=f"stale-review-evolution:{uuid4()}",
        db_url=database_url(),
    )
    execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url()) as store:
        item = store._fetch_one(  # noqa: SLF001
            "SELECT * FROM memory_review_items WHERE semantic_memory_id = %s",
            (child["id"],),
        )
    with MemoryStore(url=database_url()) as store:
        store.invalidate(
            memory_id=str(child["id"]),
            actor="pytest.operator",
            reason="closed outside the review flow",
        )

    with pytest.raises(OperationConflictError, match="no longer current"):
        preview_review_resolution(
            review_item_id=str(item["id"]),
            action="retracted",
            actor="pytest.operator",
            reason="reject stale review action",
            authorized_namespaces=namespaces,
            db_url=database_url(),
        )
    with MemoryStore(url=database_url()) as store:
        unchanged = store._fetch_one(  # noqa: SLF001
            "SELECT status FROM memory_review_items WHERE id = %s",
            (item["id"],),
        )
        assert unchanged["status"] == "open"


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
def test_empty_rewind_preview_locks_the_first_namespace_revision():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_rewind

    provider = DeterministicEmbeddingProvider()
    namespace = f"empty-rewind-{uuid4()}"
    with connect() as conn:
        target = conn.execute("SELECT now()").fetchone()[0]
    sleep(0.02)
    preview = preview_rewind(
        namespace=namespace,
        target_timestamp=target,
        actor="pytest.operator",
        reason="preview an empty namespace",
        db_url=database_url(),
    )
    assert preview["expected_revisions"] == {namespace: 0}

    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        late = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="first write after the empty preview",
            provenance=Provenance("pytest", "evidence:late", "late first write"),
        )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"empty-rewind:{uuid4()}",
        db_url=database_url(),
    )
    result = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    assert result["status"] == "conflict"
    assert result["failure_detail"] == "namespace revision changed after preview"
    with MemoryStore(url=database_url()) as store:
        assert store.audit_memory(
            memory_kind="semantic", memory_id=str(late["id"])
        )["t_invalid"] is None


@requires_db
def test_cross_namespace_descendant_after_preview_conflicts_via_parent_revision():
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_retraction

    provider = DeterministicEmbeddingProvider()
    root_namespace = f"preview-root-{uuid4()}"
    child_namespace = f"preview-child-{uuid4()}"
    future_namespace = f"preview-future-{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=root_namespace,
            content="root belief",
            provenance=Provenance("pytest", "evidence:root", "root"),
        )
        child_decision = f"preview-child:{uuid4()}"
        store.record_read(
            decision_id=child_decision,
            memory_kind="semantic",
            memory_id=str(root["id"]),
            reader="pytest",
            purpose="derive child",
        )
        child = store.remember(
            memory_kind="semantic",
            namespace=child_namespace,
            content="child belief",
            provenance=Provenance("pytest", "evidence:child", "child"),
            producer_decision_id=child_decision,
            parent_memory_ids=[str(root["id"])],
        )
    preview = preview_retraction(
        root_memory_id=str(root["id"]),
        actor="pytest.operator",
        reason="preview causal closure",
        authorized_namespaces=[root_namespace, child_namespace],
        db_url=database_url(),
    )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        grandchild_decision = f"preview-grandchild:{uuid4()}"
        store.record_read(
            decision_id=grandchild_decision,
            memory_kind="semantic",
            memory_id=str(child["id"]),
            reader="pytest",
            purpose="derive after preview",
        )
        grandchild = store.remember(
            memory_kind="semantic",
            namespace=future_namespace,
            content="late descendant",
            provenance=Provenance("pytest", "evidence:late", "late descendant"),
            producer_decision_id=grandchild_decision,
            parent_memory_ids=[str(child["id"])],
        )
    with MemoryStore(url=database_url()) as store:
        child_revision = store._fetch_one(  # noqa: SLF001
            "SELECT revision FROM memory_namespaces WHERE namespace = %s",
            (child_namespace,),
        )["revision"]
    assert child_revision > preview["expected_revisions"][child_namespace]
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"preview-cross-namespace:{uuid4()}",
        db_url=database_url(),
    )
    result = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="pytest",
        db_url=database_url(),
    )
    assert result["status"] == "conflict"
    assert result["failure_detail"] == "namespace revision changed after preview"
    with MemoryStore(url=database_url()) as store:
        for memory in (root, child, grandchild):
            assert store.audit_memory(
                memory_kind="semantic", memory_id=str(memory["id"])
            )["t_invalid"] is None


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
