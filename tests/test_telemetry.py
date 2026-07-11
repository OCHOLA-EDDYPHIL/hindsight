"""Tests for telemetry ingestion and the incident demo path."""

import os

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def test_demo_checkout_service_emits_metrics_logs_and_signal():
    from hindsight.telemetry import DemoCheckoutService

    service = DemoCheckoutService()
    signal = service.trigger_retry_fanout_failure()
    metrics = service.prometheus_metrics()

    assert signal.service_slug == "payments-api"
    assert signal.metric_name == "checkout_request_duration_ms_p99"
    assert signal.metric_value > signal.threshold
    assert "checkout_request_duration_ms_p99" in metrics
    assert "payment_processor_timeouts_total" in metrics
    assert service.logs[-1]["event"] == "checkout_latency_breach"
    assert service.logs[-1]["retry_fanout"] == 6


@requires_db
def test_telemetry_signal_opens_incident_and_writes_memory():
    from hindsight.db import connect, database_url
    from hindsight.telemetry import DemoCheckoutService, TelemetryIngestor

    service = DemoCheckoutService()
    signal = service.trigger_retry_fanout_failure()

    result = TelemetryIngestor(db_url=database_url()).ingest_signal(signal)

    assert result.incident["slug"].startswith("telemetry-payments-api-checkout-p99-latency")
    assert result.incident["status"] == "open"
    assert result.incident_event["event_type"] == "telemetry_alert"
    assert result.memory["writer"] == "telemetry.ingest"
    assert result.memory["source_ref"] == f"telemetry:{signal.signal_id}"
    assert result.namespace == f"telemetry:{signal.signal_id}"

    with connect(database_url()) as conn:
        linked = conn.execute(
            """
                SELECT relationship
                FROM incident_semantic_memories
                WHERE incident_id = %s AND memory_id = %s
            """,
            (result.incident["id"], result.memory["id"]),
        ).fetchone()
        metadata = conn.execute(
            """
                SELECT metadata
                FROM incident_events
                WHERE id = %s
            """,
            (result.incident_event["id"],),
        ).fetchone()[0]

    assert linked == ("summary",)
    assert metadata["signal_id"] == signal.signal_id
    assert metadata["log_excerpts"][0]["event"] == "checkout_latency_breach"


@requires_db
def test_telemetry_demo_runs_agent_end_to_end():
    from hindsight.db import database_url
    from hindsight.telemetry import run_telemetry_demo

    result = run_telemetry_demo(db_url=database_url())

    assert result.ingestion.incident["status"] == "open"
    assert result.agent_result.thread_id.startswith("telemetry-demo:signal-")
    assert not result.agent_result.interrupted
    assert result.agent_result.reflected_memory_id is not None
    assert "throttle retry fanout" in result.agent_result.proposed_action
    assert "checkout_request_duration_ms_p99" in result.metrics
    assert result.logs[-1]["event"] == "checkout_latency_breach"
