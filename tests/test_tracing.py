"""Tests for safe OpenTelemetry instrumentation."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


def setup_function() -> None:
    _EXPORTER.clear()


def teardown_function() -> None:
    _EXPORTER.clear()


def test_safe_attributes_drop_sensitive_keys_and_values():
    from hindsight.tracing import safe_attributes

    attributes = safe_attributes(
        {
            "hindsight.memory.namespace": "demo:payments",
            "hindsight.memory.query": "certificate rotation password token",
            "hindsight.memory.content": "raw memory content",
            "db.url": "postgresql://user:password@localhost/db",
            "hindsight.memory.ids": [uuid4()],
        }
    )

    assert attributes == {
        "hindsight.memory.namespace": "demo:payments",
        "hindsight.memory.ids": [str(attributes["hindsight.memory.ids"][0])],
    }
    assert "hindsight.memory.query" not in attributes
    assert "hindsight.memory.content" not in attributes
    assert "db.url" not in attributes


def test_configure_tracing_respects_explicit_disabled_flag(monkeypatch):
    import hindsight.tracing as tracing

    monkeypatch.setenv("HINDSIGHT_OTEL_ENABLED", "0")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr(tracing, "_CONFIGURED", False)

    assert tracing.configure_tracing_from_env(service_name="hindsight-test") is False


def test_memory_write_spans_include_ids_and_omit_content(monkeypatch):
    from hindsight.memory import Provenance

    memory_id = uuid4()
    store = _fake_store()
    monkeypatch.setattr(
        store,
        "_fetch_one",
        lambda query, params: {
            "id": memory_id,
            "namespace": "demo:payments",
            "writer": "agent.reflect",
        },
    )

    memory = store.remember(
        memory_kind="semantic",
        namespace="demo:payments",
        content="raw payment timeout memory that must not enter span attributes",
        provenance=Provenance(
            writer="agent.reflect",
            source_ref="decision:secret-source-ref",
            justification="raw justification that must not enter span attributes",
        ),
        metadata={"token": "secret"},
    )

    assert memory["id"] == memory_id
    remember = _span("hindsight.memory.remember")
    write = _span("hindsight.memory.write_semantic")
    assert remember.attributes["hindsight.memory.id"] == str(memory_id)
    assert write.attributes["hindsight.memory.id"] == str(memory_id)
    assert write.attributes["hindsight.memory.namespace"] == "demo:payments"
    assert write.attributes["hindsight.provenance.writer"] == "agent.reflect"
    _assert_no_sensitive_trace_values()


def test_recall_and_record_read_spans_include_decision_metadata(monkeypatch):
    memory_id = uuid4()
    read_id = uuid4()
    decision_id = "agent:demo:payments:plan"
    store = _fake_store()
    monkeypatch.setattr(
        store,
        "_fetch_all",
        lambda query, params: [{"id": memory_id, "namespace": "demo:payments"}],
    )
    monkeypatch.setattr(
        store,
        "_fetch_one",
        lambda query, params: {
            "id": read_id,
            "decision_id": decision_id,
            "memory_kind": "semantic",
            "memory_id": memory_id,
        },
    )

    rows = store.recall(
        namespace="demo:payments",
        query="raw recall query must not enter span attributes",
        decision_id=decision_id,
        reader="agent.recall",
        purpose="retrieve memory for planning",
    )

    assert rows == [{"id": memory_id, "namespace": "demo:payments"}]
    recall = _span("hindsight.memory.recall")
    record_read = _span("hindsight.memory.record_read")
    assert recall.attributes["hindsight.memory.ids"] == (str(memory_id),)
    assert recall.attributes["hindsight.memory.decision_id"] == decision_id
    assert recall.attributes["hindsight.memory.reader"] == "agent.recall"
    assert record_read.attributes["hindsight.memory.id"] == str(memory_id)
    assert record_read.attributes["hindsight.memory.read_id"] == str(read_id)
    _assert_no_sensitive_trace_values()


def test_rewind_span_includes_restored_and_invalidated_ids_without_reason(monkeypatch):
    restored_id = uuid4()
    poisoned_id = uuid4()
    derived_id = uuid4()
    operation_id = uuid4()
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    store = _fake_store()
    monkeypatch.setattr(
        store,
        "_semantic_beliefs_as_of",
        lambda **kwargs: [{"id": restored_id, "t_valid": timestamp}],
    )
    monkeypatch.setattr(
        store,
        "_semantic_rewind_candidates",
        lambda **kwargs: [{"id": poisoned_id, "t_valid": timestamp}],
    )
    monkeypatch.setattr(
        store,
        "_derived_semantic_memories",
        lambda **kwargs: [{"id": derived_id, "t_valid": timestamp}],
    )

    def invalidate_one(**kwargs):
        return {"id": kwargs["memory_id"], "t_valid": timestamp}

    monkeypatch.setattr(store, "_invalidate_one", invalidate_one)
    monkeypatch.setattr(
        store,
        "_record_memory_operation",
        lambda **kwargs: {
            "id": operation_id,
            "operation_type": "rewind",
            "namespace": kwargs["namespace"],
        },
    )

    result = store.rewind(
        timestamp=timestamp,
        namespace="demo:payments",
        actor="demo.operator",
        reason="operator reason with certificate secret must not enter spans",
    )

    assert [row["id"] for row in result.restored_memories] == [restored_id]
    assert {row["id"] for row in result.invalidated_memories} == {
        str(poisoned_id),
        str(derived_id),
    }
    rewind = _span("hindsight.memory.rewind")
    assert rewind.attributes["hindsight.memory.operation_id"] == str(operation_id)
    assert rewind.attributes["hindsight.memory.restored.ids"] == (str(restored_id),)
    assert set(rewind.attributes["hindsight.memory.invalidated.ids"]) == {
        str(poisoned_id),
        str(derived_id),
    }
    _assert_no_sensitive_trace_values()


def _fake_store():
    from hindsight.memory import MemoryStore

    class FakeConnection:
        autocommit = True

        def transaction(self):
            return nullcontext()

    store = MemoryStore.__new__(MemoryStore)
    store._conn = FakeConnection()
    store._url = "postgresql://user:password@localhost/db"
    store._owns_connection = False
    store._embedding_provider = None
    return store


def _span(name: str):
    matches = [span for span in _EXPORTER.get_finished_spans() if span.name == name]
    assert matches, f"missing span {name}"
    return matches[-1]


def _assert_no_sensitive_trace_values() -> None:
    forbidden = (
        "raw payment timeout memory",
        "raw recall query",
        "raw justification",
        "operator reason",
        "secret-source-ref",
        "postgresql://",
        "password",
        "token",
    )
    for span in _EXPORTER.get_finished_spans():
        for key, value in span.attributes.items():
            assert "content" not in key
            assert "query" not in key
            assert "reason" not in key
            assert "justification" not in key
            assert "source_ref" not in key
            rendered = " ".join(value) if isinstance(value, tuple) else str(value)
            assert not any(item in rendered for item in forbidden)
