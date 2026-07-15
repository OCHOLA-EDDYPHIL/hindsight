"""Protocol-level regression coverage for the live learning benchmark."""

from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from hindsight.benchmark import (
    ARMS,
    ALL_SIMULATOR_ACTIONS,
    HELD_OUT_SELECTION_METHOD,
    IncidentSimulator,
    ScientificTrialFailure,
    _choose_action,
    _canonical_preregistration_for_report,
    _binding_history_complete,
    _deterministic_arm_order,
    _deterministic_held_out_order,
    _exact_sign_flip_p_value,
    _identity_lineage_complete,
    _mechanism_level_differences,
    _mark_trial_infrastructure_failed,
    _mark_trial_scientific_failed,
    _parse_action_response,
    _retrieved_context_parity,
    _report_digests,
    _target_bindings_complete,
    _trial_blocks,
    _variant_level_differences,
    power_analysis,
    preregister_confirmation,
)
from hindsight.reasoning import (
    ReasoningProviderError,
    ReasoningResponse,
    retrying_reasoning_provider,
)


def _additional_contract(variant_ids: list[str]) -> dict[str, object]:
    mechanisms = (
        "retry_amplification",
        "cache_stampede",
        "connection_leak",
        "hot_partition",
        "poison_message",
        "lock_contention",
    )
    return {
        "corpus_schema_version": 3,
        "corpus_sha256": "corpus-digest",
        "held_out_variant_sha256": {variant_id: f"hash:{variant_id}" for variant_id in variant_ids},
        "embedding_max_distance": 0.4,
        "retrieval_rank_requirement": 1,
        "arm_context_policy": "matched-distractors-v1",
        "source_evidence_policy": "lesson-only-v1",
        "study_key_sha256": "study",
        "claim_family_sha256": "claim-family",
        "code_sha": "a" * 40,
        "variant_query_sha256": {variant_id: f"query:{variant_id}" for variant_id in variant_ids},
        "variant_simulator_kind": {
            variant_id: mechanisms[index % len(mechanisms)]
            for index, variant_id in enumerate(sorted(variant_ids))
        },
        "action_vocabulary": list(ALL_SIMULATOR_ACTIONS),
        "action_vocabulary_sha256": hashlib.sha256(
            json.dumps(
                list(ALL_SIMULATOR_ACTIONS),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def test_power_plan_uses_independent_mechanisms_and_powers_reference_endpoint():
    baseline = power_analysis(
        paired_differences=[1.0, 1.1, 0.9, 1.0],
        reference_paired_differences=[0.0, 0.1, -0.1, 0.0],
    )
    noisy_reference = power_analysis(
        paired_differences=[1.0, 1.1, 0.9, 1.0],
        reference_paired_differences=[-5.0, 0.0, 5.0, 1.0],
    )

    assert baseline.repetitions_per_variant == 2
    assert baseline.independent_mechanisms_required >= 6
    assert noisy_reference.independent_mechanisms_required > baseline.independent_mechanisms_required
    assert (
        noisy_reference.reference_noninferiority_pilot_sd
        > baseline.reference_noninferiority_pilot_sd
    )


def test_preregistration_uses_all_frozen_eligible_variants_and_hashes_full_contract():
    eligible = [f"heldout-{index:02d}" for index in range(12, 0, -1)]
    plan = power_analysis(
        paired_differences=[1.0, 1.0, 1.0, 1.0],
        reference_paired_differences=[0.0, 0.0, 0.0, 0.0],
    )

    preregistration = preregister_confirmation(
        pilot_experiment_id="00000000-0000-0000-0000-000000000001",
        held_out_variant_ids=eligible,
        power_plan=plan,
        provider="gemini",
        model="live-model",
        embedding_profile_id="semantic-profile",
        additional_contract=_additional_contract(eligible),
    )

    expected = _deterministic_held_out_order(
        variant_ids=sorted(eligible),
        contract=_additional_contract(eligible),
    )
    assert preregistration["held_out_variant_ids"] == expected
    assert preregistration["eligible_held_out_variant_ids"] == sorted(eligible)
    assert preregistration["held_out_selection_method"] == HELD_OUT_SELECTION_METHOD
    assert expected != sorted(eligible)[: plan.independent_mechanisms_required]
    assert preregistration["independent_analysis_unit"] == "simulator_kind"
    assert preregistration["additional_contract"]["retrieval_rank_requirement"] == 1
    without_hash = {key: value for key, value in preregistration.items() if key != "sha256"}
    from hindsight.benchmark import _digest

    assert preregistration["sha256"] == _digest(without_hash)


def test_preregistration_rejects_tampered_power_or_safety_parameters():
    eligible = [f"heldout-{index:02d}" for index in range(12)]
    plan = power_analysis(
        paired_differences=[1.0] * 6,
        reference_paired_differences=[0.0] * 6,
    )

    with pytest.raises(ValueError, match="differ from protocol v3"):
        preregister_confirmation(
            pilot_experiment_id="00000000-0000-0000-0000-000000000001",
            held_out_variant_ids=eligible,
            power_plan=replace(plan, alpha_per_comparison=1.0),
            provider="gemini",
            model="live-model",
            embedding_profile_id="semantic-profile",
            additional_contract=_additional_contract(eligible),
        )
    with pytest.raises(ValueError, match="differ from protocol v3"):
        preregister_confirmation(
            pilot_experiment_id="00000000-0000-0000-0000-000000000001",
            held_out_variant_ids=eligible,
            power_plan=plan,
            provider="gemini",
            model="live-model",
            embedding_profile_id="semantic-profile",
            max_unsafe_actions=1,
            additional_contract=_additional_contract(eligible),
        )


def test_report_preregistration_is_rebuilt_from_pilot_frozen_pool():
    mechanisms = (
        "retry_amplification",
        "cache_stampede",
        "connection_leak",
        "hot_partition",
        "poison_message",
        "lock_contention",
    )
    pilot_ids = [f"pilot-{index}" for index in range(6)]
    held_out_ids = [f"heldout-{index}" for index in range(12)]
    held_out_mechanisms = {
        variant_id: mechanisms[index % len(mechanisms)]
        for index, variant_id in enumerate(held_out_ids)
    }
    shared = {
        "schema_version": 3,
        "corpus_schema_version": 3,
        "corpus_sha256": "corpus",
        "provider": "gemini",
        "model": "live-model",
        "embedding_profile_id": "profile",
        "embedding_max_distance": 0.35,
        "simulator": "multi_mechanism_incident.v1",
        "action_budget": 6,
        "arms": list(ARMS),
        "arm_context_policy": "identical_background_and_hard_distractors",
        "source_evidence_policy": "isolated_namespace",
        "retrieval_rank_requirement": 1,
        "independent_analysis_unit": "simulator_kind",
        "action_vocabulary": list(ALL_SIMULATOR_ACTIONS),
        "action_vocabulary_sha256": hashlib.sha256(
            json.dumps(
                list(ALL_SIMULATOR_ACTIONS),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "repetitions": 2,
        "study_key_sha256": "study",
        "claim_family_sha256": "family",
        "code_sha": "a" * 40,
    }
    held_out_hashes = {item: f"hash:{item}" for item in held_out_ids}
    held_out_queries = {item: f"query:{item}" for item in held_out_ids}
    pilot_manifest = {
        **shared,
        "variant_ids": pilot_ids,
        "variant_sha256": {item: f"hash:{item}" for item in pilot_ids},
        "variant_query_sha256": {item: f"query:{item}" for item in pilot_ids},
        "variant_simulator_kind": dict(zip(pilot_ids, mechanisms, strict=True)),
        "eligible_held_out_variant_ids": held_out_ids,
        "eligible_held_out_variant_sha256": held_out_hashes,
        "eligible_held_out_query_sha256": held_out_queries,
        "eligible_held_out_simulator_kind": held_out_mechanisms,
    }
    confirmation_manifest = {
        **shared,
        "variant_ids": held_out_ids,
        "variant_sha256": held_out_hashes,
        "variant_query_sha256": held_out_queries,
        "variant_simulator_kind": held_out_mechanisms,
    }
    pilot = {
        "id": "00000000-0000-0000-0000-000000000001",
        "experiment_kind": "pilot",
        "status": "completed",
        "provider": "gemini",
        "model": "live-model",
        "embedding_profile_id": "profile",
        "manifest": pilot_manifest,
    }
    experiment = {
        "experiment_kind": "confirmation",
        "provider": "gemini",
        "model": "live-model",
        "embedding_profile_id": "profile",
        "manifest": confirmation_manifest,
    }
    pilot_trials = [
        {
            "variant_id": variant_id,
            "repetition": repetition,
            "arm": arm,
            "status": "completed",
            "penalized_action_count": {"no_lesson": 4, "reference_lesson": 2, "consolidated_lesson": 2}[arm],
        }
        for variant_id in pilot_ids
        for repetition in (1, 2)
        for arm in ARMS
    ]

    canonical = _canonical_preregistration_for_report(
        experiment=experiment,
        pilot=pilot,
        pilot_trials=pilot_trials,
    )
    assert canonical is not None
    assert canonical["eligible_held_out_variant_ids"] == sorted(held_out_ids)
    tampered_experiment = {**experiment, "manifest": dict(confirmation_manifest)}
    tampered_experiment["manifest"]["variant_sha256"] = {
        **held_out_hashes,
        held_out_ids[0]: "tampered",
    }
    assert (
        _canonical_preregistration_for_report(
            experiment=tampered_experiment,
            pilot=pilot,
            pilot_trials=pilot_trials,
        )
        is None
    )
def test_repetitions_are_aggregated_inside_exact_variant_blocks():
    trials = []
    values = {
        "variant-a": {
            1: {"no_lesson": 6, "reference_lesson": 2, "consolidated_lesson": 3},
            2: {"no_lesson": 4, "reference_lesson": 2, "consolidated_lesson": 1},
        },
        "variant-b": {
            1: {"no_lesson": 5, "reference_lesson": 3, "consolidated_lesson": 3},
            2: {"no_lesson": 3, "reference_lesson": 1, "consolidated_lesson": 2},
        },
    }
    for variant_id, repetitions in values.items():
        for repetition, arms in repetitions.items():
            for arm, value in arms.items():
                trials.append(
                    {
                        "variant_id": variant_id,
                        "repetition": repetition,
                        "arm": arm,
                        "status": "completed",
                        "penalized_action_count": value,
                    }
                )

    blocks, complete = _trial_blocks(
        trials=trials,
        expected_variant_ids=list(values),
        repetitions=2,
    )
    efficacy, reference = _variant_level_differences(
        blocks=blocks,
        variant_ids=list(values),
        repetitions=2,
    )

    assert complete is True
    assert efficacy == [3.0, 1.5]
    assert reference == [0.0, 0.5]
    _, missing_complete = _trial_blocks(
        trials=trials[:-1],
        expected_variant_ids=list(values),
        repetitions=2,
    )
    assert missing_complete is False


def test_same_mechanism_variants_are_aggregated_before_inference():
    variants = ["retry-a", "retry-b", "cache-a"]
    values = {
        "retry-a": {"no_lesson": 6, "reference_lesson": 2, "consolidated_lesson": 2},
        "retry-b": {"no_lesson": 4, "reference_lesson": 2, "consolidated_lesson": 2},
        "cache-a": {"no_lesson": 5, "reference_lesson": 3, "consolidated_lesson": 3},
    }
    blocks = {
        (variant, repetition): {
            arm: {"penalized_action_count": value}
            for arm, value in arms.items()
        }
        for variant, arms in values.items()
        for repetition in (1, 2)
    }

    efficacy, reference = _mechanism_level_differences(
        blocks=blocks,
        variant_ids=variants,
        repetitions=2,
        variant_simulator_kind={
            "retry-a": "retry_amplification",
            "retry-b": "retry_amplification",
            "cache-a": "cache_stampede",
        },
    )

    assert efficacy == [2.0, 3.0]
    assert reference == [0.0, 0.0]


def test_exact_p_value_is_finite_when_every_variant_has_the_same_effect():
    p_value = _exact_sign_flip_p_value([2.0] * 6, alternative="greater")

    assert p_value == pytest.approx(0.015625)
    assert 0 < p_value < 0.025
    assert _exact_sign_flip_p_value([0.0] * 20, alternative="greater") == 1.0
    assert _exact_sign_flip_p_value([2.0, 0.0] * 3, alternative="greater") > 0


def test_arm_order_is_a_reproducible_permutation_and_varies_between_blocks():
    orders = {
        _deterministic_arm_order(
            experiment_id="experiment",
            variant_id="variant",
            repetition=repetition,
        )
        for repetition in range(1, 20)
    }

    assert all(set(order) == set(ARMS) for order in orders)
    assert len(orders) > 1


def test_common_action_vocabulary_has_frozen_mechanism_independent_order():
    assert ALL_SIMULATOR_ACTIONS[-1] == "stop"
    assert list(ALL_SIMULATOR_ACTIONS[:-1]) == sorted(ALL_SIMULATOR_ACTIONS[:-1])
    assert len(ALL_SIMULATOR_ACTIONS) == len(set(ALL_SIMULATOR_ACTIONS))


def test_simulator_hides_retry_fanout_until_dependency_inspection():
    simulator = IncidentSimulator()

    assert "retry_fanout" not in simulator.observe()
    premature = simulator.step("throttle_retries")
    assert premature["recovered"] is False
    queue_observation = simulator.step("inspect_queue")
    assert "retry_fanout" not in queue_observation
    dependency_observation = simulator.step("inspect_dependency")
    assert dependency_observation["retry_fanout"] == 4
    recovered = simulator.step("throttle_retries")
    assert recovered["recovered"] is True


def test_action_provider_gets_blinded_memories_and_sufficient_output_budget():
    class CaptureProvider:
        provider_name = "gemini"
        model_name = "capture"

        def __init__(self) -> None:
            self.requests = []

        def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise ReasoningProviderError("transient probe failure")
            return ReasoningResponse(
                text='{"action":"inspect_dependency"}',
                provider=self.provider_name,
                model=self.model_name,
            )

    inner = CaptureProvider()
    provider = retrying_reasoning_provider(inner, max_attempts=2)
    action, usage = _choose_action(
        provider=provider,
        query="incident symptoms",
        observation={"queue_depth": 1000},
        memories=[
            {
                "id": "memory-1",
                "content": "inspect the dependency",
                "writer": "benchmark.reference_curator",
                "metadata": {"role": "consolidated-lesson"},
                "structured_payload": {"verified_by": "secret-reviewer"},
            }
        ],
        step=1,
    )

    prompt = json.loads(inner.requests[-1].prompt)
    assert action == "inspect_dependency"
    assert usage["attempts"] == 2
    assert inner.requests[-1].max_output_tokens == 256
    assert prompt["memories"] == [{"content": "inspect the dependency"}]
    assert "reference_curator" not in inner.requests[-1].prompt
    assert "consolidated-lesson" not in inner.requests[-1].prompt
    assert "secret-reviewer" not in inner.requests[-1].prompt
    assert "retry_amplification" not in inner.requests[-1].prompt
    assert "retry_amplification" not in (inner.requests[-1].system or "")
    assert "throttle_retries" in inner.requests[-1].system
    assert "coalesce_requests" in inner.requests[-1].system
    assert "quarantine_message" in inner.requests[-1].system
    assert "terminate_blocker" in inner.requests[-1].system


def test_model_observation_blinds_mechanism_identity_and_irrelevant_actions_consume_steps():
    simulator = IncidentSimulator("poison_message")

    observation = simulator.observe(include_simulator_kind=False)
    assert "simulator_kind" not in observation
    unchanged = simulator.step("inspect_lock_graph")
    assert unchanged["recovered"] is False
    assert unchanged["detail"] == "action did not address active mechanism"


def test_action_parser_accepts_only_bare_or_fenced_exact_json():
    assert _parse_action_response('{"action":"inspect_dependency"}') == "inspect_dependency"
    assert (
        _parse_action_response('```json\n{"action":"inspect_dependency"}\n```')
        == "inspect_dependency"
    )
    for invalid in (
        'Use this: {"action":"inspect_dependency"}',
        '{"action":"inspect_dependency","reason":"extra"}',
        '```json\n{"action":"inspect_dependency"}\n``` trailing',
    ):
        with pytest.raises(ScientificTrialFailure, match="invalid action"):
            _parse_action_response(invalid)


def test_provider_transport_errors_remain_distinct_from_scientific_output_failures():
    class Provider:
        provider_name = "gemini"
        model_name = "capture"

        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error

        def generate(self, _request):
            if self.error is not None:
                raise self.error
            return ReasoningResponse(
                text=self.response,
                provider=self.provider_name,
                model=self.model_name,
            )

    with pytest.raises(ReasoningProviderError):
        _choose_action(
            provider=Provider(error=ReasoningProviderError("temporary outage")),
            query="symptoms",
            observation={"queue_depth": 10},
            memories=[],
            step=1,
        )
    with pytest.raises(ScientificTrialFailure):
        _choose_action(
            provider=Provider(response="not-json"),
            query="symptoms",
            observation={"queue_depth": 10},
            memories=[],
            step=1,
        )


def test_trial_failure_paths_close_open_decisions(monkeypatch):
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self):
            self.queries = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def transaction(self):
            return self

        def execute(self, query, params=None):
            self.queries.append((query, params))
            return Result(("trial-id",) if "RETURNING id" in query else None)

    connections = []

    def fake_connect(*_args, **_kwargs):
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr("hindsight.benchmark.connect", fake_connect)
    common = {
        "experiment_id": "experiment",
        "variant_id": "variant",
        "repetition": 1,
        "arm": "no_lesson",
        "db_url": "unused",
    }
    _mark_trial_infrastructure_failed(exc=RuntimeError("network"), **common)
    _mark_trial_scientific_failed(exc=ScientificTrialFailure("invalid"), **common)

    for connection in connections:
        decision_updates = [
            (query, params)
            for query, params in connection.queries
            if "UPDATE memory_decisions" in query
        ]
        assert len(decision_updates) == 1
        assert decision_updates[0][1] == ("benchmark:trial-id:%",)
    assert any(
        "SET status = 'failed'" in query
        for query, _params in connections[-1].queries
        if "UPDATE benchmark_experiments" in query
    )
def test_identity_gate_verifies_decision_retrieval_and_ranked_read_chain():
    query_hash = hashlib.sha256(b"incident symptoms").hexdigest()
    experiment = {
        "embedding_profile_id": "profile",
        "manifest": {"variant_query_sha256": {"variant": query_hash}},
    }
    trial = {
        "id": "trial",
        "namespace": "namespace",
        "action_count": 1,
        "variant_id": "variant",
    }
    action = {
        "trial_id": "trial",
        "step": 1,
        "decision_id": "benchmark:trial:1",
        "retrieval_id": "retrieval",
        "cited_memory_ids": ["memory"],
    }
    decision = {
        "id": "benchmark:trial:1",
        "actor": "benchmark.agent",
        "decision_kind": "memory_retrieval",
        "purpose": "Choose the next externally scored simulator action",
        "status": "sealed",
        "namespace": "namespace",
    }
    retrieval = {
        "id": "retrieval",
        "decision_id": "benchmark:trial:1",
        "namespace": "namespace",
        "reader": "benchmark.agent",
        "purpose": "Choose the next externally scored simulator action",
        "policy": "semantic_strict",
        "policy_version": 1,
        "query_sha256": query_hash,
        "requested_limit": 4,
        "embedding_profile_id": "profile",
        "returned_memory_ids": ["memory"],
        "status": "succeeded",
        "selected_strategy": "semantic_vector",
        "attempts": [
            {
                "strategy": "semantic_vector",
                "outcome": "selected",
                "result_count": 1,
                "error_code": None,
            }
        ],
    }
    read = {
        "id": "read",
        "retrieval_id": "retrieval",
        "rank": 1,
        "memory_kind": "semantic",
        "memory_id": "memory",
        "semantic_memory_id": "memory",
        "decision_id": "benchmark:trial:1",
        "reader": "benchmark.agent",
        "purpose": "Choose the next externally scored simulator action",
    }

    assert _identity_lineage_complete(
        experiment=experiment,
        trials=[trial],
        actions=[action],
        decisions=[decision],
        retrievals=[retrieval],
        reads=[read],
    )
    retrieval["policy"] = "semantic_then_keyword"
    assert not _identity_lineage_complete(
        experiment=experiment,
        trials=[trial],
        actions=[action],
        decisions=[decision],
        retrievals=[retrieval],
        reads=[read],
    )


def test_target_binding_and_context_parity_reject_missing_or_displaced_evidence():
    experiment_id = "experiment"
    variant_id = "variant"
    preparations = {
        variant_id: {
            "reference_memory_id": "reference-target",
            "consolidated_memory_id": "consolidated-target",
        }
    }
    trials = [
        {
            "id": "trial-no",
            "variant_id": variant_id,
            "repetition": 1,
            "arm": "no_lesson",
            "namespace": f"benchmark:{experiment_id}:{variant_id}:arm:no-lesson",
            "lesson_memory_id": None,
        },
        {
            "id": "trial-reference",
            "variant_id": variant_id,
            "repetition": 1,
            "arm": "reference_lesson",
            "namespace": f"benchmark:{experiment_id}:{variant_id}:arm:reference-lesson",
            "lesson_memory_id": "reference-target",
        },
        {
            "id": "trial-consolidated",
            "variant_id": variant_id,
            "repetition": 1,
            "arm": "consolidated_lesson",
            "namespace": f"benchmark:{experiment_id}:{variant_id}:arm:consolidated-lesson",
            "lesson_memory_id": "consolidated-target",
        },
    ]
    assert _target_bindings_complete(
        experiment_id=experiment_id,
        trials=trials,
        preparations=preparations,
        expected_variant_ids=[variant_id],
        repetitions=1,
    )

    actions = [
        {"trial_id": "trial-no", "cited_memory_ids": ["no-a", "no-b"]},
        {
            "trial_id": "trial-reference",
            "cited_memory_ids": ["reference-target", "reference-a", "reference-b"],
        },
        {
            "trial_id": "trial-consolidated",
            "cited_memory_ids": [
                "consolidated-target",
                "consolidated-a",
                "consolidated-b",
            ],
        },
    ]
    read_memories = []
    for prefix in ("no", "reference", "consolidated"):
        for suffix, content in (("a", "shared-a"), ("b", "shared-b")):
            read_memories.append(
                {
                    "id": f"{prefix}-{suffix}",
                    "content": content,
                    "content_schema": "benchmark_context.v1",
                    "structured_payload": {"context_id": suffix, "role": "hard_distractor"},
                }
            )
    assert _retrieved_context_parity(
        trials=trials,
        actions=actions,
        read_memories=read_memories,
    )

    missing_target = [dict(row) for row in trials]
    missing_target[1]["lesson_memory_id"] = None
    assert not _target_bindings_complete(
        experiment_id=experiment_id,
        trials=missing_target,
        preparations=preparations,
        expected_variant_ids=[variant_id],
        repetitions=1,
    )
    displaced = [dict(row) for row in actions]
    displaced[1]["cited_memory_ids"] = ["reference-target", "reference-a"]
    assert not _retrieved_context_parity(
        trials=trials,
        actions=displaced,
        read_memories=read_memories,
    )


def test_binding_history_requires_contiguous_safe_replacements_and_current_latest():
    history = [
        {
            "binding_sequence": 1,
            "confirmation_experiment_id": "old",
            "preregistration_sha256": "prereg",
            "status": "incomplete",
            "scientific_failure": False,
            "outcome_bearing": False,
        },
        {
            "binding_sequence": 2,
            "confirmation_experiment_id": "current",
            "preregistration_sha256": "prereg",
            "status": "completed",
            "scientific_failure": False,
            "outcome_bearing": True,
        },
    ]
    assert _binding_history_complete(
        binding_history=history,
        experiment_id="current",
        preregistration_sha256="prereg",
    )
    tampered = [dict(row) for row in history]
    tampered[0]["outcome_bearing"] = True
    assert not _binding_history_complete(
        binding_history=tampered,
        experiment_id="current",
        preregistration_sha256="prereg",
    )
    gap = [dict(row) for row in history]
    gap[1]["binding_sequence"] = 3
    assert not _binding_history_complete(
        binding_history=gap,
        experiment_id="current",
        preregistration_sha256="prereg",
    )


def test_raw_trace_digest_is_stable_while_governance_claim_evidence_changes():
    common = {
        "experiment_id": "experiment",
        "manifest_sha256": "manifest",
        "preregistration_sha256": "preregistration",
        "raw_trace": {
            "trials": [{"lesson_memory_id": "target"}],
            "actions": [],
            "decisions": [],
            "retrievals": [],
            "reads": [],
            "read_memories": [],
            "preparations": [],
        },
        "prior_claim_attempts": [],
        "inference": {"mean_efficacy_actions": 2.0},
    }
    active_raw, active_claim = _report_digests(
        **common,
        target_governance_snapshot=[
            {
                "id": "target",
                "trust_status": "active",
                "t_invalid": None,
                "lineage_status": "complete",
            }
        ],
        binding_history=[],
        gates={"retrieval": True},
    )
    invalid_raw, invalid_claim = _report_digests(
        **common,
        target_governance_snapshot=[
            {
                "id": "target",
                "trust_status": "review_required",
                "t_invalid": "2026-07-15T00:00:00Z",
                "lineage_status": "complete",
            }
        ],
        binding_history=[],
        gates={"retrieval": False},
    )
    rebound_raw, rebound_claim = _report_digests(
        **common,
        target_governance_snapshot=[
            {
                "id": "target",
                "trust_status": "active",
                "t_invalid": None,
                "lineage_status": "complete",
            }
        ],
        binding_history=[
            {
                "binding_sequence": 1,
                "confirmation_experiment_id": "old-incomplete",
            },
            {
                "binding_sequence": 2,
                "confirmation_experiment_id": "experiment",
            },
        ],
        gates={"retrieval": True},
    )

    assert active_raw == invalid_raw == rebound_raw
    assert active_claim != invalid_claim
    assert active_claim != rebound_claim
    assert all({"retrieval": True}.values()) is True
    assert all({"retrieval": False}.values()) is False


def test_integrity_migration_guards_preregistration_and_terminal_trace_mutations():
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    migration = "\n".join(
        (migrations / name).read_text()
        for name in (
            "0013_benchmark_protocol_integrity.sql",
            "0014_benchmark_protocol_guards.sql",
        )
    )

    assert "benchmark_confirmation_preregistrations" in migration
    assert "benchmark_confirmation_requires_preregistration" in migration
    assert "benchmark_trial_insert_running_only" in migration
    assert "benchmark_action_insert_running_only" in migration
    assert "benchmark_experiment_delete_immutable" in migration
    assert "benchmark_trial_delete_immutable" in migration
    assert "benchmark_action_delete_immutable" in migration
