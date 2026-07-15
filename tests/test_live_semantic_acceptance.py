"""Opt-in live Gemini acceptance for semantic retrieval and lesson publication."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

requires_live_gemini = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEMINI_ACCEPTANCE") != "1"
    or not os.environ.get("DATABASE_URL"),
    reason="live Gemini acceptance and an isolated DATABASE_URL are required",
)


def _providers():
    from hindsight.embeddings import embedding_provider_from_env
    from hindsight.gemini import gemini_pool_from_env
    from hindsight.reasoning import reasoning_provider_from_env

    pool = gemini_pool_from_env()
    embeddings = embedding_provider_from_env(gemini_pool=pool)
    reasoning = reasoning_provider_from_env(gemini_pool=pool)
    assert embeddings.provider_name == "gemini"
    assert embeddings.capability == "semantic"
    assert reasoning.provider_name == "gemini"
    return embeddings, reasoning


@requires_live_gemini
def test_live_gemini_database_retrieval_discriminates_paraphrase_and_no_match():
    from hindsight.db import database_url
    from hindsight.memory import MemoryStore, Provenance

    embeddings, _reasoning = _providers()
    namespace = f"live-semantic-retrieval:{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=embeddings) as store:
        profile = store.active_embedding_profile()
        assert profile.provider == "gemini"
        assert profile.capability == "semantic"
        assert profile.max_distance == pytest.approx(0.35)

        relevant = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content=(
                "When checkout latency follows downstream processor failures that "
                "multiply retries, inspect dependency health and reduce retry fanout "
                "before adding workers."
            ),
            provenance=Provenance(
                "live.acceptance",
                "calibration:payment-retry",
                "Low-overlap semantic retrieval acceptance",
            ),
        )
        for source_ref, content in (
            (
                "distractor:payment-tls",
                "When card gateway certificate expiration breaks checkout, rotate the "
                "TLS certificate and restart edge connections.",
            ),
            (
                "distractor:payment-capacity",
                "When checkout traffic rises while processors remain healthy, add "
                "worker capacity and rebalance consumers.",
            ),
            (
                "distractor:database-storage",
                "When database writes fail because disks are full, expand storage and "
                "compact old partitions.",
            ),
        ):
            store.remember(
                memory_kind="semantic",
                namespace=namespace,
                content=content,
                provenance=Provenance(
                    "live.acceptance",
                    source_ref,
                    "Hard retrieval distractor",
                ),
            )

        decision_id = f"live-retrieval:{uuid4()}"
        result = store.retrieve_semantic(
            namespace=namespace,
            query=(
                "Purchases freeze whenever the remote acquirer hesitates, and each "
                "failed attempt creates even more work."
            ),
            decision_id=decision_id,
            reader="live.acceptance",
            purpose="Verify low-overlap semantic discrimination",
            policy="semantic_strict",
            limit=1,
        )
        store.seal_decision(decision_id=decision_id)

        assert result.selected_strategy == "semantic_vector"
        assert [str(hit["id"]) for hit in result.hits] == [str(relevant["id"])]
        assert result.hits[0]["distance"] < profile.max_distance

        no_match_decision_id = f"live-no-match:{uuid4()}"
        no_match = store.retrieve_semantic(
            namespace=namespace,
            query="A scheduled analytics export lost columns after a report schema change.",
            decision_id=no_match_decision_id,
            reader="live.acceptance",
            purpose="Verify strict semantic misses stay empty",
            policy="semantic_strict",
            limit=1,
        )
        store.seal_decision(decision_id=no_match_decision_id)

        assert no_match.hits == ()
        assert no_match.selected_strategy is None


@pytest.mark.parametrize(
    ("simulator_kind", "query", "target", "distractors"),
    [
        (
            "cache_stampede",
            "At a predictable boundary every frontend simultaneously asks the origin "
            "for the same absent entry.",
            "When synchronized expiry makes many nodes recompute one object, collapse "
            "concurrent refresh work and jitter freshness windows before adding capacity.",
            (
                "At a predictable boundary every frontend asks the origin for the same "
                "absent entry, so add origin servers and leave expiry unchanged.",
                "When the same absent entry reaches every frontend, purge the whole cache "
                "at the predictable boundary and retry each request independently.",
            ),
        ),
        (
            "connection_leak",
            "Requests wait forever while database handles only move upward following "
            "failed work.",
            "If error exits retain database sessions, trace ownership, isolate the faulty "
            "cohort, and enforce unconditional resource release instead of enlarging the pool.",
            (
                "When requests wait for database handles after failed work, double the pool "
                "limit so more handles can move upward.",
                "Restart the database whenever requests wait forever after failed work, "
                "without tracing which code owns each handle.",
            ),
        ),
        (
            "hot_partition",
            "Only one shard burns while its peers idle whenever a dominant customer sends "
            "events.",
            "When one routing key concentrates writes on a single range, add deterministic "
            "key entropy and targeted admission control rather than scaling idle peers.",
            (
                "When one shard burns and peers idle for a dominant customer, add consumers "
                "to every shard without changing event routing.",
                "Move the dominant customer to the largest existing shard whenever one shard "
                "burns, leaving the event key unchanged.",
            ),
        ),
    ],
)
@requires_live_gemini
def test_live_gemini_cutoff_generalizes_across_calibration_mechanisms(
    simulator_kind: str,
    query: str,
    target: str,
    distractors: tuple[str, ...],
):
    from hindsight.db import database_url
    from hindsight.memory import MemoryStore, Provenance

    embeddings, _reasoning = _providers()
    namespace = f"live-calibration:{simulator_kind}:{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=embeddings) as store:
        relevant = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content=target,
            provenance=Provenance(
                "live.acceptance",
                f"calibration:{simulator_kind}:target",
                "Independent multi-mechanism cutoff calibration",
            ),
        )
        for index, content in enumerate(distractors, start=1):
            store.remember(
                memory_kind="semantic",
                namespace=namespace,
                content=content,
                provenance=Provenance(
                    "live.acceptance",
                    f"calibration:{simulator_kind}:distractor:{index}",
                    "Hard lexical calibration distractor",
                ),
            )
        decision_id = f"live-calibration:{uuid4()}"
        retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=query,
            decision_id=decision_id,
            reader="live.acceptance",
            purpose="Verify the frozen cutoff across an independent mechanism",
            policy="semantic_strict",
            limit=1,
        )
        store.seal_decision(decision_id=decision_id)

    assert [str(hit["id"]) for hit in retrieval.hits] == [str(relevant["id"])]
    assert retrieval.hits[0]["distance"] < 0.35


@requires_live_gemini
def test_live_gemini_consolidation_publishes_cited_retrievable_lesson():
    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.runs import create_incident, resolve_incident

    embeddings, reasoning = _providers()
    token = uuid4()
    evidence_namespace = f"live-consolidation-evidence:{token}"
    lesson_namespace = f"live-consolidation-lesson:{token}"
    slug = f"live-consolidation:{token}"
    incident = create_incident(
        slug=slug,
        title="Checkout stalls under cascading downstream pressure",
        severity="sev2",
        summary="Purchases stall in waves when downstream work begins to multiply.",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url(), embedding_provider=embeddings) as store:
        source = store.remember(
            memory_kind="semantic",
            namespace=evidence_namespace,
            content=(
                "Checkout latency rose as processor timeouts caused retry amplification "
                "and queue growth."
            ),
            provenance=Provenance(
                "live.acceptance",
                f"incident:{incident['id']}:summary",
                "Verified source evidence for live consolidation",
            ),
            content_schema="incident_summary.v1",
            structured_payload={"incident_id": str(incident["id"])},
        )
        store.remember(
            memory_kind="semantic",
            namespace=lesson_namespace,
            content=(
                "When card gateway certificate expiration breaks checkout, rotate the "
                "TLS certificate and restart edge connections."
            ),
            provenance=Provenance(
                "live.acceptance",
                "distractor:payment-tls",
                "Hard lexical distractor for published lesson",
            ),
        )
    with connect(database_url()) as conn:
        conn.execute(
            """
                INSERT INTO incident_semantic_memories (incident_id, memory_id, relationship)
                VALUES (%s, %s, 'summary')
            """,
            (incident["id"], source["id"]),
        )
        conn.commit()
    resolution = resolve_incident(
        slug=slug,
        root_cause="Retry amplification overloaded an unhealthy downstream processor.",
        action="Inspect processor health and throttle retry fanout before adding workers.",
        observation="Timeout rate and queue depth recovered after retry fanout was throttled.",
        recovered=True,
        actor="live.acceptance",
        db_url=database_url(),
    )

    result = consolidate_resolved_incident(
        incident_id=str(resolution["incident"]["id"]),
        namespace=lesson_namespace,
        db_url=database_url(),
        reasoning_provider=reasoning,
        embedding_provider=embeddings,
    )

    assert result.created is True
    assert result.memory is not None
    assert result.memory["content_schema"] == "procedural_lesson.v1"
    assert result.memory["trust_status"] == "active"
    claims = result.memory["structured_payload"]["claims"]
    assert claims
    assert all(claim["citations"] for claim in claims)
    assert str(source["id"]) in result.source_memory_ids

    decision_id = f"live-lesson-retrieval:{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=embeddings) as store:
        retrieval = store.retrieve_semantic(
            namespace=lesson_namespace,
            query=(
                "Purchases freeze whenever the remote acquirer hesitates, and repeated "
                "attempts compound the backlog."
            ),
            decision_id=decision_id,
            reader="live.acceptance",
            purpose="Verify the generated lesson is semantically retrievable",
            policy="semantic_strict",
            limit=1,
        )
        store.seal_decision(decision_id=decision_id)
    assert [str(hit["id"]) for hit in retrieval.hits] == [str(result.memory["id"])]

    with connect(database_url()) as conn:
        read_ids = {
            str(row[0])
            for row in conn.execute(
                """
                    SELECT semantic_memory_id
                    FROM memory_reads
                    WHERE decision_id = %s AND memory_kind = 'semantic'
                """,
                (f"consolidation:{result.job_id}",),
            ).fetchall()
        }
    assert str(source["id"]) in read_ids
