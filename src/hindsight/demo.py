"""Scriptable demo flows for Hindsight's memory rewind moment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import sleep
from typing import Any
from uuid import uuid4

from hindsight.db import connect, database_url
from hindsight.demo_state import (
    DEMO_INCIDENT_ID,
    DEMO_INPUT,
    DEMO_NAMESPACE,
    DEMO_SERVICE_SLUG,
    DEMO_TITLE,
    GOOD_MEMORY_CONTENT,
    POISONED_MEMORY_CONTENT,
)
from hindsight.embeddings import embedding_provider_from_env
from hindsight.memory import MemoryStore, Provenance, RewindResult
from hindsight.operations import enqueue_operation, execute_operation, preview_rewind
from hindsight.reasoning import ReasoningProvider, ReasoningRequest, ReasoningResponse
from hindsight.simulator import BoundedActionRequest, DeterministicIncidentSimulator
from hindsight.tracing import memory_ids, set_span_attributes, start_span
from hindsight.trace_contract import decision_influence

GOOD_RECOMMENDATION = (
    "Suspected cause: retry fanout is amplifying downstream payment processor "
    "timeouts. Check processor timeout rate and queue depth, then throttle retry "
    "fanout before scaling workers."
)
BAD_RECOMMENDATION = (
    "Suspected cause: checkout capacity is too low. Scale payment workers immediately "
    "while retry fanout remains elevated."
)
REWIND_REASON = "Poisoned memory led to an unsafe retry-amplifying recommendation"


@dataclass(frozen=True)
class AgentRunSummary:
    """Camera-friendly summary for one demo agent run."""

    label: str
    thread_id: str
    decision_id: str
    plan: str | None
    proposed_action: str | None
    reflected_memory_id: str | None
    recalled_memory_ids: list[str]
    action_trace: dict[str, Any]


@dataclass(frozen=True)
class PoisonRewindDemoResult:
    """Complete result for the signature poison/diagnose/rewind sequence."""

    namespace: str
    clean_run: AgentRunSummary
    poison_memory: dict[str, Any]
    bad_run: AgentRunSummary
    diagnosis: dict[str, Any]
    rewind_target: datetime
    rewind: RewindResult
    corrected_run: AgentRunSummary


class MemoryBiasedDemoReasoningProvider:
    """Deterministic provider that makes recalled memory visibly change the plan."""

    provider_name = "deterministic-demo"
    model_name = "memory-biased-v1"

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        prompt = request.prompt.lower()
        poisoned = "poisoned memory" in prompt or "scale payment workers" in prompt
        text = BAD_RECOMMENDATION if poisoned else GOOD_RECOMMENDATION
        return ReasoningResponse(
            text=text,
            provider=self.provider_name,
            model=self.model_name,
            usage={
                "prompt_characters": len(request.prompt),
                "system_characters": len(request.system or ""),
                "poisoned_memory_seen": poisoned,
            },
        )


def run_poison_rewind_demo(
    *,
    db_url: str | None = None,
    namespace: str = DEMO_NAMESPACE,
    keep_existing: bool = False,
    reasoning_provider: ReasoningProvider | None = None,
) -> PoisonRewindDemoResult:
    """Run the full poison, diagnose, and rewind sequence."""

    resolved_db_url = db_url or database_url()
    if not keep_existing:
        namespace = f"{namespace}:session:{uuid4().hex[:8]}"
    provider = reasoning_provider or MemoryBiasedDemoReasoningProvider()
    embedding_provider = embedding_provider_from_env()
    with start_span(
        "hindsight.demo.poison_rewind",
        {
            "hindsight.demo.flow": "poison_rewind",
            "hindsight.memory.namespace": namespace,
        },
    ) as span:
        _record_poison_demo_session(namespace=namespace, db_url=resolved_db_url)

        seed_good_demo_memory(namespace=namespace, db_url=resolved_db_url)

        clean_run = run_demo_agent_turn(
            label="clean",
            namespace=namespace,
            db_url=resolved_db_url,
            reasoning_provider=provider,
        )

        with connect(resolved_db_url) as conn:
            rewind_target = conn.execute("SELECT now()").fetchone()[0]
            conn.commit()
        sleep(0.05)

        poison_memory = poison_demo_memory(namespace=namespace, db_url=resolved_db_url)
        bad_run = run_demo_agent_turn(
            label="poisoned",
            namespace=namespace,
            db_url=resolved_db_url,
            reasoning_provider=provider,
        )
        diagnosis = decision_influence(
            decision_id=bad_run.decision_id,
            db_url=resolved_db_url,
        )

        preview = preview_rewind(
            namespace=namespace,
            target_timestamp=rewind_target,
            actor="demo.operator",
            reason=REWIND_REASON,
            db_url=resolved_db_url,
        )
        operation, _ = enqueue_operation(
            preview_id=str(preview["id"]),
            fingerprint=str(preview["fingerprint"]),
            idempotency_key=f"demo-rewind:{uuid4()}",
            db_url=resolved_db_url,
        )
        operation = execute_operation(
            operation_id=str(operation["id"]),
            embedding_provider=embedding_provider,
            worker_id="demo.runner",
            db_url=resolved_db_url,
        )
        with MemoryStore(url=resolved_db_url) as store:
            rewind = RewindResult(
                operation=operation,
                restored_memories=store.list_current_semantic(namespace=namespace, limit=10_000),
                invalidated_memories=[
                    memory
                    for memory_id in operation["invalidated_memory_ids"]
                    if (
                        memory := store.audit_memory(
                            memory_kind="semantic", memory_id=str(memory_id)
                        )
                    )
                    is not None
                ],
            )

        corrected_run = run_demo_agent_turn(
            label="corrected",
            namespace=namespace,
            db_url=resolved_db_url,
            reasoning_provider=provider,
        )
        set_span_attributes(
            span,
            {
                "hindsight.demo.poisoned_memory_id": str(poison_memory["id"]),
                "hindsight.memory.invalidated.ids": memory_ids(rewind.invalidated_memories),
                "hindsight.memory.restored.ids": memory_ids(rewind.restored_memories),
            },
        )

    return PoisonRewindDemoResult(
        namespace=namespace,
        clean_run=clean_run,
        poison_memory=poison_memory,
        bad_run=bad_run,
        diagnosis=diagnosis,
        rewind_target=rewind_target,
        rewind=rewind,
        corrected_run=corrected_run,
    )


def reset_poison_rewind_demo(*, namespace: str = DEMO_NAMESPACE, db_url: str | None = None) -> None:
    """Archive a demo session without deleting governed evidence."""

    resolved_db_url = db_url or database_url()
    base_namespace = namespace.split(":session:", 1)[0]
    with connect(resolved_db_url) as conn:
        conn.execute(
            """
                UPDATE demo_sessions
                SET status = 'archived', archived_at = COALESCE(archived_at, now())
                WHERE (namespace = %s OR namespace LIKE %s) AND status = 'active'
            """,
            (base_namespace, f"{base_namespace}:session:%"),
        )
        conn.commit()


def _record_poison_demo_session(*, namespace: str, db_url: str) -> None:
    with connect(db_url) as conn:
        conn.execute(
            """
                INSERT INTO demo_sessions (demo_kind, namespace, created_by)
                VALUES ('poison_rewind', %s, 'demo.runner')
                ON CONFLICT (tenant_id, namespace) DO NOTHING
            """,
            (namespace,),
        )
        conn.commit()


def seed_good_demo_memory(
    *,
    namespace: str = DEMO_NAMESPACE,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Seed the known-good memory used at the start of the signature demo."""

    with MemoryStore(
        url=db_url or database_url(),
        embedding_provider=embedding_provider_from_env(),
    ) as store:
        return store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content=GOOD_MEMORY_CONTENT,
            provenance=Provenance(
                writer="demo.seed",
                source_ref="demo:known-good-payment-incident",
                justification="Seed known-good payment latency resolution before poisoning",
            ),
            metadata={
                "demo": "poison-rewind",
                "role": "known-good",
                "kind": "procedural_lesson",
                "operator_disposition": "approved",
                "evidence_quality": "resolved_incident",
                "usage_instruction": "positive_guidance",
            },
        )


def ensure_poison_rewind_incident(*, db_url: str | None = None) -> dict[str, Any]:
    """Create or refresh the product-facing incident used by the signature demo."""

    with connect(db_url or database_url()) as conn:
        with conn.transaction():
            service = conn.execute(
                """
                    INSERT INTO services (id, slug, name, owner_team, tier)
                    VALUES (
                        '10000000-0000-0000-0000-000000000001',
                        %s,
                        'Payments API',
                        'revenue-platform',
                        'critical'
                    )
                    ON CONFLICT (tenant_id, slug) DO UPDATE SET name = excluded.name
                    RETURNING id
                """,
                (DEMO_SERVICE_SLUG,),
            ).fetchone()
            incident = conn.execute(
                """
                    INSERT INTO incidents (
                        id, slug, title, severity, status, started_at, summary
                    )
                    VALUES (
                        '40000000-0000-0000-0000-000000000001',
                        %s,
                        %s,
                        'sev2',
                        'open',
                        now(),
                        %s
                    )
                    ON CONFLICT (tenant_id, slug) DO UPDATE SET
                        title = excluded.title,
                        severity = excluded.severity,
                        status = excluded.status,
                        started_at = excluded.started_at,
                        resolved_at = NULL,
                        summary = excluded.summary,
                        root_cause = NULL
                    RETURNING *
                """,
                (DEMO_INCIDENT_ID, DEMO_TITLE, DEMO_INPUT),
            ).fetchone()
            conn.execute(
                """
                    INSERT INTO incident_services (incident_id, service_id, impact)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (incident_id, service_id) DO UPDATE SET impact = excluded.impact
                """,
                (incident[0], service[0], DEMO_INPUT),
            )
        return {
            "id": str(incident[0]),
            "slug": incident[1],
            "title": incident[2],
            "severity": incident[3],
            "status": incident[4],
        }


def poison_demo_memory(
    *,
    namespace: str = DEMO_NAMESPACE,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Insert the plausible-but-wrong memory used by the signature demo."""

    with start_span(
        "hindsight.demo.poison_memory",
        {
            "hindsight.demo.flow": "poison_rewind",
            "hindsight.memory.namespace": namespace,
        },
    ) as span:
        with MemoryStore(
            url=db_url or database_url(),
            embedding_provider=embedding_provider_from_env(),
        ) as store:
            memory = store.remember(
                memory_kind="semantic",
                namespace=namespace,
                content=POISONED_MEMORY_CONTENT,
                provenance=Provenance(
                    writer="demo.poison",
                    source_ref="demo:simulated-memory-poisoning",
                    justification="Scripted memory poisoning for rewind demonstration",
                ),
                metadata={
                "demo": "poison-rewind",
                "role": "poison",
                "attack_class": "memory_poisoning",
                "kind": "procedural_lesson",
                "operator_disposition": "unreviewed",
                "evidence_quality": "unverified_claim",
                "usage_instruction": "positive_guidance",
            },
            )
        set_span_attributes(span, {"hindsight.memory.id": str(memory["id"])})
        return memory


def run_demo_agent_turn(
    *,
    label: str,
    namespace: str = DEMO_NAMESPACE,
    db_url: str | None = None,
    reasoning_provider: ReasoningProvider | None = None,
) -> AgentRunSummary:
    """Run one deterministic incident-agent turn for the poison/rewind demo."""

    thread_id = f"{namespace}:{label}"
    decision_id = f"agent:{thread_id}:plan"
    provider = reasoning_provider or MemoryBiasedDemoReasoningProvider()
    with start_span(
        "hindsight.demo.agent_turn",
        {
            "hindsight.demo.flow": "poison_rewind",
            "hindsight.demo.label": label,
            "hindsight.agent.thread_id": thread_id,
            "hindsight.memory.namespace": namespace,
            "hindsight.memory.decision_id": decision_id,
        },
    ) as span:
        with MemoryStore(
            url=db_url or database_url(),
            embedding_provider=embedding_provider_from_env(),
        ) as store:
            recalled = list(store.retrieve_semantic(
                namespace=namespace,
                query=DEMO_INPUT,
                limit=5,
                decision_id=decision_id,
                reader="agent.recall",
                purpose="retrieve semantic incident context",
                policy="semantic_strict",
            ).hits)
            plan = provider.generate(
                ReasoningRequest(
                    system=(
                        "You are Hindsight, an incident-response copilot. "
                        "Use recalled memories as context, but propose only reversible, "
                        "operator-reviewable remediation steps."
                    ),
                    prompt=_demo_plan_prompt(recalled),
                    max_output_tokens=512,
                )
            ).text
            poisoned = any(
                isinstance(row.get("metadata"), dict)
                and row["metadata"].get("role") == "poison"
                for row in recalled
            )
            actions = (
                ("scale_workers",)
                if poisoned
                else ("inspect_dependency", "throttle_retries")
            )
            action_request_id = f"action:{thread_id}:request"
            scored = DeterministicIncidentSimulator().execute(
                BoundedActionRequest(
                    request_id=action_request_id,
                    actions=actions,
                )
            )
            action_trace = {
                "schema_version": 1,
                "request": {
                    "id": action_request_id,
                    "mode": "bounded_deterministic_simulator",
                    "tool": scored.tool,
                    "actions": list(actions),
                },
                "approval": {"approved": True, "disposition": "approved"},
                "execution": {
                    "status": "completed",
                    "tool": scored.tool,
                    "allowed_actions": list(scored.allowed_actions),
                },
                "initial_observation": scored.initial_observation,
                "observations": list(scored.observations),
                "score": {
                    "recovered": scored.recovered,
                    "unsafe_action_count": scored.unsafe_action_count,
                },
            }
            proposed_action = (
                "Scale payment workers while retry fanout remains elevated."
                if poisoned
                else "Inspect downstream processor health, then throttle retry fanout."
            )
            reflected = store.remember(
                memory_kind="semantic",
                namespace=namespace,
                content=(
                    f"Incident {DEMO_INCIDENT_ID}-{label} plan: {plan} "
                    f"Proposed action (approved): {proposed_action}"
                ),
                provenance=Provenance(
                    writer="agent.reflect",
                    source_ref=decision_id,
                    justification="Capture incident plan and proposed remediation for future recall",
                ),
                metadata={
                    "thread_id": thread_id,
                    "incident_id": f"{DEMO_INCIDENT_ID}-{label}",
                    "service_slug": DEMO_SERVICE_SLUG,
                    "recalled_memory_ids": [
                        str(row.get("memory_id") or row.get("id"))
                        for row in recalled
                        if row.get("memory_id") or row.get("id")
                    ],
                    "action_approved": True,
                    "action_trace": action_trace,
                },
            )
        set_span_attributes(
            span,
            {
                "hindsight.memory.ids": memory_ids(recalled),
                "hindsight.memory.count": len(recalled),
                "hindsight.memory.id": str(reflected["id"]),
            },
        )
    return AgentRunSummary(
        label=label,
        thread_id=thread_id,
        decision_id=decision_id,
        plan=plan,
        proposed_action=proposed_action,
        reflected_memory_id=str(reflected["id"]),
        recalled_memory_ids=[
            str(row.get("memory_id") or row.get("id"))
            for row in recalled
            if row.get("memory_id") or row.get("id")
        ],
        action_trace=action_trace,
    )


def _demo_plan_prompt(recalled: list[dict[str, Any]]) -> str:
    memory_lines = []
    for idx, memory in enumerate(recalled, start=1):
        content = memory.get("memory_content") or memory.get("content") or ""
        memory_lines.append(f"{idx}. {content}")
    if not memory_lines:
        memory_lines.append("No prior memories were recalled.")
    return "\n".join(
        [
            f"Incident: {DEMO_TITLE}",
            "Severity: sev2",
            f"Service: {DEMO_SERVICE_SLUG}",
            f"Current report: {DEMO_INPUT}",
            "",
            "Recalled memories:",
            *memory_lines,
            "",
            "Return a concise triage plan with suspected cause, checks, and safe next action.",
        ]
    )
