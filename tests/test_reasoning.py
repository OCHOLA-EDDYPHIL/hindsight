"""Reasoning provider tests."""

import os
from types import SimpleNamespace

import pytest


def test_deterministic_reasoning_provider_returns_stable_response():
    from hindsight.reasoning import DeterministicReasoningProvider, ReasoningRequest

    provider = DeterministicReasoningProvider(response_text="known answer")
    response = provider.generate(ReasoningRequest(prompt="what happened?"))

    assert response.text == "known answer"
    assert response.provider == "deterministic"
    assert response.model == "deterministic-v1"
    assert response.usage["prompt_characters"] == len("what happened?")


def test_provider_from_env_defaults_to_gemini_and_requires_key():
    from hindsight.reasoning import ReasoningProviderError, reasoning_provider_from_env

    with pytest.raises(ReasoningProviderError, match="GEMINI_API_KEY"):
        reasoning_provider_from_env({})


def test_provider_from_env_supports_deterministic():
    from hindsight.reasoning import DeterministicReasoningProvider, reasoning_provider_from_env

    provider = reasoning_provider_from_env({"LLM_PROVIDER": "deterministic"})

    assert isinstance(provider, DeterministicReasoningProvider)


def test_gemini_provider_uses_injected_client_without_network():
    from hindsight.reasoning import GeminiReasoningProvider, ReasoningRequest

    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                text="triage the payment timeout",
                usage_metadata={"prompt_token_count": 7, "candidates_token_count": 4},
            )

    provider = GeminiReasoningProvider(
        model_name="gemini-test",
        client=SimpleNamespace(models=FakeModels()),
    )

    response = provider.generate(
        ReasoningRequest(
            system="You are an incident commander.",
            prompt="Summarize the incident.",
            max_output_tokens=64,
        )
    )

    assert response.text == "triage the payment timeout"
    assert response.provider == "gemini"
    assert response.model == "gemini-test"
    assert response.usage["prompt_token_count"] == 7
    assert calls[0]["model"] == "gemini-test"
    assert "incident commander" in calls[0]["contents"]
    assert calls[0]["config"]["max_output_tokens"] == 64


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEMINI_REASONING") != "1",
    reason="live Gemini reasoning invocation is opt-in",
)
def test_live_gemini_reasoning_provider():
    from hindsight.reasoning import GeminiReasoningProvider, ReasoningRequest

    response = GeminiReasoningProvider().generate(
        ReasoningRequest(prompt="Reply with exactly: ok", max_output_tokens=8)
    )

    assert response.text.strip()
