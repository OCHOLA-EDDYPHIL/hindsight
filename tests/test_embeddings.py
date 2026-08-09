"""Embedding provider tests."""

import math
import os
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


@pytest.mark.parametrize(
    ("representation", "expected_contents", "expected_title"),
    [
        ("raw_control", "payment timeout", None),
        ("generic_title", "payment timeout", "Hindsight operational memory"),
        (
            "applicability_instruction",
            "Operational memory that may contain a relevant situation, check, or action.\n"
            "Memory:\npayment timeout",
            "Hindsight operational memory",
        ),
    ],
)
def test_gemini_document_representation_is_bounded(
    representation, expected_contents, expected_title
):
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

            return PoolExecution(
                value=operation(SimpleNamespace(models=Models())),
                slot_id="slot-a",
                attempts=1,
            )

    provider = GeminiEmbeddingProvider(credential_pool=FakePool(), representation=representation)
    provider.embed_document("payment timeout")

    assert calls[0]["contents"] == expected_contents
    assert calls[0]["config"].get("title") == expected_title


def test_embedding_provider_threads_gemini_representation_from_environment():
    from hindsight.embeddings import embedding_provider_from_env

    provider = embedding_provider_from_env(
        {
            "EMBEDDING_PROVIDER": "gemini",
            "HINDSIGHT_GEMINI_REPRESENTATION": "applicability_instruction",
        },
        gemini_pool=object(),
    )

    assert provider.representation == "applicability_instruction"
    assert provider.encoder_revision == ("gemini-retrieval-task-v2-applicability_instruction")


def test_gemini_embedding_provider_rejects_unknown_representation():
    from hindsight.embeddings import GeminiEmbeddingProvider

    with pytest.raises(ValueError, match="unsupported Gemini retrieval representation"):
        GeminiEmbeddingProvider(credential_pool=object(), representation="candidate_metadata")


def test_deterministic_embedding_provider_is_stable():
    from hindsight.embeddings import EMBEDDING_DIMENSIONS
    from tests.fakes import DeterministicEmbeddingProvider

    provider = DeterministicEmbeddingProvider()

    first = provider.embed("payment timeout in worker")
    second = provider.embed("payment timeout in worker")

    assert first == second
    assert len(first) == EMBEDDING_DIMENSIONS
    assert any(value != 0 for value in first)


def test_default_provider_constructs_gemini():
    from hindsight.embeddings import (
        GeminiEmbeddingProvider,
        embedding_provider_from_env,
    )

    provider = embedding_provider_from_env({}, gemini_pool=object())

    assert isinstance(provider, GeminiEmbeddingProvider)


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEMINI_EMBEDDINGS") != "1",
    reason="live Gemini embedding invocation is opt-in",
)
def test_live_gemini_embedding_provider_ranks_low_overlap_paraphrase():
    from hindsight.embeddings import DEFAULT_GEMINI_EMBEDDING_MODEL, GeminiEmbeddingProvider
    from hindsight.gemini import gemini_pool_from_env

    configured_model = os.environ.get("GEMINI_EMBEDDING_MODEL") or DEFAULT_GEMINI_EMBEDDING_MODEL
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


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return 1 - dot / (left_norm * right_norm)
