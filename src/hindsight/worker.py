"""SQS worker for durable asynchronous incident-agent runs."""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any
from uuid import uuid4
from opentelemetry import trace
from opentelemetry.propagate import extract

from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent
from hindsight.cloudwatch_diagnostics import optional_cloudwatch_diagnostics_from_env
from hindsight.consolidation import enqueue_consolidation_job, process_consolidation_job
from hindsight.embeddings import embedding_provider_from_env
from hindsight.gemini import GeminiPoolExhaustedError, gemini_pool_from_env
from hindsight.operations import execute_operation, reap_exhausted_operations
from hindsight.observability import structured_event
from hindsight.reasoning import reasoning_provider_from_env, retrying_reasoning_provider
from hindsight.run_dispatch import dispatch_run_commands
from hindsight.runtime import (
    invalidate_runtime_settings_cache,
    runtime_database_url,
    runtime_settings,
)
from hindsight.runs import (
    RunAttemptBusyError,
    RunAttemptsExhaustedError,
    claim_run_attempt,
    finalize_exhausted_run,
    finish_run_attempt,
    get_run,
    record_run_attempt_failure,
    reserve_run_budget,
    transition_run_attempt,
)
from hindsight.security import safe_error_detail
from hindsight.server_tenants import public_demo_tenant_id, worker_tenant_id
from hindsight.tenant import current_tenant_id, tenant_scope
from hindsight.tracing import configure_tracing_from_env, set_span_attributes, start_span

RUN_MAX_ATTEMPTS_ENV = "HINDSIGHT_RUN_MAX_ATTEMPTS"
RUN_ATTEMPT_LEASE_SECONDS_ENV = "HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS"
RUN_DLQ_ARN_ENV = "HINDSIGHT_RUN_DLQ_ARN"
LOGGER = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process SQS messages with per-record failure reporting."""

    if "command" in event and not event.get("Records"):
        return process_message(event) or {}
    failures = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId") or "unknown")
        source_arn = str(record.get("eventSourceARN") or record.get("eventSourceArn") or "")
        attributes = record.get("attributes") or {}
        receive_count = str(attributes.get("ApproximateReceiveCount") or "unknown")
        message: dict[str, Any] = {}
        try:
            message = json.loads(record.get("body") or "{}")
            process_message(
                message,
                dead_letter=bool(source_arn and source_arn == os.environ.get(RUN_DLQ_ARN_ENV)),
                worker_message_id=message_id,
            )
            _log_record_result(
                status="completed",
                message=message,
                message_id=message_id,
                receive_count=receive_count,
                source_arn=source_arn,
                context=context,
            )
        except Exception as exc:
            _log_record_result(
                status="failed",
                message=message,
                message_id=message_id,
                receive_count=receive_count,
                source_arn=source_arn,
                context=context,
                error=exc,
            )
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def _log_record_result(
    *,
    status: str,
    message: dict[str, Any],
    message_id: str,
    receive_count: str,
    source_arn: str,
    context: Any,
    error: Exception | None = None,
) -> None:
    record = {
        "event": "worker_record",
        "status": status,
        "command": str(message.get("command") or "start"),
        "message_id": message_id,
        "receive_count": receive_count,
        "source_arn": source_arn,
        "lambda_request_id": str(getattr(context, "aws_request_id", "local")),
    }
    for key in (
        "tenant_id",
        "operation_id",
        "run_id",
        "incident_id",
        "dispatch_id",
        "dispatch_attempt_id",
        "attempt_id",
    ):
        value = str(message.get(key) or "").strip()
        if value:
            record[key] = value
    if error is not None:
        record["error_code"] = type(error).__name__
        record["error_detail"] = safe_error_detail(error, max_chars=1000)
        LOGGER.error(structured_event("worker_record", record))
    else:
        LOGGER.info(structured_event("worker_record", record))


def process_message(
    message: dict[str, Any],
    *,
    dead_letter: bool = False,
    worker_message_id: str | None = None,
) -> dict[str, Any] | None:
    """Process one start or resume command."""

    supplied_tenant = message.get("tenant_id")
    tenant_id = (
        worker_tenant_id(supplied_tenant)
        if supplied_tenant is not None
        else current_tenant_id()
        or worker_tenant_id(os.environ.get("HINDSIGHT_WORKER_TENANT_ID", public_demo_tenant_id()))
    )
    configure_tracing_from_env(service_name="hindsight-worker")
    carrier = {"traceparent": str(message.get("traceparent") or "")}
    attributes = {
        f"hindsight.{key}": value
        for key in ("tenant_id", "run_id", "dispatch_id", "dispatch_attempt_id")
        if (value := message.get(key))
    }
    with tenant_scope(tenant_id), start_span(
        "hindsight.worker.message", attributes, context=extract(carrier)
    ):
        return _process_tenant_message(
            message,
            dead_letter=dead_letter,
            worker_message_id=worker_message_id,
        )


def _process_tenant_message(
    message: dict[str, Any],
    *,
    dead_letter: bool = False,
    worker_message_id: str | None = None,
) -> dict[str, Any] | None:
    configure_tracing_from_env(service_name="hindsight-worker")
    command = str(message.get("command") or "start").strip().lower()
    if command == "dispatch_run_commands":
        return dispatch_run_commands(db_url=runtime_database_url(), limit=100)
    if command == "reap_memory_operations":
        return reap_exhausted_operations(db_url=runtime_database_url())
    if command == "consolidation":
        incident_id = str(message.get("incident_id") or "").strip()
        source_event_id = str(message.get("source_event_id") or "").strip()
        if not incident_id or not source_event_id:
            raise ValueError("incident_id and source_event_id are required")
        settings = runtime_settings()
        uses_gemini = any(
            settings.provider_env.get(name, "").strip().lower() == "gemini"
            for name in ("LLM_PROVIDER", "EMBEDDING_PROVIDER")
        )
        gemini_pool = gemini_pool_from_env(settings.provider_env) if uses_gemini else None
        reasoning = reasoning_provider_from_env(settings.provider_env, gemini_pool=gemini_pool)
        embeddings = embedding_provider_from_env(
            settings.provider_env,
            gemini_pool=gemini_pool,
        )
        job = enqueue_consolidation_job(
            incident_id=incident_id,
            source_event_id=source_event_id,
            db_url=settings.database_url,
        )
        result = process_consolidation_job(
            job_id=str(job["id"]),
            db_url=settings.database_url,
            reasoning_provider=reasoning,
            embedding_provider=embeddings,
        )
        return {"job_id": result.job_id, "created": result.created, "reason": result.reason}
    if command == "memory_operation":
        operation_id = str(message.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")

        def provider_factory():
            settings = runtime_settings()
            uses_gemini = (
                settings.provider_env.get("EMBEDDING_PROVIDER", "").strip().lower() == "gemini"
            )
            gemini_pool = gemini_pool_from_env(settings.provider_env) if uses_gemini else None
            return embedding_provider_from_env(
                settings.provider_env,
                gemini_pool=gemini_pool,
            )

        return execute_operation(
            operation_id=operation_id,
            embedding_provider_factory=provider_factory,
            worker_id=str(message.get("worker_id") or f"sqs-worker:{uuid4()}"),
            db_url=runtime_database_url(),
        )

    run_id = str(message.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    if command not in {"start", "resume"}:
        raise ValueError(f"unsupported worker command: {command}")
    command_generation = message.get("command_generation", 0)
    if type(command_generation) is not int or command_generation < 0:
        raise ValueError("command_generation must be a non-negative integer")
    dispatch_id = str(message.get("dispatch_id") or "").strip()
    dispatch_attempt_id = str(message.get("dispatch_attempt_id") or "").strip()
    dispatch_sequence = message.get("dispatch_sequence")
    if not dispatch_id or not dispatch_attempt_id:
        raise ValueError("dispatch_id and dispatch_attempt_id are required")
    if type(dispatch_sequence) is not int or dispatch_sequence < 1:
        raise ValueError("dispatch_sequence must be a positive integer")
    resolved_worker_message_id = str(
        worker_message_id or f"direct:{dispatch_attempt_id}"
    ).strip()
    if not resolved_worker_message_id:
        raise ValueError("worker_message_id must not be blank")

    settings = runtime_settings()
    db_url = settings.database_url
    max_attempts = max(1, int(os.environ.get(RUN_MAX_ATTEMPTS_ENV, "3")))
    lease_seconds = max(1, int(os.environ.get(RUN_ATTEMPT_LEASE_SECONDS_ENV, "300")))
    claim = claim_run_attempt(
        run_id=run_id,
        command=command,
        command_generation=command_generation,
        lease_ttl=timedelta(seconds=lease_seconds),
        max_attempts=max_attempts,
        dispatch_id=dispatch_id,
        dispatch_attempt_id=dispatch_attempt_id,
        dispatch_sequence=dispatch_sequence,
        worker_message_id=resolved_worker_message_id,
        db_url=db_url,
    )
    if claim.outcome == "busy":
        raise RunAttemptBusyError(f"agent run attempt is still live: {run_id}")
    if claim.outcome == "exhausted":
        if dead_letter:
            return finalize_exhausted_run(
                run_id=run_id,
                command=command,
                max_attempts=max_attempts,
                db_url=db_url,
            )
        raise RunAttemptsExhaustedError(f"agent run attempts exhausted: {run_id}")
    if claim.outcome in {"duplicate", "missing"}:
        return get_run(run_id=run_id, db_url=db_url)
    if claim.run is None or claim.attempt_id is None:
        raise RuntimeError(f"claimed run attempt is incomplete: {run_id}")
    run = claim.run
    attempt_id = claim.attempt_id
    message["attempt_id"] = attempt_id
    set_span_attributes(
        trace.get_current_span(),
        {"hindsight.attempt_id": attempt_id},
    )

    def progress(phase: str, status: str, state: dict[str, Any]) -> None:
        if phase == "approval":
            return
        fields: dict[str, Any] = {}
        metadata: dict[str, Any] = {}
        if state.get("plan") is not None:
            fields["plan"] = state["plan"]
        if state.get("proposed_action") is not None:
            fields["proposed_action"] = state["proposed_action"]
        if state.get("action_approved") is not None:
            fields["action_approved"] = state["action_approved"]
        reasoning = state.get("reasoning") or {}
        if reasoning:
            fields.update(
                {
                    "provider": reasoning.get("provider"),
                    "model": reasoning.get("model"),
                    "usage": reasoning.get("usage") or {},
                }
            )
        recalled = state.get("recalled_memories") or []
        if recalled:
            metadata["recalled_memory_ids"] = [
                str(item.get("memory_id") or item.get("id"))
                for item in recalled
                if item.get("memory_id") or item.get("id")
            ]
        if state.get("action_trace"):
            metadata["action_trace"] = state["action_trace"]
        for key in (
            "plan_payload",
            "reasoning_steps",
            "tool_calls",
            "observations",
            "embedding_profile",
        ):
            value = state.get(key)
            if value:
                metadata[key] = value
        reflected = state.get("reflected_memory") or {}
        if reflected.get("id"):
            fields["reflected_memory_id"] = reflected["id"]
        transition_run_attempt(
            run_id=run_id,
            attempt_id=attempt_id,
            status=status,
            phase=phase,
            summary=_phase_summary(phase, status),
            command=command,
            metadata=metadata,
            fields=fields,
            db_url=db_url,
        )

    def reserve_model_call() -> int:
        return reserve_run_budget(
            run_id=run_id,
            attempt_id=attempt_id,
            command=command,
            budget="model",
            db_url=db_url,
        )

    def reserve_diagnostic_call() -> int:
        return reserve_run_budget(
            run_id=run_id,
            attempt_id=attempt_id,
            command=command,
            budget="cloudwatch",
            db_url=db_url,
        )

    try:
        uses_gemini = any(
            settings.provider_env.get(name, "").strip().lower() == "gemini"
            for name in ("LLM_PROVIDER", "EMBEDDING_PROVIDER")
        )
        gemini_pool = gemini_pool_from_env(settings.provider_env) if uses_gemini else None
        provider = (
            reasoning_provider_from_env(settings.provider_env, gemini_pool=gemini_pool)
            if gemini_pool is not None
            else reasoning_provider_from_env(settings.provider_env)
        )
        if provider.provider_name != "gemini":
            provider = retrying_reasoning_provider(
                provider,
                max_attempts=settings.reasoning_max_attempts,
            )
        embedding_provider = embedding_provider_from_env(
            settings.provider_env,
            gemini_pool=gemini_pool,
        )
        diagnostic_tool = optional_cloudwatch_diagnostics_from_env()
        if command == "start":
            result = run_incident_agent(
                IncidentInput(
                    user_input=run["user_input"],
                    incident_id=run["incident_slug"],
                    namespace=run["namespace"],
                    service_slug=run.get("service_slug"),
                    title=run["incident_slug"],
                    metadata={"retrieval_policy": run.get("retrieval_policy", "semantic_strict")},
                ),
                thread_id=run["thread_id"],
                run_id=run_id,
                decision_id=run["decision_id"],
                pause_before_act=True,
                db_url=db_url,
                reasoning_provider=provider,
                embedding_provider=embedding_provider,
                diagnostic_tool=diagnostic_tool,
                initial_model_call_count=int(run.get("model_call_count") or 0),
                initial_diagnostic_call_count=int(run.get("cloudwatch_call_count") or 0),
                model_call_reservation=reserve_model_call,
                diagnostic_call_reservation=reserve_diagnostic_call,
                progress_callback=progress,
            )
        else:
            result = resume_incident_agent(
                thread_id=run["thread_id"],
                approved=bool(message.get("approved")),
                recommendation_id=str(message.get("recommendation_id") or ""),
                selection_fingerprint=str(message.get("selection_fingerprint") or ""),
                db_url=db_url,
                reasoning_provider=provider,
                embedding_provider=embedding_provider,
                diagnostic_tool=diagnostic_tool,
                model_call_count=int(run.get("model_call_count") or 0),
                diagnostic_call_count=int(run.get("cloudwatch_call_count") or 0),
                model_call_reservation=reserve_model_call,
                diagnostic_call_reservation=reserve_diagnostic_call,
                progress_callback=progress,
            )
    except Exception as exc:
        if _caused_by_pool_exhaustion(exc):
            invalidate_runtime_settings_cache()
        record_run_attempt_failure(
            run_id=run_id,
            attempt_id=attempt_id,
            error_type=type(exc).__name__,
            error_detail=safe_error_detail(exc),
            db_url=db_url,
        )
        raise

    if result.interrupted:
        interrupt_value = result.interrupt or {}
        action_trace = (
            interrupt_value.get("action_trace") if isinstance(interrupt_value, dict) else None
        )
        return finish_run_attempt(
            run_id=run_id,
            attempt_id=attempt_id,
            status="awaiting_approval",
            phase="approval",
            summary="Plan is ready for operator review",
            command=command,
            fields={
                "plan": result.plan,
                "proposed_action": interrupt_value.get("proposed_action")
                if isinstance(interrupt_value, dict)
                else result.proposed_action,
            },
            metadata={"action_trace": action_trace} if action_trace else None,
            db_url=db_url,
        )

    reasoning = result.state.get("reasoning") or {}
    approved = bool(result.state.get("action_approved", False))
    guidance_eligible = bool(result.state.get("guidance_eligible", False))
    status = "completed" if approved else "rejected"
    if not approved:
        summary = "Agent recommendation was rejected"
    else:
        summary = "Agent recommendation was approved and retained as audit-only"
    return finish_run_attempt(
        run_id=run_id,
        attempt_id=attempt_id,
        status=status,
        phase="completion",
        summary=summary,
        command=command,
        fields={
            "plan": result.plan,
            "proposed_action": result.proposed_action,
            "action_approved": approved,
            "provider": reasoning.get("provider"),
            "model": reasoning.get("model"),
            "usage": reasoning.get("usage") or {},
            "reflected_memory_id": result.reflected_memory_id,
        },
        metadata=(
            {
                "action_trace": result.state["action_trace"],
                "guidance_eligible": guidance_eligible,
                "plan_payload": result.state.get("plan_payload") or {},
                "reasoning_steps": result.state.get("reasoning_steps") or [],
                "tool_calls": result.state.get("tool_calls") or [],
                "observations": result.state.get("observations") or [],
                "embedding_profile": result.state.get("embedding_profile") or {},
            }
            if result.state.get("action_trace")
            else None
        ),
        db_url=db_url,
    )


def _phase_summary(phase: str, status: str) -> str:
    summaries = {
        "triage": "Incident context captured",
        "recall": "Relevant memories recalled",
        "plan": "Agent plan generated",
        "diagnostic": "Read-only diagnostic started",
        "approval": "Plan is waiting for operator review",
        "action": "Approved bounded action started",
        "observation": "Read-only diagnostic observation recorded",
        "reflection": "Outcome reflected into long-term memory",
    }
    return summaries.get(phase, f"Agent run entered {status.replace('_', ' ')}")


def _caused_by_pool_exhaustion(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, GeminiPoolExhaustedError):
            return True
        current = current.__cause__ or current.__context__
    return False
