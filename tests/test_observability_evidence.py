from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "a" * 40


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retrieval_report_preserves_raw_measurements_and_limitations():
    module = _script("evaluate_retrieval_benchmark")
    fixture = json.loads((ROOT / "fixtures/retrieval_incidents.json").read_text())
    results = {
        "environment": {"database": "isolated"},
        "cases": [
            {"query_id": row["query_id"], "memory_ids": row["relevant_memory_ids"]}
            for row in fixture["cases"]
        ],
    }
    report = module.evaluate(fixture, results, source_revision=SOURCE_REVISION)
    assert report["measurements"]["recall_at_k"] == 1.0
    assert report["measurements"]["mrr"] == 1.0
    assert report["raw_measurements"]
    assert "not a production SLO" in report["limitations"][1]


def test_capacity_evidence_requires_bound_supplemental_artifacts():
    module = _script("validate_capacity_evidence")
    evidence = {
        "schema_version": module.SCHEMA_VERSION,
        "source_revision": SOURCE_REVISION,
        "index_qualification": {
            "qualified": False,
            "artifact_sha256": "b" * 64,
            "main_sha": SOURCE_REVISION,
        },
        "targets": module.TARGETS,
        "ceilings": module.EXPECTED_CEILINGS,
        "method": {"duration_seconds": 60},
        "environment": {
            "isolation": "run_scoped_database_and_compose_project",
            "paid_model_calls": 0,
            "live_worker_invocations": 0,
        },
        "raw_measurements": [
            {"name": "vector_seed", "duration_seconds": 1},
            {
                "name": "vector_counts",
                "total": 100_000,
                "per_tenant": [
                    {"tenant_id": f"tenant-{number}", "vectors": 5_000} for number in range(20)
                ],
            },
            {
                "name": "bounded_clients",
                "clients": [
                    {
                        "client": number,
                        "qualified_index": module.EXPECTED_INDEX,
                        "prefix_spans": "[/tenant - /tenant]",
                        "plan": f"vector search table: semantic_memory_vectors@{module.EXPECTED_INDEX}",
                    }
                    for number in range(1, 21)
                ],
            },
            {
                "name": "synthetic_backlog",
                "messages_enqueued": 1_000,
                "messages_accounted_for": 1_000,
                "clients": 20,
                "per_client_counts": [50] * 20,
                "live_worker_invocations": 0,
                "paid_model_calls": 0,
            },
            {"name": "storage", "bytes": 1_000_000},
            {"name": "total", "duration_seconds": 10},
        ],
        "limitations": ["Bounded benchmark evidence; not production SLO claims."],
    }
    with pytest.raises(ValueError, match="requires qualification, cleanup, and artifact"):
        module.validate(evidence, source_revision=SOURCE_REVISION)


def test_alert_exercise_records_only_acknowledgement_and_revision():
    module = _script("exercise_alert_delivery")

    class Client:
        def publish(self, **kwargs):
            assert kwargs["TopicArn"].endswith(":alerts")
            assert SOURCE_REVISION in kwargs["Message"]
            return {"MessageId": "message-1"}

    class Session:
        def client(self, service, *, region_name):
            assert (service, region_name) == ("sns", "us-east-1")
            return Client()

    evidence = module.publish(
        topic_arn="arn:aws:sns:us-east-1:123456789012:alerts",
        profile="test",
        source_revision=SOURCE_REVISION,
        session=Session(),
    )
    assert evidence["message_id"] == "message-1"
    assert evidence["source_revision"] == SOURCE_REVISION


def test_correlation_fields_drop_arbitrary_values_and_include_trace_ids():
    from opentelemetry.sdk.trace import TracerProvider
    from hindsight.observability import correlation_fields

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("correlation"):
        fields = correlation_fields({"tenant_id": "tenant", "secret": "never"})
    assert fields["tenant_id"] == "tenant"
    assert len(fields["trace_id"]) == 32
    assert "secret" not in fields


def test_worker_record_keeps_all_correlation_identities(caplog):
    from hindsight.worker import _log_record_result

    message = {
        "tenant_id": "tenant-1",
        "run_id": "run-1",
        "dispatch_id": "dispatch-1",
        "dispatch_attempt_id": "dispatch-attempt-1",
        "attempt_id": "attempt-1",
    }
    context = type("Context", (), {"aws_request_id": "request-1"})()
    with caplog.at_level("INFO", logger="hindsight.worker"):
        _log_record_result(
            status="succeeded",
            message=message,
            message_id="message-1",
            receive_count="1",
            source_arn="arn:aws:sqs:us-east-1:123456789012:runs",
            context=context,
        )
    event = json.loads(caplog.records[-1].message)
    assert {key: event[key] for key in message} == message
