"""Product API contract and operator-boundary tests."""

from fastapi.testclient import TestClient


def test_openapi_exposes_narrow_product_contract():
    from hindsight.api import app

    schema = app.openapi()
    paths = schema["paths"]

    assert "/v1/incidents" in paths
    assert "/v1/incidents/{slug}/runs" in paths
    assert "/v1/runs/{run_id}/approval" in paths
    assert "/v1/decisions/{decision_id}/influence" in paths
    assert "/v1/namespaces/{namespace}/rewinds/preview" in paths
    assert "/v1/demo/poison-rewind/poison" in paths


def test_public_health_and_guarded_mutation(monkeypatch):
    from hindsight.api import app

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    client = TestClient(app)

    assert client.get("/v1/health/live").json() == {"status": "live"}
    denied = client.post(
        "/v1/incidents",
        json={
            "slug": "checkout-latency",
            "title": "Checkout latency",
            "severity": "sev2",
            "summary": "p99 is above SLO",
        },
    )

    assert denied.status_code == 403


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


def test_run_creation_returns_accepted_and_enqueues_once(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setattr(
        api,
        "get_incident",
        lambda **kwargs: {"slug": kwargs["slug"], "service_slug": "payments-api"},
    )
    monkeypatch.setattr(
        api,
        "create_run",
        lambda **kwargs: (
            {"id": "run-1", "status": "queued", "service_slug": kwargs["service_slug"]},
            True,
        ),
    )
    messages = []
    monkeypatch.setattr(api, "enqueue_run", lambda message: messages.append(message) or "message-1")
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
    assert messages == [{"command": "start", "run_id": "run-1"}]


def test_rewind_execute_rejects_stale_preview(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "operator-secret")
    monkeypatch.setattr(
        api,
        "enqueue_operation",
        lambda **kwargs: (_ for _ in ()).throw(api.OperationConflictError("stale preview")),
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
