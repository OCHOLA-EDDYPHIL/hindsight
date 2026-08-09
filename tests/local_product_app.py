"""Test-only OAuth/Gateway shell for local browser acceptance."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from hindsight.api import app as product_app

LOCAL_ACCESS_TOKEN = "local-browser-acceptance-access-token"
LOCAL_SUBJECT = "local-browser-acceptance-operator"
_PKCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AUTHORIZATION_CODES: dict[str, tuple[str, float]] = {}

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


@app.middleware("http")
async def simulate_gateway_verification(request: Request, call_next):
    if request.url.path.startswith("/v2/"):
        authorization = request.headers.get("authorization", "")
        if authorization == f"Bearer {LOCAL_ACCESS_TOKEN}":
            request.scope["aws.event"] = {
                "requestContext": {
                    "authorizer": {
                        "jwt": {
                            "claims": {
                                "iss": _required_env("HINDSIGHT_COGNITO_ISSUER"),
                                "client_id": _required_env("HINDSIGHT_COGNITO_CLIENT_ID"),
                                "token_use": "access",
                                "sub": LOCAL_SUBJECT,
                                "exp": str(int(time.time()) + 900),
                                "cognito:groups": "operator",
                            }
                        }
                    }
                }
            }
    return await call_next(request)


@app.get("/config.js", include_in_schema=False)
def runtime_config() -> PlainTextResponse:
    origin = _required_env("HINDSIGHT_LOCAL_PRODUCT_ORIGIN").rstrip("/")
    payload = {
        "publicApiBase": "/v1",
        "productApiBase": "/v2",
        "snapshotBase": None,
        "websocketUrl": None,
        "defaultNamespace": "demo:payments-poison-rewind",
        "pollIntervalMs": 1500,
        "operationPollSeconds": 600,
        "auth": {
            "hostedUiBaseUrl": origin,
            "clientId": _required_env("HINDSIGHT_COGNITO_CLIENT_ID"),
            "redirectUri": f"{origin}/",
            "logoutUri": f"{origin}/",
            "scopes": ["openid"],
        },
    }
    return PlainTextResponse(
        f"window.HINDSIGHT_CONFIG = {json.dumps(payload, separators=(',', ':'))};",
        media_type="application/javascript",
        headers={"cache-control": "no-store"},
    )


@app.get("/oauth2/authorize", include_in_schema=False)
def authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
) -> RedirectResponse:
    origin = _required_env("HINDSIGHT_LOCAL_PRODUCT_ORIGIN").rstrip("/")
    if (
        response_type != "code"
        or client_id != _required_env("HINDSIGHT_COGNITO_CLIENT_ID")
        or redirect_uri != f"{origin}/"
        or scope != "openid"
        or not state
        or code_challenge_method != "S256"
        or not _PKCE_PATTERN.fullmatch(code_challenge)
    ):
        return RedirectResponse(f"{origin}/?error=invalid_request&state={state}", status_code=302)
    code = secrets.token_urlsafe(32)
    _AUTHORIZATION_CODES[code] = (code_challenge, time.monotonic() + 60)
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state})}",
        status_code=302,
    )


@app.post("/oauth2/token", include_in_schema=False)
async def token(request: Request) -> JSONResponse:
    from urllib.parse import parse_qs

    try:
        form = parse_qs((await request.body()).decode(), strict_parsing=True)
        grant_type = form["grant_type"][0]
        client_id = form["client_id"][0]
        code = form["code"][0]
        redirect_uri = form["redirect_uri"][0]
        verifier = form["code_verifier"][0]
    except (KeyError, IndexError, UnicodeDecodeError, ValueError):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    origin = _required_env("HINDSIGHT_LOCAL_PRODUCT_ORIGIN").rstrip("/")
    authorization = _AUTHORIZATION_CODES.pop(code, None)
    challenge = _pkce_challenge(verifier)
    if (
        grant_type != "authorization_code"
        or client_id != _required_env("HINDSIGHT_COGNITO_CLIENT_ID")
        or redirect_uri != f"{origin}/"
        or authorization is None
        or authorization[1] <= time.monotonic()
        or not secrets.compare_digest(challenge, authorization[0])
    ):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return JSONResponse(
        {
            "access_token": LOCAL_ACCESS_TOKEN,
            "token_type": "Bearer",
            "expires_in": 900,
        }
    )


@app.get("/logout", include_in_schema=False)
def logout(client_id: str, logout_uri: str) -> RedirectResponse:
    origin = _required_env("HINDSIGHT_LOCAL_PRODUCT_ORIGIN").rstrip("/")
    if (
        client_id != _required_env("HINDSIGHT_COGNITO_CLIENT_ID")
        or logout_uri != f"{origin}/"
    ):
        return RedirectResponse(f"{origin}/", status_code=302)
    return RedirectResponse(logout_uri, status_code=302)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for local product acceptance")
    return value


app.mount("/", product_app)
