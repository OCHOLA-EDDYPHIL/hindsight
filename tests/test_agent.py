"""Tests for the CockroachDB-backed incident agent graph."""

import os
import pathlib
import asyncio
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKPOINT_QUERY = ROOT / "queries" / "agent_checkpoint_rows.sql"
CHAT_QUERY = ROOT / "queries" / "agent_chat_history.sql"


def _database_url(name: str, *, user: str | None = None) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    if user is None:
        return urlunsplit(parts._replace(path=f"/{name}"))
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return urlunsplit(
        parts._replace(
            netloc=f"{quote(user)}@{host}{port}",
            path=f"/{name}",
        )
    )


def _create_database(name: str) -> str:
    with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return _database_url(name)


def _drop_database(name: str) -> None:
    with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_async_sqlalchemy_url_uses_async_psycopg_driver():
    from hindsight.agent import _async_sqlalchemy_url

    assert _async_sqlalchemy_url("postgresql://root@localhost/db") == (
        "cockroachdb+psycopg://root@localhost/db"
    )
    assert _async_sqlalchemy_url("cockroachdb://root@localhost/db") == (
        "cockroachdb+psycopg://root@localhost/db"
    )


def test_reasoning_prompt_uses_typed_governance_envelopes():
    from hindsight.agent import _plan_prompt

    prompt = _plan_prompt(
        {
            "incident_id": "incident-1",
            "namespace": "namespace-1",
            "user_input": "checkout latency",
            "recalled_memories": [
                {
                    "id": "memory-1",
                    "belief_id": "belief-1",
                    "version_number": 2,
                    "content": "Throttle retries after inspecting the dependency.",
                    "content_schema": "semantic.v1",
                    "transition_kind": "supersession",
                    "trust_status": "active",
                    "prompt_safety_status": "clear",
                    "writer": "operator",
                    "source_ref": "incident:resolved",
                    "justification": "Resolved incident evidence",
                    "metadata": {
                        "operator_disposition": "approved",
                        "safety_status": "safe",
                        "contradiction_status": "supported",
                        "evidence_quality": "resolved_incident",
                        "usage_instruction": "positive_guidance",
                    },
                }
            ],
        }
    )

    assert '"operator_disposition": "approved"' in prompt
    assert '"transition": "supersession"' in prompt
    assert '"usage_instruction": "positive_guidance"' in prompt
    assert '"evidence_quality": "resolved_incident"' in prompt


def test_diagnostic_action_is_selected_only_by_structured_model_output():
    from hindsight.agent import _generate_agent_decision
    from tests.fakes import DeterministicReasoningProvider, diagnostic_decision

    state = {
        "run_id": "run-1",
        "decision_id": "decision-1",
        "namespace": "live-browser:isolated-tenant",
        "incident_id": "demo-payments-checkout-latency:isolated-tenant",
        "user_input": "checkout latency is elevated",
        "recalled_memories": [
            {
                "id": "memory-1",
                "metadata": {"scenario_role": "compromised_guidance"},
            }
        ],
        "model_turn_count": 0,
        "diagnostic_call_count": 0,
    }
    provider = DeterministicReasoningProvider(
        response_text=diagnostic_decision("payments.checkout_latency_ms")
    )

    decision, response, turns = _generate_agent_decision(
        state,
        provider=provider,
        allowed_query_keys={"payments.checkout_latency_ms"},
    )

    assert decision.next_step_kind == "diagnostic_tool"
    assert decision.tool_call is not None
    assert decision.tool_call.query_key == "payments.checkout_latency_ms"
    assert response.provider == "test_deterministic"
    assert turns == 1
    assert provider.requests[0].response_json_schema is not None
    assert "role" not in provider.requests[0].prompt


def test_model_selected_retraction_preview_is_capped_at_ten_causal_effects():
    from hindsight.agent import AgentDecisionError, _bounded_retraction_effect_count

    assert _bounded_retraction_effect_count(
        {"effect_payload": {"close_memory_ids": [f"memory-{index}" for index in range(10)]}}
    ) == 10
    with pytest.raises(AgentDecisionError, match="between one and ten"):
        _bounded_retraction_effect_count(
            {
                "effect_payload": {
                    "close_memory_ids": [f"memory-{index}" for index in range(11)]
                }
            }
        )


def test_context_invalid_recommendation_repairs_to_required_diagnostic():
    from hindsight.agent import _generate_agent_decision
    from tests.fakes import (
        SequencedReasoningProvider,
        diagnostic_decision,
        recommendation_decision,
    )

    invalid_recommendation = recommendation_decision(
        "Inspect the worker before collecting current telemetry."
    )
    provider = SequencedReasoningProvider(
        [
            invalid_recommendation,
            diagnostic_decision("payments.checkout_latency_ms"),
        ]
    )
    state = {
        "run_id": "run-repair",
        "decision_id": "decision-repair",
        "namespace": "worker-recovery",
        "incident_id": "worker-recovery",
        "user_input": "A scheduled command remains pending beyond its dispatch window",
        "recalled_memories": [],
        "observations": [],
        "model_turn_count": 0,
        "diagnostic_call_count": 0,
    }

    decision, _, turns = _generate_agent_decision(
        state,
        provider=provider,
        allowed_query_keys={"payments.checkout_latency_ms"},
    )

    assert decision.next_step_kind == "diagnostic_tool"
    assert turns == 2
    assert len(provider.requests) == 2
    assert (
        "Stable repair reason: current_diagnostic_observation_required."
        in provider.requests[1].prompt
    )
    assert invalid_recommendation not in provider.requests[1].prompt
    assert provider.requests[1].response_json_schema["properties"]["next_step_kind"]["enum"] == [
        "diagnostic_tool"
    ]


def test_review_required_memory_cannot_claim_positive_guidance():
    from hindsight.agent import _governed_guidance_envelope

    envelope = _governed_guidance_envelope(
        {
            "id": "memory-rejected",
            "trust_status": "review_required",
            "metadata": {
                "operator_disposition": "rejected",
                "usage_instruction": "positive_guidance",
            },
        }
    )

    assert envelope["status"] == "review_required"
    assert envelope["usage_instruction"] == "audit_only"


def test_reflection_marks_only_cited_recalled_memories_as_causal(monkeypatch):
    import hindsight.agent as agent
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        DeterministicReasoningProvider,
        recommendation_decision,
    )

    captured = {}

    class FakeHistory:
        messages = []

        def add_message(self, message):
            self.messages.append(message)

        def close(self):
            pass

    class FakeMemoryStore:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def remember_agent_reflection(self, **kwargs):
            captured.update(kwargs)
            return {"id": "reflection-1", "belief_id": "belief-1"}

    monkeypatch.setattr(agent, "_chat_history", lambda **_kwargs: FakeHistory())
    monkeypatch.setattr(
        agent,
        "_recall_for_state",
        lambda *_args, **_kwargs: {
            "recalled_memories": [
                {"id": "memory-cited", "content": "Inspect processor saturation first."},
                {"id": "memory-context", "content": "Check unrelated cache pressure."},
            ],
            "retrieval_id": "retrieval-1",
            "selection_namespace_revision": 2,
        },
    )
    monkeypatch.setattr(agent, "MemoryStore", FakeMemoryStore)
    provider = DeterministicReasoningProvider(
        response_text=recommendation_decision(
            citations=[
                {
                    "memory_id": "memory-cited",
                    "quote": "Inspect processor saturation first.",
                }
            ]
        )
    )

    agent.build_incident_graph(
        db_url="postgresql://unused",
        reasoning_provider=provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    ).compile().invoke(
        {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "incident_id": "incident-1",
            "namespace": "namespace-1",
            "user_input": "checkout latency is above SLO",
            "metadata": {},
            "pause_before_act": False,
            "decision_id": "decision-1",
            "reasoning_steps": [],
            "model_turn_count": 0,
            "tool_calls": [],
            "observations": [],
            "diagnostic_call_count": 0,
        }
    )

    assert captured["parent_memory_ids"] == ["memory-cited"]
    assert captured["structured_payload"]["recalled_memory_ids"] == [
        "memory-cited",
        "memory-context",
    ]
    assert captured["structured_payload"]["cited_memory_ids"] == ["memory-cited"]


def test_graph_records_cloudwatch_result_then_replans_to_recommendation(monkeypatch):
    import hindsight.agent as agent
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        FakeCloudWatchDiagnostics,
        SequencedReasoningProvider,
        diagnostic_decision,
        recommendation_decision,
    )

    class FakeHistory:
        def __init__(self):
            self.messages = []

        def add_message(self, message):
            self.messages.append(message)

        def close(self):
            pass

    class FakeMemoryStore:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def remember_agent_reflection(self, **_kwargs):
            return {"id": "reflection-1", "belief_id": "belief-1"}

    history = FakeHistory()
    monkeypatch.setattr(agent, "_chat_history", lambda **_kwargs: history)
    monkeypatch.setattr(
        agent,
        "_recall_for_state",
        lambda *_args, **_kwargs: {"recalled_memories": [], "retrieval_id": "retrieval-1"},
    )
    monkeypatch.setattr(agent, "MemoryStore", FakeMemoryStore)
    reasoning = SequencedReasoningProvider(
        [
            diagnostic_decision("payments.checkout_latency_ms"),
            recommendation_decision("Throttle retry fanout after dependency inspection."),
        ]
    )
    diagnostics = FakeCloudWatchDiagnostics(
        {
            "payments.checkout_latency_ms": {
                "schema_version": 1,
                "tool": "aws_cloudwatch_diagnostics",
                "query_key": "payments.checkout_latency_ms",
                "datapoints": [{"timestamp": "2026-08-09T12:00:00Z", "value": 842.5}],
                "datapoint_count": 1,
            },
        }
    )
    model_reservations = iter((1, 2))
    diagnostic_reservations = iter((1,))
    progress = []
    graph = agent.build_incident_graph(
        db_url="postgresql://unused",
        reasoning_provider=reasoning,
        embedding_provider=DeterministicEmbeddingProvider(),
        diagnostic_tool=diagnostics,
        model_call_reservation=lambda: next(model_reservations),
        diagnostic_call_reservation=lambda: next(diagnostic_reservations),
        progress_callback=lambda phase, status, _state: progress.append((phase, status)),
    ).compile()

    state = graph.invoke(
        {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "incident_id": "incident-1",
            "namespace": "namespace-1",
            "service_slug": "payments-api",
            "user_input": "checkout latency is above SLO",
            "metadata": {},
            "pause_before_act": False,
            "decision_id": "decision-1",
            "reasoning_steps": [],
            "model_turn_count": 0,
            "tool_calls": [],
            "observations": [],
            "diagnostic_call_count": 0,
        }
    )

    assert diagnostics.calls == ["payments.checkout_latency_ms"]
    assert state["model_turn_count"] == 2
    assert state["diagnostic_call_count"] == 1
    assert ("diagnostic", "planning") in progress
    assert state["plan_payload"]["next_step_kind"] == "recommendation"
    assert state["action_trace"]["mode"] == "recommendation_only"
    assert state["action_trace"]["tool_calls"][0]["status"] == "completed"
    assert state["action_trace"]["observations"][0]["status"] == "available"
    assert state["action_trace"]["observations"][0]["datapoint_count"] == 1
    assert state["action_approved"] is False
    assert state["guidance_eligible"] is False
    assert state["reflected_memory"]["id"] == "reflection-1"
    assert "842.5" in reasoning.requests[1].prompt


def test_approved_model_selected_retraction_executes_governed_operation(monkeypatch):
    import hindsight.agent as agent
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from hindsight.tenant import tenant_scope
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        FakeCloudWatchDiagnostics,
        SequencedReasoningProvider,
        diagnostic_decision,
        retraction_decision,
    )

    class FakeHistory:
        messages = []

        def add_message(self, message):
            self.messages.append(message)

        def close(self):
            pass

    memory = {
        "id": "memory-unsafe",
        "namespace": "namespace-action",
        "belief_id": "belief-unsafe",
        "version_number": 1,
        "content": "Increase retry fanout while the processor is saturated.",
        "trust_status": "active",
        "metadata": {"usage_instruction": "positive_guidance"},
    }
    operation_calls = []
    operation_id = uuid4()
    event_id = uuid4()
    monkeypatch.setattr(agent, "_chat_history", lambda **_kwargs: FakeHistory())
    monkeypatch.setattr(
        agent,
        "_recall_for_state",
        lambda *_args, **_kwargs: {
            "recalled_memories": [memory],
            "retrieval_id": "retrieval-action",
            "selection_namespace_revision": 7,
        },
    )
    monkeypatch.setattr(
        agent,
        "preview_retraction",
        lambda **_kwargs: {
            "id": "preview-action",
            "fingerprint": "f" * 64,
            "expires_at": "2026-08-10T23:15:00Z",
            "effect_payload": {"close_memory_ids": ["memory-unsafe"]},
        },
    )

    def fake_enqueue(**kwargs):
        operation_calls.append(("enqueue", kwargs))
        return {"id": operation_id}, True

    def fake_execute(**kwargs):
        operation_calls.append(("execute", kwargs))
        return {
            "id": operation_id,
            "status": "completed",
            "events": [{"id": event_id, "sequence": 1, "status": "completed"}],
            "effects": [
                {
                    "sequence": 1,
                    "effect_type": "closed",
                    "source_memory_id": "memory-unsafe",
                }
            ],
            "invalidated_memory_ids": ["memory-unsafe"],
            "restored_memory_ids": [],
        }

    monkeypatch.setattr(agent, "enqueue_operation", fake_enqueue)
    monkeypatch.setattr(agent, "execute_operation", fake_execute)
    reasoning = SequencedReasoningProvider(
        [
            diagnostic_decision("payments.checkout_latency_ms"),
            retraction_decision(
                memory_id="memory-unsafe",
                quote="Increase retry fanout while the processor is saturated.",
            ),
        ]
    )
    diagnostics = FakeCloudWatchDiagnostics(
        {
            "payments.checkout_latency_ms": {
                "schema_version": 1,
                "tool": "aws_cloudwatch_diagnostics",
                "query_key": "payments.checkout_latency_ms",
                "datapoints": [{"timestamp": "2026-08-10T22:00:00Z", "value": 900.0}],
                "datapoint_count": 1,
            }
        }
    )
    graph = agent.build_incident_graph(
        db_url="postgresql://unused",
        reasoning_provider=reasoning,
        embedding_provider=DeterministicEmbeddingProvider(),
        diagnostic_tool=diagnostics,
    ).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread-action"}}
    tenant_id = "00000000-0000-0000-0000-000000000091"
    with tenant_scope(tenant_id):
        initial = graph.invoke(
            {
                "run_id": "run-action",
                "thread_id": "thread-action",
                "incident_id": "incident-action",
                "namespace": "namespace-action",
                "user_input": "processor saturation contradicts recalled retry guidance",
                "metadata": {},
                "pause_before_act": True,
                "decision_id": "decision-action",
                "reasoning_steps": [],
                "model_turn_count": 0,
                "tool_calls": [],
                "observations": [],
                "diagnostic_call_count": 0,
            },
            config,
        )
        approval = initial["__interrupt__"][0].value
        completed = graph.invoke(
            Command(
                resume={
                    "approved": True,
                    "remediation_action_id": approval["remediation_action_id"],
                    "selection_fingerprint": approval["selection_fingerprint"],
                    "observation_fingerprint": approval["observation_fingerprint"],
                    "preview_id": approval["preview_id"],
                    "preview_fingerprint": approval["preview_fingerprint"],
                    "actor": "product:operator:test",
                }
            ),
            config,
        )

    trace = completed["action_trace"]
    assert trace["schema_version"] == 3
    assert trace["mode"] == "governed_memory_remediation"
    assert trace["approval"]["actor"] == "product:operator:test"
    assert trace["preview"]["effect_count"] == 1
    assert trace["execution"]["status"] == "completed"
    assert trace["execution"]["events"][0]["status"] == "completed"
    assert trace["execution"]["events"][0]["id"] == str(event_id)
    assert trace["execution"]["effects"][0]["source_memory_id"] == "memory-unsafe"
    assert completed.get("reflected_memory") is None
    assert operation_calls[0][1]["actor"] == "product:operator:test"
    assert operation_calls[0][1]["idempotency_key"].startswith("agent-remediation:")
    assert operation_calls[1][1]["operation_id"] == str(operation_id)


def test_retraction_allows_one_stale_replan_then_fails_closed(monkeypatch):
    import hindsight.agent as agent
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        SequencedReasoningProvider,
        retraction_decision,
    )

    class FakeHistory:
        messages = []

        def add_message(self, message):
            self.messages.append(message)

        def close(self):
            pass

    memories = [
        {
            "id": f"memory-v{version}",
            "namespace": "namespace-stale-action",
            "belief_id": "belief-action",
            "version_number": version,
            "content": f"Unsafe retry guidance version {version}.",
            "trust_status": "active",
        }
        for version in (1, 2, 3)
    ]
    recalls = iter(memories)

    def fake_recall(*_args, **_kwargs):
        memory = next(recalls)
        return {
            "recalled_memories": [memory],
            "retrieval_id": f"retrieval-{memory['version_number']}",
            "selection_namespace_revision": memory["version_number"],
        }

    monkeypatch.setattr(agent, "_chat_history", lambda **_kwargs: FakeHistory())
    monkeypatch.setattr(agent, "_recall_for_state", fake_recall)
    monkeypatch.setattr(
        agent,
        "preview_retraction",
        lambda root_memory_id, **_kwargs: {
            "id": f"preview-{root_memory_id}",
            "fingerprint": ("1" if root_memory_id.endswith("1") else "2") * 64,
            "expires_at": "2026-08-10T23:15:00Z",
            "effect_payload": {"close_memory_ids": [root_memory_id]},
        },
    )
    reasoning = SequencedReasoningProvider(
        [
            retraction_decision(memory_id="memory-v1", quote="Unsafe retry guidance version 1."),
            retraction_decision(memory_id="memory-v2", quote="Unsafe retry guidance version 2."),
        ]
    )
    graph = agent.build_incident_graph(
        db_url="postgresql://unused",
        reasoning_provider=reasoning,
        embedding_provider=DeterministicEmbeddingProvider(),
    ).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread-stale-action"}}
    initial = graph.invoke(
        {
            "run_id": "run-stale-action",
            "thread_id": "thread-stale-action",
            "incident_id": "incident-stale-action",
            "namespace": "namespace-stale-action",
            "user_input": "recalled retry guidance is unsafe",
            "metadata": {},
            "pause_before_act": True,
            "decision_id": "decision-stale-action",
            "reasoning_steps": [],
            "model_turn_count": 0,
            "tool_calls": [],
            "observations": [],
            "diagnostic_call_count": 0,
        },
        config,
    )
    first = initial["__interrupt__"][0].value
    replanned = graph.invoke(
        Command(
            resume={
                "approved": True,
                "remediation_action_id": first["remediation_action_id"],
                "selection_fingerprint": first["selection_fingerprint"],
                "observation_fingerprint": first["observation_fingerprint"],
                "preview_id": first["preview_id"],
                "preview_fingerprint": first["preview_fingerprint"],
                "actor": "product:operator:test",
            }
        ),
        config,
    )
    second = replanned["__interrupt__"][0].value
    assert replanned["stale_replan_count"] == 1
    assert second["remediation_action_id"] != first["remediation_action_id"]

    with pytest.raises(agent.RemediationActionError, match="single replan"):
        graph.invoke(
            Command(
                resume={
                    "approved": True,
                    "remediation_action_id": second["remediation_action_id"],
                    "selection_fingerprint": second["selection_fingerprint"],
                    "observation_fingerprint": second["observation_fingerprint"],
                    "preview_id": second["preview_id"],
                    "preview_fingerprint": second["preview_fingerprint"],
                    "actor": "product:operator:test",
                }
            ),
            config,
        )


def test_unavailable_cloudwatch_result_cannot_authorize_a_recommendation(monkeypatch):
    import hindsight.agent as agent
    from hindsight.cloudwatch_diagnostics import CloudWatchDiagnosticsUnavailableError
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        FakeCloudWatchDiagnostics,
        SequencedReasoningProvider,
        diagnostic_decision,
        recommendation_decision,
    )

    class FakeHistory:
        messages = []

        def add_message(self, message):
            self.messages.append(message)

        def close(self):
            pass

    monkeypatch.setattr(agent, "_chat_history", lambda **_kwargs: FakeHistory())
    monkeypatch.setattr(
        agent,
        "_recall_for_state",
        lambda *_args, **_kwargs: {"recalled_memories": [], "retrieval_id": "retrieval-1"},
    )
    reasoning = SequencedReasoningProvider(
        [
            diagnostic_decision("payments.checkout_latency_ms"),
            recommendation_decision(),
            recommendation_decision(),
        ]
    )
    diagnostics = FakeCloudWatchDiagnostics(
        {
            "payments.checkout_latency_ms": CloudWatchDiagnosticsUnavailableError(
                "cloudwatch_timeout",
                "CloudWatch diagnostics request timed out or could not connect",
            )
        }
    )
    model_reservations = iter((1, 2, 3))
    graph = agent.build_incident_graph(
        db_url="postgresql://unused",
        reasoning_provider=reasoning,
        embedding_provider=DeterministicEmbeddingProvider(),
        diagnostic_tool=diagnostics,
        model_call_reservation=lambda: next(model_reservations),
        diagnostic_call_reservation=lambda: 1,
    ).compile()

    with pytest.raises(agent.AgentDecisionError, match="valid bounded decision"):
        graph.invoke(
            {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "incident_id": "incident-1",
                "namespace": "namespace-1",
                "user_input": "checkout latency is above SLO",
                "metadata": {},
                "pause_before_act": False,
                "decision_id": "decision-1",
                "reasoning_steps": [],
                "model_turn_count": 0,
                "tool_calls": [],
                "observations": [],
                "diagnostic_call_count": 0,
            }
        )

    assert diagnostics.calls == ["payments.checkout_latency_ms"]
    assert "cloudwatch_timeout" in reasoning.requests[1].prompt


def test_concurrent_memory_change_invalidates_approval_and_forces_replan(monkeypatch):
    import hindsight.agent as agent
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        SequencedReasoningProvider,
        recommendation_decision,
    )

    class FakeHistory:
        messages = []

        def add_message(self, message):
            self.messages.append(message)

        def close(self):
            pass

    selections = [
        [{"id": "memory-v1", "belief_id": "belief-1", "version_number": 1}],
        [{"id": "memory-v2", "belief_id": "belief-1", "version_number": 2}],
    ]

    def fake_recall(*_args, **_kwargs):
        return {
            "recalled_memories": selections.pop(0),
            "retrieval_id": f"retrieval-{2 - len(selections)}",
        }

    monkeypatch.setattr(agent, "_chat_history", lambda **_kwargs: FakeHistory())
    monkeypatch.setattr(agent, "_recall_for_state", fake_recall)
    reasoning = SequencedReasoningProvider(
        [
            recommendation_decision("Apply the first bounded recommendation."),
            recommendation_decision("Apply the replanned bounded recommendation."),
        ]
    )
    graph = agent.build_incident_graph(
        db_url="postgresql://unused",
        reasoning_provider=reasoning,
        embedding_provider=DeterministicEmbeddingProvider(),
    ).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread-stale"}}
    initial = graph.invoke(
        {
            "run_id": "run-stale",
            "thread_id": "thread-stale",
            "incident_id": "incident-stale",
            "namespace": "namespace-stale",
            "service_slug": "payments-api",
            "user_input": "checkout latency is above SLO",
            "metadata": {},
            "pause_before_act": True,
            "decision_id": "decision-stale",
            "reasoning_steps": [],
            "model_turn_count": 0,
            "tool_calls": [],
            "observations": [],
            "diagnostic_call_count": 0,
        },
        config,
    )
    first_interrupt = initial["__interrupt__"][0].value

    replanned = graph.invoke(
        Command(
            resume={
                "approved": True,
                "recommendation_id": first_interrupt["recommendation_id"],
                "selection_fingerprint": first_interrupt["selection_fingerprint"],
            }
        ),
        config,
    )
    second_interrupt = replanned["__interrupt__"][0].value

    assert len(reasoning.requests) == 2
    assert replanned["model_turn_count"] == 2
    assert second_interrupt["recommendation_id"] != first_interrupt["recommendation_id"]
    assert second_interrupt["selection_fingerprint"] != first_interrupt["selection_fingerprint"]
    assert "replanned" in second_interrupt["proposed_action"].lower()


def test_memory_change_during_reflection_rolls_back_and_forces_replan(monkeypatch):
    import hindsight.agent as agent
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        SequencedReasoningProvider,
        recommendation_decision,
    )

    class FakeHistory:
        messages = []

        def add_message(self, message):
            self.messages.append(message)

        def close(self):
            pass

    class ChangedMemoryStore:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def remember_agent_reflection(self, **_kwargs):
            raise agent.MemorySelectionChangedError("memory selection changed before reflection")

    selections = [
        {
            "recalled_memories": [
                {"id": "memory-v1", "belief_id": "belief-1", "version_number": 1}
            ],
            "retrieval_id": "retrieval-initial",
            "selection_namespace_revision": 1,
        },
        {
            "recalled_memories": [
                {"id": "memory-v1", "belief_id": "belief-1", "version_number": 1}
            ],
            "retrieval_id": "retrieval-approval",
            "selection_namespace_revision": 1,
        },
        {
            "recalled_memories": [
                {"id": "memory-v2", "belief_id": "belief-1", "version_number": 2}
            ],
            "retrieval_id": "retrieval-reflection-conflict",
            "selection_namespace_revision": 2,
        },
    ]

    monkeypatch.setattr(agent, "_chat_history", lambda **_kwargs: FakeHistory())
    monkeypatch.setattr(agent, "_recall_for_state", lambda *_args, **_kwargs: selections.pop(0))
    monkeypatch.setattr(agent, "MemoryStore", ChangedMemoryStore)
    reasoning = SequencedReasoningProvider(
        [
            recommendation_decision("Apply the approval-bound recommendation."),
            recommendation_decision("Apply the transactionally replanned recommendation."),
        ]
    )
    graph = agent.build_incident_graph(
        db_url="postgresql://unused",
        reasoning_provider=reasoning,
        embedding_provider=DeterministicEmbeddingProvider(),
    ).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread-reflection-race"}}
    initial = graph.invoke(
        {
            "run_id": "run-reflection-race",
            "thread_id": "thread-reflection-race",
            "incident_id": "incident-reflection-race",
            "namespace": "namespace-reflection-race",
            "service_slug": "payments-api",
            "user_input": "checkout latency is above SLO",
            "metadata": {},
            "pause_before_act": True,
            "decision_id": "decision-reflection-race",
            "reasoning_steps": [],
            "model_turn_count": 0,
            "tool_calls": [],
            "observations": [],
            "diagnostic_call_count": 0,
        },
        config,
    )
    first_interrupt = initial["__interrupt__"][0].value

    replanned = graph.invoke(
        Command(
            resume={
                "approved": True,
                "recommendation_id": first_interrupt["recommendation_id"],
                "selection_fingerprint": first_interrupt["selection_fingerprint"],
            }
        ),
        config,
    )
    second_interrupt = replanned["__interrupt__"][0].value

    assert selections == []
    assert len(reasoning.requests) == 2
    assert replanned["model_turn_count"] == 2
    assert second_interrupt["recommendation_id"] != first_interrupt["recommendation_id"]
    assert second_interrupt["selection_fingerprint"] != first_interrupt["selection_fingerprint"]
    assert "transactionally replanned" in second_interrupt["proposed_action"].lower()
    assert replanned.get("reflected_memory") is None


def test_recall_does_not_silently_fall_back_after_vector_error(monkeypatch):
    import hindsight.agent as agent
    from tests.fakes import DeterministicEmbeddingProvider

    calls = []

    class FakeMemoryStore:
        def __init__(self, *, url, embedding_provider=None):
            self.embedding_provider = embedding_provider
            calls.append(embedding_provider is not None)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def retrieve_semantic(self, **kwargs):
            raise RuntimeError("semantic_memory_embeddings is missing")

        def namespace_revision(self, **_kwargs):
            return 0

    monkeypatch.setattr(agent, "MemoryStore", FakeMemoryStore)

    with pytest.raises(RuntimeError, match="governed retrieval failed"):
        agent._recall_for_state(
            {
                "namespace": "incident-test",
                "service_slug": "payments-api",
                "user_input": "checkout latency",
                "decision_id": "agent:test:plan",
            },
            db_url="postgresql://db",
            embedding_provider=DeterministicEmbeddingProvider(),
        )

    assert calls == [True]


def test_agent_storage_initializer_is_idempotent(monkeypatch):
    import hindsight.agent as agent

    calls = {"checkpoint": 0, "chat": 0}

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, db_url):
            assert db_url == "postgresql://db"
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def setup(self):
            calls["checkpoint"] += 1

    class FakeHistory:
        def create_table_if_not_exists(self):
            calls["chat"] += 1

        def close(self):
            pass

    monkeypatch.setattr(agent, "CockroachDBSaver", FakeSaver)
    monkeypatch.setattr(agent, "_chat_history", lambda **kwargs: FakeHistory())

    agent.setup_agent_storage(db_url="postgresql://db")
    agent.setup_agent_storage(db_url="postgresql://db")

    assert calls == {"checkpoint": 2, "chat": 2}


def test_agent_storage_validation_reports_missing_objects_without_ddl(monkeypatch):
    import hindsight.agent as agent
    from psycopg import errors

    statements = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def execute(self, statement):
            statements.append(statement)
            if "FROM checkpoint_blobs" in statement:
                raise errors.UndefinedTable("checkpoint_blobs is missing")

    monkeypatch.setattr(agent, "connect", lambda *args, **kwargs: FakeConnection())

    with pytest.raises(
        agent.AgentStorageNotInitializedError,
        match="checkpoint_blobs is missing or incompatible",
    ):
        agent.validate_agent_storage(db_url="postgresql://db")

    assert statements
    assert all(
        not statement.lstrip().upper().startswith(("ALTER", "CREATE", "DROP"))
        for statement in statements
    )


@requires_db
def test_start_reports_uninitialized_storage_without_creating_it():
    from hindsight.agent import (
        AgentStorageNotInitializedError,
        IncidentInput,
        run_incident_agent,
    )
    from tests.fakes import DeterministicEmbeddingProvider, DeterministicReasoningProvider

    database_name = f"hindsight_agent_missing_{uuid4().hex}"
    target_url = _create_database(database_name)
    try:
        with pytest.raises(
            AgentStorageNotInitializedError,
            match="checkpoint_migrations is missing or incompatible",
        ):
            run_incident_agent(
                IncidentInput(user_input="latency", incident_id="incident-1"),
                db_url=target_url,
                reasoning_provider=DeterministicReasoningProvider(response_text="inspect"),
                embedding_provider=DeterministicEmbeddingProvider(),
            )

        with psycopg.connect(target_url) as conn:
            persistence_tables = conn.execute(
                """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name IN (
                          'agent_chat_messages', 'checkpoint_blobs',
                          'checkpoint_migrations', 'checkpoint_writes', 'checkpoints'
                      )
                """
            ).fetchall()
        assert persistence_tables == []
    finally:
        _drop_database(database_name)


def test_sync_agent_entrypoint_rejects_running_event_loop():
    from hindsight.agent import IncidentInput, run_incident_agent

    async def call_sync_helper():
        with pytest.raises(RuntimeError, match="synchronous helpers"):
            run_incident_agent(
                IncidentInput(user_input="latency", incident_id="incident-1"),
                db_url="postgresql://db",
            )

    asyncio.run(call_sync_helper())


def test_async_run_incident_agent_wraps_sync_graph(monkeypatch):
    import hindsight.agent as agent
    from tests.fakes import DeterministicEmbeddingProvider, DeterministicReasoningProvider

    calls = []
    reasoning_provider = DeterministicReasoningProvider(response_text="plan")
    embedding_provider = DeterministicEmbeddingProvider()

    def fake_invoke_graph(input_or_command, **kwargs):
        calls.append((input_or_command, kwargs))
        return {
            "plan": "check dependencies",
            "proposed_action": "review rollback",
            "reflected_memory": {"id": "memory-1"},
        }

    monkeypatch.setattr(agent, "_invoke_graph", fake_invoke_graph)

    async def call_async_helper():
        return await agent.run_incident_agent_async(
            agent.IncidentInput(
                user_input="latency",
                incident_id="incident-1",
                namespace="namespace-1",
            ),
            thread_id="thread-1",
            pause_before_act=True,
            db_url="postgresql://db",
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
        )

    result = asyncio.run(call_async_helper())

    assert result.thread_id == "thread-1"
    assert result.plan == "check dependencies"
    assert result.reflected_memory_id == "memory-1"
    state, kwargs = calls[0]
    assert state["thread_id"] == "thread-1"
    assert state["incident_id"] == "incident-1"
    assert state["namespace"] == "namespace-1"
    assert state["pause_before_act"] is True
    assert state["run_id"]
    assert state["decision_id"] == f"agent:{state['run_id']}:plan"
    assert kwargs == {
        "thread_id": "thread-1",
        "db_url": "postgresql://db",
        "reasoning_provider": reasoning_provider,
        "embedding_provider": embedding_provider,
        "diagnostic_tool": None,
        "model_call_reservation": None,
        "diagnostic_call_reservation": None,
        "progress_callback": None,
    }


def test_async_resume_incident_agent_wraps_sync_graph(monkeypatch):
    import hindsight.agent as agent
    from tests.fakes import DeterministicEmbeddingProvider, DeterministicReasoningProvider

    calls = []
    reasoning_provider = DeterministicReasoningProvider(response_text="plan")
    embedding_provider = DeterministicEmbeddingProvider()

    def fake_invoke_graph(input_or_command, **kwargs):
        calls.append((input_or_command, kwargs))
        return {
            "action_approved": False,
            "proposed_action": "hold change",
            "reflected_memory": {"id": "memory-2"},
        }

    monkeypatch.setattr(agent, "_invoke_graph", fake_invoke_graph)

    async def call_async_helper():
        return await agent.resume_incident_agent_async(
            thread_id="thread-2",
            approved=False,
            recommendation_id=f"recommendation:{'a' * 64}",
            selection_fingerprint="b" * 64,
            db_url="postgresql://db",
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
        )

    result = asyncio.run(call_async_helper())

    assert result.thread_id == "thread-2"
    assert result.proposed_action == "hold change"
    assert result.reflected_memory_id == "memory-2"
    command, kwargs = calls[0]
    assert command.resume == {
        "approved": False,
        "recommendation_id": f"recommendation:{'a' * 64}",
        "selection_fingerprint": "b" * 64,
    }
    assert kwargs == {
        "thread_id": "thread-2",
        "db_url": "postgresql://db",
        "reasoning_provider": reasoning_provider,
        "embedding_provider": embedding_provider,
        "diagnostic_tool": None,
        "model_call_reservation": None,
        "diagnostic_call_reservation": None,
        "progress_callback": None,
    }


@requires_db
def test_incident_graph_checkpoints_and_reflects_to_memory():
    from hindsight.agent import IncidentInput, run_incident_agent
    from hindsight.db import connect, database_url
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        DeterministicReasoningProvider,
        recommendation_decision,
    )

    thread_id = f"agent-{uuid4()}"
    result = run_incident_agent(
        IncidentInput(
            user_input="payments-api checkout latency is above SLO after retry fanout",
            incident_id=f"incident-{uuid4()}",
            namespace=f"agent-test-{uuid4()}",
            service_slug="payments-api",
            severity="sev2",
            title="Checkout latency",
        ),
        thread_id=thread_id,
        reasoning_provider=DeterministicReasoningProvider(
            response_text=recommendation_decision(
                "Throttle retry fanout, check processor latency, and watch checkout SLO."
            )
        ),
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert not result.interrupted
    assert "Throttle retry fanout" in result.plan
    assert result.proposed_action is not None
    assert result.reflected_memory_id is not None

    with connect(database_url()) as conn:
        checkpoint_rows = conn.execute(CHECKPOINT_QUERY.read_text(), (thread_id,)).fetchall()
        chat_rows = conn.execute(CHAT_QUERY.read_text(), (thread_id,)).fetchall()
        reflection = conn.execute(
            """
                SELECT plan, proposed_action, semantic_memory_id, belief_id
                FROM agent_reflections WHERE decision_id = %s
            """,
            (result.state["decision_id"],),
        ).fetchone()

    assert checkpoint_rows
    assert {row[1] for row in chat_rows} == {"human", "ai"}
    assert "checkout latency" in chat_rows[0][2]
    assert reflection is not None
    assert reflection[0] == result.plan
    assert reflection[1] == result.proposed_action
    assert str(reflection[2]) == result.reflected_memory_id
    assert reflection[3] is not None


@requires_db
def test_reflection_projection_failure_rolls_back_semantic_output(monkeypatch):
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore, Provenance

    decision_id = f"reflection-rollback:{uuid4()}"
    namespace = f"reflection-rollback-{uuid4()}"
    conn = connect()
    try:
        with conn.transaction():
            store = MemoryStore(conn=conn)
            store.open_decision(
                decision_id=decision_id,
                actor="agent.reflect",
                decision_kind="agent_plan",
                purpose="verify atomic reflection persistence",
                namespace=namespace,
            )
        store = MemoryStore(conn=conn)

        def fail_projection(**kwargs):
            raise RuntimeError("projection failed")

        monkeypatch.setattr(store, "record_agent_reflection", fail_projection)
        with pytest.raises(RuntimeError, match="projection failed"):
            store.remember_agent_reflection(
                decision_id=decision_id,
                run_id=str(uuid4()),
                thread_id=f"thread-{uuid4()}",
                incident_id=f"incident-{uuid4()}",
                namespace=namespace,
                service_slug=None,
                plan="inspect dependencies",
                proposed_action="hold the rollout",
                action_approved=False,
                content="typed reflection rollback sentinel",
                metadata={},
                structured_payload={"schema_version": 1},
                provenance=Provenance(
                    "agent.reflect", decision_id, "verify atomic reflection persistence"
                ),
                parent_memory_ids=[],
            )
    finally:
        conn.rollback()
        conn.close()

    with connect(database_url()) as verifier:
        assert verifier.execute(
            "SELECT count(*) FROM semantic_memories WHERE producer_decision_id = %s",
            (decision_id,),
        ).fetchone() == (0,)
        assert verifier.execute(
            "SELECT count(*) FROM agent_reflections WHERE decision_id = %s",
            (decision_id,),
        ).fetchone() == (0,)
        assert verifier.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (decision_id,)
        ).fetchone() == ("open",)


@requires_db
def test_incident_graph_interrupt_resumes_from_cockroachdb_checkpoint():
    from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent
    from hindsight.db import connect, database_url
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        DeterministicReasoningProvider,
        recommendation_decision,
    )

    thread_id = f"agent-resume-{uuid4()}"
    incident = IncidentInput(
        user_input="search-api error rate spiked after the deploy",
        incident_id=f"incident-{uuid4()}",
        namespace=f"agent-resume-test-{uuid4()}",
        service_slug="search-api",
        severity="sev2",
        title="Search error spike",
    )
    reasoning_provider = DeterministicReasoningProvider(
        response_text=recommendation_decision(
            "Roll back the deploy candidate and verify error rate."
        )
    )
    first = run_incident_agent(
        incident,
        thread_id=thread_id,
        pause_before_act=True,
        reasoning_provider=reasoning_provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert first.interrupted
    assert first.interrupt["thread_id"] == thread_id
    assert "proposed_action" in first.interrupt

    resumed = resume_incident_agent(
        thread_id=thread_id,
        approved=True,
        recommendation_id=first.interrupt["recommendation_id"],
        selection_fingerprint=first.interrupt["selection_fingerprint"],
        reasoning_provider=reasoning_provider,
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert not resumed.interrupted
    assert resumed.state["action_approved"] is True
    assert resumed.reflected_memory_id is not None

    with connect(database_url()) as conn:
        checkpoint_rows = conn.execute(CHECKPOINT_QUERY.read_text(), (thread_id,)).fetchall()
        chat_rows = conn.execute(CHAT_QUERY.read_text(), (thread_id,)).fetchall()
        reflected_trust = conn.execute(
            "SELECT trust_status FROM semantic_memories WHERE id = %s",
            (resumed.reflected_memory_id,),
        ).fetchone()

    assert len(checkpoint_rows) >= 2
    assert [row[1] for row in chat_rows] == ["human", "ai"]
    assert "Roll back the deploy candidate" in chat_rows[1][2]
    assert reflected_trust == ("review_required",)


@requires_db
def test_rejected_reflection_is_auditable_but_not_positive_retrieval():
    from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent
    from hindsight.db import database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore
    from tests.fakes import DeterministicReasoningProvider, recommendation_decision

    thread_id = f"agent-rejected-{uuid4()}"
    namespace = f"demo:payments-poison-rewind:rejected:{uuid4()}"
    provider = DeterministicEmbeddingProvider()

    reasoning_provider = DeterministicReasoningProvider(
        response_text=recommendation_decision(
            "Roll back the deploy candidate and verify error rate."
        )
    )
    first = run_incident_agent(
        IncidentInput(
            user_input="search-api error rate spiked after the deploy",
            incident_id=f"incident-{uuid4()}",
            namespace=namespace,
            service_slug="search-api",
        ),
        thread_id=thread_id,
        pause_before_act=True,
        reasoning_provider=reasoning_provider,
        embedding_provider=provider,
    )
    assert first.interrupted

    rejected = resume_incident_agent(
        thread_id=thread_id,
        approved=False,
        recommendation_id=first.interrupt["recommendation_id"],
        selection_fingerprint=first.interrupt["selection_fingerprint"],
        reasoning_provider=reasoning_provider,
        embedding_provider=provider,
    )
    memory_id = str(rejected.reflected_memory_id)
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        audit = store.audit_memory(memory_kind="semantic", memory_id=memory_id)
        result = store.retrieve_semantic(
            namespace=namespace,
            query="roll back deploy candidate",
            decision_id=f"rejected-retrieval:{uuid4()}",
            reader="pytest",
            purpose="prove rejected reflections are not positive guidance",
        )

    assert audit is not None
    assert audit["trust_status"] == "review_required"
    assert audit["structured_payload"]["action_approved"] is False
    assert rejected.state["action_trace"]["execution"]["status"] == "not_executed"
    assert rejected.state["action_trace"]["observations"] == []
    assert memory_id not in {str(hit["id"]) for hit in result.hits}


@requires_db
@pytest.mark.migration_acceptance
def test_preinitialized_agent_storage_supports_start_and_resume_without_create_privilege():
    from hindsight.agent import (
        IncidentInput,
        resume_incident_agent,
        run_incident_agent,
        setup_agent_storage,
    )
    from tests.fakes import (
        DeterministicEmbeddingProvider,
        DeterministicReasoningProvider,
        recommendation_decision,
    )

    database_name = f"hindsight_agent_runtime_{uuid4().hex}"
    role_name = f"agent_runtime_{uuid4().hex}"
    target_url = _create_database(database_name)
    try:
        with psycopg.connect(target_url, autocommit=True) as conn:
            for path in sorted((ROOT / "migrations").glob("[0-9]*.sql")):
                with conn.transaction():
                    conn.execute(path.read_text())

        setup_agent_storage(db_url=target_url)
        setup_agent_storage(db_url=target_url)

        with psycopg.connect(target_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role_name)))
            conn.execute((ROOT / "infra/db/roles.sql").read_text())
            conn.execute(
                sql.SQL("GRANT hindsight_memory_worker TO {}").format(sql.Identifier(role_name))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name),
                    sql.Identifier(role_name),
                )
            )

        runtime_url = _database_url(database_name, user=role_name)
        with psycopg.connect(runtime_url, autocommit=True) as runtime_conn:
            assert runtime_conn.execute("SELECT current_user").fetchone() == (role_name,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime_conn.execute("CREATE TABLE runtime_schema_change (id INT PRIMARY KEY)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime_conn.execute("DELETE FROM semantic_memories WHERE false")

        thread_id = f"restricted-role-{uuid4()}"
        reasoning_provider = DeterministicReasoningProvider(
            response_text=recommendation_decision(
                "Roll back the deploy candidate and verify error rate."
            )
        )
        first = run_incident_agent(
            IncidentInput(
                user_input="search-api error rate spiked after the deploy",
                incident_id=f"incident-{uuid4()}",
                namespace=f"restricted-role-{uuid4()}",
                service_slug="search-api",
                severity="sev2",
                title="Search error spike",
            ),
            thread_id=thread_id,
            pause_before_act=True,
            db_url=runtime_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=DeterministicEmbeddingProvider(),
        )
        assert first.interrupted

        resumed = resume_incident_agent(
            thread_id=thread_id,
            approved=True,
            recommendation_id=first.interrupt["recommendation_id"],
            selection_fingerprint=first.interrupt["selection_fingerprint"],
            db_url=runtime_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=DeterministicEmbeddingProvider(),
        )
        assert not resumed.interrupted
        assert resumed.reflected_memory_id is not None
    finally:
        _drop_database(database_name)
        with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name)))
