"""Telemetry demo ingestion for turning observability signals into incident context."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.agent import IncidentInput, IncidentAgentResult, run_incident_agent
from hindsight.db import connect, database_url
from hindsight.embeddings import DeterministicEmbeddingProvider
from hindsight.memory import MemoryStore, Provenance
from hindsight.reasoning import DeterministicReasoningProvider, ReasoningProvider


@dataclass(frozen=True)
class DemoTelemetrySignal:
    """One alert-like signal emitted by the demo service."""

    signal_id: str
    service_slug: str
    service_name: str
    owner_team: str
    alert_name: str
    severity: str
    title: str
    summary: str
    metric_name: str
    metric_value: float
    threshold: float
    started_at: datetime
    labels: dict[str, str] = field(default_factory=dict)
    log_excerpts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TelemetryIngestionResult:
    """Rows created from one telemetry signal."""

    incident: dict[str, Any]
    incident_event: dict[str, Any]
    memory: dict[str, Any]
    namespace: str


@dataclass(frozen=True)
class TelemetryDemoResult:
    """Complete demo result: telemetry ingestion plus an agent turn."""

    ingestion: TelemetryIngestionResult
    agent_result: IncidentAgentResult
    metrics: str
    logs: list[dict[str, Any]]


class DemoCheckoutService:
    """Tiny instrumented checkout service with a triggerable retry-fanout failure."""

    def __init__(self, *, service_slug: str = "payments-api"):
        self.service_slug = service_slug
        self.service_name = "Payments API"
        self.owner_team = "revenue-platform"
        self._checkout_requests = 0
        self._checkout_errors = 0
        self._checkout_latency_ms = 180.0
        self._processor_timeouts = 0
        self._retry_fanout = 1
        self._logs: list[dict[str, Any]] = []

    def checkout(self, *, failure_mode: bool = False) -> dict[str, Any]:
        """Handle one synthetic checkout request and emit structured telemetry."""

        self._checkout_requests += 1
        request_id = f"demo-checkout-{self._checkout_requests}"
        if failure_mode:
            self._checkout_errors += 1
            self._processor_timeouts += 7
            self._retry_fanout = 6
            self._checkout_latency_ms = 2450.0
            status = "degraded"
            event = {
                "event": "checkout_latency_breach",
                "request_id": request_id,
                "service": self.service_slug,
                "route": "/checkout",
                "status": status,
                "latency_ms": self._checkout_latency_ms,
                "processor_timeouts": self._processor_timeouts,
                "retry_fanout": self._retry_fanout,
            }
        else:
            status = "ok"
            self._checkout_latency_ms = 190.0
            event = {
                "event": "checkout_completed",
                "request_id": request_id,
                "service": self.service_slug,
                "route": "/checkout",
                "status": status,
                "latency_ms": self._checkout_latency_ms,
                "processor_timeouts": self._processor_timeouts,
                "retry_fanout": self._retry_fanout,
            }
        self._logs.append(event)
        return event

    def trigger_retry_fanout_failure(self) -> DemoTelemetrySignal:
        """Trigger the demo failure and return the alert-like signal."""

        self.checkout(failure_mode=True)
        return DemoTelemetrySignal(
            signal_id=f"signal-{uuid4()}",
            service_slug=self.service_slug,
            service_name=self.service_name,
            owner_team=self.owner_team,
            alert_name="checkout-p99-latency",
            severity="sev2",
            title="Checkout p99 latency above SLO",
            summary=(
                "payments-api checkout p99 latency breached the 2s SLO while "
                "processor timeouts and retry fanout rose together."
            ),
            metric_name="checkout_request_duration_ms_p99",
            metric_value=self._checkout_latency_ms,
            threshold=2000.0,
            started_at=datetime.now(UTC),
            labels={
                "route": "/checkout",
                "status_class": "5xx",
                "failure_mode": "retry_fanout",
            },
            log_excerpts=self._logs[-3:],
        )

    def prometheus_metrics(self) -> str:
        """Return Prometheus-style metrics text for the demo service."""

        lines = [
            "# HELP checkout_requests_total Total checkout requests.",
            "# TYPE checkout_requests_total counter",
            f'checkout_requests_total{{service="{self.service_slug}"}} {self._checkout_requests}',
            "# HELP checkout_errors_total Total failed checkout requests.",
            "# TYPE checkout_errors_total counter",
            f'checkout_errors_total{{service="{self.service_slug}"}} {self._checkout_errors}',
            "# HELP checkout_request_duration_ms_p99 Checkout p99 latency in milliseconds.",
            "# TYPE checkout_request_duration_ms_p99 gauge",
            (
                f'checkout_request_duration_ms_p99{{service="{self.service_slug}",'
                f'route="/checkout"}} {self._checkout_latency_ms}'
            ),
            "# HELP payment_processor_timeouts_total Downstream processor timeout count.",
            "# TYPE payment_processor_timeouts_total counter",
            (
                f'payment_processor_timeouts_total{{service="{self.service_slug}"}} '
                f"{self._processor_timeouts}"
            ),
        ]
        return "\n".join(lines) + "\n"

    @property
    def logs(self) -> list[dict[str, Any]]:
        """Return structured logs emitted by the demo service."""

        return list(self._logs)


class TelemetryIngestor:
    """Webhook-equivalent ingestion path for alert signals."""

    def __init__(self, *, db_url: str | None = None):
        self._db_url = db_url

    def ingest_signal(self, signal: DemoTelemetrySignal) -> TelemetryIngestionResult:
        """Open an incident and store telemetry excerpts as semantic memory."""

        namespace = f"telemetry:{signal.signal_id}"
        with connect(self._db_url) as conn:
            with conn.transaction():
                service = _upsert_service(conn, signal)
                incident = _upsert_incident(conn, signal)
                conn.execute(
                    """
                        INSERT INTO incident_services (incident_id, service_id, impact)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (incident_id, service_id) DO UPDATE SET
                            impact = excluded.impact
                    """,
                    (
                        incident["id"],
                        service["id"],
                        f"{signal.service_slug} breached {signal.metric_name}.",
                    ),
                )
                incident_event = _insert_incident_event(conn, signal, incident_id=incident["id"])
                memory = MemoryStore(conn=conn, embedding_provider=DeterministicEmbeddingProvider()).remember(
                    memory_kind="semantic",
                    namespace=namespace,
                    content=_memory_content(signal),
                    provenance=Provenance(
                        writer="telemetry.ingest",
                        source_ref=f"telemetry:{signal.signal_id}",
                        justification="Store alert metrics and log excerpts as incident context",
                    ),
                    metadata={
                        "signal_id": signal.signal_id,
                        "incident_slug": incident["slug"],
                        "service_slug": signal.service_slug,
                        "metric_name": signal.metric_name,
                        "metric_value": signal.metric_value,
                        "threshold": signal.threshold,
                        "labels": signal.labels,
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
        return TelemetryIngestionResult(
            incident=incident,
            incident_event=incident_event,
            memory=memory,
            namespace=namespace,
        )


def run_telemetry_demo(
    *,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
) -> TelemetryDemoResult:
    """Trigger the demo telemetry failure, ingest it, then run the incident agent."""

    service = DemoCheckoutService()
    signal = service.trigger_retry_fanout_failure()
    ingestion = TelemetryIngestor(db_url=db_url).ingest_signal(signal)
    provider = reasoning_provider or DeterministicReasoningProvider(
        response_text=(
            "Confirm processor timeout rate, throttle retry fanout, and watch checkout "
            "p99 before scaling workers."
        )
    )
    agent_result = run_incident_agent(
        IncidentInput(
            user_input=signal.summary,
            incident_id=ingestion.incident["slug"],
            namespace=ingestion.namespace,
            service_slug=signal.service_slug,
            severity=signal.severity,
            title=signal.title,
            metadata={
                "source": "telemetry-demo",
                "signal_id": signal.signal_id,
                "metric": signal.metric_name,
            },
        ),
        thread_id=f"telemetry-demo:{signal.signal_id}",
        db_url=db_url or database_url(),
        reasoning_provider=provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )
    return TelemetryDemoResult(
        ingestion=ingestion,
        agent_result=agent_result,
        metrics=service.prometheus_metrics(),
        logs=service.logs,
    )


def _upsert_service(conn: psycopg.Connection, signal: DemoTelemetrySignal) -> dict[str, Any]:
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
            (signal.service_slug, signal.service_name, signal.owner_team),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("Expected service row")
    return dict(row)


def _upsert_incident(conn: psycopg.Connection, signal: DemoTelemetrySignal) -> dict[str, Any]:
    slug = _incident_slug(signal)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                INSERT INTO incidents (
                    slug, title, severity, status, started_at, summary
                )
                VALUES (%s, %s, %s, 'open', %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title = excluded.title,
                    severity = excluded.severity,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    summary = excluded.summary
                RETURNING *
            """,
            (slug, signal.title, signal.severity, signal.started_at, signal.summary),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("Expected incident row")
    return dict(row)


def _insert_incident_event(
    conn: psycopg.Connection,
    signal: DemoTelemetrySignal,
    *,
    incident_id: Any,
) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                INSERT INTO incident_events (
                    incident_id, occurred_at, event_type, summary, metadata
                )
                VALUES (%s, %s, 'telemetry_alert', %s, %s)
                RETURNING *
            """,
            (
                incident_id,
                signal.started_at,
                signal.summary,
                Jsonb(
                    {
                        "signal_id": signal.signal_id,
                        "alert_name": signal.alert_name,
                        "metric_name": signal.metric_name,
                        "metric_value": signal.metric_value,
                        "threshold": signal.threshold,
                        "labels": signal.labels,
                        "log_excerpts": signal.log_excerpts,
                    }
                ),
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("Expected incident event row")
    return dict(row)


def _memory_content(signal: DemoTelemetrySignal) -> str:
    log_lines = [
        json.dumps(excerpt, sort_keys=True, separators=(",", ":"))
        for excerpt in signal.log_excerpts
    ]
    return "\n".join(
        [
            f"Telemetry alert {signal.alert_name} for {signal.service_slug}: {signal.summary}",
            f"Metric {signal.metric_name}={signal.metric_value} breached threshold {signal.threshold}.",
            f"Labels: {json.dumps(signal.labels, sort_keys=True)}",
            "Log excerpts:",
            *log_lines,
        ]
    )


def _incident_slug(signal: DemoTelemetrySignal) -> str:
    alert = re.sub(r"[^a-z0-9]+", "-", signal.alert_name.lower()).strip("-")
    suffix = signal.signal_id.replace("signal-", "")[:8]
    return f"telemetry-{signal.service_slug}-{alert}-{suffix}"
