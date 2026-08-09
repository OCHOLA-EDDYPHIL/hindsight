"""LangGraph incident agent with CockroachDB-backed state."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from collections.abc import Callable, Collection
import json
from typing import Any, NotRequired, Protocol, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict
from langchain_cockroachdb import CockroachDBChatMessageHistory, CockroachDBSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from hindsight.db import TenantConnection, connect, database_url, database_url_with_tls_roots
from hindsight.agent_decision import (
    AGENT_DECISION_JSON_SCHEMA,
    MAX_DIAGNOSTIC_CALLS,
    MAX_MODEL_TURNS,
    AgentDecisionError,
    AgentDecisionV1,
    memory_selection_fingerprint,
    parse_agent_decision,
    recommendation_id,
)
from hindsight.embeddings import (
    EmbeddingProvider,
    embedding_profile,
    embedding_provider_from_env,
)
from hindsight.cloudwatch_diagnostics import (
    CloudWatchCallBudget,
    CloudWatchDiagnosticsError,
)
from hindsight.memory import (
    MemorySelectionChangedError,
    MemoryStore,
    Provenance,
    positive_guidance_eligible,
)
from hindsight.reasoning import ReasoningProvider, ReasoningRequest, reasoning_provider_from_env
from hindsight.tracing import memory_ids, set_span_attributes, start_span
from hindsight.tenant import current_tenant_id

AGENT_CHAT_TABLE = "agent_chat_messages"
_AGENT_STORAGE_PROBES = (
    (
        "checkpoint_migrations",
        "SELECT v FROM checkpoint_migrations LIMIT 0",
    ),
    (
        "checkpoints",
        """
            SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                   type, checkpoint, metadata, created_at
            FROM checkpoints LIMIT 0
        """,
    ),
    (
        "checkpoint_blobs",
        """
            SELECT thread_id, checkpoint_ns, channel, version, type, blob, created_at
            FROM checkpoint_blobs LIMIT 0
        """,
    ),
    (
        "checkpoint_writes",
        """
            SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, task_path,
                   idx, channel, type, blob, created_at
            FROM checkpoint_writes LIMIT 0
        """,
    ),
    (
        AGENT_CHAT_TABLE,
        f"""
            SELECT id, session_id, message, created_at
            FROM {AGENT_CHAT_TABLE} LIMIT 0
        """,
    ),
)


class AgentStorageNotInitializedError(RuntimeError):
    """Raised when the agent's durable persistence objects are unavailable."""


class StaleRecommendationError(RuntimeError):
    """Raised when approval does not name the currently interrupted recommendation."""


class DiagnosticTool(Protocol):
    """Read-only diagnostic surface available to model-selected tool calls."""

    name: str

    @property
    def query_keys(self) -> Collection[str]:
        """Return server-configured query identifiers visible to the model."""

    def observe(self, query_key: str, *, budget: CloudWatchCallBudget) -> dict[str, Any]:
        """Run one bounded read-only query and return a JSON-ready observation."""


@dataclass(frozen=True)
class IncidentInput:
    """One incident turn accepted by the agent graph."""

    user_input: str
    incident_id: str
    namespace: str | None = None
    service_slug: str | None = None
    severity: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IncidentAgentResult:
    """Normalized result returned by start/resume helpers."""

    thread_id: str
    interrupted: bool
    interrupt: Any | None
    state: dict[str, Any]
    plan: str | None = None
    proposed_action: str | None = None
    reflected_memory_id: str | None = None


class IncidentAgentState(TypedDict, total=False):
    run_id: str
    thread_id: str
    incident_id: str
    namespace: str
    service_slug: NotRequired[str | None]
    severity: NotRequired[str | None]
    title: NotRequired[str | None]
    user_input: str
    metadata: dict[str, Any]
    pause_before_act: bool
    triage: dict[str, Any]
    chat_messages: list[dict[str, Any]]
    recalled_memories: list[dict[str, Any]]
    recall_error: NotRequired[str | None]
    decision_id: str
    plan: str
    plan_payload: dict[str, Any]
    reasoning: dict[str, Any]
    reasoning_steps: list[dict[str, Any]]
    model_turn_count: int
    tool_calls: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    diagnostic_call_count: int
    embedding_profile: dict[str, Any]
    selection_fingerprint: str
    selection_namespace_revision: int
    approval_namespace_revision: int
    recommendation_id: str
    approval_stale: bool
    proposed_action: str
    action_trace: NotRequired[dict[str, Any]]
    action_approved: bool
    guidance_eligible: bool
    reflected_memory: dict[str, Any]
    retrieval_id: NotRequired[str | None]


ProgressCallback = Callable[[str, str, dict[str, Any]], None]
BudgetReservation = Callable[[], int]


def run_incident_agent(
    incident: IncidentInput,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    pause_before_act: bool = False,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    diagnostic_tool: DiagnosticTool | None = None,
    initial_model_call_count: int = 0,
    initial_diagnostic_call_count: int = 0,
    model_call_reservation: BudgetReservation | None = None,
    diagnostic_call_reservation: BudgetReservation | None = None,
    progress_callback: ProgressCallback | None = None,
) -> IncidentAgentResult:
    """Start or continue an incident thread with a new user incident turn."""

    _ensure_sync_entrypoint()
    _validate_initial_call_count(
        initial_model_call_count,
        limit=MAX_MODEL_TURNS,
        name="initial_model_call_count",
    )
    _validate_initial_call_count(
        initial_diagnostic_call_count,
        limit=MAX_DIAGNOSTIC_CALLS,
        name="initial_diagnostic_call_count",
    )
    resolved_thread_id = thread_id or incident.incident_id or f"incident-{uuid4()}"
    resolved_run_id = run_id or str(uuid4())
    namespace = incident.namespace or incident.incident_id
    initial_state: IncidentAgentState = {
        "run_id": resolved_run_id,
        "thread_id": resolved_thread_id,
        "incident_id": incident.incident_id,
        "namespace": namespace,
        "service_slug": incident.service_slug,
        "severity": incident.severity,
        "title": incident.title,
        "user_input": incident.user_input,
        "metadata": dict(incident.metadata),
        "pause_before_act": pause_before_act,
        "decision_id": decision_id or f"agent:{resolved_run_id}:plan",
        "reasoning_steps": [],
        "model_turn_count": initial_model_call_count,
        "tool_calls": [],
        "observations": [],
        "diagnostic_call_count": initial_diagnostic_call_count,
    }
    state = _invoke_graph(
        initial_state,
        thread_id=resolved_thread_id,
        db_url=db_url,
        reasoning_provider=reasoning_provider,
        embedding_provider=embedding_provider,
        diagnostic_tool=diagnostic_tool,
        model_call_reservation=model_call_reservation,
        diagnostic_call_reservation=diagnostic_call_reservation,
        progress_callback=progress_callback,
    )
    return _agent_result(resolved_thread_id, state)


async def run_incident_agent_async(
    incident: IncidentInput,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    pause_before_act: bool = False,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    diagnostic_tool: DiagnosticTool | None = None,
    initial_model_call_count: int = 0,
    initial_diagnostic_call_count: int = 0,
    model_call_reservation: BudgetReservation | None = None,
    diagnostic_call_reservation: BudgetReservation | None = None,
    progress_callback: ProgressCallback | None = None,
) -> IncidentAgentResult:
    """Async-safe wrapper for starting or continuing an incident thread."""

    return await asyncio.to_thread(
        partial(
            run_incident_agent,
            incident,
            thread_id=thread_id,
            run_id=run_id,
            decision_id=decision_id,
            pause_before_act=pause_before_act,
            db_url=db_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
            diagnostic_tool=diagnostic_tool,
            initial_model_call_count=initial_model_call_count,
            initial_diagnostic_call_count=initial_diagnostic_call_count,
            model_call_reservation=model_call_reservation,
            diagnostic_call_reservation=diagnostic_call_reservation,
            progress_callback=progress_callback,
        )
    )


def resume_incident_agent(
    *,
    thread_id: str,
    approved: bool,
    recommendation_id: str,
    selection_fingerprint: str,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    diagnostic_tool: DiagnosticTool | None = None,
    model_call_count: int | None = None,
    diagnostic_call_count: int | None = None,
    model_call_reservation: BudgetReservation | None = None,
    diagnostic_call_reservation: BudgetReservation | None = None,
    progress_callback: ProgressCallback | None = None,
) -> IncidentAgentResult:
    """Resume an interrupted incident thread in a fresh graph context."""

    _ensure_sync_entrypoint()
    if model_call_count is not None:
        _validate_initial_call_count(
            model_call_count,
            limit=MAX_MODEL_TURNS,
            name="model_call_count",
        )
    if diagnostic_call_count is not None:
        _validate_initial_call_count(
            diagnostic_call_count,
            limit=MAX_DIAGNOSTIC_CALLS,
            name="diagnostic_call_count",
        )
    state = _invoke_graph(
        Command(
            resume={
                "approved": approved,
                "recommendation_id": recommendation_id,
                "selection_fingerprint": selection_fingerprint,
            },
            update={
                **({"model_turn_count": model_call_count} if model_call_count is not None else {}),
                **(
                    {"diagnostic_call_count": diagnostic_call_count}
                    if diagnostic_call_count is not None
                    else {}
                ),
            },
        ),
        thread_id=thread_id,
        db_url=db_url,
        reasoning_provider=reasoning_provider,
        embedding_provider=embedding_provider,
        diagnostic_tool=diagnostic_tool,
        model_call_reservation=model_call_reservation,
        diagnostic_call_reservation=diagnostic_call_reservation,
        progress_callback=progress_callback,
    )
    return _agent_result(thread_id, state)


async def resume_incident_agent_async(
    *,
    thread_id: str,
    approved: bool,
    recommendation_id: str,
    selection_fingerprint: str,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    diagnostic_tool: DiagnosticTool | None = None,
    model_call_count: int | None = None,
    diagnostic_call_count: int | None = None,
    model_call_reservation: BudgetReservation | None = None,
    diagnostic_call_reservation: BudgetReservation | None = None,
    progress_callback: ProgressCallback | None = None,
) -> IncidentAgentResult:
    """Async-safe wrapper for resuming an interrupted incident thread."""

    return await asyncio.to_thread(
        partial(
            resume_incident_agent,
            thread_id=thread_id,
            approved=approved,
            recommendation_id=recommendation_id,
            selection_fingerprint=selection_fingerprint,
            db_url=db_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
            diagnostic_tool=diagnostic_tool,
            model_call_count=model_call_count,
            diagnostic_call_count=diagnostic_call_count,
            model_call_reservation=model_call_reservation,
            diagnostic_call_reservation=diagnostic_call_reservation,
            progress_callback=progress_callback,
        )
    )


def build_incident_graph(
    *,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    diagnostic_tool: DiagnosticTool | None = None,
    model_call_reservation: BudgetReservation | None = None,
    diagnostic_call_reservation: BudgetReservation | None = None,
    progress_callback: ProgressCallback | None = None,
):
    """Build the incident graph; compile it with a checkpointer before use."""

    resolved_db_url = db_url or database_url()
    resolved_embedding_provider = embedding_provider or embedding_provider_from_env()
    resolved_embedding_profile = embedding_profile(resolved_embedding_provider)
    resolved_reasoning_provider = reasoning_provider or reasoning_provider_from_env()

    def triage(state: IncidentAgentState) -> dict[str, Any]:
        history = _chat_history(
            thread_id=state["thread_id"],
            db_url=resolved_db_url,
        )
        try:
            previous_messages = history.messages
            history.add_message(HumanMessage(content=state["user_input"]))
            current_messages = history.messages
        finally:
            history.close()

        triaged = {
            "incident_id": state["incident_id"],
            "namespace": state["namespace"],
            "service_slug": state.get("service_slug"),
            "severity": state.get("severity"),
            "title": state.get("title"),
            "summary": state["user_input"].strip(),
            "prior_chat_messages": len(previous_messages),
        }
        update = {
            "triage": triaged,
            "chat_messages": messages_to_dict(current_messages),
        }
        _report_progress(progress_callback, "triage", "triaging", state, update)
        return update

    def recall(state: IncidentAgentState) -> dict[str, Any]:
        with start_span(
            "hindsight.agent.recall",
            {
                "hindsight.agent.thread_id": state["thread_id"],
                "hindsight.agent.incident_id": state["incident_id"],
                "hindsight.memory.namespace": state["namespace"],
                "hindsight.memory.decision_id": state["decision_id"],
            },
        ) as span:
            update = _recall_for_state(
                state,
                db_url=resolved_db_url,
                embedding_provider=resolved_embedding_provider,
            )
            update["embedding_profile"] = {
                "profile_id": resolved_embedding_profile.profile_id,
                "provider": resolved_embedding_profile.provider,
                "model": resolved_embedding_profile.model,
                "dimensions": resolved_embedding_profile.dimensions,
                "capability": resolved_embedding_profile.capability,
                "encoder_revision": resolved_embedding_profile.encoder_revision,
                "configuration": dict(resolved_embedding_profile.configuration),
                "max_distance": resolved_embedding_profile.max_distance,
            }
            recalled = update.get("recalled_memories", [])
            set_span_attributes(
                span,
                {
                    "hindsight.memory.count": len(recalled),
                    "hindsight.memory.ids": memory_ids(recalled),
                    "hindsight.agent.recall_error": bool(update.get("recall_error")),
                },
            )
            _report_progress(progress_callback, "recall", "recalling", state, update)
            return update

    def decide(state: IncidentAgentState) -> dict[str, Any]:
        selection_fingerprint = memory_selection_fingerprint(state.get("recalled_memories", []))
        with start_span(
            "hindsight.agent.reason",
            {
                "hindsight.agent.thread_id": state["thread_id"],
                "hindsight.agent.incident_id": state["incident_id"],
                "hindsight.memory.namespace": state["namespace"],
                "hindsight.memory.decision_id": state["decision_id"],
                "hindsight.reasoning.provider": resolved_reasoning_provider.provider_name,
                "hindsight.reasoning.model": resolved_reasoning_provider.model_name,
            },
        ) as span:
            decision, response, model_turn_count = _generate_agent_decision(
                state,
                provider=resolved_reasoning_provider,
                allowed_query_keys=set(diagnostic_tool.query_keys) if diagnostic_tool else set(),
                call_reservation=model_call_reservation,
            )
            set_span_attributes(
                span,
                {
                    "hindsight.reasoning.prompt_characters": response.usage.get(
                        "prompt_characters"
                    ),
                    "hindsight.reasoning.system_characters": response.usage.get(
                        "system_characters"
                    ),
                },
            )
        step = {
            "turn": model_turn_count,
            "provider": response.provider,
            "model": response.model,
            "decision": decision.model_dump(mode="json"),
        }
        reasoning_steps = [*state.get("reasoning_steps", []), step]
        plan = _decision_plan_text(decision)
        update = {
            "plan": plan,
            "plan_payload": decision.model_dump(mode="json"),
            "reasoning": {
                "provider": response.provider,
                "model": response.model,
                "usage": {
                    **dict(response.usage),
                    "logical_model_turns": model_turn_count,
                },
            },
            "reasoning_steps": reasoning_steps,
            "model_turn_count": model_turn_count,
            "selection_fingerprint": selection_fingerprint,
            "approval_stale": False,
        }
        if decision.next_step_kind == "recommendation":
            resolved_recommendation_id = recommendation_id(
                run_id=state["run_id"],
                decision=decision,
                selection_fingerprint=selection_fingerprint,
            )
            update.update(
                {
                    "recommendation_id": resolved_recommendation_id,
                    "proposed_action": decision.recommendation or "",
                    "action_trace": _recommendation_trace(
                        state={**state, **update},
                        decision=decision,
                        recommendation_identity=resolved_recommendation_id,
                    ),
                }
            )
        _report_progress(progress_callback, "plan", "planning", state, update)
        return update

    def diagnose(state: IncidentAgentState) -> dict[str, Any]:
        if diagnostic_tool is None:
            raise AgentDecisionError("diagnostic tool is not configured")
        decision = AgentDecisionV1.model_validate(state["plan_payload"])
        if decision.next_step_kind != "diagnostic_tool" or decision.tool_call is None:
            raise AgentDecisionError("diagnostic node received a non-tool decision")
        calls_used = int(state.get("diagnostic_call_count") or 0)
        if not 0 <= calls_used <= MAX_DIAGNOSTIC_CALLS:
            raise AgentDecisionError("diagnostic call count is invalid")
        if diagnostic_call_reservation is None:
            if calls_used >= MAX_DIAGNOSTIC_CALLS:
                raise AgentDecisionError("diagnostic call budget is exhausted")
            call_number = calls_used + 1
        else:
            call_number = diagnostic_call_reservation()
            if not calls_used < call_number <= MAX_DIAGNOSTIC_CALLS:
                raise RuntimeError("diagnostic call reservation returned an invalid count")
        call = {
            "id": f"diagnostic:{state['run_id']}:{call_number}",
            "tool": diagnostic_tool.name,
            "query_key": decision.tool_call.query_key,
            "status": "executing",
        }
        _report_progress(
            progress_callback,
            "diagnostic",
            "planning",
            state,
            {"tool_calls": [*state.get("tool_calls", []), call]},
        )
        budget = CloudWatchCallBudget(initial_used_calls=call_number - 1)
        try:
            tool_observation = diagnostic_tool.observe(
                decision.tool_call.query_key,
                budget=budget,
            )
            observation = {**tool_observation, "status": "available"}
        except CloudWatchDiagnosticsError as exc:
            observation = {
                "schema_version": 1,
                "tool": diagnostic_tool.name,
                "query_key": decision.tool_call.query_key,
                "status": "unavailable",
                "error_code": exc.error_code,
                "detail": str(exc),
            }
            call_status = "failed"
        else:
            call_status = "completed"
        if budget.used_calls != call_number:
            raise RuntimeError("diagnostic tool did not consume exactly one call budget unit")
        completed_call = {**call, "status": call_status}
        recorded_observation = {
            "id": f"observation:{state['run_id']}:{call_number}",
            "tool_call_id": call["id"],
            **observation,
        }
        update = {
            "tool_calls": [*state.get("tool_calls", []), completed_call],
            "observations": [*state.get("observations", []), recorded_observation],
            "diagnostic_call_count": call_number,
        }
        _report_progress(progress_callback, "observation", "planning", state, update)
        return update

    def approve(state: IncidentAgentState) -> dict[str, Any]:
        decision = AgentDecisionV1.model_validate(state["plan_payload"])
        if decision.next_step_kind != "recommendation":
            raise AgentDecisionError("approval node received a non-recommendation decision")
        expected_recommendation_id = state["recommendation_id"]
        expected_selection_fingerprint = state["selection_fingerprint"]
        approval: dict[str, Any] = {
            "approved": False,
            "recommendation_id": expected_recommendation_id,
            "selection_fingerprint": expected_selection_fingerprint,
        }
        if state.get("pause_before_act"):
            _report_progress(
                progress_callback,
                "approval",
                "awaiting_approval",
                state,
                {
                    "proposed_action": decision.recommendation or "",
                    "recommendation_id": expected_recommendation_id,
                    "selection_fingerprint": expected_selection_fingerprint,
                },
            )
            resumed = interrupt(
                {
                    "thread_id": state["thread_id"],
                    "incident_id": state["incident_id"],
                    "proposed_action": decision.recommendation or "",
                    "recommendation_id": expected_recommendation_id,
                    "selection_fingerprint": expected_selection_fingerprint,
                    "action_trace": state.get("action_trace") or {},
                }
            )
            if not isinstance(resumed, dict):
                raise StaleRecommendationError("approval payload must be an object")
            approval = dict(resumed)
        _validate_approval(
            approval,
            recommendation_identity=expected_recommendation_id,
            selection_fingerprint=expected_selection_fingerprint,
        )
        approved = approval["approved"] is True
        action_trace = {
            **(state.get("action_trace") or {}),
            "approval": {
                "approved": approved,
                "disposition": "approved" if approved else "rejected",
                "recommendation_id": expected_recommendation_id,
                "selection_fingerprint": expected_selection_fingerprint,
            },
            "execution": {
                "status": "recommendation_approved" if approved else "not_executed",
                "mode": "recommendation_only",
            },
        }
        if approved:
            refreshed = _recall_for_state(
                state,
                db_url=resolved_db_url,
                embedding_provider=resolved_embedding_provider,
            )
            refreshed_fingerprint = memory_selection_fingerprint(
                refreshed.get("recalled_memories", [])
            )
            if refreshed_fingerprint != expected_selection_fingerprint:
                if int(state.get("model_turn_count") or 0) >= MAX_MODEL_TURNS:
                    raise StaleRecommendationError(
                        "memory selection changed and the model turn budget is exhausted"
                    )
                return {
                    **refreshed,
                    "selection_fingerprint": refreshed_fingerprint,
                    "approval_stale": True,
                    "action_approved": False,
                    "guidance_eligible": False,
                    "action_trace": _stale_approval_trace(action_trace),
                }
        update = {
            "proposed_action": decision.recommendation or "",
            "action_trace": action_trace,
            "action_approved": approved,
            "guidance_eligible": False,
            "approval_stale": False,
            **(
                {"approval_namespace_revision": refreshed["selection_namespace_revision"]}
                if approved
                else {}
            ),
        }
        _report_progress(progress_callback, "approval", "reflecting", state, update)
        return update

    def reflect(state: IncidentAgentState) -> dict[str, Any]:
        with start_span(
            "hindsight.agent.reflect",
            {
                "hindsight.agent.thread_id": state["thread_id"],
                "hindsight.agent.incident_id": state["incident_id"],
                "hindsight.memory.namespace": state["namespace"],
                "hindsight.memory.decision_id": state["decision_id"],
            },
        ) as span:
            content = _reflection_content(state)
            with MemoryStore(
                url=resolved_db_url,
                embedding_provider=resolved_embedding_provider,
            ) as store:
                recalled_memory_ids = [
                    str(row.get("memory_id") or row.get("id"))
                    for row in state.get("recalled_memories", [])
                    if row.get("memory_id") or row.get("id")
                ]
                decision = AgentDecisionV1.model_validate(state["plan_payload"])
                parent_memory_ids = [
                    citation.memory_id for citation in decision.recalled_memory_citations
                ]
                structured_payload = {
                    "schema_version": 1,
                    "thread_id": state["thread_id"],
                    "run_id": state["run_id"],
                    "incident_id": state["incident_id"],
                    "namespace": state["namespace"],
                    "service_slug": state.get("service_slug"),
                    "plan": state.get("plan", "").strip(),
                    "plan_payload": state.get("plan_payload") or {},
                    "proposed_action": state.get("proposed_action", "").strip(),
                    "action_approved": bool(state.get("action_approved")),
                    "guidance_eligible": bool(state.get("guidance_eligible")),
                    "recommendation_id": state.get("recommendation_id"),
                    "selection_fingerprint": state.get("selection_fingerprint"),
                    "retrieval_id": state.get("retrieval_id"),
                    "recalled_memory_ids": recalled_memory_ids,
                    "cited_memory_ids": parent_memory_ids,
                    "reasoning_steps": state.get("reasoning_steps") or [],
                    "tool_calls": state.get("tool_calls") or [],
                    "observations": state.get("observations") or [],
                    "reasoning": state.get("reasoning") or {},
                    "action_trace": state.get("action_trace") or {},
                }
                unsafe_action_count = int(
                    (state.get("action_trace") or {}).get("score", {}).get("unsafe_action_count")
                    or 0
                )
                try:
                    memory = store.remember_agent_reflection(
                        decision_id=state["decision_id"],
                        run_id=state["run_id"],
                        thread_id=state["thread_id"],
                        incident_id=state["incident_id"],
                        namespace=state["namespace"],
                        service_slug=state.get("service_slug"),
                        plan=str(structured_payload["plan"]),
                        proposed_action=str(structured_payload["proposed_action"]),
                        action_approved=bool(structured_payload["action_approved"]),
                        guidance_eligible=bool(structured_payload["guidance_eligible"]),
                        content=content,
                        provenance=Provenance(
                            writer="agent.reflect",
                            source_ref=state["decision_id"],
                            justification=(
                                "Capture incident plan and proposed remediation for future recall"
                            ),
                        ),
                        metadata={
                            "thread_id": state["thread_id"],
                            "incident_id": state["incident_id"],
                            "service_slug": state.get("service_slug"),
                            "recalled_memory_ids": recalled_memory_ids,
                            "cited_memory_ids": parent_memory_ids,
                            "action_approved": state.get("action_approved", False),
                            "operator_disposition": (
                                "approved" if state.get("action_approved") else "rejected"
                            ),
                            "safety_status": ("unsafe" if unsafe_action_count else "unassessed"),
                            "contradiction_status": "unassessed",
                            "usage_instruction": "audit_only",
                            "kind": "incident_reflection",
                            "evidence_quality": "operator_reviewed_recommendation",
                            "action_trace": state.get("action_trace") or {},
                        },
                        structured_payload=structured_payload,
                        parent_memory_ids=parent_memory_ids,
                        expected_namespace_revision=(
                            state.get("approval_namespace_revision")
                            if state.get("action_approved")
                            else None
                        ),
                        require_current_parents=bool(state.get("action_approved")),
                    )
                except MemorySelectionChangedError as exc:
                    if int(state.get("model_turn_count") or 0) >= MAX_MODEL_TURNS:
                        raise StaleRecommendationError(
                            "memory selection changed and the model turn budget is exhausted"
                        ) from exc
                    refreshed = _recall_for_state(
                        state,
                        db_url=resolved_db_url,
                        embedding_provider=resolved_embedding_provider,
                    )
                    return {
                        **refreshed,
                        "selection_fingerprint": memory_selection_fingerprint(
                            refreshed.get("recalled_memories", [])
                        ),
                        "approval_stale": True,
                        "action_approved": False,
                        "guidance_eligible": False,
                        "action_trace": _stale_approval_trace(state.get("action_trace") or {}),
                    }
            history = _chat_history(
                thread_id=state["thread_id"],
                db_url=resolved_db_url,
            )
            try:
                history.add_message(AIMessage(content=content))
            finally:
                history.close()
            set_span_attributes(span, {"hindsight.memory.id": str(memory["id"])})
            update = {"reflected_memory": _jsonable_row(memory), "approval_stale": False}
            _report_progress(progress_callback, "reflection", "reflecting", state, update)
            return update

    builder = StateGraph(IncidentAgentState)
    builder.add_node("triage", triage)
    builder.add_node("recall", recall)
    builder.add_node("decide", decide)
    builder.add_node("diagnose", diagnose)
    builder.add_node("approve", approve)
    builder.add_node("reflect", reflect)
    builder.add_edge(START, "triage")
    builder.add_edge("triage", "recall")
    builder.add_edge("recall", "decide")
    builder.add_conditional_edges(
        "decide",
        lambda state: AgentDecisionV1.model_validate(state["plan_payload"]).next_step_kind,
        {
            "diagnostic_tool": "diagnose",
            "recommendation": "approve",
        },
    )
    builder.add_edge("diagnose", "decide")
    builder.add_conditional_edges(
        "approve",
        lambda state: "replan" if state.get("approval_stale") else "reflect",
        {"replan": "decide", "reflect": "reflect"},
    )
    builder.add_conditional_edges(
        "reflect",
        lambda state: "replan" if state.get("approval_stale") else "done",
        {"replan": "decide", "done": END},
    )
    return builder


def setup_agent_storage(*, db_url: str | None = None) -> None:
    """Create checkpoint and chat-history tables used by the agent runtime."""

    resolved_db_url = db_url or database_url()
    with CockroachDBSaver.from_conn_string(resolved_db_url) as checkpointer:
        checkpointer.setup()
    history = _chat_history(thread_id="setup", db_url=resolved_db_url)
    try:
        history.create_table_if_not_exists()
    finally:
        history.close()


def validate_agent_storage(*, db_url: str | None = None) -> None:
    """Verify runtime persistence objects without attempting schema changes."""

    resolved_db_url = db_url or database_url()
    with connect(
        resolved_db_url,
        application_name="hindsight-agent-storage-check",
    ) as conn:
        for object_name, query in _AGENT_STORAGE_PROBES:
            try:
                conn.execute(query)
            except (errors.UndefinedColumn, errors.UndefinedTable) as exc:
                raise AgentStorageNotInitializedError(
                    "agent persistence storage is not initialized: "
                    f"{object_name} is missing or incompatible; run "
                    "scripts/initialize_agent_storage.py with deployment credentials "
                    "before starting or resuming the agent"
                ) from exc


def _recall_for_state(
    state: IncidentAgentState,
    *,
    db_url: str,
    embedding_provider: EmbeddingProvider,
) -> dict[str, Any]:
    memories: list[dict[str, Any]] = []
    retrieval_id = None
    with MemoryStore(
        url=db_url,
        embedding_provider=embedding_provider,
    ) as store:
        try:
            selection_namespace_revision = store.namespace_revision(namespace=state["namespace"])
            requested_policy = str(
                state.get("metadata", {}).get("retrieval_policy") or "semantic_strict"
            )
            if requested_policy not in {"semantic_strict", "semantic_then_keyword"}:
                raise ValueError(f"unsupported retrieval policy: {requested_policy}")
            result = store.retrieve_semantic(
                namespace=state["namespace"],
                query=state["user_input"],
                decision_id=state["decision_id"],
                reader="agent.recall",
                purpose="retrieve governed incident context",
                policy=requested_policy,  # type: ignore[arg-type]
                positive_guidance_only=True,
            )
            memories = list(result.hits)
            retrieval_id = result.retrieval_id
        except Exception as exc:
            raise RuntimeError(f"governed retrieval failed: {exc}") from exc
    update: dict[str, Any] = {
        "recalled_memories": _jsonable_rows(memories),
        "retrieval_id": retrieval_id,
        "selection_namespace_revision": selection_namespace_revision,
    }
    return update


def _invoke_graph(
    input_or_command: IncidentAgentState | Command,
    *,
    thread_id: str,
    db_url: str | None,
    reasoning_provider: ReasoningProvider | None,
    embedding_provider: EmbeddingProvider | None,
    diagnostic_tool: DiagnosticTool | None,
    model_call_reservation: BudgetReservation | None,
    diagnostic_call_reservation: BudgetReservation | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    resolved_db_url = db_url or database_url()
    validate_agent_storage(db_url=resolved_db_url)
    with _tenant_checkpointer(resolved_db_url) as checkpointer:
        graph = build_incident_graph(
            db_url=resolved_db_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
            diagnostic_tool=diagnostic_tool,
            model_call_reservation=model_call_reservation,
            diagnostic_call_reservation=diagnostic_call_reservation,
            progress_callback=progress_callback,
        ).compile(checkpointer=checkpointer)
        return graph.invoke(
            input_or_command,
            {"configurable": {"thread_id": thread_id}},
        )


def _ensure_sync_entrypoint() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "run_incident_agent and resume_incident_agent are synchronous helpers; "
        "call them outside an active event loop"
    )


def _report_progress(
    callback: ProgressCallback | None,
    phase: str,
    status: str,
    state: IncidentAgentState,
    update: dict[str, Any],
) -> None:
    if callback is not None:
        callback(phase, status, {**state, **update})


def _chat_history(*, thread_id: str, db_url: str) -> CockroachDBChatMessageHistory:
    tenant_id = current_tenant_id()
    if tenant_id is None:
        return CockroachDBChatMessageHistory(
            session_id=thread_id,
            connection_string=_async_sqlalchemy_url(db_url),
            table_name=AGENT_CHAT_TABLE,
        )
    engine = create_async_engine(_async_sqlalchemy_url(db_url))

    @event.listens_for(engine.sync_engine, "begin")
    def bind_tenant(connection: Any) -> None:
        connection.exec_driver_sql(
            "SELECT set_config('hindsight.tenant_id', %s, true)",
            (tenant_id,),
        )

    history = CockroachDBChatMessageHistory(
        session_id=thread_id,
        engine=engine,
        table_name=AGENT_CHAT_TABLE,
    )
    history._owns_engine = True
    return history


class _TenantCockroachDBSaver(CockroachDBSaver):
    """Run each vendor checkpoint cursor inside one tenant-bound transaction."""

    @contextmanager
    def _cursor(self, *, pipeline: bool = False):
        del pipeline
        with self.lock, self.conn.transaction():
            with self.conn.cursor(binary=True, row_factory=dict_row) as cursor:
                yield cursor


@contextmanager
def _tenant_checkpointer(db_url: str):
    tenant_id = current_tenant_id(required=True)
    with psycopg.connect(
        database_url_with_tls_roots(db_url),
        autocommit=False,
        prepare_threshold=5,
        row_factory=dict_row,
    ) as raw_connection:
        connection = TenantConnection(raw_connection, tenant_id=tenant_id)
        yield _TenantCockroachDBSaver(connection)


def _async_sqlalchemy_url(url: str) -> str:
    if url.startswith("cockroachdb+psycopg://"):
        return url
    if url.startswith("cockroachdb://"):
        return url.replace("cockroachdb://", "cockroachdb+psycopg://", 1)
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "cockroachdb+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "cockroachdb+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "cockroachdb+psycopg://", 1)
    return url


def _agent_result(thread_id: str, state: dict[str, Any]) -> IncidentAgentResult:
    interrupts = state.get("__interrupt__") or []
    interrupt_value = None
    if interrupts:
        interrupt_value = getattr(interrupts[0], "value", interrupts[0])
    reflected_memory = state.get("reflected_memory") or {}
    return IncidentAgentResult(
        thread_id=thread_id,
        interrupted=bool(interrupts),
        interrupt=interrupt_value,
        state=state,
        plan=state.get("plan"),
        proposed_action=state.get("proposed_action"),
        reflected_memory_id=(
            str(reflected_memory["id"]) if reflected_memory.get("id") is not None else None
        ),
    )


def _plan_prompt(state: IncidentAgentState) -> str:
    recalled = state.get("recalled_memories", [])
    memory_lines = []
    for idx, memory in enumerate(recalled, start=1):
        envelope = _governed_guidance_envelope(memory)
        memory_lines.append(f"{idx}. {json.dumps(envelope, sort_keys=True)}")
    if not memory_lines:
        memory_lines.append("No prior memories were recalled.")

    triage = state.get("triage", {})
    return "\n".join(
        [
            f"Incident: {triage.get('title') or state.get('title') or state['incident_id']}",
            f"Severity: {triage.get('severity') or state.get('severity') or 'unknown'}",
            f"Service: {triage.get('service_slug') or state.get('service_slug') or 'unknown'}",
            f"Current report: {state['user_input']}",
            "",
            "Recalled memories:",
            *memory_lines,
            "",
            "Use this evidence to produce the next AgentDecisionV1 step.",
        ]
    )


def _governed_guidance_envelope(memory: dict[str, Any]) -> dict[str, Any]:
    metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
    payload = (
        memory.get("structured_payload")
        if isinstance(memory.get("structured_payload"), dict)
        else {}
    )
    action_approved = payload.get("action_approved")
    if isinstance(action_approved, bool):
        disposition = "approved" if action_approved else "rejected"
    else:
        disposition = str(metadata.get("operator_disposition") or "unreviewed")
    trust = str(memory.get("trust_status") or "review_required")
    usage_instruction = "positive_guidance" if positive_guidance_eligible(memory) else "audit_only"
    return {
        "memory_id": str(memory.get("id") or memory.get("memory_id") or ""),
        "belief_id": str(memory.get("belief_id") or ""),
        "version": memory.get("version_number"),
        "content": str(memory.get("memory_content") or memory.get("content") or ""),
        "kind": str(
            metadata.get("kind")
            or (
                "incident_reflection"
                if memory.get("content_schema") == "agent_reflection.v1"
                else "procedural_lesson"
            )
        ),
        "status": "review_required" if trust != "active" else "active",
        "trust": trust,
        "transition": str(memory.get("transition_kind") or "assertion"),
        "operator_disposition": disposition,
        "safety_status": str(metadata.get("safety_status") or "unassessed"),
        "contradiction_status": str(metadata.get("contradiction_status") or "unassessed"),
        "applicability": metadata.get("applicability") or None,
        "evidence_quality": str(metadata.get("evidence_quality") or "provenance_only"),
        "evidence": [
            {
                "writer": str(memory.get("writer") or ""),
                "source_ref": str(memory.get("source_ref") or ""),
                "justification": str(memory.get("justification") or ""),
            }
        ],
        "usage_instruction": usage_instruction,
    }


def _generate_agent_decision(
    state: IncidentAgentState,
    *,
    provider: ReasoningProvider,
    allowed_query_keys: set[str],
    call_reservation: BudgetReservation | None = None,
) -> tuple[AgentDecisionV1, Any, int]:
    """Generate one valid decision with at most one schema-repair turn."""

    starting_turn = int(state.get("model_turn_count") or 0)
    if starting_turn < 0:
        raise AgentDecisionError("model turn count is invalid")
    if starting_turn >= MAX_MODEL_TURNS:
        raise AgentDecisionError("model turn budget is exhausted")
    prompt = _decision_prompt(state, allowed_query_keys=allowed_query_keys)
    attempts = 0
    last_error: AgentDecisionError | None = None
    while attempts < 2 and starting_turn + attempts < MAX_MODEL_TURNS:
        attempts += 1
        logical_turn = (
            call_reservation() if call_reservation is not None else starting_turn + attempts
        )
        if not starting_turn < logical_turn <= MAX_MODEL_TURNS:
            raise RuntimeError("model call reservation returned an invalid count")
        request_prompt = prompt
        if attempts == 2:
            request_prompt = (
                f"{prompt}\n\n"
                "The prior response failed AgentDecisionV1 validation. Return only one "
                "JSON object matching the supplied schema. Do not add markdown or commentary."
            )
        response = provider.generate(
            ReasoningRequest(
                system=(
                    "You are Hindsight, an incident-response copilot. Use only memories "
                    "whose usage_instruction is positive_guidance as recommendations. "
                    "Audit-only memories may support diagnosis but must never direct a next "
                    "step. Diagnostic tools are read-only. You cannot execute remediation. "
                    "Every recalled-memory citation quote must be a verbatim excerpt. "
                    "Every recommendation must be reversible, verifiable, and suitable for "
                    "operator review. Return only AgentDecisionV1 JSON."
                ),
                prompt=request_prompt,
                temperature=0,
                max_output_tokens=1_024,
                routing_key=f"{state['decision_id']}:turn:{logical_turn}",
                response_json_schema=AGENT_DECISION_JSON_SCHEMA,
            )
        )
        try:
            decision = parse_agent_decision(
                response.text,
                recalled_memory_ids={
                    str(memory.get("memory_id") or memory.get("id"))
                    for memory in state.get("recalled_memories", [])
                    if memory.get("memory_id") or memory.get("id")
                },
                recalled_memory_text={
                    str(memory.get("memory_id") or memory.get("id")): str(
                        memory.get("memory_content") or memory.get("content") or ""
                    )
                    for memory in state.get("recalled_memories", [])
                    if memory.get("memory_id") or memory.get("id")
                },
                allowed_query_keys=allowed_query_keys,
                diagnostic_calls_used=int(state.get("diagnostic_call_count") or 0),
                diagnostic_observation_available=_has_current_diagnostic_observation(state),
                model_turn=logical_turn,
            )
        except AgentDecisionError as exc:
            last_error = exc
            continue
        usage = dict(response.usage)
        usage["response_sha256"] = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
        normalized_response = type(response)(
            text=response.text,
            provider=response.provider,
            model=response.model,
            usage=usage,
        )
        return decision, normalized_response, logical_turn
    raise AgentDecisionError("model did not produce a valid bounded decision") from last_error


def _validate_initial_call_count(value: int, *, limit: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError(f"{name} must be between zero and {limit}")


def _has_current_diagnostic_observation(state: IncidentAgentState) -> bool:
    for observation in state.get("observations", []):
        datapoint_count = observation.get("datapoint_count")
        if (
            observation.get("status") == "available"
            and type(datapoint_count) is int
            and datapoint_count > 0
        ):
            return True
    return False


def _decision_prompt(
    state: IncidentAgentState,
    *,
    allowed_query_keys: set[str],
) -> str:
    observations = state.get("observations", [])
    remaining_turns = MAX_MODEL_TURNS - int(state.get("model_turn_count") or 0)
    query_keys = sorted(allowed_query_keys)
    return "\n".join(
        [
            _plan_prompt(state),
            "",
            "Recorded diagnostic observations:",
            json.dumps(observations, sort_keys=True, default=str) if observations else "None.",
            "",
            f"Configured CloudWatch query keys: {json.dumps(query_keys)}",
            f"Remaining logical model turns including this turn: {remaining_turns}",
            (
                "When configured query keys are available and there is no current observation, "
                "choose the most relevant diagnostic_tool before recommending. Otherwise choose "
                "diagnostic_tool only when another configured observation is necessary. The final "
                "available turn must recommend."
            ),
        ]
    )


def _decision_plan_text(decision: AgentDecisionV1) -> str:
    verification = "; ".join(decision.verification)
    safety = "; ".join(decision.safety_constraints)
    action = decision.recommendation or (
        f"Read configured CloudWatch query {decision.tool_call.query_key}"
        if decision.tool_call is not None
        else "No next step"
    )
    return "\n".join(
        [
            f"Cause: {decision.diagnosis}",
            f"Checks: {verification}",
            f"Action: {action}",
            f"Safety: {safety}",
        ]
    )


def _recommendation_trace(
    *,
    state: IncidentAgentState,
    decision: AgentDecisionV1,
    recommendation_identity: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "mode": "recommendation_only",
        "selection": {
            "fingerprint": state["selection_fingerprint"],
            "memory_ids": [
                str(memory.get("memory_id") or memory.get("id"))
                for memory in state.get("recalled_memories", [])
                if memory.get("memory_id") or memory.get("id")
            ],
            "provider": (state.get("reasoning") or {}).get("provider"),
            "model": (state.get("reasoning") or {}).get("model"),
        },
        "reasoning_steps": state.get("reasoning_steps", []),
        "tool_calls": state.get("tool_calls", []),
        "observations": state.get("observations", []),
        "recommendation": {
            "id": recommendation_identity,
            "summary": decision.recommendation,
            "diagnosis": decision.diagnosis,
            "rationale": decision.rationale,
            "rollback": decision.rollback,
            "verification": decision.verification,
            "safety_constraints": decision.safety_constraints,
            "status": "awaiting_approval",
        },
        "execution": {
            "status": "awaiting_approval",
            "mode": "recommendation_only",
        },
    }


def _validate_approval(
    approval: dict[str, Any],
    *,
    recommendation_identity: str,
    selection_fingerprint: str,
) -> None:
    required = {"approved", "recommendation_id", "selection_fingerprint"}
    if set(approval) != required or not isinstance(approval.get("approved"), bool):
        raise StaleRecommendationError("approval payload does not match the required contract")
    if approval["recommendation_id"] != recommendation_identity:
        raise StaleRecommendationError("recommendation identity changed before approval")
    if approval["selection_fingerprint"] != selection_fingerprint:
        raise StaleRecommendationError("memory selection changed before approval")


def _stale_approval_trace(action_trace: dict[str, Any]) -> dict[str, Any]:
    approval = action_trace.get("approval") or {}
    return {
        **action_trace,
        "approval": {
            **approval,
            "approved": False,
            "disposition": "stale",
        },
        "execution": {
            "status": "replan_required",
            "mode": "recommendation_only",
        },
    }


def _reflection_content(state: IncidentAgentState) -> str:
    if not state.get("action_approved"):
        status = "not approved"
    else:
        status = "approved as an unobserved recommendation retained for audit"
    return (
        f"Incident {state['incident_id']} plan: {state.get('plan', '').strip()} "
        f"Proposed action ({status}): {state.get('proposed_action', '').strip()}"
    )


def _jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_jsonable_row(row) for row in rows]


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    converted = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            converted[key] = value.isoformat()
        else:
            converted[key] = value
    return converted
