"""SQS worker for durable asynchronous incident-agent runs."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from contextvars import ContextVar
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import boto3
from opentelemetry import trace
from opentelemetry.propagate import extract

from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent
from hindsight.aws import aws_client_config
from hindsight.cloudwatch_diagnostics import optional_cloudwatch_diagnostics_from_env
from hindsight.consolidation import enqueue_consolidation_job, process_consolidation_job
from hindsight.embeddings import embedding_provider_from_env
from hindsight.gemini import GeminiPoolExhaustedError, gemini_pool_from_env
from hindsight.operations import execute_operation, reap_exhausted_operations
from hindsight.observability import structured_event
from hindsight.quarantine import (
    QUARANTINE_INDEX_DEFAULT,
    QUARANTINE_INDEX_ENV,
    QUARANTINE_METRIC_NAMESPACE_DEFAULT,
    QUARANTINE_METRIC_NAMESPACE_ENV,
    QUARANTINE_METRIC_STAGE_ENV,
    persist_quarantine_record,
    quarantine_table_from_env,
    report_quarantine_metrics,
)
from hindsight.reasoning import reasoning_provider_from_env, retrying_reasoning_provider
from hindsight.run_dispatch import dispatch_run_commands
from hindsight.runtime import (
    invalidate_runtime_settings_cache,
    runtime_database_url,
    runtime_settings,
)
from hindsight.runs import (
    RunAttemptBusyError,
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
LOGGER.setLevel(logging.INFO)
_RECORD_OBSERVER: ContextVar[Callable[[Exception | None], None] | None] = ContextVar(
    "worker_record_observer", default=None
)


class TerminalWorkerMessage(ValueError):
    """A deterministic worker envelope that must be quarantined, not retried."""

    def __init__(
        self,
        reason_code: str,
        *,
        work_kind: str = "unknown",
        command: str = "unsupported",
        tenant_id: str | None = None,
        run_id: str | None = None,
        operation_id: str | None = None,
        command_generation: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or reason_code)
        self.reason_code = reason_code
        self.work_kind = work_kind
        self.command = command
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.operation_id = operation_id
        self.command_generation = command_generation


class RawDlqConsumptionRefused(RuntimeError):
    """Raised when a deployed mapping attempts to consume the fallback DLQ."""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process SQS messages with per-record failure reporting."""

    if "command" in event and not event.get("Records"):
        return process_message(event) or {}
    failures = []
    quarantine_table = None
    for record in event.get("Records", []):
        message_id = str(record.get("messageId") or "unknown")
        source_arn = str(record.get("eventSourceARN") or record.get("eventSourceArn") or "")
        attributes = record.get("attributes") or {}
        receive_count = str(attributes.get("ApproximateReceiveCount") or "unknown")
        message: dict[str, Any] = {}
        observed = False
        raw_body = record.get("body")
        if source_arn and source_arn == os.environ.get(RUN_DLQ_ARN_ENV):
            error = RawDlqConsumptionRefused("the raw worker DLQ has no consumer")
            _log_record_result(
                status="failed",
                message=message,
                message_id=message_id,
                receive_count=receive_count,
                source_arn=source_arn,
                context=context,
                error=error,
            )
            failures.append({"itemIdentifier": message_id})
            continue
        try:
            if not isinstance(raw_body, str):
                raise TerminalWorkerMessage("invalid_envelope")
            try:
                parsed = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise TerminalWorkerMessage("malformed_json") from exc
            if not isinstance(parsed, dict):
                raise TerminalWorkerMessage("invalid_envelope")
            message = parsed

            def observe(error: Exception | None) -> None:
                nonlocal observed
                observed = True
                _log_record_result(
                    status="failed" if error is not None else "completed",
                    message=message,
                    message_id=message_id,
                    receive_count=receive_count,
                    source_arn=source_arn,
                    context=context,
                    error=error,
                )

            token = _RECORD_OBSERVER.set(observe)
            try:
                process_message(
                    message,
                    worker_message_id=message_id,
                )
            finally:
                _RECORD_OBSERVER.reset(token)
            if not observed:
                observe(None)
        except TerminalWorkerMessage as exc:
            try:
                if quarantine_table is None:
                    quarantine_table = quarantine_table_from_env()
                write = persist_quarantine_record(
                    table=quarantine_table,
                    source_arn=source_arn,
                    source_message_id=message_id,
                    raw_body=raw_body,
                    reason_code=exc.reason_code,
                    work_kind=exc.work_kind,
                    command=exc.command,
                    receive_count=_receive_count(receive_count),
                    tenant_id=exc.tenant_id,
                    run_id=exc.run_id,
                    operation_id=exc.operation_id,
                    command_generation=exc.command_generation,
                )
            except Exception as persist_error:
                if not observed:
                    _log_record_result(
                        status="failed",
                        message={},
                        message_id=message_id,
                        receive_count=receive_count,
                        source_arn=source_arn,
                        context=context,
                        error=persist_error,
                        command_override=exc.command,
                    )
                failures.append({"itemIdentifier": message_id})
                continue
            _log_record_result(
                status="quarantined",
                message={
                    key: write.item[key]
                    for key in ("tenant_id", "run_id", "operation_id")
                    if key in write.item
                },
                message_id=message_id,
                receive_count=receive_count,
                source_arn=source_arn,
                context=context,
                quarantine_id=str(write.item["quarantine_id"]),
                reason_code=exc.reason_code,
                command_override=exc.command,
            )
        except Exception as exc:
            if not observed:
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
    quarantine_id: str | None = None,
    reason_code: str | None = None,
    command_override: str | None = None,
) -> None:
    record = {
        "event": "worker_record",
        "status": status,
        "command": _logged_command(command_override or message.get("command")),
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
        value = _logged_uuid(message.get(key))
        if value is not None:
            record[key] = value
    if quarantine_id is not None:
        record["quarantine_id"] = quarantine_id
    if reason_code is not None:
        record["reason_code"] = reason_code
    if error is not None:
        record["error_code"] = type(error).__name__
        record["error_detail"] = safe_error_detail(error, max_chars=1000)
        LOGGER.error(structured_event("worker_record", record))
    else:
        LOGGER.info(structured_event("worker_record", record))


def process_message(
    message: dict[str, Any],
    *,
    worker_message_id: str | None = None,
) -> dict[str, Any] | None:
    """Process one start or resume command."""

    if not isinstance(message, dict):
        raise TerminalWorkerMessage("invalid_envelope")
    supplied_tenant = message.get("tenant_id")
    try:
        tenant_id = (
            worker_tenant_id(supplied_tenant)
            if supplied_tenant is not None
            else current_tenant_id()
            or worker_tenant_id(
                os.environ.get("HINDSIGHT_WORKER_TENANT_ID", public_demo_tenant_id())
            )
        )
    except (TypeError, ValueError) as exc:
        raise TerminalWorkerMessage("invalid_envelope") from exc
    configure_tracing_from_env(service_name="hindsight-worker")
    carrier = {"traceparent": str(message.get("traceparent") or "")}
    attributes = {}
    for key, value in (
        ("tenant_id", tenant_id),
        ("run_id", message.get("run_id")),
        ("dispatch_id", message.get("dispatch_id")),
        ("dispatch_attempt_id", message.get("dispatch_attempt_id")),
    ):
        if (logged_value := _logged_uuid(value)) is not None:
            attributes[f"hindsight.{key}"] = logged_value
    with (
        tenant_scope(tenant_id),
        start_span("hindsight.worker.message", attributes, context=extract(carrier)),
    ):
        observer = _RECORD_OBSERVER.get()
        try:
            result = _process_tenant_message(
                message,
                tenant_id=tenant_id,
                worker_message_id=worker_message_id,
            )
        except TerminalWorkerMessage:
            raise
        except Exception as exc:
            if observer is not None:
                observer(exc)
            raise
        if observer is not None:
            observer(None)
        return result


def _process_tenant_message(
    message: dict[str, Any],
    *,
    tenant_id: str,
    worker_message_id: str | None = None,
) -> dict[str, Any] | None:
    configure_tracing_from_env(service_name="hindsight-worker")
    command = str(message.get("command") or "start").strip().lower()
    if command == "dispatch_run_commands":
        return dispatch_run_commands(db_url=runtime_database_url(), limit=100)
    if command == "reap_memory_operations":
        return reap_exhausted_operations(db_url=runtime_database_url())
    if command == "report_quarantine_metrics":
        cloudwatch = boto3.client(
            "cloudwatch",
            region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
            config=aws_client_config(read_timeout=10),
        )
        return report_quarantine_metrics(
            table=quarantine_table_from_env(),
            cloudwatch_client=cloudwatch,
            stage=str(os.environ.get(QUARANTINE_METRIC_STAGE_ENV) or ""),
            index_name=os.environ.get(QUARANTINE_INDEX_ENV, QUARANTINE_INDEX_DEFAULT),
            namespace=os.environ.get(
                QUARANTINE_METRIC_NAMESPACE_ENV,
                QUARANTINE_METRIC_NAMESPACE_DEFAULT,
            ),
        )
    if command == "consolidation":
        try:
            incident_id = _canonical_uuid(message.get("incident_id"), "incident_id")
            source_event_id = _canonical_uuid(message.get("source_event_id"), "source_event_id")
        except ValueError as exc:
            raise TerminalWorkerMessage(
                "invalid_envelope",
                command="consolidation",
                detail="incident_id and source_event_id are required",
            ) from exc
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
        try:
            operation_id = _canonical_uuid(message.get("operation_id"), "operation_id")
        except ValueError as exc:
            raise TerminalWorkerMessage(
                "invalid_envelope",
                command="memory_operation",
                detail="operation_id is required",
            ) from exc

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
            worker_id=f"sqs-worker:{uuid4()}",
            db_url=runtime_database_url(),
        )

    if command not in {"start", "resume"}:
        raise TerminalWorkerMessage(
            "unsupported_command",
            detail="unsupported worker command",
        )
    try:
        run_id = _canonical_uuid(message.get("run_id"), "run_id")
    except ValueError as exc:
        raise TerminalWorkerMessage(
            "invalid_envelope",
            command=command,
            detail="run_id is required",
        ) from exc
    command_generation = message.get("command_generation", 0)
    if type(command_generation) is not int or command_generation < 0:
        raise TerminalWorkerMessage(
            "invalid_envelope",
            command=command,
            detail="command_generation must be a non-negative integer",
        )
    try:
        dispatch_id = _canonical_uuid(message.get("dispatch_id"), "dispatch_id")
        dispatch_attempt_id = _canonical_uuid(
            message.get("dispatch_attempt_id"),
            "dispatch_attempt_id",
        )
    except ValueError as exc:
        raise TerminalWorkerMessage(
            "invalid_envelope",
            command=command,
            detail="dispatch_id and dispatch_attempt_id are required",
        ) from exc
    dispatch_sequence = message.get("dispatch_sequence")
    if type(dispatch_sequence) is not int or dispatch_sequence < 1:
        raise TerminalWorkerMessage(
            "invalid_envelope",
            command=command,
            detail="dispatch_sequence must be a positive integer",
        )
    resolved_worker_message_id = str(worker_message_id or f"direct:{dispatch_attempt_id}").strip()
    if not resolved_worker_message_id:
        raise TerminalWorkerMessage(
            "invalid_envelope",
            command=command,
            detail="worker_message_id must not be blank",
        )

    db_url = runtime_database_url()
    max_attempts = max(1, int(os.environ.get(RUN_MAX_ATTEMPTS_ENV, "3")))
    lease_seconds = max(1, int(os.environ.get(RUN_ATTEMPT_LEASE_SECONDS_ENV, "300")))
    try:
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
    except ValueError as exc:
        raise TerminalWorkerMessage(
            "invalid_envelope",
            command=command,
            detail="run delivery identity is invalid",
        ) from exc
    if claim.outcome == "busy":
        raise RunAttemptBusyError(f"agent run attempt is still live: {run_id}")
    if claim.outcome == "exhausted":
        finalized = finalize_exhausted_run(
            run_id=run_id,
            command=command,
            max_attempts=max_attempts,
            db_url=db_url,
        )
        if finalized is None:
            raise TerminalWorkerMessage("run_not_found", command=command)
        raise TerminalWorkerMessage(
            "run_attempts_exhausted",
            work_kind="run",
            command=command,
            tenant_id=tenant_id,
            run_id=run_id,
            command_generation=command_generation,
        )
    if claim.outcome == "missing":
        raise TerminalWorkerMessage("run_not_found", command=command)
    if claim.outcome == "duplicate":
        if (
            claim.run is not None
            and claim.run.get("status") == "failed"
            and claim.run.get("failure_code") == "RunAttemptsExhausted"
        ):
            raise TerminalWorkerMessage(
                "run_attempts_exhausted",
                work_kind="run",
                command=command,
                tenant_id=tenant_id,
                run_id=run_id,
                command_generation=command_generation,
            )
        return get_run(run_id=run_id, db_url=db_url)
    if claim.run is None or claim.attempt_id is None:
        raise RuntimeError(f"claimed run attempt is incomplete: {run_id}")
    run = claim.run
    attempt_id = claim.attempt_id
    message["attempt_id"] = attempt_id
    if logged_attempt_id := _logged_uuid(attempt_id):
        set_span_attributes(
            trace.get_current_span(),
            {"hindsight.attempt_id": logged_attempt_id},
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
        settings = runtime_settings()
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
                recommendation_id=(
                    str(message["recommendation_id"])
                    if message.get("recommendation_id") is not None
                    else None
                ),
                selection_fingerprint=str(message.get("selection_fingerprint") or ""),
                remediation_action_id=(
                    str(message["remediation_action_id"])
                    if message.get("remediation_action_id") is not None
                    else None
                ),
                observation_fingerprint=(
                    str(message["observation_fingerprint"])
                    if message.get("observation_fingerprint") is not None
                    else None
                ),
                preview_id=(
                    str(message["preview_id"]) if message.get("preview_id") is not None else None
                ),
                preview_fingerprint=(
                    str(message["preview_fingerprint"])
                    if message.get("preview_fingerprint") is not None
                    else None
                ),
                approval_actor=(
                    str(message["actor"]) if message.get("actor") is not None else None
                ),
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
        if int(run.get("worker_attempt_count") or 0) >= max_attempts:
            finalize_exhausted_run(
                run_id=run_id,
                command=command,
                max_attempts=max_attempts,
                attempt_id=attempt_id,
                db_url=db_url,
            )
            raise TerminalWorkerMessage(
                "run_attempts_exhausted",
                work_kind="run",
                command=command,
                tenant_id=tenant_id,
                run_id=run_id,
                command_generation=command_generation,
            ) from exc
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
    action_trace = result.state.get("action_trace") or {}
    remediation = action_trace.get("mode") == "governed_memory_remediation"
    status = "completed" if approved else "rejected"
    if not approved:
        summary = (
            "Governed-memory remediation was rejected"
            if remediation
            else "Agent recommendation was rejected"
        )
    else:
        summary = (
            "Governed-memory remediation completed"
            if remediation
            else "Agent recommendation was approved and retained as audit-only"
        )
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
                "action_trace": action_trace,
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
        "action": "Approved governed-memory action completed",
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


def _receive_count(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _canonical_uuid(value: object, field: str) -> str:
    try:
        normalized = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(value) != normalized:
        raise ValueError(f"{field} must be a canonical UUID")
    return normalized


def _logged_uuid(value: object) -> str | None:
    try:
        return _canonical_uuid(value, "log identity")
    except ValueError:
        return None


def _logged_command(value: object) -> str:
    command = str(value or "start").strip().lower()
    return (
        command
        if command
        in {
            "consolidation",
            "dispatch_run_commands",
            "memory_operation",
            "reap_memory_operations",
            "report_quarantine_metrics",
            "resume",
            "start",
            "unsupported",
        }
        else "unsupported"
    )
