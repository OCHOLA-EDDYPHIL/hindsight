"""Product API contract and operator-boundary tests."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request


def _operator_request(
    *, origin: str, host: str = "api.example.test", forwarded_host: str | None = None
) -> Request:
    headers = [(b"host", host.encode()), (b"origin", origin.encode())]
    if forwarded_host is not None:
        headers.append((b"x-forwarded-host", forwarded_host.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/demo/poison-rewind/reset",
            "raw_path": b"/v1/demo/poison-rewind/reset",
            "query_string": b"",
            "headers": headers,
            "server": (host, 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_openapi_exposes_narrow_product_contract():
    from hindsight.api import app

    schema = app.openapi()
    paths = schema["paths"]

    assert "/v1/incidents" in paths
    assert "/v1/incidents/{slug}/runs" in paths
    assert "/v2/incidents/{slug}/resolution" in paths
    assert "/v1/runs/{run_id}/approval" in paths
    assert "/v1/decisions/{decision_id}/influence" in paths
    assert "/v1/signature-scenarios" in paths
    assert "/v1/signature-scenarios/{scenario_id}" in paths
    assert "/v1/namespaces/{namespace}/rewinds/preview" in paths
    assert "/v1/demo/poison-rewind/poison" in paths


def test_public_health_and_guarded_mutation(monkeypatch):
    from hindsight.api import app

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    client = TestClient(app)

    assert client.get("/v1/health/live").json() == {
        "status": "live",
        "revision": "unknown",
    }
    denied = client.post(
        "/v1/incidents",
        json={
            "slug": "checkout-latency",
            "title": "Checkout latency",
            "severity": "sev2",
            "summary": "p99 is above SLO",
        },
    )
    denied_reset = client.post(
        "/v1/demo/poison-rewind/reset",
        json={"namespace": "demo:payments-poison-rewind"},
    )
    denied_poison = client.post(
        "/v1/demo/poison-rewind/poison",
        json={"namespace": "demo:payments-poison-rewind:session:untrusted"},
    )

    assert denied.status_code == 403
    assert denied_reset.status_code == 403
    assert denied_poison.status_code == 403


def test_server_binds_v1_and_v2_without_tenant_request_selectors(monkeypatch):
    import hindsight.api as api
    from hindsight.realtime_ticket import verify_realtime_ticket
    from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, PUBLIC_DEMO_TENANT_ID
    from hindsight.tenant import current_tenant_id

    calls = []
    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "protected-secret")
    monkeypatch.setenv("HINDSIGHT_DEPLOYED_REVISION", "a" * 40)
    monkeypatch.setattr(api, "_api_database_url", lambda: "postgresql://resolved/database")

    def incidents(**kwargs):
        calls.append((current_tenant_id(required=True), kwargs))
        if kwargs["limit"] == 2:
            return [
                {
                    "id": "00000000-0000-0000-0000-000000000010",
                    "started_at": "2026-07-19T00:00:00+00:00",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000009",
                    "started_at": "2026-07-18T00:00:00+00:00",
                },
            ]
        return []

    monkeypatch.setattr(api, "list_incidents", incidents)

    def incident(*, slug, db_url):
        calls.append((current_tenant_id(required=True), {"slug": slug, "db_url": db_url}))
        if current_tenant_id(required=True) == ACCEPTANCE_TENANT_ID:
            return {"slug": slug}
        return None

    created = []

    def create(**kwargs):
        created.append((current_tenant_id(required=True), kwargs))
        return {"slug": kwargs["slug"]}

    monkeypatch.setattr(api, "get_incident", incident)
    monkeypatch.setattr(api, "create_incident", create)
    client = TestClient(api.app)

    public = client.get(
        "/v1/incidents?limit=1&tenant_id=00000000-0000-0000-0000-000000000003",
        headers={"X-Tenant-Id": ACCEPTANCE_TENANT_ID},
    )
    assert public.status_code == 200
    assert calls[0][0] == PUBLIC_DEMO_TENANT_ID
    hidden = client.get(
        "/v1/incidents/hidden",
        headers={"X-Tenant-Id": ACCEPTANCE_TENANT_ID},
    )
    assert hidden.status_code == 404

    public_create = client.post(
        "/v1/incidents",
        headers={
            "Authorization": "Bearer protected-secret",
            "X-Tenant-Id": ACCEPTANCE_TENANT_ID,
        },
        json={
            "slug": "public-incident",
            "title": "Public incident",
            "severity": "sev3",
            "summary": "Public tenant write",
            "tenant_id": ACCEPTANCE_TENANT_ID,
        },
    )
    assert public_create.status_code == 201
    assert created[-1][0] == PUBLIC_DEMO_TENANT_ID
    assert "tenant_id" not in created[-1][1]

    assert client.get("/v2/incidents").status_code == 401
    assert (
        client.get("/v2/incidents", headers={"Authorization": "Bearer invalid"}).status_code == 401
    )
    protected = client.get(
        "/v2/incidents?limit=1",
        headers={
            "Authorization": "Bearer protected-secret",
            "X-Tenant-Id": PUBLIC_DEMO_TENANT_ID,
        },
    )
    assert protected.status_code == 200
    next_cursor = protected.json()["next_cursor"]
    assert next_cursor
    assert calls[-1][0] == ACCEPTANCE_TENANT_ID
    assert calls[-1][1] == {
        "limit": 2,
        "before_started_at": None,
        "before_id": None,
        "db_url": "postgresql://resolved/database",
    }
    protected_hidden = client.get(
        "/v2/incidents/hidden",
        headers={"Authorization": "Bearer protected-secret"},
    )
    assert protected_hidden.status_code == 200
    assert calls[-1][0] == ACCEPTANCE_TENANT_ID
    assert (
        client.get(
            f"/v2/incidents?limit=1&cursor={next_cursor}x",
            headers={"Authorization": "Bearer protected-secret"},
        ).status_code
        == 422
    )

    public_ticket = client.post("/v1/realtime/ticket").json()["ticket"]
    protected_ticket = client.post(
        "/v2/realtime/ticket",
        headers={"Authorization": "Bearer protected-secret"},
    ).json()["ticket"]
    assert verify_realtime_ticket(public_ticket, secret="protected-secret") == PUBLIC_DEMO_TENANT_ID
    assert (
        verify_realtime_ticket(protected_ticket, secret="protected-secret") == ACCEPTANCE_TENANT_ID
    )
    health = client.get(
        "/v2/health/ready",
        headers={"Authorization": "Bearer protected-secret"},
    )
    assert health.status_code in {200, 503}

    monkeypatch.setattr(
        api,
        "_v2_identity",
        lambda _request: api.V2Identity(
            tenant_id=ACCEPTANCE_TENANT_ID,
            scopes=frozenset({"read"}),
        ),
    )
    forbidden = client.post(
        "/v2/incidents",
        headers={"Authorization": "Bearer protected-secret"},
        json={
            "slug": "forbidden-write",
            "title": "Forbidden write",
            "severity": "sev3",
            "summary": "Read-only credential",
        },
    )
    assert forbidden.status_code == 403


def test_operator_session_sets_httponly_cookie(monkeypatch):
    from hindsight.api import app

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setenv("HINDSIGHT_SECURE_COOKIES", "0")
    client = TestClient(app)

    response = client.post(
        "/v1/operator/session",
        json={"token": "operator-secret"},
    )

    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/v1/operator/session").json() == {"operator": True}


def test_operator_cookie_accepts_configured_product_origin(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setenv("HINDSIGHT_ALLOWED_ORIGINS", "https://product.example.test")
    monkeypatch.setattr(api, "_api_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        api,
        "create_incident",
        lambda **values: {"id": "incident-1", **values},
    )
    client = TestClient(api.app, base_url="https://api.example.test")

    unlocked = client.post(
        "/v1/operator/session",
        json={"token": "operator-secret"},
    )
    created = client.post(
        "/v1/incidents",
        headers={"Origin": "https://product.example.test"},
        json={
            "slug": "proxy-session",
            "title": "Proxy session",
            "severity": "sev3",
            "summary": "Validate the configured product origin.",
        },
    )

    assert unlocked.status_code == 200
    assert created.status_code == 201
    assert created.json()["slug"] == "proxy-session"


def test_operator_cookie_accepts_direct_same_origin(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.delenv("HINDSIGHT_ALLOWED_ORIGINS", raising=False)

    api._operator_required_impl(
        _operator_request(
            origin="https://api.example.test",
            host="api.example.test",
        ),
        session=api._signed_session("operator-secret"),
    )


def test_operator_cookie_rejects_foreign_origin_and_forwarded_host(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setenv("HINDSIGHT_ALLOWED_ORIGINS", "https://product.example.test")

    with pytest.raises(HTTPException) as raised:
        api._operator_required_impl(
            _operator_request(
                origin="https://foreign.example.test",
                forwarded_host="foreign.example.test",
            ),
            session=api._signed_session("operator-secret"),
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "cross-origin operator request denied"


def test_operator_bearer_authorization_does_not_require_origin_match(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")

    api._operator_required_impl(
        _operator_request(origin="https://foreign.example.test"),
        authorization="Bearer operator-secret",
        session=api._signed_session("operator-secret"),
    )


def test_run_creation_returns_accepted_and_dispatches_durable_command(monkeypatch):
    import hindsight.api as api

    settings = SimpleNamespace(
        database_url="postgresql://resolved/database",
        provider_env={},
    )
    database_calls = []
    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setattr(api, "runtime_database_url", lambda: settings.database_url)
    monkeypatch.setattr(
        api,
        "get_incident",
        lambda **kwargs: (
            database_calls.append(("get_incident", kwargs))
            or {"slug": kwargs["slug"], "service_slug": "payments-api"}
        ),
    )
    monkeypatch.setattr(
        api,
        "create_run",
        lambda **kwargs: (
            database_calls.append(("create_run", kwargs))
            or (
                {"id": "run-1", "status": "queued", "service_slug": kwargs["service_slug"]},
                True,
            )
        ),
    )
    dispatches = []
    monkeypatch.setattr(
        api,
        "dispatch_run_commands",
        lambda **kwargs: (
            dispatches.append(kwargs)
            or {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
        ),
    )
    client = TestClient(api.app)

    response = client.post(
        "/v1/incidents/checkout-latency/runs",
        headers={
            "Authorization": "Bearer operator-secret",
            "Idempotency-Key": "request-1",
        },
        json={
            "namespace": "demo:payments",
            "user_input": "checkout p99 is above SLO",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "status": "queued", "created": True}
    assert dispatches == [
        {
            "db_url": settings.database_url,
            "run_id": "run-1",
            "command": "start",
            "limit": 1,
        }
    ]
    assert database_calls[0] == (
        "get_incident",
        {"slug": "checkout-latency", "db_url": settings.database_url},
    )
    assert database_calls[1][0] == "create_run"
    assert database_calls[1][1]["db_url"] == settings.database_url


def test_run_creation_remains_accepted_when_immediate_dispatch_fails(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        api,
        "get_incident",
        lambda **_kwargs: {"slug": "checkout-latency", "service_slug": "payments-api"},
    )
    monkeypatch.setattr(
        api,
        "create_run",
        lambda **_kwargs: ({"id": "run-pending", "status": "queued"}, True),
    )
    monkeypatch.setattr(
        api,
        "dispatch_run_commands",
        lambda **_kwargs: {"leased": 1, "dispatched": 0, "failed": 1, "lease_lost": 0},
    )
    client = TestClient(api.app)

    response = client.post(
        "/v1/incidents/checkout-latency/runs",
        headers={"Authorization": "Bearer operator-secret"},
        json={"namespace": "demo:payments", "user_input": "checkout p99 is above SLO"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": "run-pending",
        "status": "queued",
        "created": True,
    }


def test_idempotent_run_request_retries_its_pending_dispatch(monkeypatch):
    import hindsight.api as api

    dispatches = []
    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        api,
        "get_incident",
        lambda **_kwargs: {"slug": "checkout-latency", "service_slug": "payments-api"},
    )
    monkeypatch.setattr(
        api,
        "create_run",
        lambda **_kwargs: ({"id": "run-existing", "status": "queued"}, False),
    )
    monkeypatch.setattr(
        api,
        "dispatch_run_commands",
        lambda **kwargs: (
            dispatches.append(kwargs)
            or {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0}
        ),
    )

    accepted = api.runs_create(
        "checkout-latency",
        api.RunCreate(namespace="demo:payments", user_input="checkout p99 is above SLO"),
        idempotency_key="request-1",
    )

    assert accepted == api.AcceptedRun(run_id="run-existing", status="queued", created=False)
    assert dispatches == [
        {
            "db_url": "postgresql://resolved/database",
            "run_id": "run-existing",
            "command": "start",
            "limit": 1,
        }
    ]


def test_run_creation_rejects_mismatched_idempotency_key_without_dispatch(monkeypatch):
    import hindsight.api as api

    dispatches = []
    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        api,
        "get_incident",
        lambda **_kwargs: {"slug": "checkout-latency", "service_slug": "payments-api"},
    )
    monkeypatch.setattr(
        api,
        "create_run",
        lambda **_kwargs: (_ for _ in ()).throw(
            api.RunIdempotencyConflictError("different request")
        ),
    )
    monkeypatch.setattr(
        api,
        "dispatch_run_commands",
        lambda **kwargs: dispatches.append(kwargs),
    )

    with pytest.raises(HTTPException) as raised:
        api.runs_create(
            "checkout-latency",
            api.RunCreate(
                namespace="demo:payments",
                user_input="a different request body",
            ),
            idempotency_key="request-1",
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "idempotency key is already bound to a different request"
    assert dispatches == []


def test_approval_remains_accepted_when_immediate_dispatch_fails(monkeypatch):
    import hindsight.api as api

    transitions = []
    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        api,
        "prepare_approval",
        lambda **kwargs: transitions.append(kwargs) or {"id": kwargs["run_id"]},
    )
    monkeypatch.setattr(
        api,
        "dispatch_run_commands",
        lambda **_kwargs: {"leased": 1, "dispatched": 0, "failed": 1, "lease_lost": 0},
    )
    client = TestClient(api.app)

    response = client.post(
        "/v1/runs/run-pending/approval",
        headers={"Authorization": "Bearer operator-secret"},
        json={
            "approved": True,
            "recommendation_id": f"recommendation:{'a' * 64}",
            "selection_fingerprint": "b" * 64,
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": "run-pending",
        "status": "resuming",
        "approved": True,
        "recommendation_id": f"recommendation:{'a' * 64}",
        "selection_fingerprint": "b" * 64,
    }
    assert transitions == [
        {
            "run_id": "run-pending",
            "approved": True,
            "recommendation_id": f"recommendation:{'a' * 64}",
            "selection_fingerprint": "b" * 64,
            "db_url": "postgresql://resolved/database",
        }
    ]


def test_rewind_execute_rejects_stale_preview(monkeypatch):
    import hindsight.api as api

    settings = SimpleNamespace(
        database_url="postgresql://resolved/database",
        provider_env={},
    )
    calls = []
    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setattr(api, "runtime_database_url", lambda: settings.database_url)
    monkeypatch.setattr(
        api,
        "enqueue_operation",
        lambda **kwargs: (
            calls.append(kwargs)
            or (_ for _ in ()).throw(api.OperationConflictError("stale preview"))
        ),
    )
    client = TestClient(api.app)

    response = client.post(
        "/v1/namespaces/demo:payments/rewinds",
        headers={
            "Authorization": "Bearer operator-secret",
            "Idempotency-Key": "rewind-test",
        },
        json={
            "preview_id": "preview-1",
            "fingerprint": "b" * 64,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "stale preview"
    assert calls[0]["db_url"] == settings.database_url


def test_db_backed_routes_share_the_resolved_runtime_database(monkeypatch):
    import hindsight.api as api

    settings = SimpleNamespace(
        database_url="postgresql://resolved/database",
        provider_env={},
    )
    calls = []
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(api, "runtime_database_url", lambda: settings.database_url)
    monkeypatch.setattr(
        api,
        "runtime_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("provider secret is unavailable")),
    )
    monkeypatch.setattr(
        api,
        "list_incidents",
        lambda **kwargs: calls.append(("incidents", kwargs)) or [],
    )
    monkeypatch.setattr(
        api,
        "get_run",
        lambda **kwargs: calls.append(("run", kwargs)) or {"id": kwargs["run_id"]},
    )
    monkeypatch.setattr(
        api,
        "memory_snapshot",
        lambda **kwargs: calls.append(("dashboard", kwargs)) or {"memories": []},
    )
    monkeypatch.setattr(
        api,
        "get_operation",
        lambda **kwargs: calls.append(("operation", kwargs)) or {"id": kwargs["operation_id"]},
    )
    client = TestClient(api.app)

    assert client.get("/v1/incidents?limit=1").status_code == 200
    assert client.get("/v1/runs/run-1").status_code == 200
    assert client.get("/v1/namespaces/demo:payments/beliefs").status_code == 200
    assert client.get("/v1/memory/operations/operation-1").status_code == 200

    assert calls == [
        ("incidents", {"limit": 1, "db_url": settings.database_url}),
        ("run", {"run_id": "run-1", "db_url": settings.database_url}),
        (
            "dashboard",
            {
                "namespace": "demo:payments",
                "as_of": None,
                "db_url": settings.database_url,
                "limit": 100,
            },
        ),
        (
            "operation",
            {"operation_id": "operation-1", "db_url": settings.database_url},
        ),
    ]


def test_runtime_database_resolution_failure_does_not_reach_db_helpers(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("DATABASE_URL", "postgresql://fallback/should-not-be-used")
    monkeypatch.setattr(
        api,
        "runtime_database_url",
        lambda: (_ for _ in ()).throw(RuntimeError("configured secret is unavailable")),
    )
    monkeypatch.setattr(
        api,
        "list_incidents",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("DB helper was called")),
    )
    client = TestClient(api.app)

    response = client.get("/v1/incidents")

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime configuration is unavailable"}


def test_request_validation_precedes_database_resolution(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setattr(
        api,
        "runtime_database_url",
        lambda: (_ for _ in ()).throw(AssertionError("database resolution was attempted")),
    )
    client = TestClient(api.app)

    invalid_limit = client.get("/v1/namespaces/demo:payments/beliefs?limit=0")
    missing_idempotency_key = client.post(
        "/v1/memory/operations",
        headers={"Authorization": "Bearer operator-secret"},
        json={"preview_id": "preview-1", "fingerprint": "b" * 64},
    )

    assert invalid_limit.status_code == 422
    assert missing_idempotency_key.status_code == 422


def test_demo_writes_use_runtime_database_and_embedding_provider(monkeypatch):
    import hindsight.api as api
    from uuid import UUID

    settings = SimpleNamespace(
        database_url="postgresql://hosted/database",
        provider_env={"EMBEDDING_PROVIDER": "gemini"},
    )
    provider = object()
    fixture_id = UUID("12345678-1234-5678-1234-567812345678")
    calls = []
    monkeypatch.setattr(api, "uuid4", lambda: fixture_id)
    monkeypatch.setattr(api, "runtime_settings", lambda: settings)
    monkeypatch.setattr(
        api,
        "embedding_provider_from_env",
        lambda provider_env: calls.append(("provider", provider_env)) or provider,
    )
    monkeypatch.setattr(
        api,
        "reset_poison_rewind_state",
        lambda **kwargs: calls.append(("reset", kwargs)) or "demo:session:hosted",
    )
    monkeypatch.setattr(
        api,
        "ensure_poison_rewind_incident",
        lambda **kwargs: calls.append(("incident", kwargs)) or {"id": "incident-1"},
    )
    monkeypatch.setattr(
        api,
        "seed_good_demo_memory",
        lambda **kwargs: calls.append(("seed", kwargs)) or {"id": "seed-1"},
    )
    monkeypatch.setattr(
        api,
        "current_database_timestamp",
        lambda **kwargs: calls.append(("anchor", kwargs)) or "2026-07-17T12:00:00.123456+00:00",
    )
    monkeypatch.setattr(
        api,
        "poison_demo_memory",
        lambda **kwargs: calls.append(("poison", kwargs)) or {"id": "poison-1"},
    )

    reset = api.demo_reset(api.DemoResetRequest(namespace="demo:hosted"))
    poison = api.demo_poison(api.DemoPoisonRequest(namespace=reset["namespace"]))

    assert reset["seed_memory"] == {"id": "seed-1"}
    assert reset["rewind_anchor"] == "2026-07-17T12:00:00.123456+00:00"
    assert poison == {"id": "poison-1"}
    assert calls == [
        ("provider", settings.provider_env),
        (
            "reset",
            {
                "namespace": "demo:hosted",
                "session_id": fixture_id,
                "db_url": settings.database_url,
            },
        ),
        (
            "incident",
            {"fixture_id": fixture_id, "db_url": settings.database_url},
        ),
        (
            "seed",
            {
                "namespace": "demo:session:hosted",
                "db_url": settings.database_url,
                "embedding_provider": provider,
            },
        ),
        ("anchor", {"db_url": settings.database_url}),
        ("provider", settings.provider_env),
        (
            "poison",
            {
                "namespace": "demo:session:hosted",
                "db_url": settings.database_url,
                "embedding_provider": provider,
            },
        ),
    ]
