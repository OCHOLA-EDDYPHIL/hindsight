"""Local browser shell preserves the production PKCE and Gateway boundaries."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def test_local_shell_exchanges_each_pkce_code_once(monkeypatch):
    import tests.local_product_app as local

    monkeypatch.setenv("HINDSIGHT_LOCAL_PRODUCT_ORIGIN", "http://127.0.0.1:8766")
    monkeypatch.setenv("HINDSIGHT_COGNITO_CLIENT_ID", "local-browser-client")
    verifier = "a" * 64
    client = TestClient(local.app, follow_redirects=False)

    authorization = client.get(
        "/oauth2/authorize",
        params={
            "response_type": "code",
            "client_id": "local-browser-client",
            "redirect_uri": "http://127.0.0.1:8766/",
            "scope": "openid",
            "state": "opaque-state",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        },
    )
    callback = urlsplit(authorization.headers["location"])
    code = parse_qs(callback.query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "client_id": "local-browser-client",
        "redirect_uri": "http://127.0.0.1:8766/",
        "code": code,
        "code_verifier": verifier,
    }

    accepted = client.post("/oauth2/token", data=form)
    replay = client.post("/oauth2/token", data=form)

    assert accepted.status_code == 200
    assert accepted.json() == {
        "access_token": local.LOCAL_ACCESS_TOKEN,
        "token_type": "Bearer",
        "expires_in": 900,
    }
    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_grant"}


def test_local_runtime_config_exposes_no_test_token(monkeypatch):
    import tests.local_product_app as local

    monkeypatch.setenv("HINDSIGHT_LOCAL_PRODUCT_ORIGIN", "http://127.0.0.1:8766")
    monkeypatch.setenv("HINDSIGHT_COGNITO_CLIENT_ID", "local-browser-client")

    response = TestClient(local.app).get("/config.js")

    assert response.status_code == 200
    assert "local-browser-client" in response.text
    assert local.LOCAL_ACCESS_TOKEN not in response.text
    assert TestClient(local.app).get("/v1/health/live").status_code == 200
