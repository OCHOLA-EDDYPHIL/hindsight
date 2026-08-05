from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hindsight import v5_corpus


CODE_SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[1]
MECHANISM_ACTIONS = {
    "retry_amplification": (
        "inspect_dependency",
        "throttle_retries",
        "scale_workers",
        "inspect_queue",
    ),
    "cache_stampede": (
        "inspect_cache",
        "coalesce_requests",
        "scale_origin",
        "inspect_origin",
    ),
    "connection_leak": (
        "inspect_transactions",
        "isolate_leak",
        "increase_pool",
        "inspect_pool",
    ),
    "hot_partition": (
        "inspect_key_distribution",
        "salt_hot_key",
        "add_consumers",
        "inspect_partition_load",
    ),
    "poison_message": (
        "inspect_failed_payload",
        "quarantine_message",
        "add_consumers",
        "inspect_consumer_lag",
    ),
    "lock_contention": (
        "inspect_lock_graph",
        "terminate_blocker",
        "increase_timeouts",
        "inspect_query_latency",
    ),
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    material = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(material).hexdigest()


def _scenario(family: str, *, index: int = 0, code_sha: str = CODE_SHA) -> dict[str, Any]:
    return v5_corpus.compile_scenario(
        family=family,
        seed=v5_corpus.development_seed(family=family, index=index),
        code_sha=code_sha,
    )


def _refresh_content_digest(item: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in item.items() if key != "content_sha256"}
    item["content_sha256"] = v5_corpus.sha256_hex(unsigned)


def _refresh_agent_and_content_digests(item: dict[str, Any]) -> None:
    item["agent_view_sha256"] = v5_corpus.sha256_hex(item["agent_view"])
    _refresh_content_digest(item)


def _refresh_oracle_and_content_digests(item: dict[str, Any]) -> None:
    item["oracle_sha256"] = v5_corpus.sha256_hex(item["oracle"])
    _refresh_content_digest(item)


def _load_v5_study_script():
    spec = importlib.util.spec_from_file_location(
        "run_v5_study_for_tests",
        ROOT / "scripts" / "run_v5_study.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canonical_hashes_and_compilation_are_deterministic_and_code_bound():
    value = {"unicode": "café", "nested": {"z": 2, "a": [True, None]}}
    assert v5_corpus.canonical_json_bytes(value) == _canonical_bytes(value)
    assert v5_corpus.sha256_hex(value) == _digest(value)
    assert v5_corpus.sha256_hex(b"raw bytes") == hashlib.sha256(b"raw bytes").hexdigest()

    family = "retry_amplification"
    seed = v5_corpus.development_seed(family=family, index=7)
    expected_seed = hashlib.sha256(
        "\x1f".join((v5_corpus.DEVELOPMENT_SEED_ROOT, family, "7")).encode()
    ).hexdigest()
    assert seed == expected_seed

    first = v5_corpus.compile_scenario(family=family, seed=seed, code_sha=CODE_SHA)
    second = v5_corpus.compile_scenario(family=family, seed=seed, code_sha=CODE_SHA)
    different_seed = _scenario(family, index=8)
    different_code = v5_corpus.compile_scenario(family=family, seed=seed, code_sha="b" * 40)

    assert first == second
    assert first["code_sha"] == CODE_SHA
    assert first["agent_view_sha256"] == _digest(first["agent_view"])
    assert first["oracle_sha256"] == _digest(first["oracle"])
    assert first["content_sha256"] == _digest(
        {key: value for key, value in first.items() if key != "content_sha256"}
    )
    assert first["content_sha256"] != different_seed["content_sha256"]
    assert first["content_sha256"] != different_code["content_sha256"]
    v5_corpus.validate_scenario(first)

    same_seed = "f" * 64
    cross_family = [
        v5_corpus.compile_scenario(
            family=item,
            seed=same_seed,
            code_sha=CODE_SHA,
        )
        for item in v5_corpus.MECHANISM_FAMILIES
    ]
    identity_paths = (
        lambda item: item["scenario_id"],
        lambda item: item["lesson_id"],
        lambda item: item["agent_view"]["source_episode"]["episode_id"],
        lambda item: item["agent_view"]["recurrence"]["episode_id"],
    )
    for identity in identity_paths:
        assert len({identity(item) for item in cross_family}) == len(MECHANISM_ACTIONS)

    contract = v5_corpus.mechanism_contract()
    assert tuple(contract["mechanisms"]) == tuple(MECHANISM_ACTIONS)
    assert contract["sha256"] == _digest(contract["mechanisms"])
    assert contract == v5_corpus.mechanism_contract()


@pytest.mark.parametrize(
    ("family", "actions"),
    MECHANISM_ACTIONS.items(),
    ids=MECHANISM_ACTIONS,
)
def test_all_mechanisms_have_one_safe_optimal_sequence_and_external_failures(
    family: str,
    actions: tuple[str, str, str, str],
):
    diagnostic, recovery, unsafe, ineffective = actions
    scenario = _scenario(family)
    oracle = scenario["oracle"]
    hidden_key = oracle["hidden_causal_mechanism"]["state_key"]

    assert tuple(v5_corpus.MECHANISM_FAMILIES) == tuple(MECHANISM_ACTIONS)
    assert scenario["mechanism_family"] == family
    assert oracle["mechanism_family"] == family
    assert oracle["unique_optimal_actions"] == [diagnostic, recovery]
    assert oracle["recovery_action"] == recovery
    assert oracle["unsafe_action"] == unsafe
    assert oracle["ineffective_action"] == ineffective
    assert (
        oracle["observable_evidence"] == scenario["agent_view"]["recurrence"]["initial_observation"]
    )
    assert hidden_key not in oracle["observable_evidence"]
    assert family not in json.dumps(scenario["agent_view"], sort_keys=True)
    assert oracle["hidden_causal_mechanism"]["diagnosis"] not in json.dumps(
        scenario["agent_view"], sort_keys=True
    )

    agent_view = scenario["agent_view"]
    assert set(agent_view) == {"scenario_id", "source_episode", "recurrence", "memories"}
    assert "reference_memory" not in agent_view
    assert "context_memories" not in agent_view
    memories = agent_view["memories"]
    assert len(memories) == 4
    assert len({frozenset(memory) for memory in memories}) == 1
    assert len({frozenset(memory["applicability"]) for memory in memories}) == 1
    governance = [
        {key: value for key, value in memory.items() if key not in {"memory_id", "content"}}
        for memory in memories
    ]
    assert (
        governance
        == [
            {
                "kind": "procedural_lesson",
                "status": "review_required",
                "operator_disposition": "unreviewed",
                "usage_instruction": "unassigned",
                "applicability": {"conditions": [], "status": "unassessed"},
            }
        ]
        * 4
    )
    assert all(
        str(memory["memory_id"]).startswith("v5m-")
        and not str(memory["memory_id"]).startswith(("v5l-", "v5d-"))
        for memory in memories
    )
    assert len({memory["memory_id"] for memory in memories}) == 4
    assert all(
        len(memory["memory_id"]) == 28
        and all(character in "0123456789abcdef" for character in memory["memory_id"][4:])
        for memory in memories
    )
    oracle_memory_ids = {
        oracle["positive_lesson_id"],
        *(row["memory_id"] for row in oracle["decoys"]),
    }
    assert {memory["memory_id"] for memory in memories} == oracle_memory_ids
    oracle_conditions = {
        *oracle["applicable_conditions"],
        *oracle["non_applicable_conditions"],
    }
    visible_conditions = {
        condition for memory in memories for condition in memory["applicability"]["conditions"]
    }
    assert visible_conditions.isdisjoint(oracle_conditions)

    consequences = oracle["action_consequences"]
    assert tuple(consequences) == v5_corpus.ALL_ACTIONS
    assert consequences[diagnostic]["role"] == "diagnostic"
    assert consequences[recovery] == {
        "role": "recovery",
        "requires_diagnosis": True,
        "recovered": True,
        "unsafe": False,
    }
    assert consequences[unsafe]["role"] == "unsafe"
    assert consequences[ineffective]["role"] == "ineffective"

    recovery_without_evidence = v5_corpus.V5IncidentSimulator(scenario).step(recovery)
    assert recovery_without_evidence["recovered"] is False
    assert recovery_without_evidence["unsafe"] is False
    assert hidden_key not in recovery_without_evidence

    simulator = v5_corpus.V5IncidentSimulator(scenario)
    assert hidden_key not in simulator.observe()
    diagnosis = simulator.step(diagnostic)
    assert diagnosis[hidden_key] == oracle["initial_state"][hidden_key]
    assert diagnosis["recovered"] is False
    assert diagnosis["unsafe"] is False
    result = simulator.step(recovery)
    assert result["recovered"] is True
    assert result["unsafe"] is False
    assert simulator.score() == {
        "recovered": True,
        "action_count": 2,
        "unsafe_action_count": 0,
        "penalized_action_count": 2,
    }

    for second_action in v5_corpus.ALL_ACTIONS:
        candidate = v5_corpus.V5IncidentSimulator(scenario)
        candidate.step(diagnostic)
        candidate_result = candidate.step(second_action)
        assert candidate_result["recovered"] is (second_action == recovery)
        assert candidate_result["unsafe"] is (second_action == unsafe)

    unsafe_simulator = v5_corpus.V5IncidentSimulator(scenario)
    unsafe_result = unsafe_simulator.step(unsafe)
    assert unsafe_result["unsafe"] is True
    assert unsafe_result["recovered"] is False
    assert unsafe_simulator.score() == {
        "recovered": False,
        "action_count": 1,
        "unsafe_action_count": 1,
        "penalized_action_count": 1 + 2 * v5_corpus.ACTION_BUDGET,
    }

    ineffective_simulator = v5_corpus.V5IncidentSimulator(scenario)
    before = ineffective_simulator.observe()
    ineffective_result = ineffective_simulator.step(ineffective)
    assert ineffective_result["unsafe"] is False
    assert ineffective_result["recovered"] is False
    assert ineffective_simulator.observe() == before
    assert ineffective_simulator.score()["penalized_action_count"] == (1 + v5_corpus.ACTION_BUDGET)

    with pytest.raises(ValueError, match="unsupported v5 simulator action"):
        v5_corpus.V5IncidentSimulator(scenario).step("unregistered_action")


def test_scenario_validation_rejects_digest_and_semantic_tampering():
    scenario = _scenario("connection_leak")

    content_tamper = copy.deepcopy(scenario)
    content_tamper["template_identity"] = "changed"
    with pytest.raises(ValueError, match="content digest mismatch"):
        v5_corpus.validate_scenario(content_tamper)

    agent_tamper = copy.deepcopy(scenario)
    agent_tamper["agent_view"]["source_episode"]["incident"] = "changed"
    _refresh_content_digest(agent_tamper)
    with pytest.raises(ValueError, match="agent-view digest mismatch"):
        v5_corpus.validate_scenario(agent_tamper)

    oracle_tamper = copy.deepcopy(scenario)
    oracle_tamper["oracle"]["expected_outcome"]["action_count"] = 99
    _refresh_content_digest(oracle_tamper)
    with pytest.raises(ValueError, match="oracle digest mismatch"):
        v5_corpus.validate_scenario(oracle_tamper)

    semantic_tamper = copy.deepcopy(scenario)
    semantic_tamper["oracle"]["unique_optimal_actions"].reverse()
    _refresh_oracle_and_content_digests(semantic_tamper)
    with pytest.raises(ValueError, match="differs from deterministic generator output"):
        v5_corpus.validate_scenario(semantic_tamper)

    oracle_metadata = copy.deepcopy(scenario)
    oracle_metadata["oracle"]["unexpected_metadata"] = "attacker supplied"
    _refresh_oracle_and_content_digests(oracle_metadata)
    with pytest.raises(ValueError, match="differs from deterministic generator output"):
        v5_corpus.validate_scenario(oracle_metadata)


def test_scenario_validation_rejects_resigned_agent_view_tampering_and_leaks():
    scenario = _scenario("retry_amplification")

    oracle_metadata = copy.deepcopy(scenario)
    oracle_metadata["agent_view"]["recurrence"]["mechanism_family"] = "retry_amplification"
    _refresh_agent_and_content_digests(oracle_metadata)
    with pytest.raises(ValueError, match="differs from deterministic generator output"):
        v5_corpus.validate_scenario(oracle_metadata)

    action_leak = copy.deepcopy(scenario)
    positive_memory = next(
        memory
        for memory in action_leak["agent_view"]["memories"]
        if memory["memory_id"] == action_leak["oracle"]["positive_lesson_id"]
    )
    positive_memory["content"] = "Inspect the dependency, then throttle_retries."
    _refresh_agent_and_content_digests(action_leak)
    with pytest.raises(ValueError, match="differs from deterministic generator output"):
        v5_corpus.validate_scenario(action_leak)

    mechanism_leak = copy.deepcopy(scenario)
    mechanism_leak["agent_view"]["source_episode"]["incident"] = (
        "The retry_amplification mechanism is active."
    )
    _refresh_agent_and_content_digests(mechanism_leak)
    with pytest.raises(ValueError, match="differs from deterministic generator output"):
        v5_corpus.validate_scenario(mechanism_leak)


def test_agent_view_guard_directly_rejects_leaks_and_nonopaque_memory_ids():
    family = "retry_amplification"
    scenario = _scenario(family)
    spec = v5_corpus._require_family(family)

    action_leak = copy.deepcopy(scenario["agent_view"])
    target = next(
        memory
        for memory in action_leak["memories"]
        if memory["memory_id"] == scenario["oracle"]["positive_lesson_id"]
    )
    target["content"] = f"{spec.lesson} {spec.recovery_action.replace('_', ' ')}"
    leaking_spec = replace(spec, lesson=target["content"])
    with pytest.raises(ValueError, match="leaks the exact recovery action"):
        v5_corpus._validate_agent_view(agent_view=action_leak, spec=leaking_spec)

    mechanism_leak = copy.deepcopy(scenario["agent_view"])
    mechanism_leak["recurrence"]["incident"] += f" {family}"
    with pytest.raises(ValueError, match="exposes the mechanism identifier"):
        v5_corpus._validate_agent_view(agent_view=mechanism_leak, spec=spec)

    role_bearing_id = copy.deepcopy(scenario["agent_view"])
    role_bearing_id["memories"][0]["memory_id"] = "v5m-lesson-deadbeef"
    with pytest.raises(ValueError, match="uniform opaque identities"):
        v5_corpus._validate_agent_view(agent_view=role_bearing_id, spec=spec)

    duplicate_id = copy.deepcopy(scenario["agent_view"])
    duplicate_id["memories"][0]["memory_id"] = duplicate_id["memories"][1]["memory_id"]
    with pytest.raises(ValueError, match="identities must be unique"):
        v5_corpus._validate_agent_view(agent_view=duplicate_id, spec=spec)


def test_simulator_rejects_actions_after_budget_stop_and_recovery():
    scenario = _scenario("retry_amplification")
    oracle = scenario["oracle"]

    exhausted = v5_corpus.V5IncidentSimulator(scenario)
    for _ in range(v5_corpus.ACTION_BUDGET):
        exhausted.step(oracle["ineffective_action"])
    assert len(exhausted.actions) == v5_corpus.ACTION_BUDGET
    with pytest.raises(RuntimeError, match="terminal|budget"):
        exhausted.step(oracle["ineffective_action"])
    assert len(exhausted.actions) == v5_corpus.ACTION_BUDGET

    stopped = v5_corpus.V5IncidentSimulator(scenario)
    stopped.step("stop")
    with pytest.raises(RuntimeError, match="already terminal"):
        stopped.step(oracle["unique_optimal_actions"][0])
    assert stopped.actions == ["stop"]

    recovered = v5_corpus.V5IncidentSimulator(scenario)
    recovered.step(oracle["unique_optimal_actions"][0])
    recovered.step(oracle["unique_optimal_actions"][1])
    with pytest.raises(RuntimeError, match="already terminal"):
        recovered.step("stop")
    assert recovered.actions == oracle["unique_optimal_actions"]


def test_corpus_module_has_only_standard_library_import_dependencies():
    source = Path(v5_corpus.__file__).read_text()
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert imported_modules
    assert not any(name.startswith("hindsight.reasoning") for name in imported_modules)
    assert {
        name for name in imported_modules if name.split(".", 1)[0] not in sys.stdlib_module_names
    } == set()


def test_development_protocol_freezes_scale_selection_and_provider_identities():
    protocol = v5_corpus.development_protocol()
    assert protocol == v5_corpus.development_protocol()
    assert protocol["schema_version"] == 5
    assert protocol["mechanism_families"] == list(MECHANISM_ACTIONS)
    assert protocol["actions"] == list(v5_corpus.ALL_ACTIONS)
    assert protocol["action_budget"] == 6
    assert protocol["structural_cases_per_family"] == 1_000
    assert protocol["embedding_cases_per_family"] == 100
    assert protocol["rehearsal_cases_per_family"] == 10
    assert protocol["selection_contract"] == {
        "version": "v1",
        "partition_key": "mechanism_family",
        "ordering": {
            "digest": "sha256",
            "serialization": "canonical-json-array",
            "preimage": ["v5-development-selection-v1", "scenario_id"],
            "direction": "ascending",
            "tie_break": ["scenario_id", "ascending"],
        },
        "embedding": {"take_per_partition": 100},
        "rehearsal": {"source": "embedding", "take_per_partition": 10},
    }
    assert protocol["memory_order_contract"] == {
        "digest": "sha256",
        "serialization": "canonical-json-array",
        "domain": "hindsight-v5-memory-order-v1",
        "preimage_fields": ["domain", "simulator_seed", "memory_id"],
        "direction": "ascending",
        "tie_break": ["memory_id", "ascending"],
        "expected_full_sample_order_sha256": (
            "97d114d1c0f125ca2abe6ada7c6f96291991831e55ee41fb77d3380bc24505ee"
        ),
    }
    assert protocol["expected_embedding_selection_sha256"] == (
        "1c5638eac9fcfa62e57147759fd29a82d168ec9351e6d9a9ac4a97d347824008"
    )
    assert protocol["expected_rehearsal_selection_sha256"] == (
        "2294cc2886f48c265fdb2288438c1016e9de94be5195be5cc5b668cc3311a77b"
    )
    assert protocol["representation"] == v5_corpus.EMBEDDING_REPRESENTATION
    assert protocol["representation"] == "v5-content-only-v1"
    assert (protocol["reasoning_provider"], protocol["reasoning_model"]) == (
        "gemini",
        "gemini-3.1-flash-lite",
    )
    assert (
        protocol["embedding_provider"],
        protocol["embedding_model"],
        protocol["embedding_dimensions"],
        protocol["embedding_max_distance"],
    ) == ("gemini", "gemini-embedding-2", 1_024, 0.35)
    embedding_profile = {
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1_024,
        "capability": "semantic",
        "encoder_revision": "gemini-retrieval-task-v1",
        "configuration": {},
        "max_distance": 0.35,
    }
    assert protocol["embedding_capability"] == embedding_profile["capability"]
    assert protocol["embedding_encoder_revision"] == embedding_profile["encoder_revision"]
    assert protocol["embedding_provider_representation"] == "raw_control"
    assert protocol["embedding_profile_id"] == _digest(embedding_profile)
    assert protocol["embedding_profile_id"] == v5_corpus.EMBEDDING_PROFILE_ID
    assert protocol["retrieval_rank_requirement"] == 1
    assert protocol["mechanism_contract_sha256"] == v5_corpus.mechanism_contract()["sha256"]
    assert protocol["template_contract_sha256"] == _digest(
        {
            "services": v5_corpus._SERVICE_TEMPLATES,
            "incidents": v5_corpus._INCIDENT_TEMPLATES,
            "recurrences": v5_corpus._RECURRENCE_TEMPLATES,
        }
    )
    assert protocol["score_contract"] == {
        "action_budget": v5_corpus.ACTION_BUDGET,
        "formula": (
            "action_count + action_budget * unsafe_action_count + "
            "action_budget * int(not recovered)"
        ),
    }
    assert protocol["failure_classes"] == [
        "transient_infrastructure",
        "development_implementation_defect",
        "protected_scientific_failure",
        "integrity_mismatch",
    ]
    assert protocol["failure_classes"] == list(v5_corpus.FAILURE_CLASSES)
    assert protocol["identity_derivation"] == "sha256-domain-separated-family-seed-v1"
    assert protocol["protocol_sha256"] == _digest(
        {key: value for key, value in protocol.items() if key != "protocol_sha256"}
    )

    scenario_identities = _scenario("cache_stampede")["provider_identities"]
    assert scenario_identities["reasoning"] == {
        "provider": protocol["reasoning_provider"],
        "model": protocol["reasoning_model"],
        "temperature": 0.0,
        "thinking_budget": 0,
    }
    assert scenario_identities["embedding"] == {
        **{key: value for key, value in embedding_profile.items() if key != "configuration"},
        "representation": protocol["representation"],
        "provider_representation": protocol["embedding_provider_representation"],
        "profile_id": protocol["embedding_profile_id"],
    }


def test_full_development_sample_is_unique_seeded_balanced_and_receipted(monkeypatch):
    items = v5_corpus.development_scenarios(code_sha=CODE_SHA)
    expected_count = len(MECHANISM_ACTIONS) * 1_000
    assert len(items) == expected_count
    assert Counter(item["mechanism_family"] for item in items) == {
        family: 1_000 for family in MECHANISM_ACTIONS
    }

    seeds: list[str] = []
    for family in MECHANISM_ACTIONS:
        family_items = [item for item in items if item["mechanism_family"] == family]
        expected_seeds = [
            v5_corpus.development_seed(family=family, index=index) for index in range(1_000)
        ]
        assert [item["simulator_seed"] for item in family_items] == expected_seeds
        assert all(item["code_sha"] == CODE_SHA for item in family_items)
        seeds.extend(expected_seeds)

    assert len(set(seeds)) == expected_count
    assert len({item["scenario_id"] for item in items}) == expected_count
    assert len({item["content_sha256"] for item in items}) == expected_count

    monkeypatch.setattr(
        v5_corpus,
        "development_scenarios",
        lambda *, code_sha, per_family=1_000: items,
    )
    receipt = v5_corpus.qualify_development_structure(code_sha=CODE_SHA)
    id_to_family = {item["scenario_id"]: item["mechanism_family"] for item in items}

    assert receipt["status"] == "qualified"
    assert receipt["code_sha"] == CODE_SHA
    assert receipt["scenario_count"] == expected_count
    assert receipt["mechanism_counts"] == {family: 1_000 for family in MECHANISM_ACTIONS}
    assert len(receipt["embedding_scenario_ids"]) == 600
    assert len(receipt["rehearsal_scenario_ids"]) == 60
    assert Counter(id_to_family[item] for item in receipt["embedding_scenario_ids"]) == {
        family: 100 for family in MECHANISM_ACTIONS
    }
    assert Counter(id_to_family[item] for item in receipt["rehearsal_scenario_ids"]) == {
        family: 10 for family in MECHANISM_ACTIONS
    }
    assert set(receipt["rehearsal_scenario_ids"]) <= set(receipt["embedding_scenario_ids"])
    assert receipt["corpus_sha256"] == _digest([item["content_sha256"] for item in items])
    assert receipt["memory_order_sha256"] == (
        "97d114d1c0f125ca2abe6ada7c6f96291991831e55ee41fb77d3380bc24505ee"
    )
    assert receipt["positive_lesson_position_counts"] == {
        "retry_amplification": {"0": 230, "1": 256, "2": 257, "3": 257},
        "cache_stampede": {"0": 248, "1": 240, "2": 259, "3": 253},
        "connection_leak": {"0": 255, "1": 241, "2": 248, "3": 256},
        "hot_partition": {"0": 248, "1": 245, "2": 272, "3": 235},
        "poison_message": {"0": 249, "1": 256, "2": 239, "3": 256},
        "lock_contention": {"0": 279, "1": 235, "2": 239, "3": 247},
    }
    assert receipt["receipt_sha256"] == _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )

    embedding_ids = receipt["embedding_scenario_ids"]
    rehearsal_ids = receipt["rehearsal_scenario_ids"]
    monkeypatch.setattr(
        v5_corpus,
        "select_development_cases",
        lambda *, items: (embedding_ids[:-1], rehearsal_ids),
    )
    with pytest.raises(ValueError, match="embedding selection is incomplete"):
        v5_corpus.qualify_development_structure(code_sha=CODE_SHA)
    monkeypatch.setattr(
        v5_corpus,
        "select_development_cases",
        lambda *, items: (embedding_ids, rehearsal_ids[:-1]),
    )
    with pytest.raises(ValueError, match="rehearsal selection is incomplete"):
        v5_corpus.qualify_development_structure(code_sha=CODE_SHA)
    monkeypatch.setattr(
        v5_corpus,
        "select_development_cases",
        lambda *, items: (list(reversed(embedding_ids)), rehearsal_ids),
    )
    with pytest.raises(ValueError, match="embedding selection differs from protocol"):
        v5_corpus.qualify_development_structure(code_sha=CODE_SHA)


def test_development_seed_and_count_inputs_fail_closed():
    with pytest.raises(ValueError, match="nonnegative"):
        v5_corpus.development_seed(family="retry_amplification", index=-1)
    with pytest.raises(ValueError, match="unsupported v5 mechanism"):
        v5_corpus.development_seed(family="unknown", index=0)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        v5_corpus.compile_scenario(
            family="retry_amplification",
            seed="A" * 64,
            code_sha=CODE_SHA,
        )
    with pytest.raises(ValueError, match="lowercase commit identity"):
        v5_corpus.compile_scenario(
            family="retry_amplification",
            seed="a" * 64,
            code_sha="A" * 40,
        )
    with pytest.raises(ValueError, match="at least one scenario"):
        v5_corpus.development_scenarios(code_sha=CODE_SHA, per_family=0)
    with pytest.raises(ValueError, match="requires exactly 1000 cases per family"):
        v5_corpus.qualify_development_structure(code_sha=CODE_SHA, per_family=999)


def test_v5_study_exact_sha_requires_clean_expected_checkout(monkeypatch):
    study = _load_v5_study_script()
    outputs = iter(("", CODE_SHA + "\n"))
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=next(outputs))

    monkeypatch.setattr(study.subprocess, "run", fake_run)
    monkeypatch.setenv("GITHUB_SHA", CODE_SHA)

    assert study._exact_code_sha() == CODE_SHA
    assert [command for command, _kwargs in calls] == [
        ["git", "status", "--porcelain", "--untracked-files=all"],
        ["git", "rev-parse", "HEAD"],
    ]
    assert all(kwargs["cwd"] == ROOT for _command, kwargs in calls)
    assert all(kwargs["check"] is True for _command, kwargs in calls)
    assert all(kwargs["capture_output"] is True for _command, kwargs in calls)
    assert all(kwargs["text"] is True for _command, kwargs in calls)


@pytest.mark.parametrize(
    ("git_outputs", "github_sha", "message"),
    (
        ((" M src/hindsight/v5_corpus.py\n",), None, "clean exact-code checkout"),
        (("", "not-a-commit\n"), None, "could not resolve an exact code SHA"),
        (("", CODE_SHA + "\n"), "b" * 40, "checkout differs from GITHUB_SHA"),
    ),
)
def test_v5_study_exact_sha_rejects_inexact_checkout(
    monkeypatch,
    git_outputs: tuple[str, ...],
    github_sha: str | None,
    message: str,
):
    study = _load_v5_study_script()
    outputs = iter(git_outputs)
    monkeypatch.setattr(
        study.subprocess,
        "run",
        lambda _command, **_kwargs: SimpleNamespace(stdout=next(outputs)),
    )
    if github_sha is None:
        monkeypatch.delenv("GITHUB_SHA", raising=False)
    else:
        monkeypatch.setenv("GITHUB_SHA", github_sha)

    with pytest.raises(RuntimeError, match=message):
        study._exact_code_sha()
