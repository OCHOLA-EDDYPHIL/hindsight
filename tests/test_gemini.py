"""Gemini credential-pool behavior."""

from types import SimpleNamespace

import pytest


class ProviderError(RuntimeError):
    def __init__(self, code, message="provider failure", *, headers=None):
        super().__init__(message)
        self.code = code
        self.response = SimpleNamespace(status_code=code, headers=headers or {})


def test_pool_fails_over_and_cools_only_failed_slot():
    from hindsight.gemini import (
        GeminiCredential,
        GeminiCredentialPool,
        InMemoryCooldownStore,
    )

    store = InMemoryCooldownStore()
    pool = GeminiCredentialPool(
        [GeminiCredential("slot-a", "key-a"), GeminiCredential("slot-b", "key-b")],
        cooldown_store=store,
        client_factory=lambda key: SimpleNamespace(key=key),
        clock=lambda: 1_000,
        jitter=lambda low, high: low,
    )
    calls = []

    def invoke(client):
        calls.append(client.key)
        if len(calls) == 1:
            raise ProviderError(429)
        return "ok"

    result = pool.execute(invoke, routing_key="run-1")

    assert result.value == "ok"
    assert result.attempts == 2
    states = store.get_states(["slot-a", "slot-b"])
    failed_slot = "slot-a" if calls[0] == "key-a" else "slot-b"
    assert states[failed_slot].cooldown_until > 1_000
    assert result.slot_id != failed_slot


def test_pool_honours_retry_after_and_reports_exhaustion_without_keys():
    from hindsight.gemini import (
        GeminiCredential,
        GeminiCredentialPool,
        GeminiPoolExhaustedError,
        InMemoryCooldownStore,
    )

    store = InMemoryCooldownStore()
    pool = GeminiCredentialPool(
        [GeminiCredential("slot-a", "sensitive-key")],
        cooldown_store=store,
        client_factory=lambda key: object(),
        clock=lambda: 2_000,
        jitter=lambda low, high: low,
    )

    with pytest.raises(GeminiPoolExhaustedError) as raised:
        pool.execute(
            lambda client: (_ for _ in ()).throw(
                ProviderError(429, headers={"Retry-After": "60"})
            ),
            routing_key="run-2",
        )

    assert raised.value.retry_after_seconds == 60
    assert "sensitive-key" not in str(raised.value)


def test_pool_honours_google_retry_info_details():
    from hindsight.gemini import (
        GeminiCredential,
        GeminiCredentialPool,
        GeminiPoolExhaustedError,
    )

    error = ProviderError(429)
    error.details = {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "22.361672167s",
                }
            ]
        }
    }
    pool = GeminiCredentialPool(
        [GeminiCredential("slot-a", "sensitive-key")],
        client_factory=lambda key: object(),
        clock=lambda: 2_000,
    )

    with pytest.raises(GeminiPoolExhaustedError) as raised:
        pool.execute(
            lambda client: (_ for _ in ()).throw(error),
            routing_key="run-structured-retry",
        )

    assert raised.value.retry_after_seconds == 23


def test_pool_does_not_rotate_invalid_requests():
    from hindsight.gemini import GeminiCredential, GeminiCredentialPool

    calls = []
    pool = GeminiCredentialPool(
        [GeminiCredential("slot-a", "key-a"), GeminiCredential("slot-b", "key-b")],
        client_factory=lambda key: SimpleNamespace(key=key),
    )

    with pytest.raises(ProviderError):
        pool.execute(
            lambda client: calls.append(client.key) or (_ for _ in ()).throw(ProviderError(400)),
            routing_key="invalid",
        )

    assert len(calls) == 1


def test_authentication_failure_quarantines_slot_and_fails_over():
    from hindsight.gemini import (
        GeminiCredential,
        GeminiCredentialPool,
        InMemoryCooldownStore,
    )

    store = InMemoryCooldownStore()
    attempts = 0

    def invoke(client):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError(403)
        return client.key

    pool = GeminiCredentialPool(
        [GeminiCredential("slot-a", "key-a"), GeminiCredential("slot-b", "key-b")],
        cooldown_store=store,
        client_factory=lambda key: SimpleNamespace(key=key),
        clock=lambda: 5_000,
    )
    result = pool.execute(invoke, routing_key="auth")

    assert attempts == 2
    assert result.value in {"key-a", "key-b"}
    states = store.get_states(["slot-a", "slot-b"])
    assert len(states) == 1
    assert next(iter(states.values())).cooldown_until == 8_600


def test_key_document_parsing_supports_versioned_and_local_keys():
    from hindsight.gemini import parse_gemini_credentials

    document = '{"version":1,"keys":[{"id":"project-a","api_key":"key-a"}]}'
    configured = parse_gemini_credentials({"GEMINI_API_KEYS": document})
    local = parse_gemini_credentials(
        {"GEMINI_API_KEY": "key-a", "GEMINI_API_KEY_1": "key-b"}
    )

    assert [(item.slot_id, item.api_key) for item in configured] == [("project-a", "key-a")]
    assert [item.slot_id for item in local] == ["gemini-1", "gemini-2"]


def test_dynamodb_store_reads_and_updates_low_level_items():
    from hindsight.gemini import DynamoDbCooldownStore

    class FakeClient:
        def __init__(self):
            self.update = None
            self.delete = None

        def batch_get_item(self, **kwargs):
            return {
                "Responses": {
                    "health": [
                        {
                            "slot_id": {"S": "slot-a"},
                            "cooldown_until": {"N": "90"},
                            "failure_count": {"N": "2"},
                        }
                    ]
                }
            }

        def update_item(self, **kwargs):
            self.update = kwargs

        def delete_item(self, **kwargs):
            self.delete = kwargs

    client = FakeClient()
    store = DynamoDbCooldownStore(table_name="health", client=client)

    assert store.get_states(["slot-a"])["slot-a"].failure_count == 2
    store.record_failure(
        "slot-a", cooldown_until=120, error_code="rate_limit", now=60
    )
    store.record_success("slot-a", operation_started_at=121)

    assert client.update["TableName"] == "health"
    assert client.update["ExpressionAttributeValues"][":code"] == {"S": "rate_limit"}
    assert client.delete["ExpressionAttributeValues"][":started"] == {"N": "121"}


def test_runtime_settings_loads_versioned_pool_from_ssm():
    from hindsight.runtime import (
        DATABASE_URL_PARAM_ENV,
        GEMINI_API_KEYS_PARAM_ENV,
        runtime_settings,
    )

    document = '{"version":1,"keys":[{"id":"project-a","api_key":"key-a"}]}'

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            assert WithDecryption is True
            values = {
                "/hindsight/test/database-url": "postgresql://db",
                "/hindsight/test/gemini-api-keys": document,
            }
            return {"Parameter": {"Value": values[Name]}}

    settings = runtime_settings(
        environ={
            DATABASE_URL_PARAM_ENV: "/hindsight/test/database-url",
            GEMINI_API_KEYS_PARAM_ENV: "/hindsight/test/gemini-api-keys",
            "LLM_PROVIDER": "gemini",
            "EMBEDDING_PROVIDER": "gemini",
        },
        ssm_client=FakeSsm(),
        use_cache=False,
    )

    assert settings.provider_env["GEMINI_API_KEYS"] == document
    assert settings.provider_env["EMBEDDING_PROVIDER"] == "gemini"


def test_runtime_database_url_does_not_resolve_provider_secrets():
    from hindsight.runtime import (
        DATABASE_URL_PARAM_ENV,
        GEMINI_API_KEYS_PARAM_ENV,
        runtime_database_url,
    )

    requested = []

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            requested.append(Name)
            assert WithDecryption is True
            if Name != "/hindsight/test/database-url":
                raise AssertionError("provider secrets must be deferred until after claim")
            return {"Parameter": {"Value": "postgresql://db"}}

    result = runtime_database_url(
        environ={
            DATABASE_URL_PARAM_ENV: "/hindsight/test/database-url",
            GEMINI_API_KEYS_PARAM_ENV: "/hindsight/test/gemini-api-keys",
            "EMBEDDING_PROVIDER": "gemini",
        },
        ssm_client=FakeSsm(),
    )

    assert result == "postgresql://db"
    assert requested == ["/hindsight/test/database-url"]


def test_cooldown_registry_failure_does_not_block_model_call():
    from hindsight.gemini import (
        FailOpenCooldownStore,
        GeminiCredential,
        GeminiCredentialPool,
    )

    class BrokenStore:
        def get_states(self, slot_ids):
            raise RuntimeError("dynamodb unavailable")

        def record_failure(self, *args, **kwargs):
            raise RuntimeError("dynamodb unavailable")

        def record_success(self, *args, **kwargs):
            raise RuntimeError("dynamodb unavailable")

    pool = GeminiCredentialPool(
        [GeminiCredential("slot-a", "key-a")],
        cooldown_store=FailOpenCooldownStore(BrokenStore()),
        client_factory=lambda key: object(),
    )

    assert pool.execute(lambda client: "ok", routing_key="run").value == "ok"
