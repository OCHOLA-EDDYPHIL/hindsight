"""LangGraph incident agent with CockroachDB-backed state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import Any, NotRequired, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict
from langchain_cockroachdb import CockroachDBChatMessageHistory, CockroachDBSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from hindsight.db import database_url
from hindsight.embeddings import DeterministicEmbeddingProvider, EmbeddingProvider
from hindsight.memory import MemoryStore, Provenance
from hindsight.reasoning import ReasoningProvider, ReasoningRequest, reasoning_provider_from_env
from hindsight.tracing import memory_ids, set_span_attributes, start_span

AGENT_CHAT_TABLE = "agent_chat_messages"
_SETUP_DB_URLS: set[str] = set()


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
    reasoning: dict[str, Any]
    proposed_action: str
    action_approved: bool
    reflected_memory: dict[str, Any]


def run_incident_agent(
    incident: IncidentInput,
    *,
    thread_id: str | None = None,
    pause_before_act: bool = False,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> IncidentAgentResult:
    """Start or continue an incident thread with a new user incident turn."""

    _ensure_sync_entrypoint()
    resolved_thread_id = thread_id or incident.incident_id or f"incident-{uuid4()}"
    namespace = incident.namespace or incident.incident_id
    initial_state: IncidentAgentState = {
        "thread_id": resolved_thread_id,
        "incident_id": incident.incident_id,
        "namespace": namespace,
        "service_slug": incident.service_slug,
        "severity": incident.severity,
        "title": incident.title,
        "user_input": incident.user_input,
        "metadata": dict(incident.metadata),
        "pause_before_act": pause_before_act,
        "decision_id": f"agent:{resolved_thread_id}:plan",
    }
    state = _invoke_graph(
        initial_state,
        thread_id=resolved_thread_id,
        db_url=db_url,
        reasoning_provider=reasoning_provider,
        embedding_provider=embedding_provider,
    )
    return _agent_result(resolved_thread_id, state)


async def run_incident_agent_async(
    incident: IncidentInput,
    *,
    thread_id: str | None = None,
    pause_before_act: bool = False,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> IncidentAgentResult:
    """Async-safe wrapper for starting or continuing an incident thread."""

    return await asyncio.to_thread(
        partial(
            run_incident_agent,
            incident,
            thread_id=thread_id,
            pause_before_act=pause_before_act,
            db_url=db_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
        )
    )


def resume_incident_agent(
    *,
    thread_id: str,
    approved: bool = True,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> IncidentAgentResult:
    """Resume an interrupted incident thread in a fresh graph context."""

    _ensure_sync_entrypoint()
    state = _invoke_graph(
        Command(resume=approved),
        thread_id=thread_id,
        db_url=db_url,
        reasoning_provider=reasoning_provider,
        embedding_provider=embedding_provider,
    )
    return _agent_result(thread_id, state)


async def resume_incident_agent_async(
    *,
    thread_id: str,
    approved: bool = True,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> IncidentAgentResult:
    """Async-safe wrapper for resuming an interrupted incident thread."""

    return await asyncio.to_thread(
        partial(
            resume_incident_agent,
            thread_id=thread_id,
            approved=approved,
            db_url=db_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
        )
    )


def build_incident_graph(
    *,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
):
    """Build the incident graph; compile it with a checkpointer before use."""

    resolved_db_url = db_url or database_url()
    resolved_embedding_provider = embedding_provider or DeterministicEmbeddingProvider()

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
        return {
            "triage": triaged,
            "chat_messages": messages_to_dict(current_messages),
        }

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
            recalled = update.get("recalled_memories", [])
            set_span_attributes(
                span,
                {
                    "hindsight.memory.count": len(recalled),
                    "hindsight.memory.ids": memory_ids(recalled),
                    "hindsight.agent.recall_error": bool(update.get("recall_error")),
                },
            )
            return update

    def plan(state: IncidentAgentState) -> dict[str, Any]:
        provider = reasoning_provider or reasoning_provider_from_env()
        with start_span(
            "hindsight.agent.reason",
            {
                "hindsight.agent.thread_id": state["thread_id"],
                "hindsight.agent.incident_id": state["incident_id"],
                "hindsight.memory.namespace": state["namespace"],
                "hindsight.memory.decision_id": state["decision_id"],
                "hindsight.reasoning.provider": provider.provider_name,
                "hindsight.reasoning.model": provider.model_name,
            },
        ) as span:
            response = provider.generate(
                ReasoningRequest(
                    system=(
                        "You are Hindsight, an incident-response copilot. "
                        "Use recalled memories as context, but propose only reversible, "
                        "operator-reviewable remediation steps."
                    ),
                    prompt=_plan_prompt(state),
                    max_output_tokens=512,
                )
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
        return {
            "plan": response.text,
            "reasoning": {
                "provider": response.provider,
                "model": response.model,
                "usage": dict(response.usage),
            },
        }

    def act(state: IncidentAgentState) -> dict[str, Any]:
        proposed_action = _proposed_action(state)
        approved = True
        if state.get("pause_before_act"):
            approved = bool(
                interrupt(
                    {
                        "thread_id": state["thread_id"],
                        "incident_id": state["incident_id"],
                        "proposed_action": proposed_action,
                    }
                )
            )
        return {
            "proposed_action": proposed_action,
            "action_approved": approved,
        }

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
                memory = store.remember(
                    memory_kind="semantic",
                    namespace=state["namespace"],
                    content=content,
                    provenance=Provenance(
                        writer="agent.reflect",
                        source_ref=state["decision_id"],
                        justification="Capture incident plan and proposed remediation for future recall",
                    ),
                    metadata={
                        "thread_id": state["thread_id"],
                        "incident_id": state["incident_id"],
                        "service_slug": state.get("service_slug"),
                        "recalled_memory_ids": [
                            str(row.get("memory_id") or row.get("id"))
                            for row in state.get("recalled_memories", [])
                            if row.get("memory_id") or row.get("id")
                        ],
                        "action_approved": state.get("action_approved", False),
                    },
                )
            history = _chat_history(
                thread_id=state["thread_id"],
                db_url=resolved_db_url,
            )
            try:
                history.add_message(AIMessage(content=content))
            finally:
                history.close()
            set_span_attributes(span, {"hindsight.memory.id": str(memory["id"])})
            return {"reflected_memory": _jsonable_row(memory)}

    builder = StateGraph(IncidentAgentState)
    builder.add_node("triage", triage)
    builder.add_node("recall", recall)
    builder.add_node("plan", plan)
    builder.add_node("act", act)
    builder.add_node("reflect", reflect)
    builder.add_edge(START, "triage")
    builder.add_edge("triage", "recall")
    builder.add_edge("recall", "plan")
    builder.add_edge("plan", "act")
    builder.add_edge("act", "reflect")
    builder.add_edge("reflect", END)
    return builder


def setup_agent_storage(*, db_url: str | None = None) -> None:
    """Create checkpoint and chat-history tables used by the agent runtime."""

    resolved_db_url = db_url or database_url()
    _setup_agent_storage_once(resolved_db_url)


def _recall_for_state(
    state: IncidentAgentState,
    *,
    db_url: str,
    embedding_provider: EmbeddingProvider,
) -> dict[str, Any]:
    memories: list[dict[str, Any]] = []
    recall_error = None
    with MemoryStore(
        url=db_url,
        embedding_provider=embedding_provider,
    ) as store:
        try:
            service_slug = state.get("service_slug")
            if service_slug:
                memories = store.recall_similar_incidents(
                    namespace=state["namespace"],
                    query=state["user_input"],
                    service_slug=service_slug,
                    decision_id=state["decision_id"],
                    reader="agent.recall",
                    purpose="retrieve similar incident context",
                )
        except Exception as exc:
            recall_error = str(exc)
    if not memories:
        try:
            fallback_embedding_provider = None if recall_error else embedding_provider
            with MemoryStore(
                url=db_url,
                embedding_provider=fallback_embedding_provider,
            ) as fallback_store:
                memories = fallback_store.recall(
                    namespace=state["namespace"],
                    query=state["user_input"],
                    decision_id=state["decision_id"],
                    reader="agent.recall",
                    purpose="retrieve semantic incident context",
                )
        except Exception as exc:
            recall_error = _append_error(recall_error, str(exc))
            memories = []
    update: dict[str, Any] = {"recalled_memories": _jsonable_rows(memories)}
    if recall_error:
        update["recall_error"] = recall_error
    return update


def _setup_agent_storage_once(resolved_db_url: str) -> None:
    if resolved_db_url in _SETUP_DB_URLS:
        return
    with CockroachDBSaver.from_conn_string(resolved_db_url) as checkpointer:
        checkpointer.setup()
    history = _chat_history(thread_id="setup", db_url=resolved_db_url)
    try:
        history.create_table_if_not_exists()
    finally:
        history.close()
    _SETUP_DB_URLS.add(resolved_db_url)


def _invoke_graph(
    input_or_command: IncidentAgentState | Command,
    *,
    thread_id: str,
    db_url: str | None,
    reasoning_provider: ReasoningProvider | None,
    embedding_provider: EmbeddingProvider | None,
) -> dict[str, Any]:
    resolved_db_url = db_url or database_url()
    _setup_agent_storage_once(resolved_db_url)
    with CockroachDBSaver.from_conn_string(resolved_db_url) as checkpointer:
        graph = build_incident_graph(
            db_url=resolved_db_url,
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
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


def _append_error(existing: str | None, new_error: str) -> str:
    if existing:
        return f"{existing}; {new_error}"
    return new_error


def _chat_history(*, thread_id: str, db_url: str) -> CockroachDBChatMessageHistory:
    return CockroachDBChatMessageHistory(
        session_id=thread_id,
        connection_string=_async_sqlalchemy_url(db_url),
        table_name=AGENT_CHAT_TABLE,
    )


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
        content = memory.get("memory_content") or memory.get("content") or ""
        incident = memory.get("incident_slug")
        prefix = f"{idx}. "
        if incident:
            prefix += f"[{incident}] "
        memory_lines.append(prefix + str(content))
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
            "Return a concise triage plan with suspected cause, checks, and safe next action.",
        ]
    )


def _proposed_action(state: IncidentAgentState) -> str:
    service = state.get("service_slug") or "affected service"
    plan = (state.get("plan") or "").strip()
    if plan:
        return f"Review and execute the safest reversible step for {service}: {plan}"
    return f"Review telemetry for {service} and prepare a reversible mitigation."


def _reflection_content(state: IncidentAgentState) -> str:
    status = "approved" if state.get("action_approved") else "not approved"
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
