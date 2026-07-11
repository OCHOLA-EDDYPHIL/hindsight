"""Provider-pluggable reasoning backends.

The agent runtime should choose a model provider through configuration, not
architecture. Live hosted providers are opt-in; tests use deterministic
responses unless explicitly configured otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

DEFAULT_LLM_PROVIDER = "gemini"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
LIVE_GEMINI_REASONING_FLAG = "RUN_LIVE_GEMINI_REASONING"


class ReasoningProviderError(RuntimeError):
    """Raised when a reasoning provider cannot be configured or invoked."""


@dataclass(frozen=True)
class ReasoningRequest:
    """One reasoning turn sent to a provider."""

    prompt: str
    system: str | None = None
    temperature: float = 0.2
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ReasoningResponse:
    """Normalized provider response."""

    text: str
    provider: str
    model: str
    usage: Mapping[str, Any] = field(default_factory=dict)


class ReasoningProvider(Protocol):
    """Common contract for hosted or local model providers."""

    provider_name: str
    model_name: str

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        """Generate a response for one reasoning request."""


class DeterministicReasoningProvider:
    """No-network provider for tests, fixtures, and replay-style runs."""

    provider_name = "deterministic"

    def __init__(self, *, response_text: str = "deterministic response"):
        self.model_name = "deterministic-v1"
        self._response_text = response_text

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        return ReasoningResponse(
            text=self._response_text,
            provider=self.provider_name,
            model=self.model_name,
            usage={
                "prompt_characters": len(request.prompt),
                "system_characters": len(request.system or ""),
            },
        )


class GeminiReasoningProvider:
    """Gemini Developer API reasoning provider."""

    provider_name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = DEFAULT_GEMINI_MODEL,
        client: Any | None = None,
    ):
        if client is None:
            api_key = api_key or os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ReasoningProviderError("GEMINI_API_KEY is required for Gemini")
            from google import genai

            client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self._client = client

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        contents = request.prompt
        if request.system:
            contents = f"{request.system}\n\n{request.prompt}"
        config: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            config["max_output_tokens"] = request.max_output_tokens
        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
        except Exception as exc:  # pragma: no cover - provider SDK details vary.
            raise ReasoningProviderError(f"Gemini generation failed: {exc}") from exc
        text = getattr(response, "text", None)
        if not text:
            raise ReasoningProviderError("Gemini returned an empty response")
        return ReasoningResponse(
            text=text,
            provider=self.provider_name,
            model=self.model_name,
            usage=_usage_dict(getattr(response, "usage_metadata", None)),
        )


def reasoning_provider_from_env(
    environ: Mapping[str, str] | None = None,
) -> ReasoningProvider:
    """Build the configured reasoning provider from environment values."""

    env = os.environ if environ is None else environ
    provider = (env.get("LLM_PROVIDER") or DEFAULT_LLM_PROVIDER).strip().lower()
    if provider == "deterministic":
        return DeterministicReasoningProvider()
    if provider == "gemini":
        return GeminiReasoningProvider(
            api_key=env.get("GEMINI_API_KEY"),
            model_name=(env.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip(),
        )
    if provider == "bedrock":
        raise ReasoningProviderError("Bedrock reasoning is not available until quota is granted")
    if provider == "azure":
        raise ReasoningProviderError("Azure reasoning adapter is not implemented yet")
    raise ReasoningProviderError(f"Unsupported LLM_PROVIDER: {provider}")


def _usage_dict(usage_metadata: Any) -> dict[str, Any]:
    if usage_metadata is None:
        return {}
    if hasattr(usage_metadata, "model_dump"):
        return dict(usage_metadata.model_dump(exclude_none=True))
    if hasattr(usage_metadata, "to_json_dict"):
        return dict(usage_metadata.to_json_dict())
    if isinstance(usage_metadata, Mapping):
        return dict(usage_metadata)
    return {}
