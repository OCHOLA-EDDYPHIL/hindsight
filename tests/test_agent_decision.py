"""Unit tests for the strict incident decision contract."""

import json

import pytest

from hindsight.agent_decision import (
    AGENT_DECISION_JSON_SCHEMA,
    MAX_MODEL_TURNS,
    AgentDecisionError,
    AgentDecisionV1,
    agent_decision_provider_schema,
    memory_selection_fingerprint,
    parse_agent_decision,
    recommendation_id,
)


def _payload(**overrides):
    payload = {
        "schema_version": 1,
        "diagnosis": "Checkout latency follows downstream saturation.",
        "recalled_memory_citations": [
            {"memory_id": "memory-1", "quote": "Inspect the downstream processor first."}
        ],
        "next_step_kind": "recommendation",
        "tool_call": None,
        "recommendation": "Throttle retry fanout while the processor recovers.",
        "rationale": "The retries amplify load on the constrained dependency.",
        "rollback": "Restore the previous retry policy.",
        "verification": ["Confirm checkout latency and processor depth both fall."],
        "safety_constraints": ["Do not change worker capacity from this workflow."],
    }
    payload.update(overrides)
    return payload


def test_recommendation_contract_is_strict_and_content_addressed():
    decision = parse_agent_decision(
        json.dumps(_payload()),
        recalled_memory_ids={"memory-1"},
        allowed_query_keys={"payments.checkout_latency"},
        diagnostic_calls_used=1,
        diagnostic_observation_available=True,
        model_turn=1,
    )

    assert isinstance(decision, AgentDecisionV1)
    first = recommendation_id(
        run_id="run-1",
        decision=decision,
        selection_fingerprint="selection-1",
    )
    assert first == recommendation_id(
        run_id="run-1",
        decision=decision,
        selection_fingerprint="selection-1",
    )
    assert first.startswith("recommendation:")


def test_unobserved_provider_schema_requires_exact_diagnostic_and_empty_citations():
    schema = agent_decision_provider_schema(
        recalled_memory_ids=set(),
        allowed_query_keys={"payments.retry_fanout", "payments.checkout_latency_ms"},
        diagnostic_calls_used=0,
        diagnostic_observation_available=False,
        model_turn=1,
    )

    assert schema["properties"]["next_step_kind"]["enum"] == ["diagnostic_tool"]
    assert schema["properties"]["tool_call"] == {"$ref": "#/$defs/DiagnosticToolCall"}
    assert schema["properties"]["recommendation"] == {"type": "null"}
    assert schema["properties"]["recalled_memory_citations"]["maxItems"] == 0
    assert schema["$defs"]["DiagnosticToolCall"]["properties"]["query_key"]["enum"] == [
        "payments.checkout_latency_ms",
        "payments.retry_fanout",
    ]
    assert AGENT_DECISION_JSON_SCHEMA["properties"]["next_step_kind"]["enum"] == [
        "diagnostic_tool",
        "recommendation",
    ]


def test_observed_provider_schema_exposes_both_coherent_bounded_branches():
    schema = agent_decision_provider_schema(
        recalled_memory_ids={"memory-2", "memory-1"},
        allowed_query_keys={"payments.checkout_latency_ms"},
        diagnostic_calls_used=1,
        diagnostic_observation_available=True,
        model_turn=2,
    )

    assert schema["properties"]["next_step_kind"]["enum"] == [
        "diagnostic_tool",
        "recommendation",
    ]
    assert schema["anyOf"] == [
        {
            "properties": {
                "next_step_kind": {"const": "diagnostic_tool"},
                "tool_call": {"$ref": "#/$defs/DiagnosticToolCall"},
                "recommendation": {"type": "null"},
            }
        },
        {
            "properties": {
                "next_step_kind": {"const": "recommendation"},
                "tool_call": {"type": "null"},
                "recommendation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4_000,
                },
            }
        },
    ]
    assert schema["properties"]["recalled_memory_citations"]["maxItems"] == 2
    assert schema["$defs"]["MemoryCitation"]["properties"]["memory_id"]["enum"] == [
        "memory-1",
        "memory-2",
    ]


def test_final_unobserved_turn_exposes_no_diagnostic_and_still_fails_closed():
    schema = agent_decision_provider_schema(
        recalled_memory_ids=set(),
        allowed_query_keys={"payments.checkout_latency_ms"},
        diagnostic_calls_used=0,
        diagnostic_observation_available=False,
        model_turn=MAX_MODEL_TURNS,
    )

    assert schema["properties"]["next_step_kind"]["enum"] == ["recommendation"]
    assert schema["properties"]["tool_call"] == {"type": "null"}
    assert schema["properties"]["recommendation"]["type"] == "string"

    with pytest.raises(AgentDecisionError, match="current diagnostic observation"):
        parse_agent_decision(
            json.dumps(_payload(recalled_memory_citations=[])),
            recalled_memory_ids=set(),
            allowed_query_keys={"payments.checkout_latency_ms"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=MAX_MODEL_TURNS,
        )


@pytest.mark.parametrize(
    "payload,match",
    [
        ({**_payload(), "unexpected": True}, "AgentDecisionV1"),
        (_payload(recalled_memory_citations=[{"memory_id": "other", "quote": "x"}]), "cited"),
        (
            _payload(
                next_step_kind="diagnostic_tool",
                tool_call={
                    "name": "aws_cloudwatch_diagnostics",
                    "query_key": "unconfigured.metric",
                },
                recommendation=None,
            ),
            "allowlist",
        ),
    ],
)
def test_invalid_or_unbounded_model_output_fails_closed(payload, match):
    with pytest.raises(AgentDecisionError, match=match):
        parse_agent_decision(
            json.dumps(payload),
            recalled_memory_ids={"memory-1"},
            allowed_query_keys={"payments.checkout_latency"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )


def test_final_model_turn_cannot_request_another_diagnostic():
    with pytest.raises(AgentDecisionError, match="final model turn"):
        parse_agent_decision(
            json.dumps(
                _payload(
                    next_step_kind="diagnostic_tool",
                    tool_call={
                        "name": "aws_cloudwatch_diagnostics",
                        "query_key": "payments.checkout_latency",
                    },
                    recommendation=None,
                )
            ),
            recalled_memory_ids={"memory-1"},
            allowed_query_keys={"payments.checkout_latency"},
            diagnostic_calls_used=2,
            diagnostic_observation_available=True,
            model_turn=4,
        )


def test_configured_diagnostics_require_one_current_observation_before_recommendation():
    with pytest.raises(AgentDecisionError, match="current diagnostic observation"):
        parse_agent_decision(
            json.dumps(_payload()),
            recalled_memory_ids={"memory-1"},
            allowed_query_keys={"payments.checkout_latency"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    decision = parse_agent_decision(
        json.dumps(_payload()),
        recalled_memory_ids={"memory-1"},
        allowed_query_keys={"payments.checkout_latency"},
        diagnostic_calls_used=1,
        diagnostic_observation_available=True,
        model_turn=2,
    )
    assert decision.next_step_kind == "recommendation"


def test_memory_citation_must_quote_the_recalled_version():
    with pytest.raises(AgentDecisionError, match="not a quote"):
        parse_agent_decision(
            json.dumps(_payload()),
            recalled_memory_ids={"memory-1"},
            recalled_memory_text={"memory-1": "Throttle retries only after checking saturation."},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    decision = parse_agent_decision(
        json.dumps(
            _payload(
                recalled_memory_citations=[
                    {"memory_id": "memory-1", "quote": "checking saturation"}
                ]
            )
        ),
        recalled_memory_ids={"memory-1"},
        recalled_memory_text={"memory-1": "Throttle retries only after checking saturation."},
        allowed_query_keys=set(),
        diagnostic_calls_used=0,
        diagnostic_observation_available=False,
        model_turn=1,
    )
    assert decision.recalled_memory_citations[0].memory_id == "memory-1"


def test_memory_selection_fingerprint_tracks_order_and_governance():
    memory = {
        "id": "memory-1",
        "belief_id": "belief-1",
        "version_number": 2,
        "trust_status": "active",
        "t_valid": "2026-08-09T12:00:00Z",
        "t_invalid": None,
        "profile_id": "gemini-profile",
        "distance": 0.12,
        "metadata": {
            "operator_disposition": "approved",
            "safety_status": "safe",
            "contradiction_status": "supported",
            "usage_instruction": "positive_guidance",
        },
    }

    baseline = memory_selection_fingerprint([memory])
    assert baseline == memory_selection_fingerprint([{**memory, "distance": 0.987654}])
    assert baseline != memory_selection_fingerprint(
        [{**memory, "metadata": {**memory["metadata"], "usage_instruction": "audit_only"}}]
    )
    assert baseline != memory_selection_fingerprint([memory, {**memory, "id": "memory-2"}])
