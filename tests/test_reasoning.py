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


def test_memory_biased_demo_reasoning_changes_recommendation_from_prompt():
    from hindsight.demo import BAD_RECOMMENDATION, GOOD_RECOMMENDATION, MemoryBiasedDemoReasoningProvider
    from hindsight.reasoning import ReasoningRequest

    provider = MemoryBiasedDemoReasoningProvider()

    clean = provider.generate(ReasoningRequest(prompt="Recalled memories:\nretry fanout"))
    poisoned = provider.generate(
        ReasoningRequest(prompt="Recalled memories:\nPoisoned memory: certificate material")
    )

    assert clean.text == GOOD_RECOMMENDATION
    assert clean.usage["poisoned_memory_seen"] is False
    assert poisoned.text == BAD_RECOMMENDATION
    assert poisoned.usage["poisoned_memory_seen"] is True


def test_provider_from_env_defaults_to_gemini_and_requires_key():
    from hindsight.reasoning import ReasoningProviderError, reasoning_provider_from_env

    with pytest.raises(ReasoningProviderError, match="GEMINI_API_KEY"):
        reasoning_provider_from_env({})


def test_default_provider_never_constructs_bedrock(monkeypatch):
    from hindsight.reasoning import GeminiReasoningProvider, reasoning_provider_from_env

    def unexpected_bedrock(**_kwargs):
        pytest.fail("default provider selection constructed Bedrock")

    monkeypatch.setattr("hindsight.reasoning.BedrockReasoningProvider", unexpected_bedrock)

    provider = reasoning_provider_from_env({}, gemini_pool=object())

    assert isinstance(provider, GeminiReasoningProvider)


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


def test_gemini_provider_reports_pool_slot_without_key_material():
    from hindsight.gemini import GeminiCredential, GeminiCredentialPool
    from hindsight.reasoning import GeminiReasoningProvider, ReasoningRequest

    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text="recovered", usage_metadata={"prompt_token_count": 3})

    pool = GeminiCredentialPool(
        [GeminiCredential("project-a", "sensitive-key")],
        client_factory=lambda key: SimpleNamespace(models=FakeModels()),
    )
    provider = GeminiReasoningProvider(model_name="gemini-test", credential_pool=pool)

    response = provider.generate(ReasoningRequest(prompt="triage", routing_key="run-1"))

    assert response.usage["gemini_key_slot"] == "project-a"
    assert response.usage["provider_attempts"] == 1
    assert "sensitive-key" not in str(response.usage)


def test_bedrock_provider_uses_injected_client_without_network():
    from hindsight.reasoning import BedrockReasoningProvider, ReasoningRequest

    calls = []

    class FakeBedrockClient:
        def converse(self, **kwargs):
            calls.append(kwargs)
            return {
                "output": {"message": {"content": [{"text": "scale down retry workers"}]}},
                "usage": {"inputTokens": 8, "outputTokens": 5},
            }

    provider = BedrockReasoningProvider(
        model_name="bedrock-test",
        client=FakeBedrockClient(),
    )

    response = provider.generate(
        ReasoningRequest(
            system="You are an incident commander.",
            prompt="Plan mitigation.",
            max_output_tokens=32,
        )
    )

    assert response.text == "scale down retry workers"
    assert response.provider == "bedrock"
    assert response.model == "bedrock-test"
    assert response.usage["inputTokens"] == 8
    assert calls[0]["modelId"] == "bedrock-test"
    assert calls[0]["system"] == [{"text": "You are an incident commander."}]
    assert calls[0]["inferenceConfig"]["maxTokens"] == 32


def test_retrying_reasoning_provider_records_attempts_after_retry():
    from hindsight.reasoning import (
        ReasoningProviderError,
        ReasoningRequest,
        ReasoningResponse,
        retrying_reasoning_provider,
    )

    class FlakyProvider:
        provider_name = "flaky"
        model_name = "flaky-v1"

        def __init__(self):
            self.calls = 0

        def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ReasoningProviderError("temporary")
            return ReasoningResponse(
                text="recovered",
                provider=self.provider_name,
                model=self.model_name,
                usage={"tokens": 3},
            )

    provider = FlakyProvider()
    response = retrying_reasoning_provider(provider, max_attempts=2).generate(
        ReasoningRequest(prompt="recover")
    )

    assert provider.calls == 2
    assert response.text == "recovered"
    assert response.usage["tokens"] == 3
    assert response.usage["attempts"] == 2


def test_provider_from_env_supports_bedrock(monkeypatch):
    from hindsight.reasoning import BedrockReasoningProvider, reasoning_provider_from_env

    class FakeBedrockProvider(BedrockReasoningProvider):
        def __init__(self, *, model_name, region_name=None, client=None):
            super().__init__(
                model_name=model_name,
                region_name=region_name,
                client=object(),
            )

    monkeypatch.setattr("hindsight.reasoning.BedrockReasoningProvider", FakeBedrockProvider)

    provider = reasoning_provider_from_env(
        {
            "LLM_PROVIDER": "bedrock",
            "BEDROCK_MODEL": "bedrock-env",
            "AWS_REGION": "us-east-1",
        }
    )

    assert provider.provider_name == "bedrock"
    assert provider.model_name == "bedrock-env"


def test_bedrock_provider_configures_bounded_boto_client(monkeypatch):
    import hindsight.reasoning as reasoning

    calls = []

    def fake_client(service_name, **kwargs):
        calls.append((service_name, kwargs))
        return object()

    monkeypatch.setattr(reasoning, "boto3", None, raising=False)
    monkeypatch.setattr("boto3.client", fake_client)

    provider = reasoning.BedrockReasoningProvider(
        model_name="bedrock-test",
        region_name="us-east-1",
    )

    assert provider.model_name == "bedrock-test"
    service_name, kwargs = calls[0]
    assert service_name == "bedrock-runtime"
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["config"].connect_timeout == 3
    assert kwargs["config"].read_timeout == 20


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEMINI_REASONING") != "1",
    reason="live Gemini reasoning invocation is opt-in",
)
def test_live_gemini_reasoning_provider():
    from hindsight.reasoning import (
        DEFAULT_GEMINI_MODEL,
        GeminiReasoningProvider,
        ReasoningRequest,
    )

    configured_model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    provider = GeminiReasoningProvider(model_name=configured_model)

    assert provider.model_name == configured_model
    response = provider.generate(
        ReasoningRequest(prompt="Reply with exactly: ok", max_output_tokens=64)
    )

    assert response.text.strip() == "ok"


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BEDROCK_REASONING") != "1",
    reason="live Bedrock reasoning invocation is opt-in",
)
def test_live_bedrock_reasoning_provider():
    from hindsight.reasoning import BedrockReasoningProvider, ReasoningRequest

    response = BedrockReasoningProvider(
        region_name=os.environ.get("AWS_REGION", "us-east-1")
    ).generate(ReasoningRequest(prompt="Reply with exactly: ok", max_output_tokens=64))

    assert response.text.strip()
