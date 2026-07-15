"""Embedding providers for semantic memory.

The vector store is provider-pluggable. Tests and local deterministic runs use
the stable hashing provider; production providers are selected explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from hindsight.aws import aws_client_config
from hindsight.gemini import GeminiCredentialPool, gemini_pool_from_env

EMBEDDING_DIMENSIONS = 1024
BEDROCK_TITAN_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
LIVE_BEDROCK_EMBEDDINGS_FLAG = "RUN_LIVE_BEDROCK_EMBEDDINGS"
DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-2"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProvider(Protocol):
    """Provider contract used by the memory layer."""

    provider_name: str
    model_name: str
    dimensions: int
    capability: str
    encoder_revision: str

    def embed(self, text: str) -> list[float]:
        """Return one embedding vector for text."""

    def embed_document(self, text: str) -> list[float]:
        """Embed stored content for the document side of retrieval."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a query for the query side of retrieval."""


@dataclass(frozen=True)
class EmbeddingProfile:
    """Content-addressed description of one compatible vector space."""

    profile_id: str
    provider: str
    model: str
    dimensions: int
    capability: str
    encoder_revision: str
    configuration: Mapping[str, Any] = field(default_factory=dict)
    max_distance: float | None = None


def embedding_profile(
    provider: EmbeddingProvider,
    *,
    configuration: Mapping[str, Any] | None = None,
    max_distance: float | None = None,
) -> EmbeddingProfile:
    """Return a stable profile identity for provider/model/encoding behavior."""

    payload = {
        "provider": provider.provider_name,
        "model": provider.model_name,
        "dimensions": provider.dimensions,
        "capability": provider.capability,
        "encoder_revision": provider.encoder_revision,
        "configuration": dict(configuration or {}),
        "max_distance": max_distance,
    }
    profile_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return EmbeddingProfile(
        profile_id=profile_id,
        provider=provider.provider_name,
        model=provider.model_name,
        dimensions=provider.dimensions,
        capability=provider.capability,
        encoder_revision=provider.encoder_revision,
        configuration=payload["configuration"],
        max_distance=max_distance,
    )


def vector_literal(values: Sequence[float], *, dimensions: int = EMBEDDING_DIMENSIONS) -> str:
    """Serialize a vector for CockroachDB's VECTOR cast."""

    if len(values) != dimensions:
        raise ValueError(f"expected {dimensions} dimensions, got {len(values)}")
    return "[" + ",".join(f"{value:.9g}" for value in values) + "]"


class DeterministicEmbeddingProvider:
    """Stable, dependency-free embeddings for tests and repeatable local runs."""

    provider_name = "deterministic"
    model_name = "stable-hash-v1"
    capability = "lexical_hash"
    encoder_revision = "hashed-unigram-tf-v1"

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

    def embed_document(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)


class GeminiEmbeddingProvider:
    """Gemini Developer API embeddings routed through the shared key pool."""

    provider_name = "gemini"
    capability = "semantic"
    encoder_revision = "gemini-retrieval-task-v1"

    def __init__(
        self,
        *,
        credential_pool: GeminiCredentialPool,
        model_name: str = DEFAULT_GEMINI_EMBEDDING_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
    ):
        self.model_name = model_name
        self.dimensions = dimensions
        self._credential_pool = credential_pool

    def _embed(self, text: str, *, task_type: str) -> list[float]:
        def invoke(client: Any) -> Any:
            return client.models.embed_content(
                model=self.model_name,
                contents=text,
                config={
                    "output_dimensionality": self.dimensions,
                    "task_type": task_type,
                },
            )

        execution = self._credential_pool.execute(invoke, routing_key=text)
        embeddings = getattr(execution.value, "embeddings", None) or []
        if not embeddings:
            raise RuntimeError("Gemini returned no embedding")
        values = getattr(embeddings[0], "values", None)
        if values is None and isinstance(embeddings[0], dict):
            values = embeddings[0].get("values")
        if values is None:
            raise RuntimeError("Gemini returned an embedding without values")
        vector = [float(value) for value in values]
        if len(vector) != self.dimensions:
            raise ValueError(
                f"Gemini returned {len(vector)} dimensions, expected {self.dimensions}"
            )
        return vector

    def embed(self, text: str) -> list[float]:
        return self.embed_document(text)

    def embed_document(self, text: str) -> list[float]:
        return self._embed(text, task_type="RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text, task_type="RETRIEVAL_QUERY")


class BedrockTitanEmbeddingProvider:
    """Amazon Bedrock Titan Text Embeddings V2 provider."""

    provider_name = "bedrock"
    capability = "semantic"
    encoder_revision = "titan-text-v2-normalized-v1"

    def __init__(
        self,
        *,
        model_id: str = BEDROCK_TITAN_EMBED_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
        region_name: str | None = None,
    ):
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

    def embed_document(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)


def embedding_provider_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    gemini_pool: GeminiCredentialPool | None = None,
) -> EmbeddingProvider:
    """Build the configured embedding provider without enabling live calls implicitly."""

    env = os.environ if environ is None else environ
    provider = (env.get("EMBEDDING_PROVIDER") or "deterministic").strip().lower()
    if provider == "deterministic":
        return DeterministicEmbeddingProvider()
    if provider == "gemini":
        pool = gemini_pool or gemini_pool_from_env(env)
        return GeminiEmbeddingProvider(
            credential_pool=pool,
            model_name=(
                env.get("GEMINI_EMBEDDING_MODEL") or DEFAULT_GEMINI_EMBEDDING_MODEL
            ).strip(),
        )
    if provider == "bedrock":
        return BedrockTitanEmbeddingProvider(
            model_id=(env.get("BEDROCK_EMBEDDING_MODEL") or BEDROCK_TITAN_EMBED_MODEL).strip(),
            region_name=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION"),
        )
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider}")
