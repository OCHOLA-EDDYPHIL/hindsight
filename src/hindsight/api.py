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

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from mangum import Mangum
from psycopg import errors as psycopg_errors
from pydantic import BaseModel, ConfigDict, Field

from hindsight.dashboard import memory_snapshot
from hindsight.db import connect
from hindsight.demo import (
    DEMO_NAMESPACE,
    ensure_poison_rewind_incident,
    poison_demo_memory,
    reset_poison_rewind_demo,
    seed_good_demo_memory,
)
from hindsight.lambda_handler import function_auth_token, runtime_settings
from hindsight.memory import MemoryStore
from hindsight.queueing import RunQueueUnavailableError, enqueue_run
from hindsight.runs import (
    RunConflictError,
    RunNotFoundError,
    create_incident,
    create_run,
    fail_run,
    get_incident,
    get_run,
    list_incidents,
    prepare_approval,
    transition_run,
)
from hindsight.security import safe_error_detail

API_PREFIX = "/v1"
OPERATOR_COOKIE = "hindsight_operator_session"
OPERATOR_SESSION_TTL_SECONDS = 4 * 60 * 60

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
    allow_origins=[
        origin
        for origin in os.environ.get("HINDSIGHT_ALLOWED_ORIGINS", "").split(",")
        if origin
    ],
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


class ApprovalRequest(BaseModel):
    approved: bool


class RewindRequest(BaseModel):
    target_timestamp: datetime
    reason: str = Field(min_length=1, max_length=500)
    state_hash: str | None = Field(default=None, min_length=64, max_length=64)


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
    try:
        settings = runtime_settings()
        with connect(settings.database_url, application_name="hindsight-health") as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    return {"status": "ready"}


@app.get(f"{API_PREFIX}/incidents", tags=["incidents"])
def incidents_index(limit: Annotated[int, Field(ge=1, le=100)] = 30) -> dict[str, Any]:
    rows = list_incidents(limit=limit)
    return {"items": rows, "count": len(rows)}


@app.post(
    f"{API_PREFIX}/incidents",
    tags=["incidents"],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_operator_required)],
)
def incidents_create(payload: IncidentCreate) -> dict[str, Any]:
    try:
        return create_incident(**payload.model_dump())
    except psycopg_errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="incident slug already exists") from exc


@app.get(f"{API_PREFIX}/incidents/{{slug}}", tags=["incidents"])
def incidents_get(slug: str) -> dict[str, Any]:
    incident = get_incident(slug=slug)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return incident


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
    incident = get_incident(slug=slug)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    run, created = create_run(
        incident_slug=slug,
        namespace=payload.namespace,
        user_input=payload.user_input,
        service_slug=incident.get("service_slug"),
        thread_id=payload.thread_id,
        idempotency_key=idempotency_key,
    )
    if created:
        try:
            enqueue_run({"command": "start", "run_id": run["id"]})
        except RunQueueUnavailableError as exc:
            fail_run(
                run_id=run["id"],
                failure_code="queue_unavailable",
                failure_detail=safe_error_detail(exc),
            )
            raise HTTPException(status_code=503, detail="run queue is unavailable") from exc
    return AcceptedRun(run_id=run["id"], status=run["status"], created=created)


@app.get(f"{API_PREFIX}/runs/{{run_id}}", tags=["runs"])
def runs_get(run_id: str) -> dict[str, Any]:
    run = get_run(run_id=run_id)
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
    try:
        prepare_approval(run_id=run_id, approved=payload.approved)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        enqueue_run(
            {"command": "resume", "run_id": run_id, "approved": payload.approved}
        )
    except RunQueueUnavailableError as exc:
        transition_run(
            run_id=run_id,
            status="awaiting_approval",
            phase="queue",
            summary="Approval could not be queued",
            fields={"failure_detail": safe_error_detail(exc)},
        )
        raise HTTPException(status_code=503, detail="run queue is unavailable") from exc
    return {"run_id": run_id, "status": "resuming", "approved": payload.approved}


@app.get(f"{API_PREFIX}/namespaces/{{namespace}}/beliefs", tags=["memory"])
def beliefs_get(namespace: str, as_of: datetime | None = None, limit: int = 100) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    return memory_snapshot(
        namespace=namespace,
        as_of=as_of.isoformat() if as_of else None,
        limit=limit,
    )


@app.get(f"{API_PREFIX}/memories/{{memory_kind}}/{{memory_id}}", tags=["memory"])
def memories_get(memory_kind: Literal["episodic", "semantic"], memory_id: str) -> dict[str, Any]:
    with MemoryStore() as store:
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
    settings = runtime_settings()
    with connect(settings.database_url, application_name="hindsight-api") as conn:
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
    return {"decision_id": decision_id, "count": len(memories), "memories": _jsonable(memories)}


@app.post(f"{API_PREFIX}/namespaces/{{namespace}}/rewinds/preview", tags=["memory"])
def rewind_preview(namespace: str, payload: RewindRequest) -> dict[str, Any]:
    with MemoryStore() as store:
        preview = store.preview_rewind(timestamp=payload.target_timestamp, namespace=namespace)
    return _jsonable(dataclasses.asdict(preview))


@app.post(
    f"{API_PREFIX}/namespaces/{{namespace}}/rewinds",
    tags=["memory"],
    dependencies=[Depends(_operator_required)],
)
def rewind_execute(namespace: str, payload: RewindRequest) -> dict[str, Any]:
    if payload.state_hash is None:
        raise HTTPException(status_code=422, detail="state_hash from rewind preview is required")
    with MemoryStore() as store:
        preview = store.preview_rewind(timestamp=payload.target_timestamp, namespace=namespace)
        if not hmac.compare_digest(preview.state_hash, payload.state_hash):
            raise HTTPException(status_code=409, detail="belief state changed; preview rewind again")
        result = store.rewind(
            timestamp=payload.target_timestamp,
            namespace=namespace,
            actor="dashboard.operator",
            reason=payload.reason,
        )
    return _jsonable(dataclasses.asdict(result))


@app.post(
    f"{API_PREFIX}/demo/poison-rewind/reset",
    tags=["demo"],
    dependencies=[Depends(_operator_required)],
)
def demo_reset(payload: DemoResetRequest) -> dict[str, Any]:
    reset_poison_rewind_demo(namespace=payload.namespace)
    incident = ensure_poison_rewind_incident()
    memory = seed_good_demo_memory(namespace=payload.namespace)
    return {
        "namespace": payload.namespace,
        "incident": incident,
        "seed_memory": _jsonable(memory),
    }


@app.post(
    f"{API_PREFIX}/demo/poison-rewind/poison",
    tags=["demo"],
    dependencies=[Depends(_operator_required)],
)
def demo_poison(payload: DemoPoisonRequest) -> dict[str, Any]:
    return _jsonable(poison_demo_memory(namespace=payload.namespace))


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
        if origin and urlparse(origin).netloc != request.url.netloc:
            raise HTTPException(status_code=403, detail="cross-origin operator request denied")


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
