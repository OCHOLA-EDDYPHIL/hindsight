"""Gemini embeddings and content-addressed semantic-memory profiles."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from hindsight.gemini import GeminiCredentialPool, gemini_pool_from_env

EMBEDDING_DIMENSIONS = 1024
DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-2"
GEMINI_REPRESENTATIONS = (
    "raw_control",
    "generic_title",
    "applicability_instruction",
)


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
        representation: str = "raw_control",
    ):
        if representation not in GEMINI_REPRESENTATIONS:
            raise ValueError("unsupported Gemini retrieval representation")
        self.model_name = model_name
        self.dimensions = dimensions
        self.representation = representation
        if representation != "raw_control":
            self.encoder_revision = f"gemini-retrieval-task-v2-{representation}"
        self._credential_pool = credential_pool

    def _embed(self, text: str, *, task_type: str, title: str | None = None) -> list[float]:
        def invoke(client: Any) -> Any:
            config = {
                "output_dimensionality": self.dimensions,
                "task_type": task_type,
            }
            if title is not None:
                config["title"] = title
            return client.models.embed_content(
                model=self.model_name,
                contents=text,
                config=config,
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
        content, title = _gemini_representation(
            text, representation=self.representation, query=False
        )
        return self._embed(
            content,
            task_type="RETRIEVAL_DOCUMENT",
            title=title,
        )

    def embed_query(self, text: str) -> list[float]:
        content, _ = _gemini_representation(text, representation=self.representation, query=True)
        return self._embed(
            content,
            task_type="RETRIEVAL_QUERY",
        )


def embedding_provider_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    gemini_pool: GeminiCredentialPool | None = None,
) -> EmbeddingProvider:
    """Build the configured Gemini embedding provider."""

    env = os.environ if environ is None else environ
    provider = (env.get("EMBEDDING_PROVIDER") or "gemini").strip().lower()
    if provider == "gemini":
        pool = gemini_pool or gemini_pool_from_env(env)
        return GeminiEmbeddingProvider(
            credential_pool=pool,
            model_name=(
                env.get("GEMINI_EMBEDDING_MODEL") or DEFAULT_GEMINI_EMBEDDING_MODEL
            ).strip(),
            representation=(env.get("HINDSIGHT_GEMINI_REPRESENTATION") or "raw_control").strip(),
        )
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider}")


def _gemini_representation(
    text: str, *, representation: str, query: bool
) -> tuple[str, str | None]:
    """Format raw text without accepting candidate metadata or identity."""

    normalized = unicodedata.normalize(
        "NFC", text.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()
    if representation == "raw_control":
        return normalized, None
    if representation == "generic_title":
        return normalized, None if query else "Hindsight operational memory"
    if representation == "applicability_instruction":
        if query:
            return (
                "Retrieve the operational memory most applicable to this incident.\n"
                f"Incident:\n{normalized}",
                None,
            )
        return (
            "Operational memory that may contain a relevant situation, check, or action.\n"
            f"Memory:\n{normalized}",
            "Hindsight operational memory",
        )
    raise ValueError("unsupported Gemini retrieval representation")
