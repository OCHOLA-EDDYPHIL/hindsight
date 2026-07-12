"""Unit tests for the Lambda incident endpoint."""

import json
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace

import pytest

AUTH_TOKEN = "test-token"


def _event(path: str, body: dict, *, method: str = "POST", token: str | None = AUTH_TOKEN):
    headers = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": headers,
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
        auth_token=AUTH_TOKEN,
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
        auth_token=AUTH_TOKEN,
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
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "user_input is required"


def test_handle_request_rejects_missing_fields_before_settings(monkeypatch):
    from hindsight.lambda_handler import handle_request

    monkeypatch.setattr(
        "hindsight.lambda_handler.runtime_settings",
        lambda: pytest.fail("runtime settings should not resolve for invalid payloads"),
    )

    response = handle_request(
        _event("/incident", {"incident_id": "incident-1"}),
        context=SimpleNamespace(),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 400


def test_handle_request_requires_bearer_token_before_settings(monkeypatch):
    from hindsight.lambda_handler import handle_request

    monkeypatch.setattr(
        "hindsight.lambda_handler.runtime_settings",
        lambda: pytest.fail("runtime settings should not resolve before auth"),
    )

    response = handle_request(
        _event("/incident", {"incident_id": "incident-1"}, token=None),
        context=SimpleNamespace(),
    )

    assert response["statusCode"] == 401


def test_handle_request_rejects_wrong_bearer_token_before_settings(monkeypatch):
    from hindsight.lambda_handler import handle_request

    monkeypatch.setattr(
        "hindsight.lambda_handler.runtime_settings",
        lambda: pytest.fail("runtime settings should not resolve before auth"),
    )

    response = handle_request(
        _event("/incident", {"incident_id": "incident-1"}, token="wrong"),
        context=SimpleNamespace(),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 403


def test_handle_request_rejects_get_before_settings(monkeypatch):
    from hindsight.lambda_handler import handle_request

    monkeypatch.setattr(
        "hindsight.lambda_handler.runtime_settings",
        lambda: pytest.fail("runtime settings should not resolve for rejected methods"),
    )

    response = handle_request(
        _event(
            "/incident",
            {"incident_id": "incident-1", "user_input": "payments latency"},
            method="GET",
        ),
        context=SimpleNamespace(),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 405


def test_handle_request_rejects_string_boolean_for_pause():
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    response = handle_request(
        _event(
            "/incident",
            {
                "incident_id": "incident-1",
                "user_input": "payments latency",
                "pause_before_act": "false",
            },
        ),
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "pause_before_act must be a JSON boolean"


def test_handle_request_accepts_boolean_false_for_pause(monkeypatch):
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    calls = []

    def fake_start(incident, **kwargs):
        calls.append(kwargs)
        return _result(thread_id=kwargs["thread_id"])

    monkeypatch.setattr("hindsight.lambda_handler.run_incident_agent", fake_start)

    response = handle_request(
        _event(
            "/incident",
            {
                "thread_id": "thread-1",
                "incident_id": "incident-1",
                "user_input": "payments latency",
                "pause_before_act": False,
            },
        ),
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 200
    assert calls[0]["pause_before_act"] is False


def test_handle_request_rejects_string_boolean_for_resume():
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    response = handle_request(
        _event("/incident/resume", {"thread_id": "thread-2", "approved": "false"}),
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "approved must be a JSON boolean"


def test_handle_request_rejects_invalid_base64_body():
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    event = _event("/incident", {})
    event["isBase64Encoded"] = True
    event["body"] = "not valid base64%%"

    response = handle_request(
        event,
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "request body must be valid base64"


def test_handle_request_rejects_oversized_user_input():
    from hindsight.lambda_handler import RuntimeSettings, handle_request

    response = handle_request(
        _event(
            "/incident",
            {
                "incident_id": "incident-1",
                "user_input": "x" * 8001,
            },
        ),
        context=SimpleNamespace(),
        settings=RuntimeSettings(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
        ),
        auth_token=AUTH_TOKEN,
    )

    assert response["statusCode"] == 400
    assert "user_input must be at most" in json.loads(response["body"])["error"]


def test_function_auth_token_reads_ssm_parameter():
    from hindsight.lambda_handler import FUNCTION_AUTH_TOKEN_PARAM_ENV, function_auth_token

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            assert Name == "/hindsight/test/function-token"
            assert WithDecryption is True
            return {"Parameter": {"Value": "secret-token"}}

    token = function_auth_token(
        environ={FUNCTION_AUTH_TOKEN_PARAM_ENV: "/hindsight/test/function-token"},
        ssm_client=FakeSsm(),
        use_cache=False,
    )

    assert token == "secret-token"


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


def test_runtime_settings_skips_gemini_secret_for_deterministic_provider():
    from hindsight.lambda_handler import (
        DATABASE_URL_PARAM_ENV,
        GEMINI_API_KEY_PARAM_ENV,
        runtime_settings,
    )

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            assert Name == "/hindsight/test/database-url"
            return {"Parameter": {"Value": "postgresql://db"}}

    settings = runtime_settings(
        environ={
            DATABASE_URL_PARAM_ENV: "/hindsight/test/database-url",
            GEMINI_API_KEY_PARAM_ENV: "/hindsight/test/missing-gemini-key",
            "LLM_PROVIDER": "deterministic",
        },
        ssm_client=FakeSsm(),
        use_cache=False,
    )

    assert settings.database_url == "postgresql://db"
    assert "GEMINI_API_KEY" not in settings.provider_env


def test_runtime_settings_adds_certifi_root_for_verify_full_database_url():
    import certifi

    from hindsight.lambda_handler import DATABASE_URL_PARAM_ENV, runtime_settings

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            return {
                "Parameter": {
                    "Value": (
                        "postgresql://user:pass@example.com:26257/db"
                        "?sslmode=verify-full"
                    )
                }
            }

    settings = runtime_settings(
        environ={
            DATABASE_URL_PARAM_ENV: "/hindsight/test/database-url",
            "LLM_PROVIDER": "deterministic",
        },
        ssm_client=FakeSsm(),
        use_cache=False,
    )

    assert "sslmode=verify-full" in settings.database_url
    query = parse_qs(urlsplit(settings.database_url).query)
    assert query["sslrootcert"] == [certifi.where()]


def test_safe_error_detail_redacts_connection_secrets():
    from hindsight.lambda_handler import _safe_error_detail

    detail = _safe_error_detail(
        RuntimeError(
            "failed postgresql://user:supersecret@example.com/db?sslmode=verify-full "
            "password=hidden token=opaque api_key=abc123"
        )
    )

    assert detail is not None
    assert "supersecret" not in detail
    assert "hidden" not in detail
    assert "opaque" not in detail
    assert "abc123" not in detail
    assert "postgresql://user:***@example.com/db" in detail
