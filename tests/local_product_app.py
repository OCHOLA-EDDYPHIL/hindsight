"""Test-only OAuth/Gateway shell for local browser acceptance."""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from hindsight.api import app as product_app

LOCAL_ACCESS_TOKEN = "local-browser-acceptance-access-token"
LOCAL_SUBJECT = "local-browser-acceptance-operator"
_PKCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AUTHORIZATION_CODES: dict[str, tuple[str, float]] = {}
_CAUSAL_EVIDENCE_STATES = {
    "changed",
    "unchanged",
    "unavailable",
    "mismatched",
    "corrected-only",
}

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


@app.get(
    "/v1/signature-scenarios/browser-fixture-{state}",
    include_in_schema=False,
)
def causal_evidence_browser_fixture_get(state: str) -> dict:
    try:
        return causal_evidence_browser_fixture(state)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def causal_evidence_browser_fixture(state: str) -> dict:
    """Return one deterministic public scenario for browser-only evidence rendering."""

    if state not in _CAUSAL_EVIDENCE_STATES:
        raise ValueError("unknown causal evidence browser fixture")

    namespace = f"browser-fixture:causal-evidence:{state}"
    before_envelope_sha = f"sha256:{'1' * 64}"
    after_envelope_sha = f"sha256:{'2' * 64}"
    compromised_memory_sha = f"sha256:{'3' * 64}"
    restored_memory_sha = f"sha256:{'4' * 64}"
    compromised_fragment_sha = f"sha256:{'5' * 64}"
    restored_fragment_sha = f"sha256:{'6' * 64}"
    observation = {
        "id": "observation-controlled",
        "tool_call_id": "diagnostic-controlled",
        "schema_version": 1,
        "tool": "aws_cloudwatch_diagnostics",
        "query_key": "payments.retry_fanout",
        "query_fingerprint": f"cloudwatch_query:{'b' * 64}",
        "region": "us-east-1",
        "metric": {
            "namespace": "Hindsight/ControlledIncidentTelemetry",
            "name": "RetryFanout",
            "dimensions": [{"name": "Service", "value": "payments-api"}],
            "statistic": "Maximum",
            "unit": "Count",
            "period_seconds": 60,
        },
        "window": {
            "start": "2026-07-17T10:15:00Z",
            "end": "2026-07-17T10:30:00Z",
            "seconds": 900,
        },
        "datapoints": [
            {"timestamp": "2026-07-17T10:28:00Z", "value": 7},
            {"timestamp": "2026-07-17T10:29:00Z", "value": 8},
        ],
        "datapoint_count": 2,
        "truncated": False,
    }
    operation_effects = [
        {
            "sequence": 1,
            "effect_type": "closed",
            "source_memory_id": "memory-compromised",
            "result_memory_id": None,
            "belief_id": "belief-payments-guidance",
            "namespace": namespace,
        },
        {
            "sequence": 2,
            "effect_type": "reasserted",
            "source_memory_id": "memory-baseline",
            "result_memory_id": "memory-restored",
            "belief_id": "belief-payments-guidance",
            "namespace": namespace,
        },
    ]

    def envelope(*, after_correction: bool) -> dict:
        memory_id = "memory-restored" if after_correction else "memory-compromised"
        memory_sha = restored_memory_sha if after_correction else compromised_memory_sha
        fragment_sha = (
            restored_fragment_sha if after_correction else compromised_fragment_sha
        )
        return {
            "schema_version": 4,
            "canonicalization": "hindsight.canonical-json.v1",
            "identity": {
                "scenario_id": f"browser-fixture-{state}",
                "namespace": namespace,
                "replay_anchor": "2026-07-17T10:30:00Z",
                "scenario_routing_key": "signature:browser-fixture",
                "release_revision": "a" * 40,
            },
            "invariant_inputs": {
                "ordered_observations": [deepcopy(observation)],
                "release_revision": "a" * 40,
            },
            "invariant_inputs_sha256": f"sha256:{'7' * 64}",
            "permitted_intervention": {
                "kind": "governed_memory_version_selection.v1",
                "ordered_memory_versions": [
                    {
                        "ordinal": 1,
                        "memory": {
                            "memory_id": memory_id,
                            "belief_id": "belief-payments-guidance",
                            "version": 3 if after_correction else 2,
                        },
                        "memory_sha256": memory_sha,
                        "prompt_fragment_sha256": fragment_sha,
                    }
                ],
                "selection_fingerprint": (
                    f"selection:{'8' * 64}"
                    if after_correction
                    else f"selection:{'9' * 64}"
                ),
                "expected_changed_prompt_fragments": [fragment_sha],
                "correction_operation_id": (
                    "operation-rewind" if after_correction else None
                ),
                "correction_target_timestamp": (
                    "2026-07-17T10:30:00Z" if after_correction else None
                ),
                "operation_effects": (
                    deepcopy(operation_effects) if after_correction else []
                ),
                "invalidated_memory_fingerprints": (
                    [f"sha256:{'c' * 64}"] if after_correction else []
                ),
                "restored_memory_fingerprints": (
                    [f"sha256:{'d' * 64}"] if after_correction else []
                ),
            },
            "rendered_prompt_sha256": [fragment_sha],
            "envelope_sha256": (
                after_envelope_sha if after_correction else before_envelope_sha
            ),
        }

    def action(action_id: str) -> dict:
        directive = {
            "scale_workers": "Scale payment workers.",
            "throttle_retries": "Throttle retry fanout.",
        }[action_id]
        return {
            "catalog_id": "payments_retry_amplification.actions.v1",
            "contract": "payments_retry_amplification.v1",
            "action_id": action_id,
            "disposition": "recommend",
            "parameters": {},
            "primary_action": action_id,
            "directive": directive,
            "consistency_status": "consistent",
            "fingerprint": f"operational_action:{action_id}",
        }

    before_action = action("scale_workers")
    after_action = action("throttle_retries")

    def run(*, after_correction: bool) -> dict:
        selected_action = after_action if after_correction else before_action
        memory_id = "memory-restored" if after_correction else "memory-compromised"
        return {
            "id": "run-corrected" if after_correction else "run-rejected",
            "status": "completed" if after_correction else "rejected",
            "decision_id": (
                "decision-corrected" if after_correction else "decision-rejected"
            ),
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "action_approved": after_correction,
            "user_input": "Inspect elevated retry fanout with governed memory.",
            "proposed_action": selected_action["directive"],
            "trace": {
                "reads": [
                    {
                        "id": "read-corrected" if after_correction else "read-compromised",
                        "memory_id": memory_id,
                        "belief_id": "belief-payments-guidance",
                        "version_number": 3 if after_correction else 2,
                        "writer": "demo.seed" if after_correction else "demo.fixture-import",
                        "source_ref": "browser-fixture:governed-memory",
                        "justification": "Recorded governed-memory input.",
                    }
                ]
            },
            "action_trace": {
                "schema_version": 4,
                "mode": "recommendation_only",
                "selection": {
                    "fingerprint": (
                        f"selection:{'8' * 64}"
                        if after_correction
                        else f"selection:{'9' * 64}"
                    ),
                    "provider": "gemini",
                    "model": "gemini-2.5-flash",
                },
                "recommendation": {
                    "id": (
                        f"recommendation:{'a' * 64}"
                        if after_correction
                        else f"recommendation:{'f' * 64}"
                    ),
                    "summary": selected_action["directive"],
                    "status": "awaiting_approval",
                    "operational_action": deepcopy(selected_action),
                },
                "execution": {
                    "status": (
                        "recommendation_approved"
                        if after_correction
                        else "not_executed"
                    ),
                    "mode": "recommendation_only",
                },
                "tool_calls": [
                    {
                        "id": "diagnostic-controlled",
                        "tool": "aws_cloudwatch_diagnostics",
                        "query_key": "payments.retry_fanout",
                        "status": "completed",
                    }
                ],
                "observations": [deepcopy(observation)],
                "observation_fingerprint": f"telemetry:{'e' * 64}",
                "causal_envelope": envelope(after_correction=after_correction),
            },
        }

    scenario = {
        "scenario_id": f"browser-fixture-{state}",
        "namespace": namespace,
        "status": "completed",
        "session_status": "active",
        "rewind_anchor": "2026-07-17T10:30:00Z",
        "completed_at": "2026-07-17T11:30:00Z",
        "incident": {
            "slug": f"browser-fixture-causal-evidence-{state}",
            "title": "Controlled retry-fanout evidence",
            "summary": "A deterministic browser fixture for causal evidence rendering.",
            "severity": "SEV-1",
            "service_slug": "payments-api",
        },
        "runs": [run(after_correction=False), run(after_correction=True)],
        "operation": {
            "id": "operation-rewind",
            "operation_type": "rewind",
            "status": "completed",
            "target_timestamp": "2026-07-17T10:30:00Z",
            "invalidated_memory_ids": ["memory-compromised"],
            "restored_memory_ids": ["memory-restored"],
            "effects": deepcopy(operation_effects),
        },
        "operation_effects": deepcopy(operation_effects),
        "memories": [
            {
                "id": "memory-compromised",
                "belief_id": "belief-payments-guidance",
                "version_number": 2,
                "content": "Scale workers while retry fanout remains elevated.",
                "writer": "demo.fixture-import",
                "source_ref": "browser-fixture:governed-memory",
                "status": "invalidated",
                "t_invalid": "2026-07-17T10:30:00Z",
            },
            {
                "id": "memory-restored",
                "belief_id": "belief-payments-guidance",
                "version_number": 3,
                "content": "Throttle retry fanout before scaling workers.",
                "writer": "demo.seed",
                "source_ref": "browser-fixture:governed-memory",
                "status": "current",
            },
        ],
        "action_comparison": {
            "status": "changed",
            "contract": "payments_retry_amplification.v1",
            "before": {"decision_id": "decision-rejected", **before_action},
            "after": {"decision_id": "decision-corrected", **after_action},
            "context": {
                "prompt_equal": True,
                "normalized_telemetry_equal": True,
            },
            "memory_correction_proven": True,
            "controlled_pair": True,
        },
        "causal_evidence": {
            "schema_version": 1,
            "canonicalization": "hindsight.canonical-json.v1",
            "scope": "recommendation_only",
            "proof_states": {
                "memory_correction_proven": {
                    "status": "proven",
                    "reason": "rewind_lineage_and_reads_verified",
                },
                "action_delta_proven": {
                    "status": "proven",
                    "reason": "catalog_action_changed",
                },
                "controlled_pair_eligible": {
                    "status": "proven",
                    "reason": "fixed_context_and_memory_delta_verified",
                },
                "repeatable_causal_effect_supported": {
                    "status": "unavailable",
                    "reason": "repeated_trials_not_measured",
                },
                "service_recovery_proven": {
                    "status": "unavailable",
                    "reason": "service_recovery_not_measured",
                },
            },
            "controlled_pair_checks": [
                {
                    "field": "invariant_inputs.ordered_observations",
                    "status": "matched",
                    "reason": "invariant_inputs_ordered_observations_matched",
                },
                {
                    "field": "permitted_intervention.ordered_memory_versions",
                    "status": "matched",
                    "reason": "declared_memory_intervention_delta_verified",
                },
                {
                    "field": "permitted_intervention.correction_operation",
                    "status": "matched",
                    "reason": "correction_operation_delta_verified",
                },
            ],
            "before_envelope_sha256": before_envelope_sha,
            "after_envelope_sha256": after_envelope_sha,
        },
        "stages": {
            "baseline_memory_id": "memory-baseline",
            "compromised_memory_id": "memory-compromised",
            "influenced_decision_id": "decision-rejected",
            "rewind_operation_id": "operation-rewind",
            "corrected_decision_id": "decision-corrected",
        },
    }

    if state == "unchanged":
        unchanged_action = deepcopy(before_action)
        scenario["action_comparison"].update(
            {
                "status": "unchanged",
                "after": {"decision_id": "decision-corrected", **unchanged_action},
                "controlled_pair": False,
            }
        )
        scenario["causal_evidence"]["proof_states"]["action_delta_proven"] = {
            "status": "not_proven",
            "reason": "catalog_action_unchanged",
        }
        corrected = scenario["runs"][1]
        corrected["proposed_action"] = unchanged_action["directive"]
        corrected["action_trace"]["recommendation"].update(
            {
                "summary": unchanged_action["directive"],
                "operational_action": unchanged_action,
            }
        )
    elif state == "unavailable":
        scenario["runs"][1]["action_trace"].pop("causal_envelope")
        scenario["action_comparison"].update(
            {"status": "unavailable", "contract": None, "controlled_pair": False}
        )
        scenario["causal_evidence"]["proof_states"]["controlled_pair_eligible"] = {
            "status": "unavailable",
            "reason": "causal_envelope_incomplete_or_invalid",
        }
        scenario["causal_evidence"]["controlled_pair_checks"] = [
            {
                "field": "causal_envelope",
                "status": "unavailable",
                "reason": "causal_envelope_incomplete_or_invalid",
            }
        ]
    elif state == "mismatched":
        scenario["causal_evidence"]["proof_states"]["controlled_pair_eligible"] = {
            "status": "not_proven",
            "reason": "invariant_inputs_ordered_observations_mismatch",
        }
        scenario["causal_evidence"]["controlled_pair_checks"] = [
            {
                "field": "invariant_inputs.ordered_observations",
                "status": "mismatched",
                "reason": "invariant_inputs_ordered_observations_mismatch",
            }
        ]
    elif state == "corrected-only":
        scenario["runs"] = [scenario["runs"][1]]
        scenario["stages"]["influenced_decision_id"] = None
        scenario["action_comparison"].update(
            {
                "status": "unavailable",
                "contract": None,
                "before": None,
                "controlled_pair": False,
            }
        )
        scenario["causal_evidence"]["before_envelope_sha256"] = None
        scenario["causal_evidence"]["proof_states"]["controlled_pair_eligible"] = {
            "status": "unavailable",
            "reason": "causal_envelope_incomplete_or_invalid",
        }
        scenario["causal_evidence"]["controlled_pair_checks"] = [
            {
                "field": "causal_envelope",
                "status": "unavailable",
                "reason": "causal_envelope_incomplete_or_invalid",
            }
        ]
    return scenario


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for local product acceptance")
    return value


app.mount("/", product_app)
