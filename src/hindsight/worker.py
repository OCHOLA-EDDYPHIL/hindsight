"""SQS worker for durable asynchronous incident-agent runs."""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent
from hindsight.consolidation import enqueue_consolidation_job, process_consolidation_job
from hindsight.embeddings import embedding_provider_from_env
from hindsight.gemini import GeminiPoolExhaustedError, gemini_pool_from_env
from hindsight.operations import execute_operation, reap_exhausted_operations
from hindsight.reasoning import reasoning_provider_from_env, retrying_reasoning_provider
from hindsight.runtime import (
    invalidate_runtime_settings_cache,
    runtime_database_url,
    runtime_settings,
)
from hindsight.runs import claim_run, fail_run, get_run, transition_run
from hindsight.security import safe_error_detail
from hindsight.tracing import configure_tracing_from_env

WORKER_MAX_RECEIVES_ENV = "HINDSIGHT_WORKER_MAX_RECEIVES"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process SQS messages with per-record failure reporting."""

    if "command" in event and not event.get("Records"):
        return process_message(event) or {}
    failures = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId") or "unknown")
        attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
        try:
            message = json.loads(record.get("body") or "{}")
            process_message(message, attempt=attempt)
        except Exception:
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def process_message(message: dict[str, Any], *, attempt: int = 1) -> dict[str, Any] | None:
    """Process one start or resume command."""

    configure_tracing_from_env(service_name="hindsight-worker")
    command = str(message.get("command") or "start").strip().lower()
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
                settings.provider_env.get("EMBEDDING_PROVIDER", "").strip().lower()
                == "gemini"
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

    settings = runtime_settings()
    db_url = settings.database_url
    expected_status = "queued" if command == "start" else "resuming"
    claimed_status = "triaging" if command == "start" else "reflecting"
    run = claim_run(
        run_id=run_id,
        expected_status=expected_status,
        next_status=claimed_status,
        db_url=db_url,
    )
    if run is None:
        return get_run(run_id=run_id, db_url=db_url)

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

    def progress(phase: str, status: str, state: dict[str, Any]) -> None:
        if command == "resume" and phase == "approval":
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
        reflected = state.get("reflected_memory") or {}
        if reflected.get("id"):
            fields["reflected_memory_id"] = reflected["id"]
        transition_run(
            run_id=run_id,
            status=status,
            phase=phase,
            summary=_phase_summary(phase, status),
            metadata=metadata,
            fields=fields,
            db_url=db_url,
        )

    try:
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
                progress_callback=progress,
            )
        else:
            result = resume_incident_agent(
                thread_id=run["thread_id"],
                approved=bool(message.get("approved")),
                db_url=db_url,
                reasoning_provider=provider,
                embedding_provider=embedding_provider,
                progress_callback=progress,
            )
    except Exception as exc:
        if _caused_by_pool_exhaustion(exc):
            invalidate_runtime_settings_cache()
        max_receives = max(1, int(os.environ.get(WORKER_MAX_RECEIVES_ENV, "3")))
        if attempt < max_receives:
            transition_run(
                run_id=run_id,
                status=expected_status,
                phase="retry",
                summary=f"Agent run will retry after attempt {attempt}",
                metadata={"attempt": attempt, "error_type": type(exc).__name__},
                db_url=db_url,
            )
        else:
            fail_run(
                run_id=run_id,
                failure_code=type(exc).__name__,
                failure_detail=safe_error_detail(exc),
                db_url=db_url,
            )
        raise

    if result.interrupted:
        interrupt_value = result.interrupt or {}
        return transition_run(
            run_id=run_id,
            status="awaiting_approval",
            phase="approval",
            summary="Plan is ready for operator review",
            fields={
                "plan": result.plan,
                "proposed_action": interrupt_value.get("proposed_action")
                if isinstance(interrupt_value, dict)
                else result.proposed_action,
            },
            db_url=db_url,
        )

    reasoning = result.state.get("reasoning") or {}
    approved = bool(result.state.get("action_approved", True))
    status = "completed" if approved else "rejected"
    return transition_run(
        run_id=run_id,
        status=status,
        phase="completion",
        summary="Agent run completed" if approved else "Agent recommendation was rejected",
        fields={
            "plan": result.plan,
            "proposed_action": result.proposed_action,
            "action_approved": approved,
            "provider": reasoning.get("provider"),
            "model": reasoning.get("model"),
            "usage": reasoning.get("usage") or {},
            "reflected_memory_id": result.reflected_memory_id,
        },
        db_url=db_url,
    )


def _phase_summary(phase: str, status: str) -> str:
    summaries = {
        "triage": "Incident context captured",
        "recall": "Relevant memories recalled",
        "plan": "Agent plan generated",
        "approval": "Plan is waiting for operator review",
        "action": "Operator decision recorded",
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
