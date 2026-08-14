"""Tests for resolved-incident consolidation."""

from datetime import UTC, datetime
import os
from typing import Any
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tests.fakes import (
    DeterministicEmbeddingProvider,
    FixtureLessonReasoningProvider,
    lesson_validation_decision,
)

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

SERVICE_SLUG = "payments-api"
ROOT_CAUSE = "Retry fanout amplified downstream payment processor timeouts."
RESOLUTION_SUMMARY = (
    "Throttle retry fanout, watch processor timeout rate, and hold worker scaling "
    "until downstream processor health recovers."
)


@pytest.fixture(autouse=True)
def _test_only_providers(monkeypatch):
    monkeypatch.setattr(
        "hindsight.consolidation.reasoning_provider_from_env",
        lambda *_args, **_kwargs: FixtureLessonReasoningProvider(),
    )
    monkeypatch.setattr(
        "hindsight.consolidation.embedding_provider_from_env",
        lambda *_args, **_kwargs: DeterministicEmbeddingProvider(),
    )
    monkeypatch.setattr(
        "hindsight.embeddings.embedding_provider_from_env",
        lambda *_args, **_kwargs: DeterministicEmbeddingProvider(),
    )


def open_demo_incident(
    *,
    label: str,
    namespace: str,
    summary: str,
    db_url: str,
    embedding_provider: Any | None = None,
) -> dict[str, Any]:
    """Build the governed incident evidence needed by consolidation tests."""

    from hindsight.db import connect
    from hindsight.embeddings import embedding_provider_from_env
    from hindsight.memory import MemoryStore, Provenance

    provider = embedding_provider or embedding_provider_from_env()
    slug = f"{namespace}:{label}"
    content = (
        f"Telemetry summary for {slug}: {summary} Signals: checkout p99 above 2s, "
        "processor timeouts rising, retry fanout high."
    )
    embedding = provider.embed_document(content)
    with connect(db_url) as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        INSERT INTO services (slug, name, owner_team, tier)
                        VALUES (%s, 'Payments API', 'revenue-platform', 'critical')
                        ON CONFLICT (tenant_id, slug) DO UPDATE SET
                            name = excluded.name,
                            owner_team = excluded.owner_team,
                            tier = excluded.tier
                        RETURNING *
                    """,
                    (SERVICE_SLUG,),
                )
                service = cur.fetchone()
                cur.execute(
                    """
                        INSERT INTO incidents (
                            slug, title, severity, status, started_at, summary
                        )
                        VALUES (%s, 'Checkout p99 latency above SLO', 'sev2', 'open', %s, %s)
                        ON CONFLICT (tenant_id, slug) DO UPDATE SET
                            title = excluded.title,
                            severity = excluded.severity,
                            status = excluded.status,
                            started_at = excluded.started_at,
                            summary = excluded.summary,
                            resolved_at = NULL,
                            root_cause = NULL
                        RETURNING *
                    """,
                    (slug, datetime.now(UTC), summary),
                )
                incident = cur.fetchone()
            assert service is not None and incident is not None
            conn.execute(
                """
                    INSERT INTO incident_services (incident_id, service_id, impact)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (incident_id, service_id) DO UPDATE SET
                        impact = excluded.impact
                """,
                (incident["id"], service["id"], "payments-api checkout latency breached SLO."),
            )
            conn.execute(
                """
                    INSERT INTO incident_events (
                        incident_id, occurred_at, event_type, summary, metadata
                    )
                    VALUES (%s, %s, 'telemetry_alert', %s, %s)
                """,
                (
                    incident["id"],
                    datetime.now(UTC),
                    summary,
                    Jsonb({"namespace": namespace, "label": label}),
                ),
            )
            memory = MemoryStore(conn=conn, embedding_provider=provider).remember(
                memory_kind="semantic",
                namespace=namespace,
                content=content,
                provenance=Provenance(
                    writer="telemetry.ingest",
                    source_ref=f"telemetry:{slug}",
                    justification="Store incident evidence for consolidation testing",
                ),
                metadata={
                    "role": "incident-summary",
                    "incident_slug": slug,
                    "service_slug": SERVICE_SLUG,
                    "label": label,
                },
                precomputed_embedding=embedding,
            )
            conn.execute(
                """
                    INSERT INTO incident_semantic_memories (
                        incident_id, memory_id, relationship
                    )
                    VALUES (%s, %s, 'summary')
                    ON CONFLICT (incident_id, memory_id) DO UPDATE SET
                        relationship = excluded.relationship
                """,
                (incident["id"], memory["id"]),
            )
    return dict(incident)


def resolve_demo_incident(
    *, incident_id: str, reflected_memory_id: str | None, db_url: str
) -> dict[str, Any]:
    """Resolve a test incident and attach optional resolution evidence."""

    from hindsight.db import connect
    from hindsight.runs import resolve_incident

    with connect(db_url) as conn:
        row = conn.execute("SELECT slug FROM incidents WHERE id = %s", (incident_id,)).fetchone()
    assert row is not None
    incident = resolve_incident(
        slug=str(row[0]),
        root_cause=ROOT_CAUSE,
        action="Throttle retry fanout and hold worker scaling",
        observation=RESOLUTION_SUMMARY,
        recovered=True,
        actor="test.operator",
        db_url=db_url,
    )["incident"]
    if reflected_memory_id:
        with connect(db_url) as conn:
            conn.execute(
                """
                    INSERT INTO incident_semantic_memories (
                        incident_id, memory_id, relationship
                    )
                    VALUES (%s, %s, 'resolution')
                    ON CONFLICT (incident_id, memory_id) DO UPDATE SET
                        relationship = excluded.relationship
                """,
                (incident["id"], reflected_memory_id),
            )
            conn.commit()
    return incident


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
                },
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


def test_lesson_generation_requests_the_validated_response_schema():
    from hindsight.consolidation import LESSON_RESPONSE_JSON_SCHEMA, _generate_lesson
    from hindsight.reasoning import ReasoningResponse

    class CapturingProvider:
        provider_name = "test-model"
        model_name = "capture-v1"

        def __init__(self):
            self.request = None

        def generate(self, request):
            self.request = request
            return ReasoningResponse(
                text=(
                    '{"schema_version":1,"title":"Retry lesson","claims":'
                    '[{"kind":"safe_action","text":"Stop retries","citations":'
                    '[{"evidence_id":"memory:source","quote":"Stop retries"}]}]}'
                ),
                provider=self.provider_name,
                model=self.model_name,
            )

    provider = CapturingProvider()
    lesson = _generate_lesson(
        provider=provider,
        context={
            "incident": {"id": "incident-1", "slug": "retry-storm"},
            "service": {"name": "checkout"},
        },
        evidence={"memory:source": "Stop retries"},
    )

    assert lesson["schema_version"] == 1
    assert provider.request is not None
    assert provider.request.response_json_schema == LESSON_RESPONSE_JSON_SCHEMA
    assert provider.request.thinking_budget == 0


def test_semantic_validator_rejects_destructive_claim_with_unrelated_quote():
    import json

    from hindsight.consolidation import _validate_lesson_semantics
    from hindsight.reasoning import ReasoningResponse

    class RejectingValidator:
        provider_name = "test-model"
        model_name = "strict-validator-v1"

        def generate(self, _request):
            return ReasoningResponse(
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "claims": [
                            {
                                "claim_index": 0,
                                "entailed": False,
                                "safe": False,
                                "reason_code": "unsafe_action",
                            }
                        ],
                        "overall_entailed": False,
                        "overall_safe": False,
                    }
                ),
                provider=self.provider_name,
                model=self.model_name,
            )

    receipt = _validate_lesson_semantics(
        provider=RejectingValidator(),
        context={"incident": {"id": "incident-1"}},
        lesson={
            "schema_version": 1,
            "title": "Unsafe lesson",
            "claims": [
                {
                    "kind": "safe_action",
                    "text": "Delete the production database",
                    "citations": [
                        {"evidence_id": "memory:source", "quote": "Queue depth increased"}
                    ],
                }
            ],
        },
        evidence={"memory:source": "Queue depth increased during the incident."},
    )

    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "semantic_entailment_or_safety_not_proven"
    assert receipt["provider"] == "test-model"
    assert len(receipt["prompt_sha256"]) == 64


@requires_db
def test_consolidation_requires_fingerprint_bound_approval_before_retrieval():
    import hindsight.consolidation as consolidation
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore
    from hindsight.trace_contract import governed_decision_trace
    from hindsight.operations import (
        enqueue_operation,
        execute_operation,
        preview_consolidation_review,
    )

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
    assert first.memory["trust_status"] == "review_required"
    assert first.memory["metadata"]["operator_disposition"] == "unreviewed"
    assert first.memory["metadata"]["usage_instruction"] == "audit_only"
    assert second.created is False
    assert second.reason == "lesson already exists"
    assert second.memory is not None
    assert second.memory["id"] == first.memory["id"]
    excluded_decision_id = str(uuid4())
    with MemoryStore(
        url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    ) as store:
        retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=first.memory["content"],
            decision_id=excluded_decision_id,
            reader="consolidation.regression",
            purpose="prove an unreviewed lesson is excluded from strict retrieval",
            limit=5,
        )
    assert str(first.memory["id"]) not in {str(hit["id"]) for hit in retrieval.hits}

    preview = preview_consolidation_review(
        candidate_id=str(first.job_id),
        action="approve",
        actor="operator:test",
        reason="Evidence and operational safety reviewed",
        db_url=database_url(),
    )
    operation, created = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=str(preview["fingerprint"]),
        idempotency_key=str(uuid4()),
        actor="operator:test",
        db_url=database_url(),
    )
    assert created is True
    completed = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=DeterministicEmbeddingProvider(),
        worker_id="consolidation-approval-test",
        db_url=database_url(),
    )
    assert completed["status"] == "completed"

    decision_id = str(uuid4())
    with MemoryStore(
        url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    ) as store:
        approved_retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=first.memory["content"],
            decision_id=decision_id,
            reader="consolidation.regression",
            purpose="prove only the approved successor reaches strict retrieval",
            limit=5,
        )
    assert approved_retrieval.status == "succeeded"
    approved_ids = {str(hit["id"]) for hit in approved_retrieval.hits}
    assert str(first.memory["id"]) not in approved_ids
    trace = governed_decision_trace(decision_id=decision_id, db_url=database_url())
    assert trace is not None
    traced_ids = {str(read["memory_id"]) for read in trace["reads"]}
    assert approved_ids.intersection(traced_ids)
    with connect() as conn:
        job = conn.execute(
            """
                SELECT review_status, review_operation_id, approved_memory_id,
                       generation_receipt, validation_receipt
                FROM consolidation_jobs WHERE id = %s
            """,
            (first.job_id,),
        ).fetchone()
        assert job[0] == "approved"
        assert str(job[1]) == str(operation["id"])
        assert str(job[2]) in approved_ids
        assert job[3]["provider"] == "test_fixture_lesson"
        assert job[4]["status"] == "passed"
        assert (
            consolidation._existing_lesson(  # noqa: SLF001 - idempotency regression
                conn,
                incident_id=resolved["id"],
                namespace=namespace,
            )
            is None
        )


@requires_db
def test_consolidation_rejection_is_operation_bound_and_preserves_audit_candidate():
    import psycopg

    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore
    from hindsight.operations import (
        enqueue_operation,
        execute_operation,
        preview_consolidation_review,
    )
    from tests.fakes import DeterministicEmbeddingProvider

    namespace = f"consolidation-rejection-{uuid4()}"
    incident = open_demo_incident(
        label="rejected-candidate",
        namespace=namespace,
        summary="retry fanout raised checkout latency",
        db_url=database_url(),
    )
    resolved = resolve_demo_incident(
        incident_id=str(incident["id"]),
        reflected_memory_id=None,
        db_url=database_url(),
    )
    candidate = consolidate_resolved_incident(
        incident_id=str(resolved["id"]),
        db_url=database_url(),
    )
    assert candidate.memory is not None

    preview = preview_consolidation_review(
        candidate_id=str(candidate.job_id),
        action="reject",
        actor="operator:reject-test",
        reason="The proposed action is too broad",
        db_url=database_url(),
    )
    operation, created = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=str(preview["fingerprint"]),
        idempotency_key=str(uuid4()),
        actor="operator:reject-test",
        db_url=database_url(),
    )
    assert created is True

    with connect() as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="terminal consolidation review approval is invalid",
        ):
            with conn.transaction():
                conn.execute(
                    """
                        UPDATE consolidation_jobs
                        SET review_status = 'rejected', reviewed_by = %s,
                            review_reason = %s, reviewed_at = now(),
                            review_operation_id = %s
                        WHERE id = %s
                    """,
                    (
                        "operator:reject-test",
                        "The proposed action is too broad",
                        operation["id"],
                        candidate.job_id,
                    ),
                )

    completed = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=DeterministicEmbeddingProvider(),
        worker_id="consolidation-rejection-test",
        db_url=database_url(),
    )
    assert completed["status"] == "completed"

    with connect() as conn:
        review = conn.execute(
            """
                SELECT review_status, approved_memory_id
                FROM consolidation_jobs WHERE id = %s
            """,
            (candidate.job_id,),
        ).fetchone()
        memory = conn.execute(
            """
                SELECT trust_status, t_invalid
                FROM semantic_memories WHERE id = %s
            """,
            (candidate.memory["id"],),
        ).fetchone()
    assert review == ("rejected", None)
    assert memory == ("review_required", None)

    with MemoryStore(
        url=database_url(),
        embedding_provider=DeterministicEmbeddingProvider(),
    ) as store:
        retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=candidate.memory["content"],
            decision_id=str(uuid4()),
            reader="consolidation.rejection-regression",
            purpose="prove a rejected candidate remains excluded",
            limit=5,
        )
    assert str(candidate.memory["id"]) not in {str(hit["id"]) for hit in retrieval.hits}


@requires_db
def test_incident_evidence_embedding_failure_writes_no_state():
    from hindsight.db import connect, database_url

    class FailingProvider:
        def embed_document(self, _text):
            raise RuntimeError("document embedding unavailable")

    namespace = f"consolidation-embedding-failure-{uuid4()}"
    label = "episode-one"
    slug = f"{namespace}:{label}"
    with pytest.raises(RuntimeError, match="document embedding unavailable"):
        open_demo_incident(
            label=label,
            namespace=namespace,
            summary="retry fanout raised checkout latency",
            db_url=database_url(),
            embedding_provider=FailingProvider(),
        )

    with connect(database_url()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM incidents WHERE slug = %s", (slug,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE namespace = %s", (namespace,)
        ).fetchone() == (0,)


@requires_db
@pytest.mark.parametrize(
    ("model_output", "reason_fragment"),
    [
        ("not JSON", "model output is not JSON"),
        (
            '{"schema_version":1,"title":"Missing claims","claims":[]}',
            "at least one claim is required",
        ),
        (
            '{"schema_version":1,"title":"Bad kind","claims":'
            '[{"kind":"unsupported","text":"Do something","citations":'
            '[{"evidence_id":"memory:missing","quote":"fabricated"}]}]}',
            "claim 0 has invalid kind",
        ),
        (
            '{"schema_version":1,"title":"Invalid citation","claims":'
            '[{"kind":"safe_action","text":"uncited action","citations":'
            '[{"evidence_id":"memory:missing","quote":"fabricated"}]}]}',
            "claim 0 cites ineligible evidence",
        ),
    ],
)
def test_invalid_model_output_publishes_no_lesson_and_records_terminal_reason(
    model_output, reason_fragment
):
    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.reasoning import ReasoningResponse

    class InvalidOutputProvider:
        provider_name = "test-model"
        model_name = "invalid-output-v1"

        def generate(self, request):
            return ReasoningResponse(
                text=model_output,
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
        reasoning_provider=InvalidOutputProvider(),
        embedding_provider=DeterministicEmbeddingProvider(),
    )
    assert result.memory is None
    assert result.created is False
    assert result.reason == f"invalid_lesson:{reason_fragment}"
    with connect() as conn:
        job = conn.execute(
            "SELECT status, reason, decision_id FROM consolidation_jobs WHERE id = %s",
            (result.job_id,),
        ).fetchone()
        assert job[:2] == ("failed", result.reason)
        assert conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (job[2],)
        ).fetchone() == ("failed",)
        assert conn.execute(
            "SELECT count(*) FROM semantic_memories WHERE producer_decision_id = %s",
            (job[2],),
        ).fetchone() == (0,)


@requires_db
@pytest.mark.parametrize("governance_state", ["invalidated", "review_required"])
def test_consolidation_rejects_governed_invalid_source_evidence(governance_state):
    from hindsight.consolidation import consolidate_resolved_incident
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider

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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.reasoning import ReasoningResponse

    class CaptureEvidenceProvider:
        provider_name = "test-model"
        model_name = "capture-eligible-evidence-v1"

        def __init__(self):
            self.evidence_ids = set()

        def generate(self, request):
            prompt = json.loads(request.prompt)
            if prompt.get("validation_kind") == "procedural_lesson_entailment.v1":
                return ReasoningResponse(
                    text=lesson_validation_decision(prompt["lesson"]),
                    provider=self.provider_name,
                    model=self.model_name,
                )
            self.evidence_ids = set(prompt["evidence"])
            evidence_id = next(key for key in prompt["evidence"] if key.startswith("memory:"))
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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.reasoning import ReasoningResponse

    class QuarantineDuringSynthesisProvider:
        provider_name = "test-model"
        model_name = "quarantine-during-synthesis-v1"

        def generate(self, request):
            prompt = json.loads(request.prompt)
            if prompt.get("validation_kind") == "procedural_lesson_entailment.v1":
                return ReasoningResponse(
                    text=lesson_validation_decision(prompt["lesson"]),
                    provider=self.provider_name,
                    model=self.model_name,
                )
            evidence_id = next(key for key in prompt["evidence"] if key.startswith("memory:"))
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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider
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
            if prompt.get("validation_kind") == "procedural_lesson_entailment.v1":
                return ReasoningResponse(
                    text=lesson_validation_decision(prompt["lesson"]),
                    provider=self.provider_name,
                    model=self.model_name,
                )
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
                                "citations": [{"evidence_id": evidence_id, "quote": quote}],
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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider

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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider

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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider

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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider

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
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider
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
            prompt = json.loads(request.prompt)
            if prompt.get("validation_kind") == "procedural_lesson_entailment.v1":
                return ReasoningResponse(
                    text=lesson_validation_decision(prompt["lesson"]),
                    provider=self.provider_name,
                    model=self.model_name,
                )
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
