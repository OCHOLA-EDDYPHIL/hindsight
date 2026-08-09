"""Versioned FastAPI surface for the Hindsight product UI."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mangum import Mangum
from psycopg import errors as psycopg_errors
from pydantic import BaseModel, ConfigDict, Field

from hindsight.db import connect
from hindsight.demo_state import (
    DEMO_NAMESPACE,
    current_database_timestamp,
    ensure_poison_rewind_incident,
    poison_demo_memory,
    reset_poison_rewind_state,
    seed_good_demo_memory,
)
from hindsight.embeddings import embedding_provider_from_env
from hindsight.identity import (
    IdentityForbidden,
    IdentityUnauthenticated,
    IdentityUnavailable,
    ProductIdentity,
    resolve_product_identity,
)
from hindsight.memory import MemoryStore
from hindsight.operations import (
    OperationAuthorizationError,
    OperationConflictError,
    enqueue_operation,
    get_operation,
    preview_retraction,
    preview_review_resolution,
    preview_rewind,
    preview_supersession,
)
from hindsight.queueing import RunQueueUnavailableError, enqueue_run
from hindsight.run_dispatch import dispatch_run_commands
from hindsight.runtime import (
    RuntimeSettings,
    runtime_database_url,
    runtime_settings,
)
from hindsight.realtime_ticket import issue_realtime_ticket
from hindsight.server_tenants import public_demo_tenant_id
from hindsight.snapshots import memory_snapshot
from hindsight.tenant import current_tenant_id, tenant_scope
from hindsight.runs import (
    RunConflictError,
    RunIdempotencyConflictError,
    RunNotFoundError,
    create_incident,
    create_run,
    get_incident,
    get_run,
    list_incidents,
    prepare_approval,
    resolve_incident,
)
from hindsight.trace_contract import (
    decision_influence,
    signature_scenario_trace,
)

API_PREFIX = "/v1"
V2_PREFIX = "/v2"
PUBLIC_REALTIME_SESSION_SECONDS = 60 * 60
TENANT_SELECTOR_HEADERS = frozenset(
    {"x-tenant-id", "x-hindsight-tenant", "x-hindsight-tenant-id"}
)
TENANT_SELECTOR_QUERY_KEYS = frozenset({"tenant", "tenant_id"})


def _normalize_origin(value: str) -> str | None:
    parsed = urlparse(value.strip())
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _configured_allowed_origins() -> set[str]:
    return {
        normalized
        for value in os.environ.get("HINDSIGHT_ALLOWED_ORIGINS", "").split(",")
        if (normalized := _normalize_origin(value)) is not None
    }


app = FastAPI(
    title="Hindsight product API",
    summary="Incident runs and inspectable, rewindable agent memory",
    version="1.0.0",
    openapi_url=f"{API_PREFIX}/openapi.json",
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(_configured_allowed_origins()),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["authorization", "content-type", "idempotency-key"],
)


@app.middleware("http")
async def bind_server_tenant(request: Request, call_next):
    """Bind product routes without accepting a tenant selector."""

    if request.method == "OPTIONS" and request.url.path.startswith(
        (f"{API_PREFIX}/", f"{V2_PREFIX}/")
    ):
        return await call_next(request)
    if request.url.path.startswith((f"{API_PREFIX}/", f"{V2_PREFIX}/")):
        if _request_has_tenant_selector(request):
            return JSONResponse(
                status_code=400,
                content={"detail": "tenant selectors are not accepted"},
            )
    if request.url.path.startswith(f"{API_PREFIX}/"):
        with tenant_scope(public_demo_tenant_id()):
            return await call_next(request)
    if request.url.path.startswith(f"{V2_PREFIX}/"):
        try:
            identity = _v2_identity(request)
        except IdentityUnauthenticated:
            return JSONResponse(
                status_code=401,
                content={"detail": "verified product identity is required"},
            )
        except IdentityForbidden:
            return JSONResponse(
                status_code=403,
                content={"detail": "product authorization is unavailable"},
            )
        except IdentityUnavailable:
            return JSONResponse(
                status_code=503,
                content={"detail": "product identity service is unavailable"},
            )
        request.state.v2_identity = identity
        request.state.v2_scopes = identity.scopes
        with tenant_scope(identity.tenant_id):
            return await call_next(request)
    return await call_next(request)


def _request_has_tenant_selector(request: Request) -> bool:
    if TENANT_SELECTOR_HEADERS.intersection(request.headers):
        return True
    return any(key.lower() in TENANT_SELECTOR_QUERY_KEYS for key in request.query_params)


def _v2_identity(request: Request) -> ProductIdentity:
    try:
        db_url = _api_database_url()
    except HTTPException as exc:
        raise IdentityUnavailable("product identity service is unavailable") from exc
    return resolve_product_identity(
        request.scope,
        db_url=db_url,
    )


def _v2_scope(scope: str):
    def required(request: Request) -> None:
        if scope not in getattr(request.state, "v2_scopes", frozenset()):
            raise HTTPException(status_code=403, detail="credential scope is insufficient")

    return required


def _current_identity(request: Request) -> ProductIdentity:
    identity = getattr(request.state, "v2_identity", None)
    if not isinstance(identity, ProductIdentity):
        raise HTTPException(status_code=401, detail="verified product identity is required")
    return identity


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IncidentCreate(StrictRequestModel):

    slug: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9:-]*$")
    title: str = Field(min_length=1, max_length=300)
    severity: Literal["sev1", "sev2", "sev3"]
    summary: str = Field(min_length=1, max_length=10_000)
    service_slug: str | None = Field(default=None, max_length=200)


class RunCreate(StrictRequestModel):

    user_input: str = Field(min_length=1, max_length=20_000)
    namespace: str = Field(min_length=1, max_length=500)
    thread_id: str | None = Field(default=None, max_length=500)
    retrieval_policy: Literal["semantic_strict", "semantic_then_keyword"] = "semantic_strict"


class ApprovalRequest(StrictRequestModel):
    approved: bool
    recommendation_id: str = Field(
        min_length=79,
        max_length=79,
        pattern=r"^recommendation:[a-f0-9]{64}$",
    )
    selection_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class IncidentResolutionRequest(StrictRequestModel):
    root_cause: str = Field(min_length=1, max_length=10_000)
    action: str = Field(min_length=1, max_length=10_000)
    observation: str = Field(min_length=1, max_length=10_000)
    recovered: bool


class RewindRequest(StrictRequestModel):
    target_timestamp: datetime
    reason: str = Field(min_length=1, max_length=500)


class OperationApprovalRequest(StrictRequestModel):
    preview_id: str = Field(min_length=1, max_length=100)
    fingerprint: str = Field(min_length=64, max_length=64)


class RetractionPreviewRequest(StrictRequestModel):
    root_memory_id: str
    reason: str = Field(min_length=1, max_length=500)
    authorized_namespaces: list[str] = Field(min_length=1)


class SupersessionPreviewRequest(RetractionPreviewRequest):
    intent: Literal["correction", "evolution"]
    content: str = Field(min_length=1, max_length=20_000)
    structured_payload: dict[str, Any]


class ReviewResolutionPreviewRequest(StrictRequestModel):
    action: Literal["confirmed", "retracted"]
    reason: str = Field(min_length=1, max_length=500)
    authorized_namespaces: list[str] = Field(min_length=1)


class DemoResetRequest(StrictRequestModel):
    namespace: str = Field(default=DEMO_NAMESPACE, min_length=1, max_length=500)


class DemoPoisonRequest(StrictRequestModel):
    namespace: str = Field(default=DEMO_NAMESPACE, min_length=1, max_length=500)


class AcceptedRun(BaseModel):
    run_id: str
    status: str
    created: bool


def _revision() -> str:
    return os.environ.get("HINDSIGHT_DEPLOYED_REVISION", "unknown").strip() or "unknown"


def _encode_incident_cursor(row: dict[str, Any]) -> str:
    payload = json.dumps(
        {"started_at": str(row["started_at"]), "id": str(row["id"])},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_incident_cursor(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if len(value) > 1024:
        raise HTTPException(status_code=422, detail="cursor is too long")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
            raise ValueError("non-canonical encoding")
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict) or set(parsed) != {"started_at", "id"}:
            raise ValueError("shape")
        started_at = str(parsed["started_at"])
        incident_id = str(parsed["id"])
        if not started_at or not incident_id or len(started_at) > 100 or len(incident_id) > 100:
            raise ValueError("empty")
        return started_at, incident_id
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="cursor is invalid") from exc


def _api_runtime_settings() -> RuntimeSettings:
    try:
        return runtime_settings()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="runtime configuration is unavailable",
        ) from exc


def _api_database_url() -> str:
    try:
        return runtime_database_url()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="runtime configuration is unavailable",
        ) from exc


@app.get(f"{API_PREFIX}/health/live", tags=["health"])
def health_live() -> dict[str, str]:
    return {"status": "live", "revision": _revision()}


@app.get(f"{API_PREFIX}/health/ready", tags=["health"])
def health_ready() -> dict[str, str]:
    db_url = _api_database_url()
    try:
        with connect(db_url, application_name="hindsight-health") as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    return {"status": "ready", "revision": _revision()}


@app.get(
    f"{V2_PREFIX}/health/ready",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
def v2_health_ready() -> dict[str, str]:
    return health_ready()


@app.get(
    f"{V2_PREFIX}/me",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
def v2_me(request: Request) -> dict[str, Any]:
    return _current_identity(request).public_payload()


@app.post(f"{API_PREFIX}/realtime/ticket", tags=["realtime"])
def public_realtime_ticket() -> dict[str, Any]:
    now = int(time.time())
    return {
        "ticket": issue_realtime_ticket(
            tenant_id=current_tenant_id(required=True),
            access_class="public",
            session_expires_at=now + PUBLIC_REALTIME_SESSION_SECONDS,
            now=now,
        ),
        "expires_in": 60,
    }


@app.post(
    f"{V2_PREFIX}/realtime/ticket",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("realtime"))],
)
def v2_realtime_ticket(request: Request) -> dict[str, Any]:
    identity = _current_identity(request)
    return {
        "ticket": issue_realtime_ticket(
            tenant_id=identity.tenant_id,
            access_class=identity.effective_role,
            principal_id=identity.principal_id,
            session_expires_at=identity.expires_at,
        ),
        "expires_in": 60,
    }


@app.get(
    f"{V2_PREFIX}/incidents",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
def v2_incidents_index(
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> dict[str, Any]:
    before_started_at, before_id = _decode_incident_cursor(cursor)
    rows = list_incidents(
        limit=limit + 1,
        before_started_at=before_started_at,
        before_id=before_id,
        db_url=_api_database_url(),
    )
    page = rows[:limit]
    next_cursor = _encode_incident_cursor(page[-1]) if len(rows) > limit and page else None
    return {"items": page, "next_cursor": next_cursor}


@app.get(
    f"{V2_PREFIX}/incidents/{{slug}}",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
def v2_incidents_get(slug: str) -> dict[str, Any]:
    incident = get_incident(slug=slug, db_url=_api_database_url())
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return incident


@app.post(
    f"{V2_PREFIX}/incidents",
    tags=["v2"],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_v2_scope("write"))],
)
def v2_incidents_create(payload: IncidentCreate) -> dict[str, Any]:
    try:
        return create_incident(**payload.model_dump(), db_url=_api_database_url())
    except psycopg_errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="incident slug already exists") from exc


@app.get(f"{API_PREFIX}/incidents", tags=["incidents"])
def incidents_index(limit: Annotated[int, Field(ge=1, le=100)] = 30) -> dict[str, Any]:
    db_url = _api_database_url()
    rows = list_incidents(limit=limit, db_url=db_url)
    return {"items": rows, "count": len(rows)}


@app.get(f"{API_PREFIX}/incidents/{{slug}}", tags=["incidents"])
def incidents_get(slug: str) -> dict[str, Any]:
    db_url = _api_database_url()
    incident = get_incident(slug=slug, db_url=db_url)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return incident


@app.post(
    f"{V2_PREFIX}/incidents/{{slug}}/resolution",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("write"))],
)
def incidents_resolve(
    slug: str,
    payload: IncidentResolutionRequest,
    request: Request,
) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return resolve_incident(
            slug=slug,
            actor=_current_identity(request).actor,
            db_url=db_url,
            **payload.model_dump(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc

@app.post(
    f"{V2_PREFIX}/incidents/{{slug}}/runs",
    tags=["v2"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedRun,
    dependencies=[Depends(_v2_scope("write"))],
)
def runs_create(
    slug: str,
    payload: RunCreate,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AcceptedRun:
    db_url = _api_database_url()
    incident = get_incident(slug=slug, db_url=db_url)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    try:
        run, created = create_run(
            incident_slug=slug,
            namespace=payload.namespace,
            user_input=payload.user_input,
            service_slug=incident.get("service_slug"),
            thread_id=payload.thread_id,
            idempotency_key=idempotency_key,
            retrieval_policy=payload.retrieval_policy,
            db_url=db_url,
        )
    except RunIdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency key is already bound to a different request",
        ) from exc
    dispatch_run_commands(
        db_url=db_url,
        run_id=run["id"],
        command="start",
        limit=1,
    )
    return AcceptedRun(run_id=run["id"], status=run["status"], created=created)


@app.get(
    f"{V2_PREFIX}/runs/{{run_id}}",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
@app.get(f"{API_PREFIX}/runs/{{run_id}}", tags=["runs"])
def runs_get(run_id: str) -> dict[str, Any]:
    db_url = _api_database_url()
    run = get_run(run_id=run_id, db_url=db_url)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.post(
    f"{V2_PREFIX}/runs/{{run_id}}/approval",
    tags=["v2"],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_v2_scope("write"))],
)
def runs_approve(run_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        prepare_approval(
            run_id=run_id,
            approved=payload.approved,
            recommendation_id=payload.recommendation_id,
            selection_fingerprint=payload.selection_fingerprint,
            db_url=db_url,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    dispatch_run_commands(
        db_url=db_url,
        run_id=run_id,
        command="resume",
        limit=1,
    )
    return {
        "run_id": run_id,
        "status": "resuming",
        "approved": payload.approved,
        "recommendation_id": payload.recommendation_id,
        "selection_fingerprint": payload.selection_fingerprint,
    }


@app.get(
    f"{V2_PREFIX}/namespaces/{{namespace}}/beliefs",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
@app.get(f"{API_PREFIX}/namespaces/{{namespace}}/beliefs", tags=["memory"])
def beliefs_get(
    namespace: str,
    as_of: datetime | None = None,
    system_as_of: datetime | None = None,
    valid_at: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    db_url = _api_database_url()
    if system_as_of is not None or valid_at is not None:
        resolved_system_time = system_as_of or datetime.now().astimezone()
        with MemoryStore(url=db_url) as store:
            rows = store.list_semantic_as_of(
                namespace=namespace,
                system_as_of=resolved_system_time,
                valid_at=valid_at,
                limit=limit,
            )
        return {
            "namespace": namespace,
            "system_as_of": resolved_system_time.isoformat(),
            "valid_at": (valid_at or resolved_system_time).isoformat(),
            "count": len(rows),
            "memories": _jsonable(rows),
        }
    return memory_snapshot(
        namespace=namespace,
        as_of=as_of.isoformat() if as_of else None,
        db_url=db_url,
        limit=limit,
    )


@app.get(
    f"{V2_PREFIX}/memories/{{memory_kind}}/{{memory_id}}",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
@app.get(f"{API_PREFIX}/memories/{{memory_kind}}/{{memory_id}}", tags=["memory"])
def memories_get(memory_kind: Literal["episodic", "semantic"], memory_id: str) -> dict[str, Any]:
    db_url = _api_database_url()
    with MemoryStore(url=db_url) as store:
        memory = store.audit_memory(memory_kind=memory_kind, memory_id=memory_id)
        if memory is None:
            raise HTTPException(status_code=404, detail="memory not found")
        provenance = store.provenance_for_memory(
            memory_kind=memory_kind,
            memory_id=memory_id,
        )
    return {"memory": _jsonable(memory), "provenance": _jsonable(provenance)}


@app.get(
    f"{V2_PREFIX}/decisions/{{decision_id}}/influence",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
@app.get(f"{API_PREFIX}/decisions/{{decision_id}}/influence", tags=["memory"])
def decisions_influence(decision_id: str) -> dict[str, Any]:
    return _jsonable(
        decision_influence(decision_id=decision_id, db_url=_api_database_url())
    )


@app.get(
    f"{V2_PREFIX}/signature-scenarios",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
@app.get(f"{API_PREFIX}/signature-scenarios", tags=["memory"])
def signature_scenarios_get(
    decision_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    try:
        scenario = signature_scenario_trace(
            decision_id=decision_id,
            namespace=namespace,
            db_url=_api_database_url(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if scenario is None:
        raise HTTPException(status_code=404, detail="signature scenario not found")
    return _jsonable(scenario)


@app.get(
    f"{V2_PREFIX}/signature-scenarios/{{scenario_id}}",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
@app.get(f"{API_PREFIX}/signature-scenarios/{{scenario_id}}", tags=["memory"])
def signature_scenarios_get_by_id(scenario_id: str) -> dict[str, Any]:
    scenario = signature_scenario_trace(
        scenario_id=scenario_id,
        db_url=_api_database_url(),
    )
    if scenario is None:
        raise HTTPException(status_code=404, detail="signature scenario not found")
    return _jsonable(scenario)


@app.post(
    f"{V2_PREFIX}/namespaces/{{namespace}}/rewinds/preview",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("write"))],
)
def rewind_preview(
    namespace: str,
    payload: RewindRequest,
    request: Request,
) -> dict[str, Any]:
    db_url = _api_database_url()
    return _jsonable(
        preview_rewind(
            namespace=namespace,
            target_timestamp=payload.target_timestamp,
            actor=_current_identity(request).actor,
            reason=payload.reason,
            db_url=db_url,
        )
    )


@app.post(
    f"{V2_PREFIX}/namespaces/{{namespace}}/rewinds",
    tags=["v2"],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_v2_scope("write"))],
)
def rewind_execute(
    namespace: str,
    payload: OperationApprovalRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    del namespace
    return _approve_operation(
        payload=payload,
        idempotency_key=idempotency_key,
    )


@app.post(
    f"{V2_PREFIX}/memory/retractions/preview",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("write"))],
)
def retraction_preview(payload: RetractionPreviewRequest, request: Request) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return _jsonable(
            preview_retraction(
                root_memory_id=payload.root_memory_id,
                actor=_current_identity(request).actor,
                reason=payload.reason,
                authorized_namespaces=payload.authorized_namespaces,
                db_url=db_url,
            )
        )
    except OperationAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    f"{V2_PREFIX}/memory/supersessions/preview",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("write"))],
)
def supersession_preview(
    payload: SupersessionPreviewRequest,
    request: Request,
) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return _jsonable(
            preview_supersession(
                root_memory_id=payload.root_memory_id,
                intent=payload.intent,
                content=payload.content,
                structured_payload=payload.structured_payload,
                actor=_current_identity(request).actor,
                reason=payload.reason,
                authorized_namespaces=payload.authorized_namespaces,
                db_url=db_url,
            )
        )
    except OperationAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    f"{V2_PREFIX}/memory/reviews/{{review_item_id}}/preview",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("write"))],
)
def review_resolution_preview(
    review_item_id: str,
    payload: ReviewResolutionPreviewRequest,
    request: Request,
) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return _jsonable(
            preview_review_resolution(
                review_item_id=review_item_id,
                action=payload.action,
                actor=_current_identity(request).actor,
                reason=payload.reason,
                authorized_namespaces=payload.authorized_namespaces,
                db_url=db_url,
            )
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OperationAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    f"{V2_PREFIX}/memory/operations",
    tags=["v2"],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_v2_scope("write"))],
)
def operations_create(
    payload: OperationApprovalRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    return _approve_operation(payload=payload, idempotency_key=idempotency_key)


@app.get(
    f"{V2_PREFIX}/memory/operations/{{operation_id}}",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("read"))],
)
@app.get(f"{API_PREFIX}/memory/operations/{{operation_id}}", tags=["memory"])
def operations_get(operation_id: str) -> dict[str, Any]:
    db_url = _api_database_url()
    operation = get_operation(operation_id=operation_id, db_url=db_url)
    if operation is None:
        raise HTTPException(status_code=404, detail="memory operation not found")
    return _jsonable(operation)


def _approve_operation(
    *, payload: OperationApprovalRequest, idempotency_key: str | None
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    db_url = _api_database_url()
    try:
        operation, created = enqueue_operation(
            preview_id=payload.preview_id,
            fingerprint=payload.fingerprint,
            idempotency_key=idempotency_key,
            db_url=db_url,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OperationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created or operation["status"] in {"queued", "retrying"}:
        try:
            enqueue_run({"command": "memory_operation", "operation_id": str(operation["id"])})
        except RunQueueUnavailableError as exc:
            raise HTTPException(status_code=503, detail="operation queue is unavailable") from exc
    return {
        "operation_id": str(operation["id"]),
        "status": operation["status"],
        "created": created,
        "operation_url": f"{V2_PREFIX}/memory/operations/{operation['id']}",
    }


@app.post(
    f"{V2_PREFIX}/demo/poison-rewind/reset",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("write"))],
)
def demo_reset(payload: DemoResetRequest) -> dict[str, Any]:
    settings = _api_runtime_settings()
    embedding_provider = embedding_provider_from_env(settings.provider_env)
    fixture_id = uuid4()
    session_namespace = reset_poison_rewind_state(
        namespace=payload.namespace,
        session_id=fixture_id,
        db_url=settings.database_url,
    )
    incident = ensure_poison_rewind_incident(
        fixture_id=fixture_id,
        db_url=settings.database_url,
    )
    memory = seed_good_demo_memory(
        namespace=session_namespace,
        db_url=settings.database_url,
        embedding_provider=embedding_provider,
    )
    rewind_anchor = current_database_timestamp(db_url=settings.database_url)
    return {
        "namespace": session_namespace,
        "incident": incident,
        "seed_memory": _jsonable(memory),
        "rewind_anchor": _jsonable(rewind_anchor),
    }


@app.post(
    f"{V2_PREFIX}/demo/poison-rewind/poison",
    tags=["v2"],
    dependencies=[Depends(_v2_scope("write"))],
)
def demo_poison(payload: DemoPoisonRequest) -> dict[str, Any]:
    settings = _api_runtime_settings()
    embedding_provider = embedding_provider_from_env(settings.provider_env)
    return _jsonable(
        poison_demo_memory(
            namespace=payload.namespace,
            db_url=settings.database_url,
            embedding_provider=embedding_provider,
        )
    )


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value


WEB_ROOT = Path(__file__).with_name("web")
app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="ui")

handler = Mangum(app, lifespan="off")
