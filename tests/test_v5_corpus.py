from __future__ import annotations

import ast
import copy
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from hindsight import v5_corpus


CODE_SHA = "a" * 40
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
    assert oracle["observable_evidence"] == scenario["agent_view"]["recurrence"][
        "initial_observation"
    ]
    assert hidden_key not in oracle["observable_evidence"]
    assert family not in json.dumps(scenario["agent_view"], sort_keys=True)

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
    assert ineffective_simulator.score()["penalized_action_count"] == (
        1 + v5_corpus.ACTION_BUDGET
    )

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
    with pytest.raises(ValueError, match="optimal action sequence"):
        v5_corpus.validate_scenario(semantic_tamper)


def test_scenario_validation_rejects_resigned_agent_view_leaks():
    scenario = _scenario("retry_amplification")

    oracle_metadata = copy.deepcopy(scenario)
    oracle_metadata["agent_view"]["recurrence"]["mechanism_family"] = (
        "retry_amplification"
    )
    _refresh_agent_and_content_digests(oracle_metadata)
    with pytest.raises(ValueError, match="exposes oracle metadata"):
        v5_corpus.validate_scenario(oracle_metadata)

    action_leak = copy.deepcopy(scenario)
    action_leak["agent_view"]["reference_memory"]["content"] = (
        "Inspect the dependency, then throttle_retries."
    )
    _refresh_agent_and_content_digests(action_leak)
    with pytest.raises(ValueError, match="leaks the exact recovery action"):
        v5_corpus.validate_scenario(action_leak)

    mechanism_leak = copy.deepcopy(scenario)
    mechanism_leak["agent_view"]["source_episode"]["incident"] = (
        "The retry_amplification mechanism is active."
    )
    _refresh_agent_and_content_digests(mechanism_leak)
    with pytest.raises(ValueError, match="exposes the mechanism identifier"):
        v5_corpus.validate_scenario(mechanism_leak)


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
        name
        for name in imported_modules
        if name.split(".", 1)[0] not in sys.stdlib_module_names
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
    assert protocol["embedding_selection"] == "sha256-order-first-100-per-family-v1"
    assert protocol["rehearsal_selection"] == (
        "sha256-order-first-10-of-embedding-per-family-v1"
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
    assert protocol["retrieval_rank_requirement"] == 1
    assert protocol["protocol_sha256"] == _digest(
        {key: value for key, value in protocol.items() if key != "protocol_sha256"}
    )


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
            v5_corpus.development_seed(family=family, index=index)
            for index in range(1_000)
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
    assert receipt["mechanism_counts"] == {
        family: 1_000 for family in MECHANISM_ACTIONS
    }
    assert len(receipt["embedding_scenario_ids"]) == 600
    assert len(receipt["rehearsal_scenario_ids"]) == 60
    assert Counter(id_to_family[item] for item in receipt["embedding_scenario_ids"]) == {
        family: 100 for family in MECHANISM_ACTIONS
    }
    assert Counter(id_to_family[item] for item in receipt["rehearsal_scenario_ids"]) == {
        family: 10 for family in MECHANISM_ACTIONS
    }
    assert set(receipt["rehearsal_scenario_ids"]) <= set(receipt["embedding_scenario_ids"])
    assert receipt["corpus_sha256"] == _digest(
        [item["content_sha256"] for item in items]
    )
    assert receipt["receipt_sha256"] == _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


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
