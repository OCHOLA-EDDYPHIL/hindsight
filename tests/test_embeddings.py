"""Embedding provider tests."""

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


def test_deterministic_embedding_provider_is_stable():
    from hindsight.embeddings import EMBEDDING_DIMENSIONS, DeterministicEmbeddingProvider

    provider = DeterministicEmbeddingProvider()

    first = provider.embed("payment timeout in worker")
    second = provider.embed("payment timeout in worker")

    assert first == second
    assert len(first) == EMBEDDING_DIMENSIONS
    assert any(value != 0 for value in first)


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BEDROCK_EMBEDDINGS") != "1",
    reason="live Bedrock embedding invocation is opt-in",
)
def test_live_bedrock_titan_embedding_provider():
    from hindsight.embeddings import EMBEDDING_DIMENSIONS, BedrockTitanEmbeddingProvider

    provider = BedrockTitanEmbeddingProvider(region_name=os.environ.get("AWS_REGION", "us-east-1"))

    embedding = provider.embed("payment timeout in worker")

    assert len(embedding) == EMBEDDING_DIMENSIONS
    assert any(value != 0 for value in embedding)
