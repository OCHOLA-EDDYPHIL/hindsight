"""Tests for strict, digest-bound controlled-replay evidence."""

import math

import pytest


def test_canonical_json_rejects_ambiguous_unknown_and_nonfinite_values():
    from hindsight.causal_evidence import (
        CausalEvidenceError,
        canonical_json_bytes,
        strict_json_loads,
    )

    with pytest.raises(CausalEvidenceError, match="duplicate JSON object key"):
        strict_json_loads('{"action":"scale_workers","action":"inspect_only"}')
    with pytest.raises(CausalEvidenceError, match="non-finite"):
        strict_json_loads('{"value":NaN}')
    with pytest.raises(CausalEvidenceError, match="unsupported canonical value"):
        canonical_json_bytes({"unknown": object()})
    with pytest.raises(CausalEvidenceError, match="non-finite"):
        canonical_json_bytes({"value": math.inf})
    with pytest.raises(CausalEvidenceError, match="non-string object key"):
        canonical_json_bytes({1: "ambiguous"})
    assert canonical_json_bytes({"value": 1.0}) == canonical_json_bytes({"value": 1})


def test_causal_envelope_preserves_order_and_rejects_tampering():
    from hindsight.causal_evidence import (
        build_causal_envelope,
        text_sha256,
        validated_causal_envelope,
    )

    prompt = "Inspect the ordered evidence."
    envelope = build_causal_envelope(
        identity={"scenario_id": "scenario-1"},
        invariant_inputs={"ordered_observations": [{"sequence": 2}, {"sequence": 1}]},
        permitted_intervention={"ordered_memories": ["memory-b", "memory-a"]},
        actual_decision_inputs={"ordered_model_requests": [{"prompt": prompt}]},
        rendered_prompt_sha256=[text_sha256(prompt)],
        decision_output={"primary_action": "inspect_only"},
    )

    assert validated_causal_envelope(envelope) == envelope
    assert envelope["invariant_inputs"]["ordered_observations"] == [
        {"sequence": 2},
        {"sequence": 1},
    ]
    envelope["permitted_intervention"]["ordered_memories"].reverse()
    assert validated_causal_envelope(envelope) is None


def test_controlled_decision_rejects_duplicate_keys_and_catalog_contradictions():
    import json

    from hindsight.agent_decision import (
        PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        AgentDecisionError,
        canonicalize_operational_action,
        operational_action_fingerprint,
        parse_controlled_action_selection,
    )
    from tests.fakes import controlled_recommendation_decision

    valid = controlled_recommendation_decision(
        "inspect_only",
        "Recorded evidence supports this catalog selection.",
    )
    duplicate = valid.replace(
        '"action_id": "inspect_only"',
        '"action_id": "inspect_only", "action_id": "scale_workers"',
        1,
    )
    with pytest.raises(AgentDecisionError, match="ControlledActionSelectionV1"):
        parse_controlled_action_selection(
            duplicate,
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )

    rationale_contradiction = controlled_recommendation_decision(
        "throttle_retries",
        "Scale payment workers before checking retry fanout.",
    )
    with pytest.raises(AgentDecisionError, match="rationale"):
        parse_controlled_action_selection(
            rationale_contradiction,
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )

    for action_id, rationale in (
        ("throttle_retries", "Immediately provision more payment workers."),
        ("scale_workers", "Cut back retry attempts until the queue settles."),
        ("inspect_only", "Immediately add payment workers instead of inspecting."),
    ):
        with pytest.raises(AgentDecisionError, match="rationale"):
            parse_controlled_action_selection(
                controlled_recommendation_decision(action_id, rationale),
                contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
            )

    first = parse_controlled_action_selection(
        valid,
        contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    )
    paraphrase = parse_controlled_action_selection(
        controlled_recommendation_decision(
            "inspect_only",
            "The current telemetry supports the selected catalog action.",
        ),
        contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    )
    assert operational_action_fingerprint(
        canonicalize_operational_action(
            first,
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )
    ) == operational_action_fingerprint(
        canonicalize_operational_action(
            paraphrase,
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )
    )

    negated = controlled_recommendation_decision(
        "inspect_only",
        "Do not inspect current telemetry; scale payment workers instead.",
    )
    with pytest.raises(AgentDecisionError, match="rationale"):
        parse_controlled_action_selection(
            negated,
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )

    with pytest.raises(AgentDecisionError, match="rationale"):
        parse_controlled_action_selection(
            controlled_recommendation_decision(
                "inspect_only",
                "Scale payment workers was considered but rejected; inspect current telemetry instead.",
            ),
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )

    contradiction = json.loads(valid)
    contradiction["catalog_id"] = "another.actions.v1"
    with pytest.raises(AgentDecisionError, match="ControlledActionSelectionV1"):
        parse_controlled_action_selection(
            json.dumps(contradiction),
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )
