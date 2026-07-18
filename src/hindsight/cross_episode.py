"""Scriptable cross-episode mechanism demo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import time
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.agent import IncidentInput, IncidentAgentResult, run_incident_agent
from hindsight.consolidation import (
    ConsolidationLeaseBusyError,
    ConsolidationResult,
    handle_incident_changefeed_event,
    incident_closed_changefeed_event,
)
from hindsight.db import connect, database_url
from hindsight.embeddings import embedding_provider_from_env
from hindsight.memory import MemoryStore, Provenance
from hindsight.reasoning import ReasoningProvider, ReasoningRequest, ReasoningResponse
from hindsight.runs import resolve_incident
from hindsight.trace_contract import governed_decision_trace, lesson_identity_trace
from hindsight.tracing import set_span_attributes, start_span

CROSS_EPISODE_NAMESPACE = "demo:cross-episode-payments"
SERVICE_SLUG = "payments-api"
SERVICE_NAME = "Payments API"
OWNER_TEAM = "revenue-platform"
FIRST_INCIDENT_SUMMARY = (
    "payments-api checkout p99 latency breached SLO while payment processor "
    "timeouts and retry fanout climbed together."
)
SECOND_INCIDENT_SUMMARY = (
    "payments-api checkout p99 latency is breaching again with payment processor "
    "timeouts and retry fanout rising."
)
FIRST_PLAN = (
    "First episode path: inspect edge gateway certificate health, scale checkout "
    "workers, compare the last deploy, then confirm processor timeout rate and "
    "throttle retry fanout."
)
SECOND_PLAN = (
    "Repeat incident path: use the consolidated lesson, confirm processor timeout "
    "rate and queue depth, then throttle retry fanout before scaling workers."
)
RESOLUTION_SUMMARY = (
    "Throttle retry fanout, watch processor timeout rate, and hold worker scaling "
    "until downstream processor health recovers."
)
ROOT_CAUSE = "Retry fanout amplified downstream payment processor timeouts."
CONSOLIDATION_WAIT_SECONDS = 600
CONSOLIDATION_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class CrossEpisodeRunSummary:
    """Camera-friendly summary for one episode."""

    label: str
    incident_slug: str
    thread_id: str
    decision_id: str
    plan: str | None
    proposed_action: str | None
    reflected_memory_id: str | None
    retrieval_id: str | None
    recalled_memory_ids: list[str]
    recalled_lesson_memory_ids: list[str]
    recalled_memory_traces: list[dict[str, Any]]


@dataclass(frozen=True)
class CrossEpisodeDemoResult:
    """Complete result for the cross-episode mechanism demo."""

    namespace: str
    episode_one: CrossEpisodeRunSummary
    consolidation: ConsolidationResult
    episode_two: CrossEpisodeRunSummary
    lesson_trace: dict[str, Any]


class CrossEpisodeMechanismReasoningProvider:
    """Fixture provider illustrating that recalled lessons enter the next prompt."""

    provider_name = "deterministic-demo"
    model_name = "cross-episode-v1"

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        prompt = request.prompt.lower()
        learned = "lesson from" in prompt and "safe action:" in prompt
        plan = SECOND_PLAN if learned else FIRST_PLAN
        return ReasoningResponse(
            text=plan,
            provider=self.provider_name,
            model=self.model_name,
            usage={
                "prompt_characters": len(request.prompt),
                "system_characters": len(request.system or ""),
                "consolidated_lesson_seen": learned,
            },
        )


def run_cross_episode_demo(
    *,
    db_url: str | None = None,
    namespace: str = CROSS_EPISODE_NAMESPACE,
    keep_existing: bool = False,
    reasoning_provider: ReasoningProvider | None = None,
) -> CrossEpisodeDemoResult:
    """Illustrate cross-episode wiring; this is not a performance benchmark."""

    resolved_db_url = db_url or database_url()
    if not keep_existing:
        namespace = f"{namespace}:session:{uuid4().hex[:8]}"
    provider = reasoning_provider or CrossEpisodeMechanismReasoningProvider()
    with start_span(
        "hindsight.demo.cross_episode",
        {
            "hindsight.demo.flow": "cross_episode",
            "hindsight.memory.namespace": namespace,
        },
    ) as span:
        _record_demo_session(namespace=namespace, db_url=resolved_db_url)

        first_incident = open_demo_incident(
            label="episode-one",
            namespace=namespace,
            summary=FIRST_INCIDENT_SUMMARY,
            db_url=resolved_db_url,
        )
        episode_one = _run_episode(
            label="episode-one",
            incident=first_incident,
            namespace=namespace,
            summary=FIRST_INCIDENT_SUMMARY,
            db_url=resolved_db_url,
            reasoning_provider=provider,
        )
        resolved_incident = resolve_demo_incident(
            incident_id=str(first_incident["id"]),
            reflected_memory_id=episode_one.reflected_memory_id,
            db_url=resolved_db_url,
        )
        consolidation_results = _complete_consolidation(
            resolved_incident=resolved_incident,
            db_url=resolved_db_url,
        )
        if not consolidation_results:
            raise RuntimeError("resolved incident did not produce a consolidation result")

        second_incident = open_demo_incident(
            label="episode-two",
            namespace=namespace,
            summary=SECOND_INCIDENT_SUMMARY,
            db_url=resolved_db_url,
        )
        episode_two = _run_episode(
            label="episode-two",
            incident=second_incident,
            namespace=namespace,
            summary=SECOND_INCIDENT_SUMMARY,
            db_url=resolved_db_url,
            reasoning_provider=provider,
        )
        lesson_trace = lesson_identity_trace(
            decision_id=episode_two.decision_id,
            db_url=resolved_db_url,
        )
        if lesson_trace is None:
            raise RuntimeError("second episode did not produce a lesson identity trace")
        set_span_attributes(
            span,
            {
                "hindsight.memory.id": str(consolidation_results[0].memory["id"])
                if consolidation_results[0].memory
                else None,
            },
        )

    return CrossEpisodeDemoResult(
        namespace=namespace,
        episode_one=episode_one,
        consolidation=consolidation_results[0],
        episode_two=episode_two,
        lesson_trace=lesson_trace,
    )


def _complete_consolidation(
    *, resolved_incident: dict[str, Any], db_url: str
) -> list[ConsolidationResult]:
    event = incident_closed_changefeed_event(
        resolved_incident,
        source_event_id=str(resolved_incident["resolution_event_id"]),
    )
    deadline = time.monotonic() + CONSOLIDATION_WAIT_SECONDS
    while True:
        try:
            return handle_incident_changefeed_event(event, db_url=db_url)
        except ConsolidationLeaseBusyError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "managed consolidation did not release its active lease"
                ) from None
            time.sleep(CONSOLIDATION_POLL_SECONDS)


def reset_cross_episode_demo(*, namespace: str, db_url: str | None = None) -> None:
    """Archive a demo session without deleting governed evidence."""

    resolved_db_url = db_url or database_url()
    base_namespace = namespace.split(":session:", 1)[0]
    with connect(resolved_db_url) as conn:
        conn.execute(
            """
                UPDATE demo_sessions
                SET status = 'archived', archived_at = COALESCE(archived_at, now())
                WHERE (namespace = %s OR namespace LIKE %s) AND status = 'active'
            """,
            (base_namespace, f"{base_namespace}:session:%"),
        )
        conn.commit()


def _record_demo_session(*, namespace: str, db_url: str) -> None:
    with connect(db_url) as conn:
        conn.execute(
            """
                INSERT INTO demo_sessions (demo_kind, namespace, created_by)
                VALUES ('cross_episode_mechanism', %s, 'demo.runner')
                ON CONFLICT (namespace) DO NOTHING
            """,
            (namespace,),
        )
        conn.commit()


def open_demo_incident(
    *,
    label: str,
    namespace: str,
    summary: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Create one open incident and linked telemetry summary memory."""

    resolved_db_url = db_url or database_url()
    slug = f"{namespace}:{label}"
    memory_content = (
        f"Telemetry summary for {slug}: {summary} "
        "Signals: checkout p99 above 2s, processor timeouts rising, retry fanout high."
    )
    embedding_provider = embedding_provider_from_env()
    prepared_embedding = embedding_provider.embed_document(memory_content)
    with connect(resolved_db_url) as conn:
        with conn.transaction():
            service = _upsert_service(conn)
            incident = _upsert_incident(conn, slug=slug, summary=summary)
            conn.execute(
                """
                    INSERT INTO incident_services (incident_id, service_id, impact)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (incident_id, service_id) DO UPDATE SET
                        impact = excluded.impact
                """,
                (incident["id"], service["id"], f"{SERVICE_SLUG} checkout latency breached SLO."),
            )
            _insert_incident_event(
                conn,
                incident_id=incident["id"],
                event_type="telemetry_alert",
                summary=summary,
                metadata={"namespace": namespace, "label": label},
            )
            memory = MemoryStore(
                conn=conn,
                embedding_provider=embedding_provider,
            ).remember(
                memory_kind="semantic",
                namespace=namespace,
                content=memory_content,
                provenance=Provenance(
                    writer="telemetry.ingest",
                    source_ref=f"telemetry:{slug}",
                    justification="Store incident telemetry summary for cross-episode demo",
                ),
                metadata={
                    "demo": "cross-episode-mechanism",
                    "role": "incident-summary",
                    "incident_slug": slug,
                    "service_slug": SERVICE_SLUG,
                    "label": label,
                },
                precomputed_embedding=prepared_embedding,
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
    return incident


def resolve_demo_incident(
    *,
    incident_id: str,
    reflected_memory_id: str | None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Mark episode one resolved and link the agent reflection as resolution evidence."""

    resolved_db_url = db_url or database_url()
    with connect(resolved_db_url) as conn:
        row = conn.execute("SELECT slug FROM incidents WHERE id = %s", (incident_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"incident not found: {incident_id}")
    resolution = resolve_incident(
        slug=str(row[0]),
        root_cause=ROOT_CAUSE,
        action="Throttle retry fanout and hold worker scaling",
        observation=RESOLUTION_SUMMARY,
        recovered=True,
        actor="demo.operator",
        db_url=resolved_db_url,
    )
    incident = resolution["incident"]
    with connect(resolved_db_url) as conn:
        with conn.transaction():
            if reflected_memory_id:
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
    return incident


def _run_episode(
    *,
    label: str,
    incident: dict[str, Any],
    namespace: str,
    summary: str,
    db_url: str,
    reasoning_provider: ReasoningProvider,
) -> CrossEpisodeRunSummary:
    with start_span(
        "hindsight.demo.cross_episode.turn",
        {
            "hindsight.demo.flow": "cross_episode",
            "hindsight.demo.label": label,
            "hindsight.agent.thread_id": f"{namespace}:{label}",
            "hindsight.memory.namespace": namespace,
        },
    ) as span:
        result = run_incident_agent(
            IncidentInput(
                user_input=summary,
                incident_id=incident["slug"],
                namespace=namespace,
                service_slug=SERVICE_SLUG,
                severity=incident["severity"],
                title=incident["title"],
                metadata={"demo": "cross-episode-mechanism", "episode": label},
            ),
            thread_id=f"{namespace}:{label}",
            db_url=db_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider_from_env(),
        )
        summary_result = _episode_summary(
            label=label,
            incident=incident,
            result=result,
            db_url=db_url,
        )
        set_span_attributes(
            span,
            {
                "hindsight.memory.ids": summary_result.recalled_memory_ids,
                "hindsight.memory.count": len(summary_result.recalled_memory_ids),
                "hindsight.memory.id": summary_result.reflected_memory_id,
            },
        )
        return summary_result


def _episode_summary(
    *,
    label: str,
    incident: dict[str, Any],
    result: IncidentAgentResult,
    db_url: str,
) -> CrossEpisodeRunSummary:
    recalled = result.state.get("recalled_memories") or []
    recalled_ids = [str(row.get("memory_id") or row.get("id")) for row in recalled]
    recalled_lesson_ids = [
        str(row.get("memory_id") or row.get("id"))
        for row in recalled
        if row.get("content_schema") == "procedural_lesson.v1"
    ]
    decision_trace = governed_decision_trace(
        decision_id=str(result.state["decision_id"]),
        db_url=db_url,
    )
    return CrossEpisodeRunSummary(
        label=label,
        incident_slug=incident["slug"],
        thread_id=result.thread_id,
        decision_id=str(result.state["decision_id"]),
        plan=result.plan,
        proposed_action=result.proposed_action,
        reflected_memory_id=result.reflected_memory_id,
        retrieval_id=result.state.get("retrieval_id"),
        recalled_memory_ids=recalled_ids,
        recalled_lesson_memory_ids=recalled_lesson_ids,
        recalled_memory_traces=list(decision_trace["reads"]) if decision_trace else [],
    )


def _upsert_service(conn: Any) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                INSERT INTO services (slug, name, owner_team, tier)
                VALUES (%s, %s, %s, 'critical')
                ON CONFLICT (slug) DO UPDATE SET
                    name = excluded.name,
                    owner_team = excluded.owner_team,
                    tier = excluded.tier
                RETURNING *
            """,
            (SERVICE_SLUG, SERVICE_NAME, OWNER_TEAM),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected service row")
    return dict(row)


def _upsert_incident(conn: Any, *, slug: str, summary: str) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                INSERT INTO incidents (
                    slug, title, severity, status, started_at, summary
                )
                VALUES (%s, %s, 'sev2', 'open', %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title = excluded.title,
                    severity = excluded.severity,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    summary = excluded.summary,
                    resolved_at = NULL,
                    root_cause = NULL
                RETURNING *
            """,
            (slug, "Checkout p99 latency above SLO", datetime.now(UTC), summary),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected incident row")
    return dict(row)


def _insert_incident_event(
    conn: Any,
    *,
    incident_id: Any,
    event_type: str,
    summary: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                INSERT INTO incident_events (
                    incident_id, occurred_at, event_type, summary, metadata
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            """,
            (incident_id, datetime.now(UTC), event_type, summary, Jsonb(metadata)),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected incident event row")
    return dict(row)
