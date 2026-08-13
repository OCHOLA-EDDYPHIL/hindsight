"""Unit tests for the strict incident decision contract."""

import json

import pytest

from hindsight.agent_decision import (
    AGENT_DECISION_JSON_SCHEMA,
    MAX_MODEL_TURNS,
    PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    AgentDecisionError,
    AgentDecisionV2,
    AgentDecisionV3,
    agent_decision_from_payload,
    agent_decision_provider_schema,
    memory_selection_fingerprint,
    normalize_agent_decision_provider_text,
    operational_action_fingerprint,
    parse_agent_decision,
    recommendation_id,
)
from tests.fakes import controlled_recommendation_decision


def _payload(**overrides):
    payload = {
        "schema_version": 2,
        "diagnosis": "Checkout latency follows downstream saturation.",
        "recalled_memory_citations": [
            {"memory_id": "memory-1", "quote": "Inspect the downstream processor first."}
        ],
        "next_step_kind": "recommendation",
        "tool_call": None,
        "recommendation": "Throttle retry fanout while the processor recovers.",
        "remediation_action": None,
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

    assert isinstance(decision, AgentDecisionV2)
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


def test_controlled_replay_requires_model_produced_operational_action():
    raw = controlled_recommendation_decision(
        "scale_workers",
        "Scale payment workers, then inspect queue depth.",
        citations=[
            {
                "memory_id": "memory-1",
                "quote": "Inspect the downstream processor first.",
            }
        ],
    )
    decision = parse_agent_decision(
        raw,
        recalled_memory_ids={"memory-1"},
        recalled_memory_text={"memory-1": "Inspect the downstream processor first."},
        allowed_query_keys=set(),
        diagnostic_calls_used=0,
        diagnostic_observation_available=False,
        model_turn=1,
        operational_action_contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    )

    assert isinstance(decision, AgentDecisionV3)
    assert decision.operational_action is not None
    assert decision.operational_action.primary_action == "scale_workers"
    assert operational_action_fingerprint(decision.operational_action).startswith(
        "operational_action:"
    )
    assert agent_decision_from_payload(decision.model_dump(mode="json")) == decision

    missing = json.loads(raw)
    missing.pop("operational_action")
    with pytest.raises(AgentDecisionError, match="AgentDecisionV3"):
        parse_agent_decision(
            normalize_agent_decision_provider_text(json.dumps(missing)),
            recalled_memory_ids={"memory-1"},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
            operational_action_contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )


def test_controlled_provider_schema_forbids_remediation_and_narrows_action_contract():
    observed = agent_decision_provider_schema(
        recalled_memory_ids={"memory-1"},
        allowed_query_keys={"payments.checkout_latency_ms"},
        diagnostic_calls_used=1,
        diagnostic_observation_available=True,
        model_turn=2,
        operational_action_contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    )

    assert observed["properties"]["schema_version"]["enum"] == [3]
    assert observed["properties"]["next_step_kind"]["enum"] == [
        "diagnostic_tool",
        "recommendation",
    ]
    assert "remediation_action" not in observed["properties"]
    assert "operational_action" in observed["properties"]
    assert "operational_action" not in observed["required"]
    action = observed["$defs"]["OperationalAction"]["properties"]
    assert action["contract"]["enum"] == [PAYMENTS_OPERATIONAL_ACTION_CONTRACT]
    assert action["primary_action"]["enum"] == [
        "scale_workers",
        "throttle_retries",
        "inspect_only",
    ]

    terminal = agent_decision_provider_schema(
        recalled_memory_ids={"memory-1"},
        allowed_query_keys=set(),
        diagnostic_calls_used=0,
        diagnostic_observation_available=False,
        model_turn=1,
        operational_action_contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    )
    assert terminal["properties"]["next_step_kind"]["enum"] == ["recommendation"]
    assert "operational_action" in terminal["required"]


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
    assert "recommendation" not in schema["properties"]
    assert "remediation_action" not in schema["properties"]
    assert "tool_call" in schema["required"]
    assert "anyOf" not in schema
    assert '"const"' not in json.dumps(schema)
    assert schema["properties"]["schema_version"]["enum"] == [2]
    assert schema["$defs"]["DiagnosticToolCall"]["properties"]["name"]["enum"] == [
        "aws_cloudwatch_diagnostics"
    ]
    assert schema["properties"]["recalled_memory_citations"]["maxItems"] == 0
    assert schema["$defs"]["DiagnosticToolCall"]["properties"]["query_key"]["enum"] == [
        "payments.checkout_latency_ms",
        "payments.retry_fanout",
    ]
    assert AGENT_DECISION_JSON_SCHEMA["properties"]["next_step_kind"]["enum"] == [
        "diagnostic_tool",
        "recommendation",
        "remediation_action",
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
        "remediation_action",
    ]
    assert "anyOf" not in schema
    assert {
        "tool_call",
        "recommendation",
        "remediation_action",
    }.isdisjoint(schema["required"])
    assert schema["properties"]["recalled_memory_citations"]["maxItems"] == 2
    assert schema["$defs"]["MemoryCitation"]["properties"]["memory_id"]["enum"] == [
        "memory-1",
        "memory-2",
    ]
    assert schema["$defs"]["RetractRecalledMemoryAction"]["properties"]["target_memory_id"][
        "enum"
    ] == ["memory-1", "memory-2"]


def test_provider_adapter_restores_omitted_nullable_branch_siblings_before_strict_parse():
    payload = _payload(
        recalled_memory_citations=[],
        next_step_kind="diagnostic_tool",
        tool_call={
            "name": "aws_cloudwatch_diagnostics",
            "query_key": "payments.checkout_latency",
        },
        recommendation=None,
        remediation_action=None,
    )
    payload.pop("recommendation")
    payload.pop("remediation_action")
    raw = json.dumps(payload)

    with pytest.raises(AgentDecisionError, match="AgentDecisionV2"):
        parse_agent_decision(
            raw,
            recalled_memory_ids=set(),
            allowed_query_keys={"payments.checkout_latency"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    decision = parse_agent_decision(
        normalize_agent_decision_provider_text(raw),
        recalled_memory_ids=set(),
        allowed_query_keys={"payments.checkout_latency"},
        diagnostic_calls_used=0,
        diagnostic_observation_available=False,
        model_turn=1,
    )
    assert decision.next_step_kind == "diagnostic_tool"
    assert decision.recommendation is None
    assert decision.remediation_action is None

    branchless = dict(payload)
    branchless.pop("tool_call")
    with pytest.raises(AgentDecisionError, match="AgentDecisionV2"):
        parse_agent_decision(
            normalize_agent_decision_provider_text(json.dumps(branchless)),
            recalled_memory_ids=set(),
            allowed_query_keys={"payments.checkout_latency"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    unexpected = dict(payload)
    unexpected["unexpected"] = True
    with pytest.raises(AgentDecisionError, match="AgentDecisionV2"):
        parse_agent_decision(
            normalize_agent_decision_provider_text(json.dumps(unexpected)),
            recalled_memory_ids=set(),
            allowed_query_keys={"payments.checkout_latency"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    assert normalize_agent_decision_provider_text("not-json") == "not-json"
    assert normalize_agent_decision_provider_text("[]") == "[]"


def test_remediation_action_requires_a_verbatim_citation_and_current_observation():
    payload = _payload(
        next_step_kind="remediation_action",
        recommendation=None,
        remediation_action={
            "name": "retract_recalled_memory",
            "target_memory_id": "memory-1",
            "reason": "The observed state contradicts this guidance.",
        },
    )

    with pytest.raises(AgentDecisionError, match="current diagnostic observation"):
        parse_agent_decision(
            json.dumps(payload),
            recalled_memory_ids={"memory-1"},
            recalled_memory_text={"memory-1": "Inspect the downstream processor first."},
            allowed_query_keys={"payments.checkout_latency"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    decision = parse_agent_decision(
        json.dumps(payload),
        recalled_memory_ids={"memory-1"},
        recalled_memory_text={"memory-1": "Inspect the downstream processor first."},
        allowed_query_keys={"payments.checkout_latency"},
        diagnostic_calls_used=1,
        diagnostic_observation_available=True,
        model_turn=2,
    )
    assert decision.remediation_action is not None
    assert decision.remediation_action.target_memory_id == "memory-1"

    payload["recalled_memory_citations"] = []
    with pytest.raises(AgentDecisionError, match="cited verbatim"):
        parse_agent_decision(
            json.dumps(payload),
            recalled_memory_ids={"memory-1"},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )


def test_remediation_branch_forbids_recommendation_and_tool_payloads():
    with pytest.raises(AgentDecisionError, match="AgentDecisionV2"):
        parse_agent_decision(
            json.dumps(
                _payload(
                    next_step_kind="remediation_action",
                    remediation_action={
                        "name": "retract_recalled_memory",
                        "target_memory_id": "memory-1",
                        "reason": "Retract contradicted guidance.",
                    },
                )
            ),
            recalled_memory_ids={"memory-1"},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )


def test_final_unobserved_turn_exposes_no_diagnostic_and_still_fails_closed():
    schema = agent_decision_provider_schema(
        recalled_memory_ids=set(),
        allowed_query_keys={"payments.checkout_latency_ms"},
        diagnostic_calls_used=0,
        diagnostic_observation_available=False,
        model_turn=MAX_MODEL_TURNS,
    )

    assert schema["properties"]["next_step_kind"]["enum"] == ["recommendation"]
    assert "tool_call" not in schema["properties"]
    assert schema["properties"]["recommendation"]["type"] == "string"
    assert "recommendation" in schema["required"]
    assert "remediation_action" not in schema["properties"]

    with pytest.raises(AgentDecisionError, match="current diagnostic observation"):
        parse_agent_decision(
            json.dumps(_payload(recalled_memory_citations=[])),
            recalled_memory_ids=set(),
            allowed_query_keys={"payments.checkout_latency_ms"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=MAX_MODEL_TURNS,
        )

    constrained = agent_decision_provider_schema(
        recalled_memory_ids={"memory-1"},
        allowed_query_keys={"payments.checkout_latency_ms"},
        diagnostic_calls_used=MAX_MODEL_TURNS - 1,
        diagnostic_observation_available=False,
        model_turn=MAX_MODEL_TURNS,
    )
    assert constrained["properties"]["next_step_kind"]["enum"] == ["recommendation"]
    assert "RetractRecalledMemoryAction" in constrained["$defs"]
    assert "remediation_action" not in constrained["properties"]
    assert "anyOf" not in constrained


@pytest.mark.parametrize(
    "payload,match",
    [
        ({**_payload(), "unexpected": True}, "AgentDecisionV2"),
        (
            _payload(
                recalled_memory_citations=[{"memory_id": "other", "quote": "Unknown memory quote."}]
            ),
            "cited",
        ),
        (
            _payload(
                next_step_kind="diagnostic_tool",
                tool_call={
                    "name": "aws_cloudwatch_diagnostics",
                    "query_key": "unconfigured.metric",
                },
                recommendation=None,
                remediation_action=None,
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
                    remediation_action=None,
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
    with pytest.raises(AgentDecisionError, match="AgentDecisionV2"):
        parse_agent_decision(
            json.dumps(
                _payload(
                    recalled_memory_citations=[{"memory_id": "memory-1", "quote": "processor"}]
                )
            ),
            recalled_memory_ids={"memory-1"},
            recalled_memory_text={"memory-1": "Inspect processor saturation first."},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    with pytest.raises(AgentDecisionError, match="AgentDecisionV2"):
        parse_agent_decision(
            json.dumps(
                _payload(
                    recalled_memory_citations=[{"memory_id": "memory-1", "quote": "a     b     c"}]
                )
            ),
            recalled_memory_ids={"memory-1"},
            recalled_memory_text={"memory-1": "Inspect processor saturation first."},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

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

    with pytest.raises(AgentDecisionError, match="not a quote"):
        parse_agent_decision(
            json.dumps(
                _payload(
                    recalled_memory_citations=[
                        {"memory_id": "memory-1", "quote": "checking saturation"}
                    ]
                )
            ),
            recalled_memory_ids={"memory-1"},
            recalled_memory_text={"memory-1": "Throttle retries only after Checking saturation."},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )

    decision = parse_agent_decision(
        json.dumps(
            _payload(
                recalled_memory_citations=[
                    {"memory_id": "memory-1", "quote": "checking  \n  saturation"}
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
    assert decision.recalled_memory_citations[0].quote == "checking  \n  saturation"


def test_legacy_short_citation_resumes_without_weakening_current_schema():
    legacy_payload = _payload(
        schema_version=1,
        recalled_memory_citations=[{"memory_id": "memory-1", "quote": "short quote"}],
    )
    legacy_payload.pop("remediation_action")

    resumed = agent_decision_from_payload(legacy_payload)

    assert resumed.schema_version == 2
    assert resumed.recalled_memory_citations[0].quote == "short quote"

    with pytest.raises(AgentDecisionError, match="AgentDecisionV2"):
        parse_agent_decision(
            json.dumps(
                _payload(
                    recalled_memory_citations=[
                        {"memory_id": "memory-1", "quote": "short quote"}
                    ]
                )
            ),
            recalled_memory_ids={"memory-1"},
            recalled_memory_text={"memory-1": "A short quote from a legacy memory."},
            allowed_query_keys=set(),
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
        )


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
