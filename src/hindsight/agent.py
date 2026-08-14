"""LangGraph incident agent with CockroachDB-backed state."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from collections.abc import Callable, Collection
import json
from typing import Any, NotRequired, Protocol, TypedDict
from uuid import UUID, uuid4

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
from hindsight.causal_evidence import (
    GOVERNED_MEMORY_PROMPT_MARKER,
    build_causal_envelope,
    canonical_sha256,
    text_sha256,
)
from hindsight.causal_projection import public_causal_envelope
from hindsight.agent_decision import (
    MAX_DIAGNOSTIC_CALLS,
    MAX_MODEL_TURNS,
    PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    AgentDecisionError,
    AgentDecisionV2,
    AgentDecisionV3,
    agent_decision_from_payload,
    agent_decision_provider_schema,
    controlled_action_selection_from_decision,
    controlled_action_selection_provider_schema,
    controlled_decision_from_selection,
    diagnostic_observation_fingerprint,
    memory_selection_fingerprint,
    normalize_agent_decision_provider_text,
    operational_action_fingerprint,
    operational_action_catalog,
    operational_action_directive,
    parse_agent_decision,
    parse_controlled_action_selection,
    recommendation_id,
    remediation_action_id,
)
from hindsight.demo_state import (
    DEMO_INPUT,
    DEMO_NAMESPACE,
    DEMO_SERVICE_SLUG,
    signature_replay_context,
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
from hindsight.operations import (
    OperationConflictError,
    enqueue_operation,
    execute_operation,
    preview_retraction,
)
from hindsight.reasoning import ReasoningProvider, ReasoningRequest, reasoning_provider_from_env
from hindsight.tracing import memory_ids, set_span_attributes, start_span
from hindsight.tenant import current_tenant_id

AGENT_CHAT_TABLE = "agent_chat_messages"
PAYMENTS_REPLAY_DIAGNOSTIC_QUERY_KEY = "payments.retry_fanout"
TRIAGE_PROMPT_TEMPLATE_ID = "hindsight.incident-triage.v1"
DECISION_PROMPT_TEMPLATE_ID = "hindsight.incident-decision.v3"
TRIAGE_PROMPT_TEMPLATE = (
    "incident_id|namespace|service_slug|severity|title|normalized_user_incident|"
    "prior_chat_message_count"
)
DECISION_PROMPT_TEMPLATE = (
    "incident|severity|service|current_report|ordered_governed_memories|"
    "ordered_diagnostic_observations|allowed_query_keys|remaining_turns|action_contract"
)
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


class RemediationActionError(RuntimeError):
    """Raised when an approved governed-memory action cannot complete safely."""


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
    remediation_action_id: str
    observation_fingerprint: str
    action_preview: dict[str, Any]
    approval_actor: str
    stale_replan_count: int
    operation_result: dict[str, Any]
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
    initial_state = _new_incident_state(
        incident,
        thread_id=resolved_thread_id,
        run_id=resolved_run_id,
        decision_id=decision_id or f"agent:{resolved_run_id}:plan",
        pause_before_act=pause_before_act,
        model_call_count=initial_model_call_count,
        diagnostic_call_count=initial_diagnostic_call_count,
    )
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


def _new_incident_state(
    incident: IncidentInput,
    *,
    thread_id: str,
    run_id: str,
    decision_id: str,
    pause_before_act: bool,
    model_call_count: int,
    diagnostic_call_count: int,
) -> IncidentAgentState:
    """Return a complete state replacement for a new run on any thread."""

    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "incident_id": incident.incident_id,
        "namespace": incident.namespace or incident.incident_id,
        "service_slug": incident.service_slug,
        "severity": incident.severity,
        "title": incident.title,
        "user_input": incident.user_input,
        "metadata": dict(incident.metadata),
        "pause_before_act": pause_before_act,
        "triage": {},
        "chat_messages": [],
        "recalled_memories": [],
        "recall_error": None,
        "decision_id": decision_id,
        "plan": "",
        "plan_payload": {},
        "reasoning": {},
        "reasoning_steps": [],
        "model_turn_count": model_call_count,
        "tool_calls": [],
        "observations": [],
        "diagnostic_call_count": diagnostic_call_count,
        "embedding_profile": {},
        "selection_fingerprint": "",
        "selection_namespace_revision": 0,
        "approval_namespace_revision": 0,
        "recommendation_id": "",
        "remediation_action_id": "",
        "observation_fingerprint": "",
        "action_preview": {},
        "approval_actor": "",
        "stale_replan_count": 0,
        "operation_result": {},
        "approval_stale": False,
        "proposed_action": "",
        "action_trace": {},
        "action_approved": False,
        "guidance_eligible": False,
        "reflected_memory": {},
        "retrieval_id": None,
    }


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
    recommendation_id: str | None = None,
    selection_fingerprint: str = "",
    remediation_action_id: str | None = None,
    observation_fingerprint: str | None = None,
    preview_id: str | None = None,
    preview_fingerprint: str | None = None,
    approval_actor: str | None = None,
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
    approval_payload: dict[str, Any]
    if remediation_action_id is not None:
        approval_payload = {
            "approved": approved,
            "remediation_action_id": remediation_action_id,
            "selection_fingerprint": selection_fingerprint,
            "observation_fingerprint": observation_fingerprint,
            "preview_id": preview_id,
            "preview_fingerprint": preview_fingerprint,
            "actor": approval_actor,
        }
    else:
        approval_payload = {
            "approved": approved,
            "recommendation_id": recommendation_id or "",
            "selection_fingerprint": selection_fingerprint,
        }
    state = _invoke_graph(
        Command(
            resume=approval_payload,
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
    recommendation_id: str | None = None,
    selection_fingerprint: str = "",
    remediation_action_id: str | None = None,
    observation_fingerprint: str | None = None,
    preview_id: str | None = None,
    preview_fingerprint: str | None = None,
    approval_actor: str | None = None,
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
            remediation_action_id=remediation_action_id,
            observation_fingerprint=observation_fingerprint,
            preview_id=preview_id,
            preview_fingerprint=preview_fingerprint,
            approval_actor=approval_actor,
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
        observation_fingerprint = diagnostic_observation_fingerprint(state.get("observations", []))
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
        response_usage = dict(response.usage)
        request_contract = response_usage.pop("request_contract", None)
        request_contracts = response_usage.pop("request_contracts", None)
        step = {
            "turn": model_turn_count,
            "provider": response.provider,
            "model": response.model,
            "request": request_contract,
            "requests": (
                request_contracts
                if isinstance(request_contracts, list)
                else [request_contract]
                if isinstance(request_contract, dict)
                else []
            ),
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
                    **response_usage,
                    "logical_model_turns": model_turn_count,
                },
            },
            "reasoning_steps": reasoning_steps,
            "model_turn_count": model_turn_count,
            "selection_fingerprint": selection_fingerprint,
            "observation_fingerprint": observation_fingerprint,
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
                    "proposed_action": _recommendation_action_text(decision),
                    "action_trace": _recommendation_trace(
                        state={**state, **update},
                        decision=decision,
                        recommendation_identity=resolved_recommendation_id,
                    ),
                }
            )
        elif decision.next_step_kind == "remediation_action":
            assert decision.remediation_action is not None
            resolved_action_id = remediation_action_id(
                run_id=state["run_id"],
                decision=decision,
                selection_fingerprint=selection_fingerprint,
                observation_fingerprint=observation_fingerprint,
            )
            update.update(
                {
                    "remediation_action_id": resolved_action_id,
                    "proposed_action": decision.remediation_action.reason,
                }
            )
        _report_progress(progress_callback, "plan", "planning", state, update)
        return update

    def prepare_action(state: IncidentAgentState) -> dict[str, Any]:
        decision = agent_decision_from_payload(state["plan_payload"])
        if decision.next_step_kind != "remediation_action":
            raise AgentDecisionError("action preparation received a non-action decision")
        action = decision.remediation_action
        assert action is not None
        target = next(
            (
                memory
                for memory in state.get("recalled_memories", [])
                if str(memory.get("memory_id") or memory.get("id")) == action.target_memory_id
            ),
            None,
        )
        if target is None or str(target.get("namespace") or "") != state["namespace"]:
            raise AgentDecisionError(
                "remediation target is not a current same-namespace recalled memory"
            )
        citation = next(
            (
                item
                for item in decision.recalled_memory_citations
                if item.memory_id == action.target_memory_id
            ),
            None,
        )
        if citation is None:
            raise AgentDecisionError("remediation target must be cited verbatim")
        preview = preview_retraction(
            root_memory_id=action.target_memory_id,
            actor=f"agent.run:{state['run_id']}",
            reason=action.reason,
            authorized_namespaces=[state["namespace"]],
            db_url=resolved_db_url,
        )
        approved_effects = _bounded_retraction_effects(preview)
        prepared = _jsonable_row(preview)
        trace = _remediation_action_trace(
            state=state,
            decision=decision,
            action_identity=state["remediation_action_id"],
            target_excerpt=citation.quote,
            preview=prepared,
            approved_effects=approved_effects,
        )
        update = {
            "action_preview": prepared,
            "action_trace": trace,
            "proposed_action": action.reason,
        }
        _report_progress(progress_callback, "plan", "planning", state, update)
        return update

    def diagnose(state: IncidentAgentState) -> dict[str, Any]:
        if diagnostic_tool is None:
            raise AgentDecisionError("diagnostic tool is not configured")
        decision = agent_decision_from_payload(state["plan_payload"])
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
        replay_context = _controlled_replay_context(state)
        controlled_contract = _operational_action_contract(state)
        if controlled_contract is not None and replay_context is None:
            raise AgentDecisionError("controlled replay requires a persisted replay anchor")
        diagnostic_identity = (
            replay_context["scenario_routing_key"]
            if replay_context is not None
            else state["run_id"]
        )
        call = {
            "id": f"diagnostic:{diagnostic_identity}:{call_number}",
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
            if replay_context is not None:
                anchored_observer = getattr(
                    diagnostic_tool,
                    "observe_at_replay_anchor",
                    None,
                )
                if not callable(anchored_observer):
                    raise AgentDecisionError(
                        "controlled replay diagnostic does not support anchored reads"
                    )
                replay_anchor = datetime.fromisoformat(
                    replay_context["replay_anchor"].replace("Z", "+00:00")
                )
                tool_observation = anchored_observer(
                    decision.tool_call.query_key,
                    budget=budget,
                    replay_anchor=replay_anchor,
                )
            else:
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
            "id": f"observation:{diagnostic_identity}:{call_number}",
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
        decision = agent_decision_from_payload(state["plan_payload"])
        if decision.next_step_kind not in {"recommendation", "remediation_action"}:
            raise AgentDecisionError("approval node received a non-terminal decision")
        is_action = decision.next_step_kind == "remediation_action"
        expected_selection_fingerprint = state["selection_fingerprint"]
        if is_action:
            action = decision.remediation_action
            assert action is not None
            preview = state.get("action_preview") or {}
            expected_action_id = state["remediation_action_id"]
            expected_observation_fingerprint = state["observation_fingerprint"]
            approval: dict[str, Any] = {
                "approved": False,
                "remediation_action_id": expected_action_id,
                "selection_fingerprint": expected_selection_fingerprint,
                "observation_fingerprint": expected_observation_fingerprint,
                "preview_id": str(preview.get("id") or ""),
                "preview_fingerprint": str(preview.get("fingerprint") or ""),
                "actor": "agent:no_operator",
            }
            proposed_action = action.reason
        else:
            expected_recommendation_id = state["recommendation_id"]
            approval = {
                "approved": False,
                "recommendation_id": expected_recommendation_id,
                "selection_fingerprint": expected_selection_fingerprint,
            }
            proposed_action = _recommendation_action_text(decision)
        if state.get("pause_before_act"):
            approval_identity = (
                {
                    "remediation_action_id": expected_action_id,
                    "selection_fingerprint": expected_selection_fingerprint,
                    "observation_fingerprint": expected_observation_fingerprint,
                    "preview_id": str(preview.get("id") or ""),
                    "preview_fingerprint": str(preview.get("fingerprint") or ""),
                }
                if is_action
                else {
                    "recommendation_id": expected_recommendation_id,
                    "selection_fingerprint": expected_selection_fingerprint,
                }
            )
            _report_progress(
                progress_callback,
                "approval",
                "awaiting_approval",
                state,
                {
                    "proposed_action": proposed_action,
                    **approval_identity,
                },
            )
            resumed = interrupt(
                {
                    "thread_id": state["thread_id"],
                    "incident_id": state["incident_id"],
                    "proposed_action": proposed_action,
                    **approval_identity,
                    "action_trace": state.get("action_trace") or {},
                }
            )
            if not isinstance(resumed, dict):
                raise StaleRecommendationError("approval payload must be an object")
            approval = dict(resumed)
        if is_action:
            _validate_action_approval(
                approval,
                action_identity=expected_action_id,
                selection_fingerprint=expected_selection_fingerprint,
                observation_fingerprint=expected_observation_fingerprint,
                preview_id=str(preview.get("id") or ""),
                preview_fingerprint=str(preview.get("fingerprint") or ""),
            )
        else:
            _validate_approval(
                approval,
                recommendation_identity=expected_recommendation_id,
                selection_fingerprint=expected_selection_fingerprint,
            )
        approved = approval["approved"] is True
        if is_action:
            action_trace = {
                **(state.get("action_trace") or {}),
                "approval": {
                    "approved": approved,
                    "disposition": "approved" if approved else "rejected",
                    "actor": approval["actor"],
                    "remediation_action_id": expected_action_id,
                    "selection_fingerprint": expected_selection_fingerprint,
                    "observation_fingerprint": expected_observation_fingerprint,
                    "preview_id": str(preview.get("id") or ""),
                    "preview_fingerprint": str(preview.get("fingerprint") or ""),
                },
                "execution": {
                    "status": "approved" if approved else "not_executed",
                    "mode": "governed_memory_remediation",
                },
            }
        else:
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
                if is_action:
                    return _stale_action_replan(
                        state=state,
                        refreshed=refreshed,
                        action_trace=action_trace,
                        detail="memory selection changed after approval",
                    )
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
            "proposed_action": proposed_action,
            "action_trace": action_trace,
            "action_approved": approved,
            "guidance_eligible": False,
            "approval_stale": False,
            **({"approval_actor": approval["actor"]} if is_action else {}),
            **(
                {"approval_namespace_revision": refreshed["selection_namespace_revision"]}
                if approved
                else {}
            ),
        }
        _report_progress(progress_callback, "approval", "reflecting", state, update)
        return update

    def execute_action(state: IncidentAgentState) -> dict[str, Any]:
        decision = agent_decision_from_payload(state["plan_payload"])
        if decision.next_step_kind != "remediation_action" or not state.get("action_approved"):
            raise RemediationActionError("remediation execution requires explicit approval")
        preview = state.get("action_preview") or {}
        tenant_id = current_tenant_id(required=True)
        identity_payload = {
            "tenant_id": tenant_id,
            "run_id": state["run_id"],
            "remediation_action_id": state["remediation_action_id"],
            "selection_fingerprint": state["selection_fingerprint"],
            "observation_fingerprint": state["observation_fingerprint"],
            "preview_id": str(preview.get("id") or ""),
            "preview_fingerprint": str(preview.get("fingerprint") or ""),
            "approval_actor": state["approval_actor"],
        }
        idempotency_key = (
            "agent-remediation:"
            + hashlib.sha256(
                json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        )
        try:
            operation, _ = enqueue_operation(
                preview_id=identity_payload["preview_id"],
                fingerprint=identity_payload["preview_fingerprint"],
                idempotency_key=idempotency_key,
                actor=state["approval_actor"],
                db_url=resolved_db_url,
            )
            result = execute_operation(
                operation_id=str(operation["id"]),
                embedding_provider=resolved_embedding_provider,
                worker_id=f"agent-run:{state['run_id']}",
                db_url=resolved_db_url,
            )
        except OperationConflictError:
            refreshed = _recall_for_state(
                state,
                db_url=resolved_db_url,
                embedding_provider=resolved_embedding_provider,
            )
            return _stale_action_replan(
                state=state,
                refreshed=refreshed,
                action_trace=state.get("action_trace") or {},
                detail="governed-memory preview became stale before execution",
            )
        if result.get("status") == "conflict":
            refreshed = _recall_for_state(
                state,
                db_url=resolved_db_url,
                embedding_provider=resolved_embedding_provider,
            )
            return _stale_action_replan(
                state=state,
                refreshed=refreshed,
                action_trace=state.get("action_trace") or {},
                detail="governed-memory state changed before execution",
            )
        if result.get("status") != "completed":
            raise RemediationActionError(
                f"governed-memory operation did not complete: {result.get('status')}"
            )
        events = _jsonable_rows(list(result.get("events") or []))
        effects = _jsonable_rows(list(result.get("effects") or []))
        action_trace = {
            **(state.get("action_trace") or {}),
            "execution": {
                "status": "completed",
                "mode": "governed_memory_remediation",
                "operation_id": str(result["id"]),
                "operation_status": str(result["status"]),
                "events": events,
                "effects": effects,
            },
        }
        update = {
            "operation_result": {
                "id": str(result["id"]),
                "status": str(result["status"]),
                "invalidated_memory_ids": list(result.get("invalidated_memory_ids") or []),
                "restored_memory_ids": list(result.get("restored_memory_ids") or []),
            },
            "action_trace": action_trace,
            "action_approved": True,
            "guidance_eligible": False,
            "approval_stale": False,
        }
        _report_progress(progress_callback, "action", "reflecting", state, update)
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
                decision = agent_decision_from_payload(state["plan_payload"])
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
    builder.add_node("prepare_action", prepare_action)
    builder.add_node("approve", approve)
    builder.add_node("execute_action", execute_action)
    builder.add_node("reflect", reflect)
    builder.add_edge(START, "triage")
    builder.add_edge("triage", "recall")
    builder.add_edge("recall", "decide")
    builder.add_conditional_edges(
        "decide",
        lambda state: agent_decision_from_payload(state["plan_payload"]).next_step_kind,
        {
            "diagnostic_tool": "diagnose",
            "recommendation": "approve",
            "remediation_action": "prepare_action",
        },
    )
    builder.add_edge("diagnose", "decide")
    builder.add_edge("prepare_action", "approve")
    builder.add_conditional_edges(
        "approve",
        lambda state: (
            "replan"
            if state.get("approval_stale")
            else "execute"
            if agent_decision_from_payload(state["plan_payload"]).next_step_kind
            == "remediation_action"
            and state.get("action_approved")
            else "done"
            if agent_decision_from_payload(state["plan_payload"]).next_step_kind
            == "remediation_action"
            else "reflect"
        ),
        {
            "replan": "decide",
            "execute": "execute_action",
            "reflect": "reflect",
            "done": END,
        },
    )
    builder.add_conditional_edges(
        "execute_action",
        lambda state: "replan" if state.get("approval_stale") else "done",
        {"replan": "decide", "done": END},
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
    if _operational_action_contract(state) is not None:
        replay = signature_replay_context(namespace=state["namespace"], db_url=db_url)
        if replay is not None:
            replay["scenario_routing_key"] = _scenario_routing_key(
                state,
                replay_context=replay,
            )
            update["metadata"] = {
                **dict(state.get("metadata") or {}),
                "causal_replay": replay,
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


def _plan_prompt(
    state: IncidentAgentState,
    *,
    governed_memory_lines: list[str] | None = None,
    decision_contract_name: str | None = None,
) -> str:
    if governed_memory_lines is None:
        recalled = state.get("recalled_memories", [])
        memory_lines = []
        for idx, memory in enumerate(recalled, start=1):
            envelope = _governed_guidance_envelope(memory)
            memory_lines.append(f"{idx}. {json.dumps(envelope, sort_keys=True)}")
        if not memory_lines:
            memory_lines.append("No prior memories were recalled.")
    else:
        memory_lines = list(governed_memory_lines)

    triage = state.get("triage", {})
    decision_contract = decision_contract_name or (
        "AgentDecisionV3" if _operational_action_contract(state) is not None else "AgentDecisionV2"
    )
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
            f"Use this evidence to produce the next {decision_contract} step.",
        ]
    )


def _operational_action_contract(state: IncidentAgentState) -> str | None:
    """Select the narrow comparison contract only for the protected replay."""

    namespace = str(state.get("namespace") or "")
    triage = state.get("triage") or {}
    service_slug = str(triage.get("service_slug") or state.get("service_slug") or "")
    replay_namespace = namespace == DEMO_NAMESPACE or namespace.startswith(
        f"{DEMO_NAMESPACE}:session:"
    )
    if (
        replay_namespace
        and service_slug == DEMO_SERVICE_SLUG
        and state.get("user_input") == DEMO_INPUT
    ):
        return PAYMENTS_OPERATIONAL_ACTION_CONTRACT
    return None


def _controlled_replay_context(state: IncidentAgentState) -> dict[str, Any] | None:
    metadata = state.get("metadata")
    replay = metadata.get("causal_replay") if isinstance(metadata, dict) else None
    if not isinstance(replay, dict):
        return None
    required = ("scenario_id", "namespace", "replay_anchor", "scenario_routing_key")
    if any(not isinstance(replay.get(key), str) or not replay[key] for key in required):
        return None
    if replay["namespace"] != state.get("namespace"):
        return None
    expected = _scenario_routing_key(state, replay_context=replay)
    if replay["scenario_routing_key"] != expected:
        return None
    try:
        anchor = datetime.fromisoformat(replay["replay_anchor"].replace("Z", "+00:00"))
    except ValueError:
        return None
    if anchor.tzinfo is None or anchor.utcoffset() is None:
        return None
    result: dict[str, Any] = {key: replay[key] for key in required}
    correction = replay.get("correction_operation")
    if correction is not None:
        if (
            not isinstance(correction, dict)
            or set(correction)
            != {
                "id",
                "target_timestamp",
                "invalidated_memory_ids",
                "restored_memory_ids",
                "effects",
            }
            or not isinstance(correction.get("id"), str)
            or not correction["id"]
            or not isinstance(correction.get("target_timestamp"), str)
            or any(
                not isinstance(correction.get(key), list)
                for key in ("invalidated_memory_ids", "restored_memory_ids", "effects")
            )
            or any(
                not isinstance(value, str) or not value
                for key in ("invalidated_memory_ids", "restored_memory_ids")
                for value in correction[key]
            )
            or any(
                not isinstance(effect, dict)
                or set(effect)
                != {
                    "sequence",
                    "effect_type",
                    "source_memory_id",
                    "result_memory_id",
                    "belief_id",
                    "namespace",
                }
                for effect in correction["effects"]
            )
        ):
            return None
        try:
            target_timestamp = datetime.fromisoformat(
                correction["target_timestamp"].replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if target_timestamp.tzinfo is None or target_timestamp.utcoffset() is None:
            return None
        result["correction_operation"] = correction
    return result


def _scenario_routing_key(
    state: IncidentAgentState,
    *,
    replay_context: dict[str, Any] | None = None,
) -> str:
    """Return one stable provider route for both sides of a signature scenario."""

    if replay_context is None:
        metadata = state.get("metadata")
        candidate = metadata.get("causal_replay") if isinstance(metadata, dict) else None
        replay = candidate if isinstance(candidate, dict) else {}
    else:
        replay = replay_context
    identity = {
        "contract": _operational_action_contract(state),
        "namespace": str(replay.get("namespace") or state.get("namespace") or ""),
        "replay_anchor": str(replay.get("replay_anchor") or "unavailable"),
        "scenario_id": str(replay.get("scenario_id") or "unavailable"),
    }
    return f"signature:{canonical_sha256(identity).removeprefix('sha256:')}"


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
) -> tuple[AgentDecisionV2 | AgentDecisionV3, Any, int]:
    """Generate one valid decision with at most one schema-repair turn."""

    starting_turn = int(state.get("model_turn_count") or 0)
    if starting_turn < 0:
        raise AgentDecisionError("model turn count is invalid")
    if starting_turn >= MAX_MODEL_TURNS:
        raise AgentDecisionError("model turn budget is exhausted")
    operational_action_contract = _operational_action_contract(state)
    if operational_action_contract == PAYMENTS_OPERATIONAL_ACTION_CONTRACT:
        if PAYMENTS_REPLAY_DIAGNOSTIC_QUERY_KEY not in allowed_query_keys:
            raise AgentDecisionError(
                "controlled replay requires the pinned diagnostic query"
            )
        allowed_query_keys = {PAYMENTS_REPLAY_DIAGNOSTIC_QUERY_KEY}
    diagnostic_calls_used = int(state.get("diagnostic_call_count") or 0)
    diagnostic_observation_available = _has_current_diagnostic_observation(
        state,
        allowed_query_keys=allowed_query_keys,
    )
    controlled_terminal_selection = bool(
        operational_action_contract is not None
        and diagnostic_observation_available
    )
    decision_contract_name = (
        "ControlledActionSelectionV1"
        if controlled_terminal_selection
        else "AgentDecisionV3"
        if operational_action_contract is not None
        else "AgentDecisionV2"
    )
    system_prompt = (
        "You are Hindsight, an incident-response copilot. Use only memories "
        "whose usage_instruction is positive_guidance as recommendations. "
        "Audit-only memories may support diagnosis but must never direct a next "
        "step. Diagnostic tools are read-only. The only executable action is "
        "retract_recalled_memory, and it may target only a recalled memory that "
        "you cite verbatim. Never propose another executable action. "
        "Every recalled-memory citation quote must be a verbatim excerpt. "
        "Every recommendation must be reversible, verifiable, and suitable for "
        "operator review. "
        + (
            (
                "This controlled terminal call returns only action_id, disposition, "
                "parameters, and rationale. Select exactly one action ID and one neutral "
                "explanatory rationale from the server-supplied schema. Do not "
                "return catalog metadata, directive text, diagnosis, or other prose. "
                "The server renders the operator directive and this workflow executes "
                "nothing. "
            )
            if controlled_terminal_selection
            else (
                "This controlled replay requires AgentDecisionV3 until the configured "
                "diagnostic observation exists. Select only a server-configured "
                "diagnostic tool and copy the controlled explanation fields from the "
                "response schema. Governed-memory remediation is unavailable. "
            )
            if operational_action_contract is not None
            else ""
        )
        + f"Return only {decision_contract_name} JSON."
    )
    prompt = _decision_prompt(
        state,
        allowed_query_keys=allowed_query_keys,
        operational_action_contract=operational_action_contract,
        decision_contract_name=decision_contract_name,
    )
    invariant_prompt = _decision_prompt(
        state,
        allowed_query_keys=allowed_query_keys,
        operational_action_contract=operational_action_contract,
        governed_memory_lines=[GOVERNED_MEMORY_PROMPT_MARKER],
        decision_contract_name=decision_contract_name,
    )
    recalled_memory_ids = {
        str(memory.get("memory_id") or memory.get("id"))
        for memory in state.get("recalled_memories", [])
        if memory.get("memory_id") or memory.get("id")
    }
    recalled_memory_text = {
        str(memory.get("memory_id") or memory.get("id")): str(
            memory.get("memory_content") or memory.get("content") or ""
        )
        for memory in state.get("recalled_memories", [])
        if memory.get("memory_id") or memory.get("id")
    }
    attempts = 0
    last_error: AgentDecisionError | None = None
    request_contracts: list[dict[str, Any]] = []
    while attempts < 2 and starting_turn + attempts < MAX_MODEL_TURNS:
        attempts += 1
        logical_turn = (
            call_reservation() if call_reservation is not None else starting_turn + attempts
        )
        if not starting_turn < logical_turn <= MAX_MODEL_TURNS:
            raise RuntimeError("model call reservation returned an invalid count")
        response_schema = (
            controlled_action_selection_provider_schema(
                contract=str(operational_action_contract)
            )
            if controlled_terminal_selection
            else agent_decision_provider_schema(
                recalled_memory_ids=recalled_memory_ids,
                allowed_query_keys=allowed_query_keys,
                diagnostic_calls_used=diagnostic_calls_used,
                diagnostic_observation_available=diagnostic_observation_available,
                model_turn=logical_turn,
                operational_action_contract=operational_action_contract,
            )
        )
        request_prompt = prompt
        request_invariant_prompt = invariant_prompt
        repair_reason = None
        if attempts == 2:
            assert last_error is not None
            repair_reason = _decision_repair_reason(last_error)
            request_prompt = (
                f"{prompt}\n\n"
                f"The prior response failed a server-enforced {decision_contract_name} constraint. "
                f"Stable repair reason: {repair_reason}. "
                "Return only one JSON object matching the supplied schema for this turn. "
                "Do not add markdown or commentary."
            )
            request_invariant_prompt = (
                f"{invariant_prompt}\n\n"
                f"The prior response failed a server-enforced {decision_contract_name} constraint. "
                f"Stable repair reason: {repair_reason}. "
                "Return only one JSON object matching the supplied schema for this turn. "
                "Do not add markdown or commentary."
            )
        response = provider.generate(
            ReasoningRequest(
                system=system_prompt,
                prompt=request_prompt,
                temperature=0,
                max_output_tokens=1_024,
                routing_key=(
                    f"{_scenario_routing_key(state)}:turn:{logical_turn}"
                    if operational_action_contract is not None
                    else f"{state['decision_id']}:turn:{logical_turn}"
                ),
                response_json_schema=response_schema,
            )
        )
        request_routing_key = (
            f"{_scenario_routing_key(state)}:turn:{logical_turn}"
            if operational_action_contract is not None
            else f"{state['decision_id']}:turn:{logical_turn}"
        )
        request_contract = {
            "schema_version": 1,
            "attempt": attempts,
            "repair_reason": repair_reason,
            "logical_turn": logical_turn,
            "provider": response.provider,
            "model": response.model,
            "system": system_prompt,
            "prompt": request_prompt,
            "prompt_invariant": request_invariant_prompt,
            "prompt_invariant_sha256": text_sha256(request_invariant_prompt),
            "temperature": 0,
            "max_output_tokens": 1_024,
            "routing_key": request_routing_key,
            "decision_contract": decision_contract_name,
            "response_schema_version": (
                1
                if controlled_terminal_selection
                else 3
                if operational_action_contract is not None
                else 2
            ),
            "response_json_schema": response_schema,
        }
        try:
            if controlled_terminal_selection:
                selection = parse_controlled_action_selection(
                    response.text,
                    contract=str(operational_action_contract),
                )
                decision = controlled_decision_from_selection(
                    selection,
                    contract=str(operational_action_contract),
                )
            else:
                decision = parse_agent_decision(
                    normalize_agent_decision_provider_text(response.text),
                    recalled_memory_ids=recalled_memory_ids,
                    recalled_memory_text=recalled_memory_text,
                    allowed_query_keys=allowed_query_keys,
                    diagnostic_calls_used=diagnostic_calls_used,
                    diagnostic_observation_available=diagnostic_observation_available,
                    model_turn=logical_turn,
                    operational_action_contract=operational_action_contract,
                )
        except AgentDecisionError as exc:
            request_contracts.append(request_contract)
            last_error = exc
            continue
        request_contracts.append(request_contract)
        usage = dict(response.usage)
        usage["response_sha256"] = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
        usage["request_contract"] = request_contract
        usage["request_contracts"] = request_contracts
        normalized_response = type(response)(
            text=response.text,
            provider=response.provider,
            model=response.model,
            usage=usage,
        )
        return decision, normalized_response, logical_turn
    raise AgentDecisionError("model did not produce a valid bounded decision") from last_error


_DECISION_REPAIR_REASONS = {
    "model response did not satisfy AgentDecisionV2": "agent_decision_schema_mismatch",
    "model response did not satisfy AgentDecisionV3": "controlled_action_schema_mismatch",
    "model response did not satisfy ControlledActionSelectionV1": (
        "controlled_action_selection_schema_mismatch"
    ),
    "controlled action rationale is outside server-approved explanatory prose": (
        "controlled_action_rationale_not_approved"
    ),
    "a recalled memory may be cited only once per decision": "duplicate_memory_citation",
    "model cited memory that was not recalled": "unrecalled_memory_citation",
    "model citation is not a quote from recalled memory": "non_verbatim_memory_citation",
    "final model turn must produce a terminal decision": "final_turn_requires_terminal_decision",
    "diagnostic call budget is exhausted": "diagnostic_call_budget_exhausted",
    "model selected a diagnostic query outside the allowlist": "diagnostic_query_not_allowed",
    "a current diagnostic observation is required before a terminal decision": (
        "current_diagnostic_observation_required"
    ),
    "remediation target must be cited verbatim": "uncited_remediation_target",
    "recommendation prose contradicts server-owned operational action": (
        "operational_action_prose_contradiction"
    ),
}


def _decision_repair_reason(error: AgentDecisionError) -> str:
    return _DECISION_REPAIR_REASONS.get(str(error), "agent_decision_constraint_violation")


def _validate_initial_call_count(value: int, *, limit: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError(f"{name} must be between zero and {limit}")


def _has_current_diagnostic_observation(
    state: IncidentAgentState,
    *,
    allowed_query_keys: set[str],
) -> bool:
    completed_calls = {
        (call.get("id"), call.get("query_key"))
        for call in state.get("tool_calls", [])
        if isinstance(call.get("id"), str)
        and bool(call["id"])
        and call.get("status") == "completed"
        and call.get("tool") == "aws_cloudwatch_diagnostics"
        and call.get("query_key") in allowed_query_keys
    }
    for observation in state.get("observations", []):
        datapoint_count = observation.get("datapoint_count")
        tool_call_id = observation.get("tool_call_id")
        if (
            isinstance(tool_call_id, str)
            and bool(tool_call_id)
            and observation.get("status") == "available"
            and observation.get("tool") == "aws_cloudwatch_diagnostics"
            and observation.get("query_key") in allowed_query_keys
            and (tool_call_id, observation.get("query_key")) in completed_calls
            and type(datapoint_count) is int
            and datapoint_count > 0
        ):
            return True
    return False


def _decision_prompt(
    state: IncidentAgentState,
    *,
    allowed_query_keys: set[str],
    operational_action_contract: str | None = None,
    governed_memory_lines: list[str] | None = None,
    decision_contract_name: str | None = None,
) -> str:
    observations = state.get("observations", [])
    remaining_turns = MAX_MODEL_TURNS - int(state.get("model_turn_count") or 0)
    query_keys = sorted(allowed_query_keys)
    return "\n".join(
        [
            _plan_prompt(
                state,
                governed_memory_lines=governed_memory_lines,
                decision_contract_name=decision_contract_name,
            ),
            "",
            "Recorded diagnostic observations:",
            json.dumps(observations, sort_keys=True, default=str) if observations else "None.",
            "",
            f"Configured CloudWatch query keys: {json.dumps(query_keys)}",
            f"Remaining logical model turns including this turn: {remaining_turns}",
            (
                f"Operational action contract: {operational_action_contract}"
                if operational_action_contract is not None
                else "Operational action contract: none"
            ),
            (
                "When configured query keys are available and there is no current observation, "
                "choose the most relevant diagnostic_tool before a terminal decision. Otherwise choose "
                "diagnostic_tool only when another configured observation is necessary. The final "
                "available turn must return a recommendation"
                + (
                    "."
                    if operational_action_contract is not None
                    else " or the allowed remediation action."
                )
            ),
        ]
    )


def _decision_plan_text(decision: AgentDecisionV2 | AgentDecisionV3) -> str:
    verification = "; ".join(decision.verification)
    safety = "; ".join(decision.safety_constraints)
    action = (
        _recommendation_action_text(decision)
        or (
            f"Retract recalled memory {decision.remediation_action.target_memory_id}: "
            f"{decision.remediation_action.reason}"
            if decision.remediation_action is not None
            else None
        )
        or (
            f"Read configured CloudWatch query {decision.tool_call.query_key}"
            if decision.tool_call is not None
            else "No next step"
        )
    )
    return "\n".join(
        [
            f"Cause: {decision.diagnosis}",
            f"Checks: {verification}",
            f"Action: {action}",
            f"Safety: {safety}",
        ]
    )


def _recommendation_action_text(decision: AgentDecisionV2 | AgentDecisionV3) -> str:
    if isinstance(decision, AgentDecisionV3) and decision.operational_action is not None:
        return operational_action_directive(decision.operational_action)
    return decision.recommendation or ""


def _recommendation_trace(
    *,
    state: IncidentAgentState,
    decision: AgentDecisionV2 | AgentDecisionV3,
    recommendation_identity: str,
) -> dict[str, Any]:
    operational_action = (
        decision.operational_action if isinstance(decision, AgentDecisionV3) else None
    )
    rendered_directive = (
        operational_action_directive(operational_action) if operational_action is not None else None
    )
    causal_envelope = (
        _recommendation_causal_envelope(state=state, decision=decision)
        if operational_action is not None and _controlled_replay_context(state) is not None
        else None
    )
    redacted_causal_envelope = (
        public_causal_envelope(causal_envelope) if causal_envelope is not None else None
    )
    return {
        "schema_version": 4 if operational_action is not None else 2,
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
        "observation_fingerprint": state.get("observation_fingerprint"),
        **({"causal_envelope": causal_envelope} if causal_envelope is not None else {}),
        **(
            {"redacted_causal_envelope": redacted_causal_envelope}
            if redacted_causal_envelope is not None
            else {}
        ),
        "recommendation": {
            "id": recommendation_identity,
            "summary": rendered_directive or decision.recommendation,
            "diagnosis": decision.diagnosis,
            "rationale": decision.rationale,
            "rollback": decision.rollback,
            "verification": decision.verification,
            "safety_constraints": decision.safety_constraints,
            "status": "awaiting_approval",
            **(
                {
                    "operational_action": {
                        **operational_action.model_dump(mode="json"),
                        "primary_action": operational_action.action_id,
                        "directive": rendered_directive,
                        "consistency_status": "consistent",
                        "fingerprint": operational_action_fingerprint(operational_action),
                    }
                }
                if operational_action is not None
                else {}
            ),
        },
        "execution": {
            "status": "awaiting_approval",
            "mode": "recommendation_only",
        },
    }


def _recommendation_causal_envelope(
    *,
    state: IncidentAgentState,
    decision: AgentDecisionV3,
) -> dict[str, Any]:
    replay = _controlled_replay_context(state)
    if replay is None or decision.operational_action is None:
        raise AgentDecisionError("controlled recommendation evidence is incomplete")
    requests: list[dict[str, Any]] = []
    for step in state.get("reasoning_steps", []):
        step_requests = step.get("requests") if isinstance(step, dict) else None
        if isinstance(step_requests, list) and step_requests:
            if any(not isinstance(request, dict) for request in step_requests):
                raise AgentDecisionError(
                    "controlled recommendation contains an invalid model request input"
                )
            requests.extend(step_requests)
            continue
        request = step.get("request") if isinstance(step, dict) else None
        if not isinstance(request, dict):
            raise AgentDecisionError("controlled recommendation omitted a model request input")
        requests.append(request)
    if not requests:
        raise AgentDecisionError("controlled recommendation omitted its model request inputs")

    catalog = operational_action_catalog(decision.operational_action.contract)
    tool_calls = list(state.get("tool_calls") or [])
    observations = list(state.get("observations") or [])
    normalized_incident = str(state.get("user_input") or "").strip()
    incident = {
        "incident_id": state.get("incident_id"),
        "namespace": state.get("namespace"),
        "service_slug": (state.get("triage") or {}).get("service_slug")
        or state.get("service_slug"),
        "severity": (state.get("triage") or {}).get("severity") or state.get("severity"),
        "title": (state.get("triage") or {}).get("title") or state.get("title"),
        "normalized_user_incident": normalized_incident,
    }
    memory_intervention = []
    for ordinal, memory in enumerate(state.get("recalled_memories", []), start=1):
        envelope = _governed_guidance_envelope(memory)
        prompt_fragment = f"{ordinal}. {json.dumps(envelope, sort_keys=True)}"
        memory_intervention.append(
            {
                "ordinal": ordinal,
                "memory": envelope,
                "memory_sha256": canonical_sha256(envelope),
                "prompt_fragment_sha256": text_sha256(prompt_fragment),
            }
        )
    release_revision = os.environ.get("HINDSIGHT_DEPLOYED_REVISION", "unknown")
    if re.fullmatch(r"[0-9a-f]{40}", release_revision) is None:
        raise AgentDecisionError("controlled recommendation requires an exact release revision")
    tool_contract = {
        "schema_version": 1,
        "diagnostic_tool": "aws_cloudwatch_diagnostics",
        "observation_schema_version": 1,
        "allowed_query_keys": [PAYMENTS_REPLAY_DIAGNOSTIC_QUERY_KEY],
        "max_diagnostic_calls": MAX_DIAGNOSTIC_CALLS,
    }
    invariant_inputs = {
        "normalized_user_incident": normalized_incident,
        "prompt_templates": {
            "triage": {
                "id": TRIAGE_PROMPT_TEMPLATE_ID,
                "sha256": text_sha256(TRIAGE_PROMPT_TEMPLATE),
            },
            "decision": {
                "id": DECISION_PROMPT_TEMPLATE_ID,
                "sha256": text_sha256(DECISION_PROMPT_TEMPLATE),
            },
            "system": {
                "id": "hindsight.incident-system.v3",
                "sha256": text_sha256(str(requests[-1].get("system") or "")),
            },
        },
        "triage_result": state.get("triage") or {},
        "ordered_tool_calls": tool_calls,
        "ordered_observations": observations,
        "ordered_model_request_configuration": [
            _model_request_invariant(request) for request in requests
        ],
        "tool_contract": tool_contract,
        "embedding_profile": state.get("embedding_profile") or {},
        "release_revision": release_revision,
        "action_catalog": catalog,
        "tenant_id": str(current_tenant_id() or ""),
        "namespace": state.get("namespace"),
        "scenario_id": replay["scenario_id"],
        "replay_anchor": replay["replay_anchor"],
        "retrieval_policy": (state.get("metadata") or {}).get("retrieval_policy"),
        "retrieval_policy_version": 1,
    }
    correction = replay.get("correction_operation")
    correction_operation = correction if isinstance(correction, dict) else None
    permitted_intervention = {
        "kind": "governed_memory_version_selection.v1",
        "ordered_memory_versions": memory_intervention,
        "selection_fingerprint": state.get("selection_fingerprint"),
        "expected_changed_prompt_fragments": [
            item["prompt_fragment_sha256"] for item in memory_intervention
        ],
        "correction_operation_id": (
            correction_operation.get("id") if correction_operation is not None else None
        ),
        "correction_target_timestamp": (
            correction_operation.get("target_timestamp")
            if correction_operation is not None
            else None
        ),
        "operation_effects": (
            correction_operation.get("effects") or [] if correction_operation is not None else []
        ),
        "invalidated_memory_fingerprints": [
            canonical_sha256(memory_id)
            for memory_id in (
                correction_operation.get("invalidated_memory_ids") or []
                if correction_operation is not None
                else []
            )
        ],
        "restored_memory_fingerprints": [
            canonical_sha256(memory_id)
            for memory_id in (
                correction_operation.get("restored_memory_ids") or []
                if correction_operation is not None
                else []
            )
        ],
    }
    actual_decision_inputs = {
        "incident": incident,
        "triage": state.get("triage") or {},
        "retrieval_policy": (state.get("metadata") or {}).get("retrieval_policy"),
        "embedding_profile": state.get("embedding_profile") or {},
        "ordered_governed_memories": memory_intervention,
        "ordered_tool_calls": tool_calls,
        "ordered_observations": observations,
        "ordered_model_requests": requests,
        "tool_contract": tool_contract,
        "action_catalog": catalog,
    }
    identity = {
        "scenario_id": replay["scenario_id"],
        "namespace": replay["namespace"],
        "replay_anchor": replay["replay_anchor"],
        "scenario_routing_key": replay["scenario_routing_key"],
        "run_id": state.get("run_id"),
        "decision_id": state.get("decision_id"),
        "release_revision": release_revision,
    }
    return build_causal_envelope(
        identity=identity,
        invariant_inputs=invariant_inputs,
        permitted_intervention=permitted_intervention,
        actual_decision_inputs=actual_decision_inputs,
        rendered_prompt_sha256=[
            text_sha256(request["prompt"])
            for request in actual_decision_inputs["ordered_model_requests"]
        ],
        decision_output=controlled_action_selection_from_decision(decision).model_dump(
            mode="json"
        ),
    )


def _model_request_invariant(request: dict[str, Any]) -> dict[str, Any]:
    schema = request.get("response_json_schema")
    if not isinstance(schema, dict):
        raise AgentDecisionError("controlled model request omitted its response schema")
    normalized_schema = json.loads(json.dumps(schema, sort_keys=True))
    definitions = normalized_schema.get("$defs")
    if isinstance(definitions, dict):
        definitions["MemoryCitation"] = {
            "bound_to": "permitted_intervention.ordered_memory_versions"
        }
    properties = normalized_schema.get("properties")
    if isinstance(properties, dict):
        properties["recalled_memory_citations"] = {
            "bound_to": "permitted_intervention.ordered_memory_versions"
        }
    return {
        key: request.get(key)
        for key in (
            "schema_version",
            "attempt",
            "repair_reason",
            "logical_turn",
            "provider",
            "model",
            "system",
            "prompt_invariant",
            "prompt_invariant_sha256",
            "temperature",
            "max_output_tokens",
            "routing_key",
            "decision_contract",
            "response_schema_version",
        )
    } | {"response_json_schema": normalized_schema}


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


def _remediation_action_trace(
    *,
    state: IncidentAgentState,
    decision: AgentDecisionV2,
    action_identity: str,
    target_excerpt: str,
    preview: dict[str, Any],
    approved_effects: dict[str, Any],
) -> dict[str, Any]:
    action = decision.remediation_action
    assert action is not None
    return {
        "schema_version": 3,
        "mode": "governed_memory_remediation",
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
        "observation_fingerprint": state["observation_fingerprint"],
        "remediation_action": {
            "id": action_identity,
            "name": action.name,
            "target_memory_id": action.target_memory_id,
            "target_excerpt": target_excerpt,
            "reason": action.reason,
            "diagnosis": decision.diagnosis,
            "rationale": decision.rationale,
            "rollback": decision.rollback,
            "verification": decision.verification,
            "safety_constraints": decision.safety_constraints,
            "status": "awaiting_approval",
        },
        "preview": {
            "id": str(preview.get("id") or ""),
            "fingerprint": str(preview.get("fingerprint") or ""),
            "expires_at": preview.get("expires_at"),
            "effect_count": approved_effects["mutation_count"],
            "effects": {
                "close_memory_ids": approved_effects["close_memory_ids"],
                "review_resolutions": approved_effects["review_resolutions"],
            },
        },
        "execution": {
            "status": "awaiting_approval",
            "mode": "governed_memory_remediation",
        },
    }


def _bounded_retraction_effects(preview: dict[str, Any]) -> dict[str, Any]:
    effect = dict(preview.get("effect_payload") or {})
    close_memory_ids = [str(value) for value in (effect.get("close_memory_ids") or [])]
    review_resolutions = [
        _jsonable_row(dict(value)) for value in (effect.get("review_resolutions") or [])
    ]
    mutation_count = len(close_memory_ids) + len(review_resolutions)
    if mutation_count < 1 or mutation_count > 10:
        raise AgentDecisionError("remediation action must contain between one and ten mutations")
    return {
        "close_memory_ids": close_memory_ids,
        "review_resolutions": review_resolutions,
        "mutation_count": mutation_count,
    }


def _validate_action_approval(
    approval: dict[str, Any],
    *,
    action_identity: str,
    selection_fingerprint: str,
    observation_fingerprint: str,
    preview_id: str,
    preview_fingerprint: str,
) -> None:
    required = {
        "approved",
        "remediation_action_id",
        "selection_fingerprint",
        "observation_fingerprint",
        "preview_id",
        "preview_fingerprint",
        "actor",
    }
    if (
        set(approval) != required
        or not isinstance(approval.get("approved"), bool)
        or not isinstance(approval.get("actor"), str)
        or not approval["actor"].strip()
    ):
        raise StaleRecommendationError("action approval payload does not match the contract")
    expected = {
        "remediation_action_id": action_identity,
        "selection_fingerprint": selection_fingerprint,
        "observation_fingerprint": observation_fingerprint,
        "preview_id": preview_id,
        "preview_fingerprint": preview_fingerprint,
    }
    if any(approval[key] != value for key, value in expected.items()):
        raise StaleRecommendationError("remediation action changed before approval")


def _stale_action_replan(
    *,
    state: IncidentAgentState,
    refreshed: dict[str, Any],
    action_trace: dict[str, Any],
    detail: str,
) -> dict[str, Any]:
    stale_count = int(state.get("stale_replan_count") or 0)
    if stale_count >= 1:
        raise RemediationActionError("remediation state changed after its single replan")
    if int(state.get("model_turn_count") or 0) >= MAX_MODEL_TURNS:
        raise RemediationActionError(
            "remediation state changed and the model turn budget is exhausted"
        )
    refreshed_fingerprint = memory_selection_fingerprint(refreshed.get("recalled_memories", []))
    approval = action_trace.get("approval") or {}
    return {
        **refreshed,
        "selection_fingerprint": refreshed_fingerprint,
        "approval_stale": True,
        "action_approved": False,
        "guidance_eligible": False,
        "stale_replan_count": stale_count + 1,
        "action_trace": {
            **action_trace,
            "approval": {
                **approval,
                "approved": False,
                "disposition": "stale",
            },
            "execution": {
                "status": "replan_required",
                "mode": "governed_memory_remediation",
                "detail": detail,
            },
        },
    }


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
    return {key: _jsonable_value(value) for key, value in row.items()}


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
