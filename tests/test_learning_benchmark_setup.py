"""Frozen-corpus and arm-parity checks for the live learning benchmark."""

from __future__ import annotations

import json
import importlib.util
import os
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

CORPUS = Path("fixtures/benchmark_variants.json")
SIMULATOR_KINDS = {
    "retry_amplification",
    "cache_stampede",
    "connection_leak",
    "hot_partition",
    "poison_message",
    "lock_contention",
}
REFERENCE_SOURCE = "project-curated-simulator-spec-v1"
_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "hindsight_run_learning_benchmark",
    Path(__file__).resolve().parents[1] / "scripts" / "run_learning_benchmark.py",
)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
benchmark_script = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(benchmark_script)
requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def _corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def test_frozen_corpus_has_balanced_curated_low_overlap_retrieval_challenges():
    corpus = _corpus()

    benchmark_script._validate_corpus(corpus)

    pilot = [row for row in corpus["variants"] if row["split"] == "pilot"]
    confirmation = [row for row in corpus["variants"] if row["split"] == "confirmation"]
    assert corpus["schema_version"] == 3
    assert len(corpus["variants"]) == 18
    assert len(pilot) == 6
    assert len(confirmation) == 12
    assert Counter(row["simulator_kind"] for row in corpus["variants"]) == {
        kind: 3 for kind in SIMULATOR_KINDS
    }
    assert Counter(row["simulator_kind"] for row in pilot) == {kind: 1 for kind in SIMULATOR_KINDS}
    assert Counter(row["simulator_kind"] for row in confirmation) == {
        kind: 2 for kind in SIMULATOR_KINDS
    }
    assert len({row["variant_id"] for row in corpus["variants"]}) == 18
    assert len({row["source_summary"] for row in corpus["variants"]}) == 18
    assert len({row["recurrence_query"] for row in corpus["variants"]}) == 18
    assert len({row["root_cause"] for row in corpus["variants"]}) == 18
    assert len({row["resolution_action"] for row in corpus["variants"]}) == 18

    for row in corpus["variants"]:
        assert row["simulator_kind"] in SIMULATOR_KINDS
        assert row["reference_source"] == REFERENCE_SOURCE
        assert row["reference_lesson"].strip()
        assert "gold_lesson" not in row
        assert "gold_verified_by" not in row
        roles = Counter(context["role"] for context in row["context_memories"])
        assert roles["background"] >= 1
        assert roles["hard_distractor"] >= 2
        target_overlap = max(
            benchmark_script._lexical_overlap(row["recurrence_query"], row["source_summary"]),
            benchmark_script._lexical_overlap(row["recurrence_query"], row["reference_lesson"]),
        )
        distractor_overlaps = [
            benchmark_script._lexical_overlap(row["recurrence_query"], context["content"])
            for context in row["context_memories"]
            if context["role"] == "hard_distractor"
        ]
        assert target_overlap <= 0.20
        assert all(overlap >= 0.25 for overlap in distractor_overlaps)
        assert all(overlap > target_overlap for overlap in distractor_overlaps)

    for kind in SIMULATOR_KINDS:
        family = [row for row in corpus["variants"] if row["simulator_kind"] == kind]
        for left, right in combinations(family, 2):
            assert (
                benchmark_script._lexical_overlap(left["source_summary"], right["source_summary"])
                <= 0.25
            )


def test_shared_context_is_byte_identical_across_all_three_arms():
    row = _corpus()["variants"][0]
    calls = []

    class FakeStore:
        def write_semantic(self, **kwargs):
            calls.append(kwargs)
            return {"id": str(uuid4()), **kwargs}

    class FakeEmbeddings:
        def __init__(self):
            self.calls = []

        def embed_document(self, content):
            self.calls.append(content)
            return [float(len(self.calls))]

    embeddings = FakeEmbeddings()
    prepared = benchmark_script._prepare_shared_arm_context(
        row=row,
        embeddings=embeddings,
    )
    namespaces = {
        "no_lesson": "benchmark:test:arm:no-lesson",
        "reference_lesson": "benchmark:test:arm:reference-lesson",
        "consolidated_lesson": "benchmark:test:arm:consolidated-lesson",
    }
    benchmark_script._seed_shared_arm_context(
        store=FakeStore(),
        row=row,
        arm_namespaces=namespaces,
        prepared_context=prepared,
    )

    assert embeddings.calls == [item["content"] for item in row["context_memories"]]
    assert len(calls) == len(row["context_memories"]) * len(benchmark_script.ARM_NAMES)
    by_namespace = defaultdict(list)
    for call in calls:
        by_namespace[call["namespace"]].append(
            {
                "content": call["content"],
                "metadata": call["metadata"],
                "content_schema": call["content_schema"],
                "structured_payload": call["structured_payload"],
                "precomputed_embedding": call["precomputed_embedding"],
                "source_ref": call["provenance"].source_ref,
            }
        )
    assert set(by_namespace) == set(namespaces.values())
    assert by_namespace[namespaces["no_lesson"]] == by_namespace[namespaces["reference_lesson"]]
    assert by_namespace[namespaces["no_lesson"]] == by_namespace[namespaces["consolidated_lesson"]]
    assert benchmark_script._variant_namespaces("benchmark:test")["source"] not in by_namespace


def test_rank_one_requirement_rejects_empty_or_second_place_targets():
    benchmark_script._assert_expected_first(
        hits=[{"id": "lesson"}],
        expected_memory_id="lesson",
        variant_id="variant-a",
        arm="reference_lesson",
    )
    with pytest.raises(RuntimeError, match="failed rank-one retrieval"):
        benchmark_script._assert_expected_first(
            hits=[{"id": "distractor"}, {"id": "lesson"}],
            expected_memory_id="lesson",
            variant_id="variant-a",
            arm="reference_lesson",
        )
    with pytest.raises(RuntimeError, match="got empty"):
        benchmark_script._assert_expected_first(
            hits=[],
            expected_memory_id="lesson",
            variant_id="variant-a",
            arm="reference_lesson",
        )


def test_preregistration_contract_freezes_full_eligible_pool_and_retrieval_policy():
    eligible = [row for row in _corpus()["variants"] if row["split"] == "confirmation"]
    manifest = {
        "corpus_schema_version": benchmark_script.CORPUS_SCHEMA_VERSION,
        "corpus_sha256": "corpus-digest",
        "embedding_max_distance": 0.42,
        "arm_context_policy": "identical_background_and_hard_distractors",
        "source_evidence_policy": "isolated_namespace",
        "study_key_sha256": "study",
        "claim_family_sha256": "claim-family",
        "code_sha": "a" * 40,
    }

    contract = benchmark_script._additional_preregistration_contract(
        manifest_base=manifest,
        eligible_variants=eligible,
    )

    assert contract["embedding_max_distance"] == 0.42
    assert contract["retrieval_rank_requirement"] == 1
    assert set(contract["held_out_variant_sha256"]) == {row["variant_id"] for row in eligible}

    frozen = benchmark_script._held_out_pool_manifest(eligible)
    assert frozen["eligible_held_out_variant_ids"] == [row["variant_id"] for row in eligible]
    assert frozen["eligible_held_out_variant_sha256"] == contract["held_out_variant_sha256"]
    assert frozen["eligible_held_out_query_sha256"] == contract["variant_query_sha256"]
    assert frozen["eligible_held_out_simulator_kind"] == contract["variant_simulator_kind"]


@requires_db
def test_shared_context_transaction_persists_equal_rows_in_every_arm():
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore

    row = _corpus()["variants"][0]
    provider = DeterministicEmbeddingProvider()
    base = f"benchmark-parity-{uuid4()}"
    namespaces = benchmark_script._variant_namespaces(base)
    arms = {name: namespaces[name] for name in benchmark_script.ARM_NAMES}
    prepared = benchmark_script._prepare_shared_arm_context(
        row=row,
        embeddings=provider,
    )
    with connect(database_url()) as conn:
        with conn.transaction():
            benchmark_script._seed_shared_arm_context(
                store=MemoryStore(conn=conn, embedding_provider=provider),
                row=row,
                arm_namespaces=arms,
                prepared_context=prepared,
            )

    with connect(database_url()) as conn:
        rows = conn.execute(
            """
                SELECT namespace, content, content_schema, structured_payload
                FROM semantic_memories
                WHERE namespace = ANY(%s)
                ORDER BY namespace, content
            """,
            (list(arms.values()),),
        ).fetchall()
    persisted = defaultdict(list)
    for namespace, content, schema, payload in rows:
        persisted[str(namespace)].append((str(content), str(schema), dict(payload)))
    assert set(persisted) == set(arms.values())
    assert persisted[arms["no_lesson"]] == persisted[arms["reference_lesson"]]
    assert persisted[arms["no_lesson"]] == persisted[arms["consolidated_lesson"]]
    assert len(persisted[arms["no_lesson"]]) == len(row["context_memories"])


def test_live_commands_reject_implicit_or_deterministic_providers(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    with pytest.raises(RuntimeError, match="explicit provider selection"):
        benchmark_script._require_explicit_live_providers("pilot")

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "deterministic")
    with pytest.raises(RuntimeError, match="semantic embedding provider"):
        benchmark_script._require_explicit_live_providers("confirmation")

    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    benchmark_script._require_explicit_live_providers("pilot")
    benchmark_script._require_explicit_live_providers("ci-smoke")


def test_interrupted_finalizer_cli_does_not_initialize_live_providers(
    monkeypatch, capsys
):
    calls = []

    def fail_provider_initialization(*_args, **_kwargs):
        raise AssertionError("finalization must not initialize a live provider")

    monkeypatch.setattr(
        benchmark_script,
        "runtime_database_url",
        lambda: "postgresql://benchmark",
    )
    monkeypatch.setattr(
        benchmark_script,
        "finalize_interrupted_experiments",
        lambda **kwargs: calls.append(kwargs) or {"experiments": 2},
    )
    monkeypatch.setattr(
        benchmark_script,
        "reasoning_provider_from_env",
        fail_provider_initialization,
    )
    monkeypatch.setattr(
        benchmark_script,
        "embedding_provider_from_env",
        fail_provider_initialization,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_learning_benchmark.py",
            "finalize-interrupted",
            "--code-sha",
            "a" * 40,
            "--reason",
            "runner timed out",
        ],
    )

    benchmark_script.main()

    assert calls == [
        {
            "code_sha": "a" * 40,
            "reason": "runner timed out",
            "db_url": "postgresql://benchmark",
        }
    ]
    assert json.loads(capsys.readouterr().out) == {"experiments": 2}


def test_profile_distance_mismatch_is_not_silently_accepted(monkeypatch):
    profile = SimpleNamespace(
        profile_id="profile-a",
        provider="gemini",
        model="gemini-embedding-2",
        dimensions=1024,
        capability="semantic",
        encoder_revision="gemini-retrieval-task-v1",
        configuration={},
        max_distance=0.42,
    )
    embeddings = SimpleNamespace(
        provider_name="gemini",
        model_name="gemini-embedding-2",
        dimensions=1024,
        capability="semantic",
        encoder_revision="gemini-retrieval-task-v1",
    )

    class FakeStore:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def active_embedding_profile(self):
            return profile

    expected = benchmark_script.embedding_profile(embeddings, max_distance=0.42)
    profile.profile_id = expected.profile_id
    monkeypatch.setattr(benchmark_script, "MemoryStore", FakeStore)

    with pytest.raises(RuntimeError, match="does not match"):
        benchmark_script._resolve_active_profile(
            command="pilot",
            db_url="postgresql://unused",
            embeddings=embeddings,
            expected_max_distance=0.40,
        )
