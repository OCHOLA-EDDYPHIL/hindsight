"""Unit tests for the Lambda incident endpoint."""

import json
from types import SimpleNamespace

import pytest


def _event(path: str, body: dict):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": "POST", "path": path}},
        "body": json.dumps(body),
    }


def _result(*, thread_id="thread-1", interrupted=False):
    from hindsight.agent import IncidentAgentResult

    return IncidentAgentResult(
        thread_id=thread_id,
        interrupted=interrupted,
        interrupt={"proposed_action": "approve"} if interrupted else None,
        state={
            "reasoning": {
                "provider": "deterministic",
                "model": "deterministic-v1",
                "usage": {"prompt_characters": 12, "attempts": 1},
            }
        },
        plan="verify and mitigate",
        proposed_action="review mitigation",
        reflected_memory_id="memory-1",
    )


def test_handle_request_starts_incident(monkeypatch):
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    calls = []

    def fake_start(incident, **kwargs):
        calls.append((incident, kwargs))
        return _result(thread_id=kwargs["thread_id"])

    monkeypatch.setattr("hindsight.lambda_handler.run_incident_agent", fake_start)

    response = handle_request(
        _event(
            "/incident",
            {
                "thread_id": "thread-1",
                "incident_id": "incident-1",
                "user_input": "payments latency",
                "service_slug": "payments-api",
            },
        ),
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
    )

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["thread_id"] == "thread-1"
    assert body["provider"] == "deterministic"
    assert calls[0][0].incident_id == "incident-1"
    assert calls[0][1]["db_url"] == "postgresql://db"


def test_handle_request_resumes_incident(monkeypatch):
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    calls = []

    def fake_resume(**kwargs):
        calls.append(kwargs)
        return _result(thread_id=kwargs["thread_id"])

    monkeypatch.setattr("hindsight.lambda_handler.resume_incident_agent", fake_resume)

    response = handle_request(
        _event("/incident/resume", {"thread_id": "thread-2", "approved": False}),
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
    )

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["thread_id"] == "thread-2"
    assert calls[0]["approved"] is False


def test_handle_request_rejects_bad_start_request():
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    response = handle_request(
        _event("/incident", {"incident_id": "incident-1"}),
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "user_input is required"


def test_runtime_settings_reads_secrets_from_ssm():
    from hindsight.lambda_handler import (
        DATABASE_URL_PARAM_ENV,
        GEMINI_API_KEY_PARAM_ENV,
        runtime_settings,
    )

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            assert WithDecryption is True
            values = {
                "/hindsight/test/database-url": "postgresql://db",
                "/hindsight/test/gemini-key": "secret-key",
            }
            return {"Parameter": {"Value": values[Name]}}

    settings = runtime_settings(
        environ={
            DATABASE_URL_PARAM_ENV: "/hindsight/test/database-url",
            GEMINI_API_KEY_PARAM_ENV: "/hindsight/test/gemini-key",
            "LLM_PROVIDER": "gemini",
            "GEMINI_MODEL": "gemini-test",
            "REASONING_MAX_ATTEMPTS": "3",
        },
        ssm_client=FakeSsm(),
        use_cache=False,
    )

    assert settings.database_url == "postgresql://db"
    assert settings.provider_env["GEMINI_API_KEY"] == "secret-key"
    assert settings.provider_env["GEMINI_MODEL"] == "gemini-test"
    assert settings.reasoning_max_attempts == 3


def test_runtime_settings_requires_ssm_parameter_in_lambda():
    from hindsight.lambda_handler import runtime_settings

    with pytest.raises(RuntimeError, match="HINDSIGHT_DATABASE_URL_PARAM"):
        runtime_settings(
            environ={"AWS_LAMBDA_FUNCTION_NAME": "hindsight-agent"},
            ssm_client=object(),
            use_cache=False,
        )


def test_runtime_settings_uses_local_fallbacks_without_ssm_client():
    from hindsight.lambda_handler import runtime_settings

    settings = runtime_settings(
        environ={
            "DATABASE_URL": "postgresql://local",
            "GEMINI_API_KEY": "local-key",
            "LLM_PROVIDER": "gemini",
        },
        use_cache=False,
    )

    assert settings.database_url == "postgresql://local"
    assert settings.provider_env["GEMINI_API_KEY"] == "local-key"
