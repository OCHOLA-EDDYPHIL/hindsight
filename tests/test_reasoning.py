"""Reasoning provider tests."""

import os
from types import SimpleNamespace

import pytest


def test_deterministic_reasoning_provider_returns_stable_response():
    from hindsight.reasoning import ReasoningRequest
    from tests.fakes import DeterministicReasoningProvider

    provider = DeterministicReasoningProvider(response_text="known answer")
    response = provider.generate(ReasoningRequest(prompt="what happened?"))

    assert response.text == "known answer"
    assert response.provider == "test_deterministic"
    assert response.model == "test-scripted-v1"
    assert response.usage["prompt_characters"] == len("what happened?")


def test_provider_from_env_defaults_to_gemini_and_requires_key():
    from hindsight.reasoning import ReasoningProviderError, reasoning_provider_from_env

    with pytest.raises(ReasoningProviderError, match="GEMINI_API_KEY"):
        reasoning_provider_from_env({})


def test_default_provider_constructs_gemini():
    from hindsight.reasoning import GeminiReasoningProvider, reasoning_provider_from_env

    provider = reasoning_provider_from_env({}, gemini_pool=object())

    assert isinstance(provider, GeminiReasoningProvider)


def test_provider_from_env_rejects_test_only_deterministic_mode():
    from hindsight.reasoning import ReasoningProviderError, reasoning_provider_from_env

    with pytest.raises(ReasoningProviderError, match="Unsupported LLM_PROVIDER"):
        reasoning_provider_from_env({"LLM_PROVIDER": "deterministic"})


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
    assert "response_mime_type" not in calls[0]["config"]
    assert "response_json_schema" not in calls[0]["config"]
    assert "thinking_config" not in calls[0]["config"]


def test_gemini_provider_requests_json_schema_when_supplied():
    from hindsight.reasoning import GeminiReasoningProvider, ReasoningRequest

    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='{"status":"ready"}', usage_metadata=None)

    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["ready"]}},
        "required": ["status"],
    }
    provider = GeminiReasoningProvider(
        model_name="gemini-test",
        client=SimpleNamespace(models=FakeModels()),
    )

    provider.generate(
        ReasoningRequest(
            prompt="Return status.",
            response_json_schema=schema,
            thinking_budget=0,
        )
    )

    assert calls[0]["config"]["response_mime_type"] == "application/json"
    assert calls[0]["config"]["response_json_schema"] == schema
    assert calls[0]["config"]["thinking_config"] == {"thinking_budget": 0}


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


def test_gemini_provider_preserves_pool_retry_after():
    from hindsight.gemini import GeminiPoolExhaustedError
    from hindsight.reasoning import (
        GeminiReasoningProvider,
        ReasoningProviderError,
        ReasoningRequest,
    )

    class CoolingPool:
        def execute(self, operation, *, routing_key):
            raise GeminiPoolExhaustedError(
                "all slots cooling down",
                retry_after_seconds=41,
            )

    provider = GeminiReasoningProvider(
        model_name="gemini-test",
        credential_pool=CoolingPool(),
    )

    with pytest.raises(ReasoningProviderError) as raised:
        provider.generate(ReasoningRequest(prompt="triage"))

    assert raised.value.retry_after_seconds == 41


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


def test_retrying_reasoning_provider_honors_provider_cooldown():
    from hindsight.reasoning import (
        ReasoningProviderError,
        ReasoningRequest,
        ReasoningResponse,
        retrying_reasoning_provider,
    )

    class CoolingProvider:
        provider_name = "gemini"
        model_name = "gemini-test"

        def __init__(self):
            self.calls = 0

        def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ReasoningProviderError("rate limited", retry_after_seconds=37)
            return ReasoningResponse(
                text="recovered",
                provider=self.provider_name,
                model=self.model_name,
            )

    provider = CoolingProvider()
    sleeps = []
    response = retrying_reasoning_provider(
        provider,
        max_attempts=2,
        sleeper=sleeps.append,
    ).generate(ReasoningRequest(prompt="recover"))

    assert provider.calls == 2
    assert sleeps == [37.0]
    assert response.usage["attempts"] == 2


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
