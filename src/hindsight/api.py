"""Versioned FastAPI surface for the Hindsight product UI."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from mangum import Mangum
from psycopg import errors as psycopg_errors
from pydantic import BaseModel, ConfigDict, Field

from hindsight.dashboard import memory_snapshot
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
    function_auth_token,
    runtime_database_url,
    runtime_settings,
)
from hindsight.runs import (
    RunConflictError,
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
    governed_decision_trace,
    lesson_identity_trace,
    lesson_identity_traces,
    signature_scenario_trace,
)

API_PREFIX = "/v1"
OPERATOR_COOKIE = "hindsight_operator_session"
OPERATOR_SESSION_TTL_SECONDS = 4 * 60 * 60


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
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["authorization", "content-type", "idempotency-key"],
)


class IncidentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9:-]*$")
    title: str = Field(min_length=1, max_length=300)
    severity: Literal["sev1", "sev2", "sev3"]
    summary: str = Field(min_length=1, max_length=10_000)
    service_slug: str | None = Field(default=None, max_length=200)


class RunCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    user_input: str = Field(min_length=1, max_length=20_000)
    namespace: str = Field(min_length=1, max_length=500)
    thread_id: str | None = Field(default=None, max_length=500)
    retrieval_policy: Literal["semantic_strict", "semantic_then_keyword"] = "semantic_strict"


class ApprovalRequest(BaseModel):
    approved: bool


class IncidentResolutionRequest(BaseModel):
    root_cause: str = Field(min_length=1, max_length=10_000)
    action: str = Field(min_length=1, max_length=10_000)
    observation: str = Field(min_length=1, max_length=10_000)
    recovered: bool


class RewindRequest(BaseModel):
    target_timestamp: datetime
    reason: str = Field(min_length=1, max_length=500)


class OperationApprovalRequest(BaseModel):
    preview_id: str = Field(min_length=1, max_length=100)
    fingerprint: str = Field(min_length=64, max_length=64)


class RetractionPreviewRequest(BaseModel):
    root_memory_id: str
    reason: str = Field(min_length=1, max_length=500)
    authorized_namespaces: list[str] = Field(min_length=1)


class SupersessionPreviewRequest(RetractionPreviewRequest):
    intent: Literal["correction", "evolution"]
    content: str = Field(min_length=1, max_length=20_000)
    structured_payload: dict[str, Any]


class ReviewResolutionPreviewRequest(BaseModel):
    action: Literal["confirmed", "retracted"]
    reason: str = Field(min_length=1, max_length=500)
    authorized_namespaces: list[str] = Field(min_length=1)


class OperatorSessionRequest(BaseModel):
    token: str = Field(min_length=1, max_length=1000)


class DemoResetRequest(BaseModel):
    namespace: str = Field(default=DEMO_NAMESPACE, min_length=1, max_length=500)


class DemoPoisonRequest(BaseModel):
    namespace: str = Field(default=DEMO_NAMESPACE, min_length=1, max_length=500)


class AcceptedRun(BaseModel):
    run_id: str
    status: str
    created: bool


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


def _operator_required(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Cookie(alias=OPERATOR_COOKIE)] = None,
) -> None:
    _operator_required_impl(request, authorization=authorization, session=session)


@app.get(f"{API_PREFIX}/health/live", tags=["health"])
def health_live() -> dict[str, str]:
    return {"status": "live"}


@app.get(f"{API_PREFIX}/health/ready", tags=["health"])
def health_ready() -> dict[str, str]:
    db_url = _api_database_url()
    try:
        with connect(db_url, application_name="hindsight-health") as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    return {"status": "ready"}


@app.get(f"{API_PREFIX}/incidents", tags=["incidents"])
def incidents_index(limit: Annotated[int, Field(ge=1, le=100)] = 30) -> dict[str, Any]:
    db_url = _api_database_url()
    rows = list_incidents(limit=limit, db_url=db_url)
    return {"items": rows, "count": len(rows)}


@app.post(
    f"{API_PREFIX}/incidents",
    tags=["incidents"],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_operator_required)],
)
def incidents_create(payload: IncidentCreate) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return create_incident(**payload.model_dump(), db_url=db_url)
    except psycopg_errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="incident slug already exists") from exc


@app.get(f"{API_PREFIX}/incidents/{{slug}}", tags=["incidents"])
def incidents_get(slug: str) -> dict[str, Any]:
    db_url = _api_database_url()
    incident = get_incident(slug=slug, db_url=db_url)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return incident


@app.post(
    f"{API_PREFIX}/incidents/{{slug}}/resolution",
    tags=["incidents"],
    dependencies=[Depends(_operator_required)],
)
def incidents_resolve(slug: str, payload: IncidentResolutionRequest) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return resolve_incident(
            slug=slug,
            actor="dashboard.operator",
            db_url=db_url,
            **payload.model_dump(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc


@app.post(
    f"{API_PREFIX}/incidents/{{slug}}/runs",
    tags=["runs"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedRun,
    dependencies=[Depends(_operator_required)],
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
    dispatch_run_commands(
        db_url=db_url,
        run_id=run["id"],
        command="start",
        limit=1,
    )
    return AcceptedRun(run_id=run["id"], status=run["status"], created=created)


@app.get(f"{API_PREFIX}/runs/{{run_id}}", tags=["runs"])
def runs_get(run_id: str) -> dict[str, Any]:
    db_url = _api_database_url()
    run = get_run(run_id=run_id, db_url=db_url)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.post(
    f"{API_PREFIX}/runs/{{run_id}}/approval",
    tags=["runs"],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_operator_required)],
)
def runs_approve(run_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        prepare_approval(
            run_id=run_id,
            approved=payload.approved,
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
    return {"run_id": run_id, "status": "resuming", "approved": payload.approved}


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


@app.get(f"{API_PREFIX}/decisions/{{decision_id}}/influence", tags=["memory"])
def decisions_influence(decision_id: str) -> dict[str, Any]:
    db_url = _api_database_url()
    with connect(db_url, application_name="hindsight-api") as conn:
        store = MemoryStore(conn=conn)
        reads = store.reads_for_decision(decision_id=decision_id)
        memories = []
        for read in reads:
            kind = read["memory_kind"]
            memory_id = str(read["memory_id"])
            memory = store.audit_memory(memory_kind=kind, memory_id=memory_id)
            provenance = store.provenance_for_memory(
                memory_kind=kind,
                memory_id=memory_id,
            )
            memories.append(
                {
                    "read": read,
                    "memory": memory,
                    "provenance": provenance,
                    "status": "invalidated"
                    if provenance and provenance.get("invalidated_at")
                    else "current",
                }
            )
    trace = governed_decision_trace(decision_id=decision_id, db_url=db_url)
    return {
        "decision_id": decision_id,
        "count": len(memories),
        "memories": _jsonable(memories),
        "decision": _jsonable(trace["decision"]) if trace else None,
        "retrievals": _jsonable(trace["retrievals"]) if trace else [],
        "trace": _jsonable(trace) if trace else None,
    }


@app.get(f"{API_PREFIX}/lesson-traces", tags=["memory"])
def lessons_traces_list(
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> dict[str, Any]:
    traces = lesson_identity_traces(db_url=_api_database_url(), limit=limit)
    return {"count": len(traces), "traces": _jsonable(traces)}


@app.get(f"{API_PREFIX}/lesson-traces/{{decision_id}}", tags=["memory"])
def lessons_traces_get(decision_id: str) -> dict[str, Any]:
    trace = lesson_identity_trace(
        decision_id=decision_id,
        db_url=_api_database_url(),
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="lesson identity trace not found")
    return _jsonable(trace)


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
    f"{API_PREFIX}/namespaces/{{namespace}}/rewinds/preview",
    tags=["memory"],
    dependencies=[Depends(_operator_required)],
)
def rewind_preview(namespace: str, payload: RewindRequest) -> dict[str, Any]:
    db_url = _api_database_url()
    return _jsonable(
        preview_rewind(
            namespace=namespace,
            target_timestamp=payload.target_timestamp,
            actor="dashboard.operator",
            reason=payload.reason,
            db_url=db_url,
        )
    )


@app.post(
    f"{API_PREFIX}/namespaces/{{namespace}}/rewinds",
    tags=["memory"],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_operator_required)],
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
    f"{API_PREFIX}/memory/retractions/preview",
    tags=["memory"],
    dependencies=[Depends(_operator_required)],
)
def retraction_preview(payload: RetractionPreviewRequest) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return _jsonable(
            preview_retraction(
                root_memory_id=payload.root_memory_id,
                actor="dashboard.operator",
                reason=payload.reason,
                authorized_namespaces=payload.authorized_namespaces,
                db_url=db_url,
            )
        )
    except OperationAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    f"{API_PREFIX}/memory/supersessions/preview",
    tags=["memory"],
    dependencies=[Depends(_operator_required)],
)
def supersession_preview(payload: SupersessionPreviewRequest) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return _jsonable(
            preview_supersession(
                root_memory_id=payload.root_memory_id,
                intent=payload.intent,
                content=payload.content,
                structured_payload=payload.structured_payload,
                actor="dashboard.operator",
                reason=payload.reason,
                authorized_namespaces=payload.authorized_namespaces,
                db_url=db_url,
            )
        )
    except OperationAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    f"{API_PREFIX}/memory/reviews/{{review_item_id}}/preview",
    tags=["memory"],
    dependencies=[Depends(_operator_required)],
)
def review_resolution_preview(
    review_item_id: str, payload: ReviewResolutionPreviewRequest
) -> dict[str, Any]:
    db_url = _api_database_url()
    try:
        return _jsonable(
            preview_review_resolution(
                review_item_id=review_item_id,
                action=payload.action,
                actor="dashboard.operator",
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
    f"{API_PREFIX}/memory/operations",
    tags=["memory"],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_operator_required)],
)
def operations_create(
    payload: OperationApprovalRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    return _approve_operation(payload=payload, idempotency_key=idempotency_key)


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
        "operation_url": f"{API_PREFIX}/memory/operations/{operation['id']}",
    }


@app.post(
    f"{API_PREFIX}/demo/poison-rewind/reset",
    tags=["demo"],
    dependencies=[Depends(_operator_required)],
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
    f"{API_PREFIX}/demo/poison-rewind/poison",
    tags=["demo"],
    dependencies=[Depends(_operator_required)],
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


@app.get(f"{API_PREFIX}/operator/session", tags=["operator"])
def operator_session_status(
    authorization: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Cookie(alias=OPERATOR_COOKIE)] = None,
) -> dict[str, bool]:
    return {"operator": _operator_valid(authorization=authorization, session=session)}


@app.post(f"{API_PREFIX}/operator/session", tags=["operator"])
def operator_session_create(payload: OperatorSessionRequest, response: Response) -> dict[str, bool]:
    secret = _operator_secret()
    if not hmac.compare_digest(payload.token, secret):
        raise HTTPException(status_code=403, detail="invalid operator token")
    response.set_cookie(
        OPERATOR_COOKIE,
        _signed_session(secret),
        max_age=OPERATOR_SESSION_TTL_SECONDS,
        httponly=True,
        secure=_secure_cookies(),
        samesite="strict",
        path="/",
    )
    return {"operator": True}


@app.delete(f"{API_PREFIX}/operator/session", tags=["operator"])
def operator_session_delete(response: Response) -> dict[str, bool]:
    response.delete_cookie(OPERATOR_COOKIE, path="/")
    return {"operator": False}


def _operator_required_impl(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Cookie(alias=OPERATOR_COOKIE)] = None,
) -> None:
    if not _operator_valid(authorization=authorization, session=session):
        raise HTTPException(status_code=403, detail="operator authorization required")
    if session and not authorization:
        origin = request.headers.get("origin")
        if origin and not _operator_origin_allowed(request=request, origin=origin):
            raise HTTPException(status_code=403, detail="cross-origin operator request denied")


def _operator_origin_allowed(*, request: Request, origin: str) -> bool:
    supplied_origin = _normalize_origin(origin)
    request_origin = _normalize_origin(f"{request.url.scheme}://{request.url.netloc}")
    return supplied_origin is not None and (
        supplied_origin == request_origin
        or supplied_origin in _configured_allowed_origins()
    )


def _operator_valid(*, authorization: str | None, session: str | None) -> bool:
    try:
        secret = _operator_secret()
    except HTTPException:
        return False
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token and hmac.compare_digest(token, secret):
            return True
    return bool(session and _valid_session(session, secret))


def _operator_secret() -> str:
    direct = os.environ.get("HINDSIGHT_FUNCTION_AUTH_TOKEN")
    if direct:
        return direct
    try:
        return function_auth_token()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="operator authorization is not configured") from exc


def _signed_session(secret: str) -> str:
    expires = str(int(time.time()) + OPERATOR_SESSION_TTL_SECONDS)
    digest = hmac.new(secret.encode(), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{digest}"


def _valid_session(value: str, secret: str) -> bool:
    expires, separator, digest = value.partition(".")
    if not separator or not expires.isdigit() or int(expires) <= int(time.time()):
        return False
    expected = hmac.new(secret.encode(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def _secure_cookies() -> bool:
    return os.environ.get("HINDSIGHT_SECURE_COOKIES", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


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
