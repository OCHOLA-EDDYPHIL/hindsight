"""Embedding providers for semantic memory.

The vector store is provider-pluggable. Tests and local deterministic runs use
the stable hashing provider; production can opt into Bedrock explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Sequence
from typing import Protocol

from hindsight.aws import aws_client_config

EMBEDDING_DIMENSIONS = 1024
BEDROCK_TITAN_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
LIVE_BEDROCK_EMBEDDINGS_FLAG = "RUN_LIVE_BEDROCK_EMBEDDINGS"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProvider(Protocol):
    """Provider contract used by the memory layer."""

    provider_name: str
    model_name: str
    dimensions: int

    def embed(self, text: str) -> list[float]:
        """Return one embedding vector for text."""


def vector_literal(values: Sequence[float], *, dimensions: int = EMBEDDING_DIMENSIONS) -> str:
    """Serialize a vector for CockroachDB's VECTOR cast."""

    if len(values) != dimensions:
        raise ValueError(f"expected {dimensions} dimensions, got {len(values)}")
    return "[" + ",".join(f"{value:.9g}" for value in values) + "]"


class DeterministicEmbeddingProvider:
    """Stable, dependency-free embeddings for tests and repeatable local runs."""

    provider_name = "deterministic"
    model_name = "stable-hash-v1"

    def __init__(self, *, dimensions: int = EMBEDDING_DIMENSIONS):
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            vector[0] = 1.0
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


class BedrockTitanEmbeddingProvider:
    """Amazon Bedrock Titan Text Embeddings V2 provider."""

    provider_name = "bedrock"

    def __init__(
        self,
        *,
        model_id: str = BEDROCK_TITAN_EMBED_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
        region_name: str | None = None,
    ):
        if os.environ.get(LIVE_BEDROCK_EMBEDDINGS_FLAG) != "1":
            raise RuntimeError(
                f"Set {LIVE_BEDROCK_EMBEDDINGS_FLAG}=1 to enable live Bedrock embeddings"
            )
        import boto3

        self.model_name = model_id
        self.dimensions = dimensions
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region_name,
            config=aws_client_config(),
        )

    def embed(self, text: str) -> list[float]:
        body = json.dumps(
            {
                "inputText": text,
                "dimensions": self.dimensions,
                "normalize": True,
            }
        )
        response = self._client.invoke_model(
            modelId=self.model_name,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        embedding = payload["embedding"]
        if len(embedding) != self.dimensions:
            raise ValueError(
                f"Bedrock returned {len(embedding)} dimensions, expected {self.dimensions}"
            )
        return [float(value) for value in embedding]
