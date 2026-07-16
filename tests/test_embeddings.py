"""Embedding provider tests."""

import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_gemini_embedding_provider_requests_1024_dimensions():
    from hindsight.embeddings import EMBEDDING_DIMENSIONS, GeminiEmbeddingProvider
    from hindsight.gemini import PoolExecution

    calls = []

    class FakePool:
        def execute(self, operation, *, routing_key):
            class Models:
                def embed_content(self, **kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(
                        embeddings=[SimpleNamespace(values=[0.25] * EMBEDDING_DIMENSIONS)]
                    )

            value = operation(SimpleNamespace(models=Models()))
            return PoolExecution(value=value, slot_id="slot-a", attempts=1)

    provider = GeminiEmbeddingProvider(credential_pool=FakePool())
    vector = provider.embed("payment timeout")

    assert len(vector) == EMBEDDING_DIMENSIONS
    assert calls[0]["model"] == "gemini-embedding-2"
    assert calls[0]["config"]["output_dimensionality"] == EMBEDDING_DIMENSIONS


def test_deterministic_embedding_provider_is_stable():
    from hindsight.embeddings import EMBEDDING_DIMENSIONS, DeterministicEmbeddingProvider

    provider = DeterministicEmbeddingProvider()

    first = provider.embed("payment timeout in worker")
    second = provider.embed("payment timeout in worker")

    assert first == second
    assert len(first) == EMBEDDING_DIMENSIONS
    assert any(value != 0 for value in first)


def test_default_provider_never_constructs_bedrock(monkeypatch):
    from hindsight.embeddings import (
        DeterministicEmbeddingProvider,
        embedding_provider_from_env,
    )

    def unexpected_bedrock(**_kwargs):
        pytest.fail("default provider selection constructed Bedrock")

    monkeypatch.setattr(
        "hindsight.embeddings.BedrockTitanEmbeddingProvider", unexpected_bedrock
    )

    provider = embedding_provider_from_env({})

    assert isinstance(provider, DeterministicEmbeddingProvider)


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BEDROCK_EMBEDDINGS") != "1",
    reason="live Bedrock embedding invocation is opt-in",
)
def test_live_bedrock_titan_embedding_provider():
    from hindsight.embeddings import (
        BEDROCK_TITAN_EMBED_MODEL,
        EMBEDDING_DIMENSIONS,
        BedrockTitanEmbeddingProvider,
    )

    configured_model = (
        os.environ.get("BEDROCK_EMBEDDING_MODEL") or BEDROCK_TITAN_EMBED_MODEL
    )
    provider = BedrockTitanEmbeddingProvider(
        model_id=configured_model,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )

    assert provider.model_name == configured_model

    query = provider.embed_query(
        "Purchases freeze whenever the remote acquirer hesitates, and every failed "
        "attempt creates even more work."
    )
    relevant = provider.embed_document(
        "When checkout latency follows downstream processor failures that multiply "
        "retries, inspect dependency health and reduce retry fanout before adding workers."
    )
    distractor = provider.embed_document(
        "When card gateway certificate expiration breaks checkout, rotate the TLS "
        "certificate and restart edge connections."
    )

    assert len(query) == len(relevant) == len(distractor) == EMBEDDING_DIMENSIONS
    assert any(value != 0 for value in query)
    assert _cosine_distance(query, relevant) < _cosine_distance(query, distractor)


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEMINI_EMBEDDINGS") != "1",
    reason="live Gemini embedding invocation is opt-in",
)
def test_live_gemini_embedding_provider_ranks_low_overlap_paraphrase():
    from hindsight.embeddings import DEFAULT_GEMINI_EMBEDDING_MODEL, GeminiEmbeddingProvider
    from hindsight.gemini import gemini_pool_from_env

    configured_model = (
        os.environ.get("GEMINI_EMBEDDING_MODEL") or DEFAULT_GEMINI_EMBEDDING_MODEL
    )
    provider = GeminiEmbeddingProvider(
        credential_pool=gemini_pool_from_env(),
        model_name=configured_model,
    )

    assert provider.model_name == configured_model
    query = provider.embed_query(
        "Purchases freeze whenever the remote acquirer hesitates, and each failed "
        "attempt creates even more work."
    )
    relevant = provider.embed_document(
        "When checkout latency follows downstream processor failures that multiply "
        "retries, inspect dependency health and reduce retry fanout before adding workers."
    )
    lexical_distractor = provider.embed_document(
        "When card gateway certificate expiration breaks checkout, rotate the TLS "
        "certificate and restart edge connections."
    )

    relevant_distance = _cosine_distance(query, relevant)
    distractor_distance = _cosine_distance(query, lexical_distractor)

    assert relevant_distance < distractor_distance
    assert relevant_distance < 0.35


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEMINI_EMBEDDINGS") != "1",
    reason="live Gemini embedding invocation is opt-in",
)
def test_live_gemini_embedding_provider_ranks_frozen_pilot_reference_lessons():
    from hindsight.embeddings import (
        DEFAULT_GEMINI_EMBEDDING_MODEL,
        GeminiEmbeddingProvider,
    )
    from hindsight.gemini import gemini_pool_from_env

    configured_model = (
        os.environ.get("GEMINI_EMBEDDING_MODEL") or DEFAULT_GEMINI_EMBEDDING_MODEL
    )
    provider = GeminiEmbeddingProvider(
        credential_pool=gemini_pool_from_env(),
        model_name=configured_model,
    )
    corpus_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "benchmark_variants.json"
    )
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    pilot = [row for row in corpus["variants"] if row["split"] == "pilot"]

    assert len(pilot) == 6
    for row in pilot:
        query = provider.embed_query(row["recurrence_query"])
        candidates = [
            ("reference_lesson", "target", row["reference_lesson"]),
            *[
                (context["context_id"], context["role"], context["content"])
                for context in row["context_memories"]
            ],
        ]
        ranked = sorted(
            (
                _cosine_distance(query, provider.embed_document(content)),
                candidate_id,
                role,
            )
            for candidate_id, role, content in candidates
        )
        reference_distance = next(
            distance
            for distance, candidate_id, _role in ranked
            if candidate_id == "reference_lesson"
        )
        distractor_distances = [
            distance for distance, _candidate_id, role in ranked if role == "hard_distractor"
        ]

        assert ranked[0][1] == "reference_lesson", (row["variant_id"], ranked)
        assert reference_distance < 0.35, (row["variant_id"], ranked)
        assert len(distractor_distances) >= 2
        assert all(distance < 0.35 for distance in distractor_distances), (
            row["variant_id"],
            ranked,
        )


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return 1 - dot / (left_norm * right_norm)
