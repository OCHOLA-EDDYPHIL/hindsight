from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
    report = module.evaluate(fixture, results, source_revision="abc123")
    assert report["measurements"]["recall_at_k"] == 1.0
    assert report["measurements"]["mrr"] == 1.0
    assert report["raw_measurements"]
    assert "not a production SLO" in report["limitations"][1]


def test_capacity_evidence_requires_qualified_index_for_exact_main_sha():
    module = _script("validate_capacity_evidence")
    evidence = {
        "source_revision": "main-sha",
        "index_qualification": {
            "qualified": False,
            "artifact_sha256": "qualified-index-digest",
            "main_sha": "main-sha",
        },
        "targets": module.TARGETS,
        "method": {"duration_seconds": 60},
        "environment": {"database": "hosted"},
        "raw_measurements": [{"latency_ms": 10}],
        "limitations": ["Bounded run only."],
    }
    with pytest.raises(ValueError, match="qualified populated vector index"):
        module.validate(evidence, source_revision="main-sha")
    evidence["index_qualification"]["qualified"] = True
    assert module.validate(evidence, source_revision="main-sha")["claim_scope"] == (
        "benchmark_evidence_not_production_slo"
    )


def test_alert_exercise_records_only_acknowledgement_and_revision():
    module = _script("exercise_alert_delivery")

    class Client:
        def publish(self, **kwargs):
            assert kwargs["TopicArn"].endswith(":alerts")
            assert "main-sha" in kwargs["Message"]
            return {"MessageId": "message-1"}

    class Session:
        def client(self, service, *, region_name):
            assert (service, region_name) == ("sns", "us-east-1")
            return Client()

    evidence = module.publish(
        topic_arn="arn:aws:sns:us-east-1:123456789012:alerts",
        profile="test",
        source_revision="main-sha",
        session=Session(),
    )
    assert evidence["message_id"] == "message-1"
    assert evidence["source_revision"] == "main-sha"


def test_correlation_fields_drop_arbitrary_values_and_include_trace_ids():
    from opentelemetry.sdk.trace import TracerProvider
    from hindsight.observability import correlation_fields

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("correlation"):
        fields = correlation_fields({"tenant_id": "tenant", "secret": "never"})
    assert fields["tenant_id"] == "tenant"
    assert len(fields["trace_id"]) == 32
    assert "secret" not in fields
