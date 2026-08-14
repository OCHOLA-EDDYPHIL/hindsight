"""Product API contract and Cognito-backed authorization-boundary tests."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


def _product_identity(*, role: str = "operator"):
    from hindsight.identity import ProductIdentity, ROLE_SCOPES
    from hindsight.server_tenants import ACCEPTANCE_TENANT_ID

    return ProductIdentity(
        principal_id="00000000-0000-0000-0000-000000000099",
        tenant_id=ACCEPTANCE_TENANT_ID,
        tenant_slug="acceptance",
        token_role=role,
        mapped_role=role,
        effective_role=role,
        scopes=ROLE_SCOPES[role],
        expires_at=2_000_000_000,
    )


def test_openapi_exposes_narrow_product_contract():
    from hindsight.api import app

    schema = app.openapi()
    paths = schema["paths"]

    assert set(paths["/v1/incidents"]) == {"get"}
    assert set(paths["/v1/realtime/ticket"]) == {"post"}
    assert "/v1/operator/session" not in paths
    assert "/v1/incidents/{slug}/runs" not in paths
    assert "post" in paths["/v2/incidents/{slug}/runs"]
    assert "post" in paths["/v2/incidents/{slug}/resolution"]
    assert "post" in paths["/v2/runs/{run_id}/approval"]
    assert "/v1/decisions/{decision_id}/influence" in paths
    assert "/v1/signature-scenarios" in paths
    assert "/v1/signature-scenarios/{scenario_id}" in paths
    assert "/v1/signature-scenarios/{scenario_id}/evidence" in paths
    assert "/v2/signature-scenarios/{scenario_id}/evidence" in paths
    assert "/v1/namespaces/{namespace}/rewinds/preview" not in paths
    assert "post" in paths["/v2/namespaces/{namespace}/rewinds/preview"]
    assert "/v1/demo/poison-rewind/poison" not in paths
    assert "post" in paths["/v2/demo/poison-rewind/poison"]
    assert "get" in paths["/v2/memory/consolidation-candidates"]
    assert "get" in paths["/v2/memory/consolidation-candidates/{candidate_id}"]
    assert "post" in paths["/v2/memory/consolidation-candidates/{candidate_id}/review-preview"]
    assert "get" in paths["/v2/me"]


def test_causal_evidence_download_is_digest_bound_and_attachment_safe(monkeypatch):
    import hindsight.api as api
    from hindsight.causal_evidence import canonical_json_bytes, canonical_sha256

    scenario_id = "49109a44-43e7-40de-b547-b4f9d0a387a2"
    monkeypatch.setattr(api, "_api_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        api,
        "signature_scenario_evidence",
        lambda **_kwargs: {
            "schema_version": 1,
            "canonicalization": "hindsight.canonical-json.v1",
            "scenario": {"scenario_id": scenario_id},
        },
    )

    response = TestClient(api.app).get(f"/v1/signature-scenarios/{scenario_id}/evidence")

    assert response.status_code == 200
    document = {
        "schema_version": 1,
        "canonicalization": "hindsight.canonical-json.v1",
        "scenario": {"scenario_id": scenario_id},
    }
    assert response.headers["x-hindsight-evidence-sha256"] == canonical_sha256(document)
    assert response.headers["content-disposition"] == (
        f'attachment; filename="hindsight-causal-evidence-{scenario_id}.json"'
    )
    assert response.content == canonical_json_bytes(document)
    assert response.json() == document


def test_consolidation_review_preview_binds_authenticated_operator(monkeypatch):
    import hindsight.api as api

    captured = []
    identity = _product_identity()
    monkeypatch.setattr(api, "_v2_identity", lambda _request: identity)
    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(
        api,
        "preview_consolidation_review",
        lambda **kwargs: (
            captured.append(kwargs)
            or {
                "id": "preview-1",
                "operation_type": "consolidation_approval",
                "fingerprint": "a" * 64,
            }
        ),
    )

    response = TestClient(api.app).post(
        "/v2/memory/consolidation-candidates/candidate-1/review-preview",
        json={"action": "approve", "reason": "Evidence reviewed"},
    )

    assert response.status_code == 200
    assert response.json()["operation_type"] == "consolidation_approval"
    assert captured == [
        {
            "candidate_id": "candidate-1",
            "action": "approve",
            "actor": identity.actor,
            "reason": "Evidence reviewed",
            "db_url": "postgresql://resolved/database",
        }
    ]


def test_public_surface_has_no_mutation_or_operator_session():
    from hindsight.api import app

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

    assert denied.status_code == 405
    assert denied_reset.status_code in {404, 405}
    assert denied_poison.status_code in {404, 405}
    assert client.get("/v1/operator/session").status_code in {404, 405}


def test_server_binds_public_and_product_tenants_without_request_selectors(monkeypatch):
    import hindsight.api as api
    from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, PUBLIC_DEMO_TENANT_ID
    from hindsight.tenant import current_tenant_id

    calls = []
    monkeypatch.setenv("HINDSIGHT_DEPLOYED_REVISION", "a" * 40)
    monkeypatch.setattr(api, "_api_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())

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

    monkeypatch.setattr(api, "get_incident", incident)
    client = TestClient(api.app)

    public = client.get("/v1/incidents?limit=1")
    assert public.status_code == 200
    assert calls[0][0] == PUBLIC_DEMO_TENANT_ID
    hidden = client.get("/v1/incidents/hidden")
    assert hidden.status_code == 404

    protected = client.get("/v2/incidents?limit=1")
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
    )
    assert protected_hidden.status_code == 200
    assert calls[-1][0] == ACCEPTANCE_TENANT_ID
    assert (
        client.get(
            f"/v2/incidents?limit=1&cursor={next_cursor}x",
        ).status_code
        == 422
    )

    monkeypatch.setattr(
        api,
        "_v2_identity",
        lambda _request: _product_identity(role="viewer"),
    )
    forbidden = client.post(
        "/v2/incidents",
        json={
            "slug": "forbidden-write",
            "title": "Forbidden write",
            "severity": "sev3",
            "summary": "Read-only credential",
        },
    )
    assert forbidden.status_code == 403


def test_tenant_selectors_and_tenant_body_fields_fail_closed(monkeypatch):
    import hindsight.api as api
    from hindsight.server_tenants import ACCEPTANCE_TENANT_ID

    monkeypatch.setattr(api, "_api_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
    monkeypatch.setattr(api, "create_incident", lambda **_values: pytest.fail("write reached"))
    client = TestClient(api.app)

    assert (
        client.get(
            "/v1/incidents",
            headers={"X-Tenant-Id": ACCEPTANCE_TENANT_ID},
        ).status_code
        == 400
    )
    assert (
        client.get(
            f"/v2/incidents?tenant_id={ACCEPTANCE_TENANT_ID}",
        ).status_code
        == 400
    )
    body_selector = client.post(
        "/v2/incidents",
        json={
            "slug": "body-selector",
            "title": "Body selector",
            "severity": "sev3",
            "summary": "The tenant must be server-derived.",
            "tenant_id": ACCEPTANCE_TENANT_ID,
        },
    )
    assert body_selector.status_code == 422


def test_bearer_header_cannot_replace_gateway_verified_claims(monkeypatch):
    import hindsight.api as api

    monkeypatch.setenv("HINDSIGHT_COGNITO_ISSUER", "https://issuer.example.test/pool")
    monkeypatch.setenv("HINDSIGHT_COGNITO_CLIENT_ID", "client-id")
    monkeypatch.setattr(api, "_api_database_url", lambda: "postgresql://resolved/database")
    response = TestClient(api.app).get(
        "/v2/me",
        headers={"Authorization": "Bearer legacy-shared-secret"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "verified product identity is required"}


def test_v2_identity_configuration_failure_is_a_structured_503(monkeypatch):
    import hindsight.api as api

    def unavailable_database_url():
        raise RuntimeError("parameter lookup failed")

    monkeypatch.setattr(api, "runtime_database_url", unavailable_database_url)
    response = TestClient(api.app, raise_server_exceptions=False).get("/v2/me")

    assert response.status_code == 503
    assert response.json() == {"detail": "product identity service is unavailable"}


def test_v2_cors_preflight_does_not_require_product_identity(monkeypatch):
    import hindsight.api as api

    monkeypatch.setattr(
        api,
        "_v2_identity",
        lambda _request: pytest.fail("preflight reached product identity resolution"),
    )
    response = TestClient(api.app).options(
        "/v2/incidents",
        headers={
            "Origin": "https://product.example.test",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code != 401


def test_me_and_realtime_tickets_use_effective_identity(monkeypatch):
    import hindsight.api as api
    from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, PUBLIC_DEMO_TENANT_ID

    issued = []
    monkeypatch.setattr(api, "_api_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
    monkeypatch.setattr(api.time, "time", lambda: 1_900_000_000)
    monkeypatch.setattr(
        api,
        "issue_realtime_ticket",
        lambda **kwargs: issued.append(kwargs) or f"ticket-{len(issued)}",
    )
    client = TestClient(api.app)

    me = client.get("/v2/me")
    public_ticket = client.post("/v1/realtime/ticket")
    protected_ticket = client.post("/v2/realtime/ticket")

    assert me.status_code == 200
    assert me.json()["effective_role"] == "operator"
    assert me.json()["scopes"] == ["read", "realtime", "write"]
    assert public_ticket.json() == {"ticket": "ticket-1", "expires_in": 60}
    assert protected_ticket.json() == {"ticket": "ticket-2", "expires_in": 60}
    assert issued == [
        {
            "tenant_id": PUBLIC_DEMO_TENANT_ID,
            "access_class": "public",
            "session_expires_at": 1_900_003_600,
            "now": 1_900_000_000,
        },
        {
            "tenant_id": ACCEPTANCE_TENANT_ID,
            "access_class": "operator",
            "principal_id": "00000000-0000-0000-0000-000000000099",
            "session_expires_at": 2_000_000_000,
        },
    ]


def test_realtime_ticket_endpoint_reports_a_retired_tenant(monkeypatch):
    import hindsight.api as api
    from hindsight.realtime_ticket import RealtimeTenantRetiredError

    def retired(**kwargs):
        del kwargs
        raise RealtimeTenantRetiredError("tenant realtime access is retired")

    monkeypatch.setattr(api, "issue_realtime_ticket", retired)

    response = TestClient(api.app).post("/v1/realtime/ticket")

    assert response.status_code == 410
    assert response.json() == {"detail": "tenant realtime access is retired"}


def test_run_creation_returns_accepted_and_dispatches_durable_command(monkeypatch):
    import hindsight.api as api

    settings = SimpleNamespace(
        database_url="postgresql://resolved/database",
        provider_env={},
    )
    database_calls = []
    monkeypatch.setattr(api, "runtime_database_url", lambda: settings.database_url)
    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
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
        "/v2/incidents/checkout-latency/runs",
        headers={
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

    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
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
        "/v2/incidents/checkout-latency/runs",
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
    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
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
        "/v2/runs/run-pending/approval",
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


def test_remediation_approval_injects_authenticated_actor_and_all_bindings(monkeypatch):
    import hindsight.api as api

    transitions = []
    identity = _product_identity()
    monkeypatch.setattr(api, "runtime_database_url", lambda: "postgresql://resolved/database")
    monkeypatch.setattr(api, "_v2_identity", lambda _request: identity)
    monkeypatch.setattr(
        api,
        "prepare_approval",
        lambda **kwargs: transitions.append(kwargs) or {"id": kwargs["run_id"]},
    )
    monkeypatch.setattr(
        api,
        "dispatch_run_commands",
        lambda **_kwargs: {"leased": 1, "dispatched": 1, "failed": 0, "lease_lost": 0},
    )
    client = TestClient(api.app)
    payload = {
        "approved": True,
        "selection_fingerprint": "a" * 64,
        "remediation_action_id": f"remediation_action:{'b' * 64}",
        "observation_fingerprint": "c" * 64,
        "preview_id": "preview-1",
        "preview_fingerprint": "d" * 64,
    }

    response = client.post("/v2/runs/run-action/approval", json=payload)

    assert response.status_code == 202
    assert response.json() == {
        "run_id": "run-action",
        "status": "resuming",
        **payload,
    }
    assert transitions == [
        {
            "run_id": "run-action",
            "approved": True,
            "recommendation_id": None,
            "selection_fingerprint": "a" * 64,
            "remediation_action_id": f"remediation_action:{'b' * 64}",
            "observation_fingerprint": "c" * 64,
            "preview_id": "preview-1",
            "preview_fingerprint": "d" * 64,
            "approval_actor": identity.actor,
            "db_url": "postgresql://resolved/database",
        }
    ]


def test_approval_contract_rejects_mixed_recommendation_and_remediation_identity(monkeypatch):
    import hindsight.api as api

    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
    client = TestClient(api.app)
    response = client.post(
        "/v2/runs/run-action/approval",
        json={
            "approved": True,
            "recommendation_id": f"recommendation:{'a' * 64}",
            "selection_fingerprint": "b" * 64,
            "remediation_action_id": f"remediation_action:{'c' * 64}",
            "observation_fingerprint": "d" * 64,
            "preview_id": "preview-1",
            "preview_fingerprint": "e" * 64,
        },
    )

    assert response.status_code == 422


def test_rewind_execute_rejects_stale_preview(monkeypatch):
    import hindsight.api as api

    settings = SimpleNamespace(
        database_url="postgresql://resolved/database",
        provider_env={},
    )
    calls = []
    monkeypatch.setattr(api, "runtime_database_url", lambda: settings.database_url)
    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
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
        "/v2/namespaces/demo:payments/rewinds",
        headers={
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

    monkeypatch.setattr(api, "_v2_identity", lambda _request: _product_identity())
    monkeypatch.setattr(
        api,
        "runtime_database_url",
        lambda: (_ for _ in ()).throw(AssertionError("database resolution was attempted")),
    )
    client = TestClient(api.app)

    invalid_limit = client.get("/v1/namespaces/demo:payments/beliefs?limit=0")
    missing_idempotency_key = client.post(
        "/v2/memory/operations",
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
        "record_poison_rewind_anchor",
        lambda **kwargs: calls.append(("anchor", kwargs)) or "2026-07-17T12:00:00.123456+00:00",
    )
    monkeypatch.setattr(
        api,
        "poison_demo_memory",
        lambda **kwargs: calls.append(("poison", kwargs)) or {"id": "poison-1"},
    )

    reset = api.demo_reset(api.DemoResetRequest(namespace="demo:hosted"))
    poison = api.demo_poison(api.DemoPoisonRequest(namespace=reset["namespace"]))

    assert reset["scenario_id"] == str(fixture_id)
    assert reset["seed_memory"] == {"id": "seed-1"}
    assert reset["rewind_anchor"] == "2026-07-17T12:00:00.123456+00:00"
    assert poison == {"id": "poison-1"}
    assert calls == [
        ("provider", settings.provider_env),
        (
            "incident",
            {"fixture_id": fixture_id, "db_url": settings.database_url},
        ),
        (
            "reset",
            {
                "namespace": "demo:hosted",
                "session_id": fixture_id,
                "incident_id": fixture_id,
                "db_url": settings.database_url,
            },
        ),
        (
            "seed",
            {
                "namespace": "demo:session:hosted",
                "db_url": settings.database_url,
                "embedding_provider": provider,
            },
        ),
        (
            "anchor",
            {
                "namespace": "demo:session:hosted",
                "db_url": settings.database_url,
            },
        ),
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
