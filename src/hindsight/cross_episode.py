"""Scriptable cross-episode learning demo for M4 #21."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.agent import IncidentInput, IncidentAgentResult, run_incident_agent
from hindsight.consolidation import (
    ConsolidationResult,
    handle_incident_changefeed_event,
    incident_closed_changefeed_event,
)
from hindsight.db import connect, database_url
from hindsight.embeddings import DeterministicEmbeddingProvider
from hindsight.memory import MemoryStore, Provenance
from hindsight.reasoning import ReasoningProvider, ReasoningRequest, ReasoningResponse

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
    recalled_memory_ids: list[str]
    recalled_lesson_memory_ids: list[str]
    steps_to_resolution: int
    elapsed_ms: int


@dataclass(frozen=True)
class CrossEpisodeDemoResult:
    """Complete result for the cross-episode learning demo."""

    namespace: str
    episode_one: CrossEpisodeRunSummary
    consolidation: ConsolidationResult
    episode_two: CrossEpisodeRunSummary

    @property
    def steps_saved(self) -> int:
        return self.episode_one.steps_to_resolution - self.episode_two.steps_to_resolution

    @property
    def improvement_ratio(self) -> float:
        if self.episode_one.steps_to_resolution == 0:
            return 0.0
        return self.steps_saved / self.episode_one.steps_to_resolution


class CrossEpisodeDemoReasoningProvider:
    """Deterministic provider that shortens the plan after a consolidated lesson."""

    provider_name = "deterministic-demo"
    model_name = "cross-episode-v1"

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        prompt = request.prompt.lower()
        learned = "consolidated lesson" in prompt or "repeat guidance" in prompt
        plan = SECOND_PLAN if learned else FIRST_PLAN
        return ReasoningResponse(
            text=plan,
            provider=self.provider_name,
            model=self.model_name,
            usage={
                "prompt_characters": len(request.prompt),
                "system_characters": len(request.system or ""),
                "consolidated_lesson_seen": learned,
                "steps_to_resolution": 2 if learned else 5,
            },
        )


def run_cross_episode_demo(
    *,
    db_url: str | None = None,
    namespace: str = CROSS_EPISODE_NAMESPACE,
    keep_existing: bool = False,
    reasoning_provider: ReasoningProvider | None = None,
) -> CrossEpisodeDemoResult:
    """Run two incident episodes where the second uses the first episode's lesson."""

    resolved_db_url = db_url or database_url()
    if namespace == CROSS_EPISODE_NAMESPACE and not keep_existing:
        namespace = f"{CROSS_EPISODE_NAMESPACE}:{uuid4().hex[:8]}"
    provider = reasoning_provider or CrossEpisodeDemoReasoningProvider()
    if not keep_existing:
        reset_cross_episode_demo(namespace=namespace, db_url=resolved_db_url)

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
    consolidation_results = handle_incident_changefeed_event(
        incident_closed_changefeed_event(resolved_incident),
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

    return CrossEpisodeDemoResult(
        namespace=namespace,
        episode_one=episode_one,
        consolidation=consolidation_results[0],
        episode_two=episode_two,
    )


def reset_cross_episode_demo(*, namespace: str, db_url: str | None = None) -> None:
    """Clear prior cross-episode demo rows for one namespace."""

    resolved_db_url = db_url or database_url()
    with connect(resolved_db_url) as conn:
        rows = conn.execute(
            """
                SELECT id
                FROM semantic_memories
                WHERE namespace = %s
            """,
            (namespace,),
        ).fetchall()
        memory_ids = [row[0] for row in rows]
        if memory_ids:
            conn.execute(
                """
                    DELETE FROM memory_reads
                    WHERE memory_kind = 'semantic'
                        AND memory_id = ANY(%s)
                """,
                (memory_ids,),
            )
            conn.execute(
                """
                    DELETE FROM incident_semantic_memories
                    WHERE memory_id = ANY(%s)
                """,
                (memory_ids,),
            )
        conn.execute("DELETE FROM semantic_memory_embeddings WHERE namespace = %s", (namespace,))
        conn.execute("DELETE FROM semantic_memories WHERE namespace = %s", (namespace,))
        conn.execute(
            """
                DELETE FROM incidents
                WHERE slug LIKE %s
            """,
            (f"{namespace}:%",),
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
                embedding_provider=DeterministicEmbeddingProvider(),
            ).remember(
                memory_kind="semantic",
                namespace=namespace,
                content=(
                    f"Telemetry summary for {slug}: {summary} "
                    "Signals: checkout p99 above 2s, processor timeouts rising, retry fanout high."
                ),
                provenance=Provenance(
                    writer="telemetry.ingest",
                    source_ref=f"telemetry:{slug}",
                    justification="Store incident telemetry summary for cross-episode demo",
                ),
                metadata={
                    "demo": "cross-episode-learning",
                    "role": "incident-summary",
                    "incident_slug": slug,
                    "service_slug": SERVICE_SLUG,
                    "label": label,
                },
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
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        UPDATE incidents
                        SET status = 'resolved',
                            resolved_at = now(),
                            root_cause = %s
                        WHERE id = %s
                        RETURNING *
                    """,
                    (ROOT_CAUSE, incident_id),
                )
                incident = cur.fetchone()
            if incident is None:
                raise RuntimeError(f"incident not found: {incident_id}")
            incident = dict(incident)
            _insert_incident_event(
                conn,
                incident_id=incident["id"],
                event_type="incident_resolved",
                summary=RESOLUTION_SUMMARY,
                metadata={
                    "root_cause": ROOT_CAUSE,
                    "steps_that_worked": ["confirm_processor_timeouts", "throttle_retry_fanout"],
                    "wasted_steps": ["scale_workers_before_confirming_downstream_health"],
                },
            )
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
    started = perf_counter()
    result = run_incident_agent(
        IncidentInput(
            user_input=summary,
            incident_id=incident["slug"],
            namespace=namespace,
            service_slug=SERVICE_SLUG,
            severity=incident["severity"],
            title=incident["title"],
            metadata={"demo": "cross-episode-learning", "episode": label},
        ),
        thread_id=f"{namespace}:{label}",
        db_url=db_url,
        reasoning_provider=reasoning_provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )
    elapsed_ms = int((perf_counter() - started) * 1000)
    return _episode_summary(
        label=label,
        incident=incident,
        result=result,
        elapsed_ms=elapsed_ms,
    )


def _episode_summary(
    *,
    label: str,
    incident: dict[str, Any],
    result: IncidentAgentResult,
    elapsed_ms: int,
) -> CrossEpisodeRunSummary:
    recalled = result.state.get("recalled_memories") or []
    recalled_ids = [str(row.get("memory_id") or row.get("id")) for row in recalled]
    recalled_lesson_ids = [
        str(row.get("memory_id") or row.get("id"))
        for row in recalled
        if "consolidated lesson" in str(row.get("memory_content") or row.get("content") or "").lower()
    ]
    usage = result.state.get("reasoning", {}).get("usage", {})
    return CrossEpisodeRunSummary(
        label=label,
        incident_slug=incident["slug"],
        thread_id=result.thread_id,
        decision_id=f"agent:{result.thread_id}:plan",
        plan=result.plan,
        proposed_action=result.proposed_action,
        reflected_memory_id=result.reflected_memory_id,
        recalled_memory_ids=recalled_ids,
        recalled_lesson_memory_ids=recalled_lesson_ids,
        steps_to_resolution=int(usage.get("steps_to_resolution", 0)),
        elapsed_ms=elapsed_ms,
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
