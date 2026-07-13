"""Provider-pluggable reasoning backends.

The agent runtime should choose a model provider through configuration, not
architecture. Live hosted providers are opt-in; tests use deterministic
responses unless explicitly configured otherwise.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from hindsight.aws import aws_client_config

DEFAULT_LLM_PROVIDER = "gemini"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_BEDROCK_MODEL = "anthropic.claude-3-haiku-20240307-v1:0"
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


class BedrockReasoningProvider:
    """Amazon Bedrock Runtime reasoning provider using the Converse API."""

    provider_name = "bedrock"

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_BEDROCK_MODEL,
        region_name: str | None = None,
        client: Any | None = None,
    ):
        if client is None:
            import boto3

            client = boto3.client(
                "bedrock-runtime",
                region_name=region_name,
                config=aws_client_config(),
            )
        self.model_name = model_name
        self._client = client

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        messages = [{"role": "user", "content": [{"text": request.prompt}]}]
        payload: dict[str, Any] = {
            "modelId": self.model_name,
            "messages": messages,
            "inferenceConfig": {"temperature": request.temperature},
        }
        if request.system:
            payload["system"] = [{"text": request.system}]
        if request.max_output_tokens is not None:
            payload["inferenceConfig"]["maxTokens"] = request.max_output_tokens
        try:
            response = self._client.converse(**payload)
        except Exception as exc:  # pragma: no cover - provider SDK details vary.
            raise ReasoningProviderError(f"Bedrock generation failed: {exc}") from exc
        text = _bedrock_text(response)
        if not text:
            raise ReasoningProviderError("Bedrock returned an empty response")
        return ReasoningResponse(
            text=text,
            provider=self.provider_name,
            model=self.model_name,
            usage=_usage_dict(response.get("usage")),
        )


class RetryingReasoningProvider:
    """Retry wrapper for transient provider failures."""

    def __init__(
        self,
        provider: ReasoningProvider,
        *,
        max_attempts: int = 2,
        retryable: Callable[[Exception], bool] | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._provider = provider
        self.provider_name = provider.provider_name
        self.model_name = provider.model_name
        self._max_attempts = max_attempts
        self._retryable = retryable or (lambda exc: isinstance(exc, ReasoningProviderError))

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        attempts = 0
        last_error: Exception | None = None
        while attempts < self._max_attempts:
            attempts += 1
            try:
                response = self._provider.generate(request)
            except Exception as exc:
                last_error = exc
                if attempts >= self._max_attempts or not self._retryable(exc):
                    break
                continue
            usage = dict(response.usage)
            usage["attempts"] = attempts
            return ReasoningResponse(
                text=response.text,
                provider=response.provider,
                model=response.model,
                usage=usage,
            )
        assert last_error is not None
        if isinstance(last_error, ReasoningProviderError):
            raise last_error
        raise ReasoningProviderError(f"Reasoning provider failed: {last_error}") from last_error


def reasoning_provider_from_env(
    environ: Mapping[str, str] | None = None,
) -> ReasoningProvider:
    """Build the configured reasoning provider from environment values."""

    env = os.environ if environ is None else environ
    provider = (env.get("LLM_PROVIDER") or DEFAULT_LLM_PROVIDER).strip().lower()
    if provider == "deterministic":
        return DeterministicReasoningProvider()
    if provider == "gemini":
        if environ is not None and not env.get("GEMINI_API_KEY"):
            raise ReasoningProviderError("GEMINI_API_KEY is required for Gemini")
        return GeminiReasoningProvider(
            api_key=env.get("GEMINI_API_KEY"),
            model_name=(env.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip(),
        )
    if provider == "bedrock":
        return BedrockReasoningProvider(
            model_name=(env.get("BEDROCK_MODEL") or DEFAULT_BEDROCK_MODEL).strip(),
            region_name=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION"),
        )
    if provider == "azure":
        raise ReasoningProviderError("Azure reasoning adapter is not implemented yet")
    raise ReasoningProviderError(f"Unsupported LLM_PROVIDER: {provider}")


def retrying_reasoning_provider(
    provider: ReasoningProvider,
    *,
    max_attempts: int = 2,
) -> ReasoningProvider:
    """Return a provider wrapper that records attempts in usage metadata."""

    return RetryingReasoningProvider(provider, max_attempts=max_attempts)


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


def _bedrock_text(response: Mapping[str, Any]) -> str:
    message = response.get("output", {}).get("message", {})
    blocks = message.get("content", [])
    return "".join(str(block.get("text", "")) for block in blocks).strip()
