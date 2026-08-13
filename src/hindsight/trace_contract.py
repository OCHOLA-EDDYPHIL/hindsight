"""Read-only identity traces for governed decisions and signature scenarios."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from hindsight.db import connect
from hindsight.demo_state import DEMO_NAMESPACE
from hindsight.agent_decision import (
    PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    operational_action_fingerprint,
)
from hindsight.memory import MemoryStore
from hindsight.redaction import redact_account_identifiers


def decision_influence(*, decision_id: str, db_url: str | None = None) -> dict[str, Any]:
    """Return cited memories, provenance, retrievals, and lineage for one decision."""

    with connect(db_url, application_name="hindsight-decision-influence") as conn:
        store = MemoryStore(conn=conn)
        memories = []
        for read in store.reads_for_decision(decision_id=decision_id):
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
        trace = _governed_decision_trace(conn, decision_id=decision_id)
    return redact_account_identifiers({
        "decision_id": decision_id,
        "count": len(memories),
        "memories": memories,
        "decision": trace["decision"] if trace else None,
        "retrievals": trace["retrievals"] if trace else [],
        "trace": trace,
    })


def governed_decision_trace(
    *, decision_id: str, db_url: str | None = None
) -> dict[str, Any] | None:
    """Return the durable identities connecting one decision to governed memory."""

    with connect(db_url, application_name="hindsight-trace-api") as conn:
        return redact_account_identifiers(
            _governed_decision_trace(conn, decision_id=decision_id)
        )


def signature_scenario_trace(
    *,
    scenario_id: str | None = None,
    decision_id: str | None = None,
    namespace: str | None = None,
    db_url: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one compromised-guidance correction without exposing memory content."""

    selectors = [value for value in (scenario_id, decision_id, namespace) if value]
    if len(selectors) > 1:
        raise ValueError("provide only one scenario selector")

    with connect(db_url, application_name="hindsight-signature-trace") as conn:
        session = _signature_session(
            conn,
            scenario_id=scenario_id,
            decision_id=decision_id,
            namespace=namespace,
        )
        if session is None:
            return None

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT incident.*, service.slug AS service_slug
                    FROM incidents AS incident
                    LEFT JOIN incident_services AS binding
                      ON binding.tenant_id = incident.tenant_id
                     AND binding.incident_id = incident.id
                    LEFT JOIN services AS service
                      ON service.tenant_id = binding.tenant_id
                     AND service.id = binding.service_id
                    WHERE incident.tenant_id = %s
                      AND incident.id = %s
                    ORDER BY service.slug NULLS LAST
                    LIMIT 1
                """,
                (session["tenant_id"], session["incident_id"]),
            )
            incident = cur.fetchone()
            cur.execute(
                """
                    SELECT id, thread_id, incident_id, incident_slug, namespace,
                           service_slug, user_input, status, decision_id, plan, proposed_action,
                           action_approved, provider, model, reflected_memory_id,
                           failure_code, created_at, started_at, updated_at, completed_at
                    FROM agent_runs
                    WHERE tenant_id = %s AND namespace = %s
                    ORDER BY created_at
                """,
                (session["tenant_id"], session["namespace"]),
            )
            runs = [dict(row) for row in cur.fetchall()]
            run_events: dict[str, list[dict[str, Any]]] = {}
            if runs:
                cur.execute(
                    """
                        SELECT run_id, sequence, phase, status, summary, metadata, created_at
                        FROM agent_run_events
                        WHERE run_id = ANY(%s)
                        ORDER BY run_id, sequence
                    """,
                    ([run["id"] for run in runs],),
                )
                for event in cur.fetchall():
                    run_events.setdefault(str(event["run_id"]), []).append(dict(event))
            cur.execute(
                """
                    SELECT id, operation_type, actor, reason, target_timestamp,
                           namespace, invalidated_memory_ids, restored_memory_ids,
                           status, attempt_count, created_at, completed_at,
                           failure_code
                    FROM memory_operations
                    WHERE tenant_id = %s
                      AND namespace = %s
                      AND operation_type = 'rewind'
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                (session["tenant_id"], session["namespace"]),
            )
            operation = cur.fetchone()
            events: list[dict[str, Any]] = []
            effects: list[dict[str, Any]] = []
            if operation is not None:
                cur.execute(
                    """
                        SELECT id, operation_id, sequence, status, summary, created_at
                        FROM memory_operation_events
                        WHERE operation_id = %s
                        ORDER BY sequence
                    """,
                    (operation["id"],),
                )
                events = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                        SELECT operation_id, sequence, effect_type, source_memory_id,
                               result_memory_id, belief_id, namespace, created_at
                        FROM memory_operation_effects
                        WHERE operation_id = %s
                        ORDER BY sequence
                    """,
                    (operation["id"],),
                )
                effects = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT id, namespace, belief_id, version_number,
                           previous_version_id, producer_decision_id, transition_kind,
                           content_schema, lineage_status, trust_status,
                           created_by_operation_id, writer, t_valid, t_invalid,
                           written_at, invalidated_at, metadata
                    FROM semantic_memories
                    WHERE tenant_id = %s AND namespace = %s
                    ORDER BY t_valid, written_at
                """,
                (session["tenant_id"], session["namespace"]),
            )
            memories = [dict(row) for row in cur.fetchall()]

        for run in runs:
            run["trace"] = _governed_decision_trace(conn, decision_id=run["decision_id"])
            run["events"] = run_events.get(str(run["id"]), [])
            run["action_trace"] = next(
                (
                    event["metadata"]["action_trace"]
                    for event in reversed(run["events"])
                    if isinstance(event.get("metadata"), dict)
                    and event["metadata"].get("action_trace")
                ),
                None,
            )

        rejected = _rejected_run_for_operation(runs=runs, operation=operation)
        corrected = next(
            (run for run in reversed(runs) if _is_proven_recovery(run=run, operation=operation)),
            None,
        )
        story_completed = bool(
            rejected
            and _run_precedes_operation(rejected, operation)
            and operation is not None
            and operation["status"] == "completed"
            and corrected
        )
        completed_at_candidates = [
            value
            for value in (
                rejected.get("completed_at") if rejected else None,
                operation.get("completed_at") if operation is not None else None,
                corrected.get("completed_at") if corrected else None,
            )
            if value is not None
        ]
        completed_at = (
            max(completed_at_candidates) if story_completed and completed_at_candidates else None
        )
        seed = next((row for row in memories if row["writer"] == "demo.seed"), None)
        compromised = next(
            (
                row
                for row in memories
                if isinstance(row.get("metadata"), dict)
                and row["metadata"].get("scenario_role") == "compromised_guidance"
            ),
            None,
        )
        for memory in memories:
            memory.pop("metadata", None)
        action_comparison = _action_comparison(
            rejected=rejected,
            corrected=corrected,
            operation=operation,
            operation_effects=effects,
            memories=memories,
            seed=seed,
            compromised=compromised,
        )
        stages = {
            "baseline_memory_id": seed["id"] if seed else None,
            "compromised_memory_id": compromised["id"] if compromised else None,
            # Compatibility alias for clients that predate the compromised-guidance scenario.
            "poison_memory_id": compromised["id"] if compromised else None,
            "influenced_decision_id": rejected["decision_id"] if rejected else None,
            "rewind_operation_id": operation["id"] if operation else None,
            "corrected_decision_id": corrected["decision_id"] if corrected else None,
        }
        return redact_account_identifiers({
            "scenario_id": session["id"],
            "namespace": session["namespace"],
            "status": (
                "completed"
                if story_completed
                else "archived"
                if session["status"] == "archived"
                else "active"
            ),
            "session_status": session["status"],
            "created_at": session["created_at"],
            "completed_at": completed_at,
            "rewind_anchor": session["rewind_anchor"],
            "incident": dict(incident) if incident is not None else None,
            "runs": runs,
            "operation": dict(operation) if operation is not None else None,
            "operation_events": events,
            "operation_effects": effects,
            "memories": memories,
            "stages": stages,
            "action_comparison": action_comparison,
        })


def _action_comparison(
    *,
    rejected: dict[str, Any] | None,
    corrected: dict[str, Any] | None,
    operation: Any | None,
    operation_effects: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    seed: dict[str, Any] | None,
    compromised: dict[str, Any] | None,
) -> dict[str, Any]:
    before = _operational_action(rejected)
    after = _operational_action(corrected)
    same_contract = bool(
        before
        and after
        and before["contract"] == after["contract"]
        and before["contract"] == PAYMENTS_OPERATIONAL_ACTION_CONTRACT
    )
    if same_contract:
        status = (
            "changed"
            if before["primary_action"] != after["primary_action"]
            else "unchanged"
        )
        contract: str | None = PAYMENTS_OPERATIONAL_ACTION_CONTRACT
    else:
        status = "unavailable"
        contract = None

    prompt_equal = bool(
        rejected
        and corrected
        and isinstance(rejected.get("user_input"), str)
        and rejected["user_input"]
        and rejected["user_input"] == corrected.get("user_input")
    )
    before_telemetry = _normalized_telemetry_fingerprint(rejected)
    after_telemetry = _normalized_telemetry_fingerprint(corrected)
    telemetry_equal = bool(
        before_telemetry
        and after_telemetry
        and before_telemetry == after_telemetry
    )
    memory_correction_proven = _memory_correction_proven(
        rejected=rejected,
        corrected=corrected,
        operation=operation,
        operation_effects=operation_effects,
        memories=memories,
        seed=seed,
        compromised=compromised,
    )
    controlled_pair = bool(
        status == "changed"
        and prompt_equal
        and telemetry_equal
        and memory_correction_proven
    )
    return {
        "status": status,
        "contract": contract,
        "before": before,
        "after": after,
        "context": {
            "prompt_equal": prompt_equal,
            "normalized_telemetry_equal": telemetry_equal,
        },
        "memory_correction_proven": memory_correction_proven,
        "controlled_pair": controlled_pair,
    }


def _operational_action(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    trace = run.get("action_trace")
    recommendation = trace.get("recommendation") if isinstance(trace, dict) else None
    action = (
        recommendation.get("operational_action")
        if isinstance(recommendation, dict)
        else None
    )
    if not isinstance(action, dict) or set(action) != {
        "contract",
        "primary_action",
        "fingerprint",
    }:
        return None
    payload = {
        "contract": action.get("contract"),
        "primary_action": action.get("primary_action"),
    }
    try:
        expected = operational_action_fingerprint(payload)
    except (TypeError, ValueError):
        return None
    if action.get("fingerprint") != expected:
        return None
    decision_id = run.get("decision_id")
    if not decision_id:
        return None
    return {
        "decision_id": decision_id,
        **payload,
        "fingerprint": expected,
    }


def _normalized_telemetry_fingerprint(run: dict[str, Any] | None) -> str | None:
    if not isinstance(run, dict):
        return None
    trace = run.get("action_trace")
    observations = trace.get("observations") if isinstance(trace, dict) else None
    if not isinstance(observations, list):
        return None
    normalized: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("status") != "available":
            continue
        tool = observation.get("tool")
        query_key = observation.get("query_key")
        metric = observation.get("metric")
        datapoints = observation.get("datapoints")
        if (
            tool != "aws_cloudwatch_diagnostics"
            or not isinstance(query_key, str)
            or not query_key
            or not isinstance(metric, dict)
            or not isinstance(datapoints, list)
            or not datapoints
        ):
            return None
        namespace = metric.get("namespace")
        name = metric.get("name")
        statistic = metric.get("statistic")
        period_seconds = metric.get("period_seconds")
        dimensions = metric.get("dimensions")
        if (
            not isinstance(namespace, str)
            or not namespace
            or not isinstance(name, str)
            or not name
            or not isinstance(statistic, str)
            or not statistic
            or isinstance(period_seconds, bool)
            or not isinstance(period_seconds, int)
            or period_seconds < 1
            or not isinstance(dimensions, list)
        ):
            return None
        normalized_dimensions: list[dict[str, str]] = []
        for dimension in dimensions:
            if (
                not isinstance(dimension, dict)
                or not isinstance(dimension.get("name"), str)
                or not dimension["name"]
                or not isinstance(dimension.get("value"), str)
                or not dimension["value"]
            ):
                return None
            normalized_dimensions.append(
                {"name": dimension["name"], "value": dimension["value"]}
            )
        finite_points: list[tuple[str, float]] = []
        for datapoint in datapoints:
            if not isinstance(datapoint, dict) or not isinstance(
                datapoint.get("timestamp"), str
            ):
                return None
            value = datapoint.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            resolved_value = float(value)
            if not math.isfinite(resolved_value):
                return None
            finite_points.append(
                (datapoint["timestamp"], 0.0 if resolved_value == 0.0 else resolved_value)
            )
        latest = max(finite_points, key=lambda item: item[0])
        normalized.append(
            {
                "tool": tool,
                "query_key": query_key,
                "metric": {
                    "namespace": namespace,
                    "name": name,
                    "dimensions": sorted(
                        normalized_dimensions,
                        key=lambda item: (item["name"], item["value"]),
                    ),
                    "statistic": statistic,
                    "period_seconds": period_seconds,
                },
                "latest_value": latest[1],
            }
        )
    if not normalized:
        return None
    normalized.sort(
        key=lambda item: (
            item["query_key"],
            item["metric"]["namespace"],
            item["metric"]["name"],
        )
    )
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"telemetry:{digest}"


def _memory_correction_proven(
    *,
    rejected: dict[str, Any] | None,
    corrected: dict[str, Any] | None,
    operation: Any | None,
    operation_effects: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    seed: dict[str, Any] | None,
    compromised: dict[str, Any] | None,
) -> bool:
    if (
        not isinstance(rejected, dict)
        or not isinstance(corrected, dict)
        or not isinstance(operation, dict)
        or operation.get("status") != "completed"
        or not isinstance(seed, dict)
        or not isinstance(compromised, dict)
    ):
        return False
    operation_id = str(operation.get("id") or "")
    seed_id = str(seed.get("id") or "")
    compromised_id = str(compromised.get("id") or "")
    belief_id = str(seed.get("belief_id") or "")
    invalidated = {str(value) for value in operation.get("invalidated_memory_ids") or []}
    if not all((operation_id, seed_id, compromised_id, belief_id)):
        return False
    if not (
        str(compromised.get("belief_id") or "") == belief_id
        and str(compromised.get("previous_version_id") or "") == seed_id
        and compromised.get("transition_kind") == "supersession"
        and compromised.get("t_invalid") is not None
        and compromised_id in invalidated
    ):
        return False
    reasserted = next(
        (
            memory
            for memory in memories
            if memory.get("transition_kind") == "rewind_reassertion"
            and str(memory.get("belief_id") or "") == belief_id
            and str(memory.get("previous_version_id") or "") == compromised_id
            and str(memory.get("created_by_operation_id") or "") == operation_id
            and memory.get("t_invalid") is None
        ),
        None,
    )
    if reasserted is None:
        return False
    reasserted_id = str(reasserted.get("id") or "")
    effect_proven = any(
        effect.get("effect_type") == "reasserted"
        and str(effect.get("source_memory_id") or "") == seed_id
        and str(effect.get("result_memory_id") or "") == reasserted_id
        and str(effect.get("belief_id") or "") == belief_id
        for effect in operation_effects
    )
    rejected_reads = _read_memory_ids(rejected)
    corrected_reads = _read_memory_ids(corrected)
    return bool(
        effect_proven
        and compromised_id in rejected_reads
        and reasserted_id in corrected_reads
        and compromised_id not in corrected_reads
    )


def _read_memory_ids(run: dict[str, Any]) -> set[str]:
    trace = run.get("trace")
    reads = trace.get("reads") if isinstance(trace, dict) else None
    if not isinstance(reads, list):
        return set()
    return {
        str(read.get("memory_id"))
        for read in reads
        if isinstance(read, dict) and read.get("memory_id")
    }


def _run_precedes_operation(run: dict[str, Any], operation: Any | None) -> bool:
    if operation is None or operation.get("completed_at") is None:
        return False
    completed_at = run.get("completed_at")
    return bool(completed_at is not None and completed_at < operation["completed_at"])


def _rejected_run_for_operation(
    *,
    runs: list[dict[str, Any]],
    operation: Any | None,
) -> dict[str, Any] | None:
    rejected = [run for run in runs if run.get("status") == "rejected"]
    if operation is not None and operation.get("completed_at") is not None:
        rejected = [run for run in rejected if _run_precedes_operation(run, operation)]
    return rejected[-1] if rejected else None


def _is_proven_recovery(*, run: dict[str, Any], operation: Any | None) -> bool:
    if (
        operation is None
        or operation.get("status") != "completed"
        or operation.get("completed_at") is None
    ):
        return False
    action_trace = run.get("action_trace")
    approval = action_trace.get("approval") if isinstance(action_trace, dict) else None
    execution = action_trace.get("execution") if isinstance(action_trace, dict) else None
    trace = run.get("trace")
    reads = trace.get("reads") if isinstance(trace, dict) else None
    observed = [run.get("created_at"), run.get("started_at")]
    invalidated = {str(value) for value in operation.get("invalidated_memory_ids") or []}
    return bool(
        run.get("status") == "completed"
        and run.get("action_approved") is True
        and any(
            timestamp is not None and timestamp > operation["completed_at"]
            for timestamp in observed
        )
        and isinstance(approval, dict)
        and approval.get("approved") is True
        and isinstance(execution, dict)
        and execution.get("status") == "recommendation_approved"
        and isinstance(reads, list)
        and reads
        and all(str(read.get("memory_id")) not in invalidated for read in reads)
    )


def _signature_session(
    conn: Any,
    *,
    scenario_id: str | None,
    decision_id: str | None,
    namespace: str | None,
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        if scenario_id:
            try:
                resolved_scenario_id = UUID(scenario_id)
            except ValueError:
                return None
            cur.execute(
                """
                    SELECT id, tenant_id, namespace, status, incident_id,
                           rewind_anchor, created_at
                    FROM demo_sessions
                    WHERE id = %s
                      AND demo_kind IN ('compromised_guidance_rewind', 'poison_rewind')
                """,
                (resolved_scenario_id,),
            )
        elif decision_id:
            cur.execute(
                """
                    SELECT session.id, session.tenant_id, session.namespace,
                           session.status, session.incident_id,
                           session.rewind_anchor, session.created_at
                    FROM agent_runs AS run
                    JOIN demo_sessions AS session
                      ON session.tenant_id = run.tenant_id
                     AND session.namespace = run.namespace
                    WHERE run.decision_id = %s
                      AND session.demo_kind IN (
                          'compromised_guidance_rewind',
                          'poison_rewind'
                      )
                """,
                (decision_id,),
            )
        elif namespace:
            cur.execute(
                """
                    SELECT id, tenant_id, namespace, status, incident_id,
                           rewind_anchor, created_at
                    FROM demo_sessions
                    WHERE namespace = %s
                      AND demo_kind IN ('compromised_guidance_rewind', 'poison_rewind')
                """,
                (namespace,),
            )
        else:
            cur.execute(
                """
                    SELECT session.id, session.tenant_id, session.namespace,
                           session.status, session.incident_id,
                           session.rewind_anchor, session.created_at
                    FROM demo_sessions AS session
                    WHERE session.demo_kind IN (
                        'compromised_guidance_rewind',
                        'poison_rewind'
                    )
                      AND session.created_by = 'dashboard.operator'
                      AND session.namespace LIKE %s
                      AND EXISTS (
                          SELECT 1
                          FROM memory_operations AS operation
                          WHERE operation.tenant_id = session.tenant_id
                            AND operation.namespace = session.namespace
                            AND operation.operation_type = 'rewind'
                            AND operation.status = 'completed'
                            AND operation.completed_at IS NOT NULL
                            AND operation.id = (
                                SELECT latest_operation.id
                                FROM memory_operations AS latest_operation
                                WHERE latest_operation.tenant_id = session.tenant_id
                                  AND latest_operation.namespace = session.namespace
                                  AND latest_operation.operation_type = 'rewind'
                                ORDER BY latest_operation.created_at DESC
                                LIMIT 1
                            )
                            AND EXISTS (
                                SELECT 1
                                FROM agent_runs AS rejected_run
                                WHERE rejected_run.tenant_id = session.tenant_id
                                  AND rejected_run.namespace = session.namespace
                                  AND rejected_run.status = 'rejected'
                                  AND rejected_run.completed_at < operation.completed_at
                            )
                            AND EXISTS (
                                SELECT 1
                                FROM agent_runs AS recovered_run
                                WHERE recovered_run.tenant_id = session.tenant_id
                                  AND recovered_run.namespace = session.namespace
                                  AND recovered_run.status = 'completed'
                                  AND recovered_run.action_approved IS TRUE
                                  AND (
                                      recovered_run.created_at > operation.completed_at
                                      OR recovered_run.started_at > operation.completed_at
                                  )
                                  AND EXISTS (
                                      SELECT 1
                                      FROM agent_run_events AS recovered_event
                                      WHERE recovered_event.tenant_id =
                                              recovered_run.tenant_id
                                        AND recovered_event.run_id = recovered_run.id
                                        AND recovered_event.metadata @>
                                            '{"action_trace":{"approval":{"approved":true},"execution":{"status":"recommendation_approved"}}}'::JSONB
                                  )
                                  AND EXISTS (
                                      SELECT 1
                                      FROM memory_reads AS recovered_read
                                      WHERE recovered_read.tenant_id =
                                              recovered_run.tenant_id
                                        AND recovered_read.decision_id =
                                              recovered_run.decision_id
                                  )
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM memory_reads AS invalidated_read
                                      WHERE invalidated_read.tenant_id =
                                              recovered_run.tenant_id
                                        AND invalidated_read.decision_id =
                                              recovered_run.decision_id
                                        AND operation.invalidated_memory_ids @>
                                            jsonb_build_array(
                                                invalidated_read.memory_id::STRING
                                            )
                                  )
                            )
                      )
                    ORDER BY session.created_at DESC
                    LIMIT 1
                """,
                (f"{DEMO_NAMESPACE}:session:%",),
            )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _governed_decision_trace(conn: Any, *, decision_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT id, actor, decision_kind, purpose, run_id, namespace,
                       status, opened_at, sealed_at
                FROM memory_decisions
                WHERE id = %s
            """,
            (decision_id,),
        )
        decision = cur.fetchone()
        if decision is None:
            return None

        cur.execute(
            """
                SELECT retrieval.id, retrieval.decision_id, retrieval.namespace,
                       retrieval.reader, retrieval.purpose, retrieval.policy,
                       retrieval.policy_version, retrieval.requested_limit,
                       retrieval.status, retrieval.selected_strategy,
                       retrieval.fallback_reason, retrieval.embedding_profile_id,
                       retrieval.returned_memory_ids, retrieval.error_code,
                       retrieval.started_at, retrieval.completed_at,
                       profile.provider AS embedding_provider,
                       profile.model AS embedding_model,
                       profile.dimensions AS embedding_dimensions,
                       profile.capability AS embedding_capability,
                       profile.encoder_revision,
                       profile.max_distance
                FROM memory_retrievals AS retrieval
                LEFT JOIN embedding_profiles AS profile
                    ON profile.id = retrieval.embedding_profile_id
                WHERE retrieval.decision_id = %s
                ORDER BY retrieval.started_at
            """,
            (decision_id,),
        )
        retrievals = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
                SELECT read.id, read.decision_id, read.memory_kind,
                       read.memory_id, read.reader, read.purpose, read.read_at,
                       read.retrieval_id, read.rank, read.distance,
                       semantic.belief_id, semantic.version_number,
                       semantic.previous_version_id,
                       semantic.producer_decision_id,
                       semantic.transition_kind, semantic.content_schema,
                       semantic.lineage_status, semantic.trust_status,
                       semantic.created_by_operation_id,
                       COALESCE(semantic.producer_decision_id,
                                episodic.producer_decision_id) AS memory_producer_decision_id,
                       COALESCE(semantic.content_schema,
                                episodic.content_schema) AS memory_content_schema,
                       COALESCE(semantic.lineage_status,
                                episodic.lineage_status) AS memory_lineage_status,
                       COALESCE(semantic.trust_status,
                                episodic.trust_status) AS memory_trust_status,
                       COALESCE(semantic.writer, episodic.writer) AS writer,
                       COALESCE(semantic.source_ref, episodic.source_ref) AS source_ref,
                       COALESCE(semantic.justification, episodic.justification) AS justification,
                       COALESCE(semantic.t_valid, episodic.t_valid) AS t_valid,
                       COALESCE(semantic.t_invalid, episodic.t_invalid) AS t_invalid,
                       CASE
                           WHEN COALESCE(semantic.t_invalid, episodic.t_invalid) IS NULL
                           THEN 'current'
                           ELSE 'invalidated'
                       END AS memory_status
                FROM memory_reads AS read
                LEFT JOIN semantic_memories AS semantic
                    ON read.semantic_memory_id = semantic.id
                LEFT JOIN episodic_memories AS episodic
                    ON read.episodic_memory_id = episodic.id
                WHERE read.decision_id = %s
                ORDER BY read.read_at, read.rank
            """,
            (decision_id,),
        )
        reads = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
                SELECT evidence.id, evidence.semantic_memory_id,
                       evidence.episodic_memory_id, evidence.evidence_kind,
                       evidence.evidence_digest, evidence.observed_at,
                       evidence.actor, evidence.created_at
                FROM memory_external_evidence AS evidence
                WHERE evidence.semantic_memory_id IN (
                    SELECT semantic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND semantic_memory_id IS NOT NULL
                ) OR evidence.episodic_memory_id IN (
                    SELECT episodic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND episodic_memory_id IS NOT NULL
                )
                ORDER BY evidence.created_at
            """,
            (decision_id, decision_id),
        )
        evidence = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
                SELECT edge.id, edge.child_semantic_memory_id,
                       edge.child_episodic_memory_id, edge.parent_read_id,
                       edge.producer_decision_id, edge.edge_type,
                       edge.created_at, parent.memory_kind AS parent_memory_kind,
                       parent.memory_id AS parent_memory_id,
                       parent.retrieval_id AS parent_retrieval_id
                FROM memory_lineage_edges AS edge
                JOIN memory_reads AS parent ON parent.id = edge.parent_read_id
                WHERE edge.parent_read_id IN (
                    SELECT id FROM memory_reads WHERE decision_id = %s
                ) OR edge.child_semantic_memory_id IN (
                    SELECT semantic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND semantic_memory_id IS NOT NULL
                ) OR edge.child_episodic_memory_id IN (
                    SELECT episodic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND episodic_memory_id IS NOT NULL
                )
                ORDER BY edge.created_at, edge.id
            """,
            (decision_id, decision_id, decision_id),
        )
        lineage = [dict(row) for row in cur.fetchall()]

    retrieval_profiles = {str(row["id"]): row.get("embedding_profile_id") for row in retrievals}
    for read in reads:
        memory_id = str(read["memory_id"])
        read_id = str(read["id"])
        read["embedding_profile_id"] = retrieval_profiles.get(str(read["retrieval_id"]))
        read["evidence_ids"] = [
            row["id"]
            for row in evidence
            if str(row.get("semantic_memory_id") or row.get("episodic_memory_id")) == memory_id
        ]
        read["incoming_lineage_edge_ids"] = [
            row["id"]
            for row in lineage
            if str(row.get("child_semantic_memory_id") or row.get("child_episodic_memory_id"))
            == memory_id
        ]
        read["outgoing_lineage_edge_ids"] = [
            row["id"] for row in lineage if str(row["parent_read_id"]) == read_id
        ]
    return {
        "decision": dict(decision),
        "retrievals": retrievals,
        "reads": reads,
        "evidence": evidence,
        "lineage_edges": lineage,
    }
