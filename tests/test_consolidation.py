"""Tests for resolved-incident consolidation."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def test_incident_changefeed_handler_ignores_non_resolved_rows(monkeypatch):
    import hindsight.consolidation as consolidation

    calls = []

    monkeypatch.setattr(
        consolidation,
        "consolidate_resolved_incident",
        lambda **kwargs: calls.append(kwargs),
    )

    results = consolidation.handle_incident_changefeed_event(
        {
            "table": "incidents",
            "value": {
                "after": {
                    "id": str(uuid4()),
                    "slug": "incident-open",
                    "status": "open",
                }
            },
        }
    )

    assert results == []
    assert calls == []


def test_incident_changefeed_handler_consolidates_resolved_after_row(monkeypatch):
    import hindsight.consolidation as consolidation
    from hindsight.consolidation import ConsolidationResult

    incident_id = uuid4()
    calls = []

    def enqueue(**kwargs):
        calls.append(kwargs)
        return {"id": "job-1"}

    def process(**kwargs):
        return ConsolidationResult(
            incident={"id": incident_id, "slug": "incident-resolved"},
            namespace="demo:cross",
            memory={"id": uuid4()},
            created=True,
        )

    monkeypatch.setattr(consolidation, "enqueue_consolidation_job", enqueue)
    monkeypatch.setattr(consolidation, "process_consolidation_job", process)

    results = consolidation.handle_incident_changefeed_event(
        {
            "table": "incidents",
            "value": {
                "before": {"status": "open"},
                "after": {
                    "id": str(incident_id),
                    "slug": "incident-resolved",
                    "status": "resolved",
                }
            },
            "source_event_id": "event-1",
        },
        db_url="postgresql://db",
    )

    assert len(results) == 1
    assert results[0].created is True
    assert calls == [
        {
            "incident_id": str(incident_id),
            "source_event_id": "event-1",
            "db_url": "postgresql://db",
        }
    ]


def test_lesson_parser_accepts_bare_or_single_fenced_json_without_prose():
    from hindsight.consolidation import LessonValidationError, _parse_lesson_response

    payload = '{"schema_version":1,"title":"Retry lesson","claims":[]}'
    expected = {
        "schema_version": 1,
        "title": "Retry lesson",
        "claims": [],
    }

    assert _parse_lesson_response(payload) == expected
    assert _parse_lesson_response(f"```json\n{payload}\n```") == expected
    assert _parse_lesson_response(f"```\n{payload}\n```") == expected

    for invalid in (
        f"Here is the lesson:\n{payload}",
        f"```json\n{payload}\n```\nExtra explanation",
        "[]",
    ):
        with pytest.raises(LessonValidationError):
            _parse_lesson_response(invalid)


@requires_db
def test_consolidation_writes_idempotent_lesson_with_provenance():
    import hindsight.consolidation as consolidation
    from hindsight.cross_episode import (
        ROOT_CAUSE,
        open_demo_incident,
        resolve_demo_incident,
    )
    from hindsight.db import connect, database_url

    namespace = f"consolidation-test-{uuid4()}"
    incident = open_demo_incident(
        label="episode-one",
        namespace=namespace,
        summary="checkout latency rose with retry fanout",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )

    first = consolidation.consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
    )
    second = consolidation.consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
    )

    assert first.created is True
    assert first.namespace == namespace
    assert first.memory is not None
    assert first.memory["writer"] == "consolidation.worker"
    assert first.memory["source_ref"] == f"incident_event:{resolved['resolution_event_id']}"
    assert ROOT_CAUSE in first.memory["content"]
    assert first.memory["content_schema"] == "procedural_lesson.v1"
    assert second.created is False
    assert second.reason == "lesson already exists"
    assert second.memory is not None
    assert second.memory["id"] == first.memory["id"]
    with connect() as conn:
        conn.execute(
            """
                UPDATE semantic_memories
                SET trust_status = 'review_required'
                WHERE id = %s
            """,
            (first.memory["id"],),
        )
        assert consolidation._existing_lesson(  # noqa: SLF001 - idempotency regression
            conn,
            incident_id=resolved["id"],
            namespace=namespace,
        ) is None


@requires_db
def test_invalid_model_citation_publishes_no_lesson_and_records_terminal_reason():
    import json

    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import ReasoningResponse

    class InvalidCitationProvider:
        provider_name = "test-model"
        model_name = "invalid-citation-v1"

        def generate(self, request):
            return ReasoningResponse(
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "title": "Invalid lesson",
                        "claims": [
                            {
                                "kind": "safe_action",
                                "text": "uncited action",
                                "citations": [
                                    {"evidence_id": "memory:missing", "quote": "fabricated"}
                                ],
                            }
                        ],
                    }
                ),
                provider=self.provider_name,
                model=self.model_name,
            )

    namespace = f"invalid-lesson-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="retry pressure caused processor timeouts",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    result = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
        reasoning_provider=InvalidCitationProvider(),
        embedding_provider=DeterministicEmbeddingProvider(),
    )
    assert result.memory is None
    assert result.created is False
    assert result.reason.startswith("invalid_lesson:")


@requires_db
@pytest.mark.parametrize("governance_state", ["invalidated", "review_required"])
def test_consolidation_rejects_governed_invalid_source_evidence(governance_state):
    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider

    namespace = f"consolidation-unsafe-source-{governance_state}-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="governed-invalid evidence must not produce a lesson",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    with connect() as conn:
        source_id = conn.execute(
            """
                SELECT memory_id FROM incident_semantic_memories
                WHERE incident_id = %s AND relationship = 'summary'
            """,
            (resolved["id"],),
        ).fetchone()[0]
        if governance_state == "invalidated":
            conn.execute(
                """
                    UPDATE semantic_memories
                    SET t_invalid = now(), invalidated_by = 'test.governance',
                        invalidation_reason = 'unsafe consolidation fixture',
                        invalidated_at = now()
                    WHERE id = %s
                """,
                (source_id,),
            )
        else:
            conn.execute(
                """
                    UPDATE semantic_memories
                    SET trust_status = 'review_required'
                    WHERE id = %s
                """,
                (source_id,),
            )

    result = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert result.created is False
    assert result.memory is None
    assert result.reason == "no eligible semantic source evidence"
    with connect() as conn:
        assert conn.execute(
            """
                SELECT count(*)
                FROM incident_semantic_memories AS link
                JOIN semantic_memories AS memory ON memory.id = link.memory_id
                WHERE link.incident_id = %s AND link.relationship = 'lesson'
                    AND memory.writer = 'consolidation.worker'
            """,
            (resolved["id"],),
        ).fetchone() == (0,)


@requires_db
def test_consolidation_excludes_quarantined_rows_from_mixed_source_evidence():
    import json

    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.reasoning import ReasoningResponse

    class CaptureEvidenceProvider:
        provider_name = "test-model"
        model_name = "capture-eligible-evidence-v1"

        def __init__(self):
            self.evidence_ids = set()

        def generate(self, request):
            prompt = json.loads(request.prompt)
            self.evidence_ids = set(prompt["evidence"])
            evidence_id = next(
                key for key in prompt["evidence"] if key.startswith("memory:")
            )
            return ReasoningResponse(
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "title": "Eligible evidence only",
                        "claims": [
                            {
                                "kind": "situation",
                                "text": "Use only current trusted evidence",
                                "citations": [
                                    {
                                        "evidence_id": evidence_id,
                                        "quote": prompt["evidence"][evidence_id],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                provider=self.provider_name,
                model=self.model_name,
            )

    namespace = f"consolidation-mixed-sources-{uuid4()}"
    trusted_namespace = f"{namespace}:trusted"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="one trusted and one quarantined source",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    with connect() as conn:
        with conn.transaction():
            quarantined_id = conn.execute(
                """
                    SELECT memory_id FROM incident_semantic_memories
                    WHERE incident_id = %s AND relationship = 'summary'
                """,
                (resolved["id"],),
            ).fetchone()[0]
            trusted = MemoryStore(
                conn=conn,
                embedding_provider=DeterministicEmbeddingProvider(),
            ).remember(
                memory_kind="semantic",
                namespace=trusted_namespace,
                content="This current trusted source is eligible for synthesis.",
                provenance=Provenance(
                    writer="test.fixture",
                    source_ref=f"test:{uuid4()}",
                    justification="Create mixed consolidation evidence",
                ),
            )
            conn.execute(
                """
                    INSERT INTO incident_semantic_memories (
                        incident_id, memory_id, relationship
                    ) VALUES (%s, %s, 'root_cause')
                """,
                (resolved["id"], trusted["id"]),
            )
            conn.execute(
                """
                    UPDATE semantic_memories
                    SET trust_status = 'review_required'
                    WHERE id = %s
                """,
                (quarantined_id,),
            )

    provider = CaptureEvidenceProvider()
    result = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
        reasoning_provider=provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert result.created is True
    assert result.namespace == trusted_namespace
    assert f"memory:{quarantined_id}" not in provider.evidence_ids
    assert result.source_memory_ids is not None
    assert result.source_memory_ids == [str(trusted["id"])]
    with connect() as conn:
        assert conn.execute(
            """
                SELECT count(*) FROM memory_reads
                WHERE decision_id = %s AND semantic_memory_id = %s
            """,
            (f"consolidation:{result.job_id}", quarantined_id),
        ).fetchone() == (0,)


@requires_db
def test_governance_change_during_synthesis_prevents_lesson_publication():
    import json

    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import ReasoningResponse

    class QuarantineDuringSynthesisProvider:
        provider_name = "test-model"
        model_name = "quarantine-during-synthesis-v1"

        def generate(self, request):
            prompt = json.loads(request.prompt)
            evidence_id = next(
                key for key in prompt["evidence"] if key.startswith("memory:")
            )
            memory_id = evidence_id.removeprefix("memory:")
            with connect() as conn:
                conn.execute(
                    """
                        UPDATE semantic_memories
                        SET trust_status = 'review_required'
                        WHERE id = %s
                    """,
                    (memory_id,),
                )
            return ReasoningResponse(
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "title": "Stale synthesis",
                        "claims": [
                            {
                                "kind": "situation",
                                "text": "This output must be rejected",
                                "citations": [
                                    {
                                        "evidence_id": evidence_id,
                                        "quote": prompt["evidence"][evidence_id],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                provider=self.provider_name,
                model=self.model_name,
            )

    namespace = f"consolidation-mid-synthesis-governance-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="evidence becomes unsafe while the model is running",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    result = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
        reasoning_provider=QuarantineDuringSynthesisProvider(),
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert result.created is False
    assert result.memory is None
    assert result.reason == "source evidence changed during synthesis"
    with connect() as conn:
        job = conn.execute(
            """
                SELECT status, decision_id FROM consolidation_jobs
                WHERE id = %s
            """,
            (result.job_id,),
        ).fetchone()
        assert job[0] == "not_eligible"
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s",
            (job[1],),
        ).fetchone() == ("failed",)
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE producer_decision_id = %s",
            (job[1],),
        ).fetchone() == (0,)


@requires_db
def test_transient_consolidation_failure_reuses_open_decision_and_recovers():
    import json

    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import ReasoningResponse

    class FailOnceProvider:
        provider_name = "test-model"
        model_name = "fail-once-v1"

        def __init__(self):
            self.calls = 0

        def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient model outage")
            prompt = json.loads(request.prompt)
            evidence_id, quote = next(iter(prompt["evidence"].items()))
            return ReasoningResponse(
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "title": "Recovered lesson",
                        "claims": [
                            {
                                "kind": "situation",
                                "text": "Use verified incident evidence",
                                "citations": [
                                    {"evidence_id": evidence_id, "quote": quote}
                                ],
                            }
                        ],
                    }
                ),
                provider=self.provider_name,
                model=self.model_name,
            )

    namespace = f"consolidation-retry-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="retry pressure caused processor timeouts",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    provider = FailOnceProvider()
    with pytest.raises(RuntimeError, match="transient model outage"):
        consolidate_resolved_incident(
            incident_id=str(resolved["id"]),
            db_url=database_url(),
            reasoning_provider=provider,
            embedding_provider=DeterministicEmbeddingProvider(),
        )
    with connect() as conn:
        job = conn.execute(
            """
                SELECT id, status, decision_id, lease_owner, lease_expires_at
                FROM consolidation_jobs WHERE incident_id = %s
            """,
            (resolved["id"],),
        ).fetchone()
        decision = conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (job[2],)
        ).fetchone()
        read_count = conn.execute(
            "SELECT count(*) FROM memory_reads WHERE decision_id = %s", (job[2],)
        ).fetchone()[0]
    assert job[1] == "retrying"
    assert job[3:] == (None, None)
    assert decision == ("open",)

    result = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
        reasoning_provider=provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )
    assert result.created is True
    with connect() as conn:
        assert conn.execute(
            """
                SELECT status, lease_owner, lease_expires_at
                FROM consolidation_jobs WHERE id = %s
            """,
            (job[0],),
        ).fetchone() == ("completed", None, None)
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (job[2],)
        ).fetchone() == ("sealed",)
        assert conn.execute(
            "SELECT count(*) FROM memory_reads WHERE decision_id = %s", (job[2],)
        ).fetchone() == (read_count,)


@requires_db
def test_consolidation_exhaustion_fails_job_and_decision_together():
    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider

    class AlwaysFailProvider:
        provider_name = "test-model"
        model_name = "always-fail-v1"

        def generate(self, request):
            raise RuntimeError("persistent model outage")

    namespace = f"consolidation-exhaustion-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="persistent retry pressure",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    for _ in range(3):
        with pytest.raises(RuntimeError, match="persistent model outage"):
            consolidate_resolved_incident(
                incident_id=str(resolved["id"]),
                db_url=database_url(),
                reasoning_provider=AlwaysFailProvider(),
                embedding_provider=DeterministicEmbeddingProvider(),
            )
    with connect() as conn:
        job = conn.execute(
            """
                SELECT status, decision_id, lease_owner, lease_expires_at
                FROM consolidation_jobs WHERE incident_id = %s
            """,
            (resolved["id"],),
        ).fetchone()
        decision = conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (job[1],)
        ).fetchone()
    assert job[0] == "failed"
    assert job[2:] == (None, None)
    assert decision == ("failed",)


@requires_db
def test_expired_last_attempt_is_terminalized_without_an_extra_retry():
    import hindsight.consolidation as consolidation
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider

    class FailProvider:
        provider_name = "test-model"
        model_name = "fail-before-expired-final-attempt-v1"

        def generate(self, request):
            raise RuntimeError("temporary model outage")

    namespace = f"consolidation-expired-final-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="final consolidation attempt expires before publishing",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="temporary model outage"):
            consolidation.consolidate_resolved_incident(
                incident_id=str(resolved["id"]),
                db_url=database_url(),
                reasoning_provider=FailProvider(),
                embedding_provider=DeterministicEmbeddingProvider(),
            )

    with connect() as conn:
        job = conn.execute(
            """
                SELECT id, decision_id, attempt_count
                FROM consolidation_jobs WHERE incident_id = %s
            """,
            (resolved["id"],),
        ).fetchone()
    assert job[2] == 2
    final_attempt = consolidation._claim_job(  # noqa: SLF001 - lease expiry regression
        job_id=str(job[0]),
        db_url=database_url(),
    )
    assert final_attempt["attempt_count"] == 3
    with connect() as conn:
        conn.execute(
            """
                UPDATE consolidation_jobs
                SET lease_expires_at = now() - INTERVAL '1 second'
                WHERE id = %s
            """,
            (job[0],),
        )

    terminal = consolidation._claim_job(  # noqa: SLF001 - lease expiry regression
        job_id=str(job[0]),
        db_url=database_url(),
    )

    assert terminal["status"] == "failed"
    assert terminal["attempt_count"] == 3
    assert terminal["lease_owner"] is None
    assert terminal["lease_expires_at"] is None
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (job[1],)
        ).fetchone() == ("failed",)


@requires_db
def test_retry_that_becomes_ineligible_fails_linked_decision_atomically():
    from hindsight.consolidation import (
        process_consolidation_job,
        consolidate_resolved_incident,
    )
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider

    class FailProvider:
        provider_name = "test-model"
        model_name = "fail-before-ineligible-v1"

        def generate(self, request):
            raise RuntimeError("temporary model outage")

    namespace = f"consolidation-ineligible-retry-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="retry evidence that later becomes ineligible",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    with pytest.raises(RuntimeError, match="temporary model outage"):
        consolidate_resolved_incident(
            incident_id=str(resolved["id"]),
            db_url=database_url(),
            reasoning_provider=FailProvider(),
            embedding_provider=DeterministicEmbeddingProvider(),
        )

    with connect() as conn:
        job = conn.execute(
            """
                SELECT id, decision_id, status
                FROM consolidation_jobs WHERE incident_id = %s
            """,
            (resolved["id"],),
        ).fetchone()
        assert job[2] == "retrying"
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (job[1],)
        ).fetchone() == ("open",)
        conn.execute(
            "UPDATE incidents SET status = 'mitigated' WHERE id = %s",
            (resolved["id"],),
        )

    result = process_consolidation_job(
        job_id=str(job[0]),
        db_url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert result.created is False
    assert result.reason == "incident is not resolved"
    with connect() as conn:
        assert conn.execute(
            """
                SELECT status, lease_owner, lease_expires_at
                FROM consolidation_jobs WHERE id = %s
            """,
            (job[0],),
        ).fetchone() == ("not_eligible", None, None)
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (job[1],)
        ).fetchone() == ("failed",)


@requires_db
def test_retry_rejects_a_previously_read_source_after_quarantine():
    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider

    class FailProvider:
        provider_name = "test-model"
        model_name = "fail-before-source-quarantine-v1"

        def generate(self, request):
            raise RuntimeError("temporary model outage")

    namespace = f"consolidation-stale-read-retry-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="a previously read source becomes quarantined",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    with pytest.raises(RuntimeError, match="temporary model outage"):
        consolidate_resolved_incident(
            incident_id=str(resolved["id"]),
            db_url=database_url(),
            reasoning_provider=FailProvider(),
            embedding_provider=DeterministicEmbeddingProvider(),
        )

    with connect() as conn:
        job = conn.execute(
            """
                SELECT id, decision_id, status
                FROM consolidation_jobs WHERE incident_id = %s
            """,
            (resolved["id"],),
        ).fetchone()
        assert job[2] == "retrying"
        source_id = conn.execute(
            """
                SELECT semantic_memory_id FROM memory_reads
                WHERE decision_id = %s AND memory_kind = 'semantic'
                LIMIT 1
            """,
            (job[1],),
        ).fetchone()[0]
        conn.execute(
            """
                UPDATE semantic_memories
                SET trust_status = 'review_required'
                WHERE id = %s
            """,
            (source_id,),
        )

    result = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert result.created is False
    assert result.reason == "source evidence changed during synthesis"
    with connect() as conn:
        assert conn.execute(
            """
                SELECT status, lease_owner, lease_expires_at
                FROM consolidation_jobs WHERE id = %s
            """,
            (job[0],),
        ).fetchone() == ("not_eligible", None, None)
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s",
            (job[1],),
        ).fetchone() == ("failed",)


@requires_db
def test_expired_attempt_cannot_publish_or_transition_after_overlapping_claim():
    import json

    import hindsight.consolidation as consolidation
    from hindsight.cross_episode import open_demo_incident, resolve_demo_incident
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import ReasoningResponse

    namespace = f"consolidation-overlap-{uuid4()}"
    incident = open_demo_incident(
        label="source",
        namespace=namespace,
        summary="overlapping consolidation attempts must be fenced",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    job = consolidation.enqueue_consolidation_job(
        incident_id=str(resolved["id"]),
        source_event_id=str(resolved["resolution_event_id"]),
        db_url=database_url(),
    )

    class OverlappingClaimProvider:
        provider_name = "test-model"
        model_name = "overlapping-claim-v1"

        def __init__(self):
            self.lease_owners = []

        def generate(self, request):
            with connect() as conn:
                first_owner = conn.execute(
                    """
                        UPDATE consolidation_jobs
                        SET lease_expires_at = now() - INTERVAL '1 second'
                        WHERE id = %s
                        RETURNING lease_owner
                    """,
                    (job["id"],),
                ).fetchone()[0]
            replacement = consolidation._claim_job(  # noqa: SLF001 - concurrency regression
                job_id=str(job["id"]),
                db_url=database_url(),
            )
            self.lease_owners = [first_owner, replacement["lease_owner"]]
            prompt = json.loads(request.prompt)
            evidence_id, quote = next(iter(prompt["evidence"].items()))
            return ReasoningResponse(
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "title": "Fenced lesson",
                        "claims": [
                            {
                                "kind": "situation",
                                "text": "Only the current claim may publish",
                                "citations": [{"evidence_id": evidence_id, "quote": quote}],
                            }
                        ],
                    }
                ),
                provider=self.provider_name,
                model=self.model_name,
            )

    provider = OverlappingClaimProvider()
    with pytest.raises(
        consolidation.ConsolidationLeaseLostError,
        match="lease is no longer current",
    ):
        consolidation.process_consolidation_job(
            job_id=str(job["id"]),
            db_url=database_url(),
            reasoning_provider=provider,
            embedding_provider=DeterministicEmbeddingProvider(),
        )

    assert len(provider.lease_owners) == 2
    assert provider.lease_owners[0] != provider.lease_owners[1]
    decision_id = f"consolidation:{job['id']}"
    with connect() as conn:
        persisted_job = conn.execute(
            """
                SELECT status, attempt_count, lease_owner, decision_id
                FROM consolidation_jobs WHERE id = %s
            """,
            (job["id"],),
        ).fetchone()
        assert persisted_job == (
            "leased",
            2,
            provider.lease_owners[1],
            decision_id,
        )
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (decision_id,)
        ).fetchone() == ("open",)
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE producer_decision_id = %s",
            (decision_id,),
        ).fetchone() == (0,)
