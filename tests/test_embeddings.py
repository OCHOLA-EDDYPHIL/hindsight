"""Embedding provider tests."""

import os

import pytest


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
