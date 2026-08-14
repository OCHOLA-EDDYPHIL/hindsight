"""Read-only identity traces for governed decisions and signature scenarios."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from pydantic import ValidationError

from hindsight.db import connect
from hindsight.demo_state import DEMO_NAMESPACE
from hindsight.causal_evidence import (
    CANONICALIZATION_ID,
    CAUSAL_EVIDENCE_SCHEMA_VERSION,
    GOVERNED_MEMORY_PROMPT_MARKER,
    canonical_sha256,
    json_contract_value,
    text_sha256,
    validated_causal_envelope,
)
from hindsight.agent_decision import (
    AgentDecisionError,
    MAX_DIAGNOSTIC_CALLS,
    MAX_MODEL_TURNS,
    PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    agent_decision_provider_schema,
    canonicalize_operational_action,
    controlled_action_selection_from_payload,
    controlled_action_selection_provider_schema,
    operational_action_catalog,
    operational_action_directive,
    operational_action_fingerprint,
)
from hindsight.memory import MemoryStore
from hindsight.redaction import redact_account_identifiers
from hindsight.runs import _public_run_response


_CONTROLLED_INVARIANT_FIELDS = (
    "normalized_user_incident",
    "prompt_templates",
    "triage_result",
    "ordered_tool_calls",
    "ordered_observations",
    "ordered_model_request_configuration",
    "tool_contract",
    "embedding_profile",
    "release_revision",
    "action_catalog",
    "tenant_id",
    "namespace",
    "scenario_id",
    "replay_anchor",
    "retrieval_policy",
    "retrieval_policy_version",
)


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
    return redact_account_identifiers(
        {
            "decision_id": decision_id,
            "count": len(memories),
            "memories": memories,
            "decision": trace["decision"] if trace else None,
            "retrievals": trace["retrievals"] if trace else [],
            "trace": trace,
        }
    )


def governed_decision_trace(
    *, decision_id: str, db_url: str | None = None
) -> dict[str, Any] | None:
    """Return the durable identities connecting one decision to governed memory."""

    with connect(db_url, application_name="hindsight-trace-api") as conn:
        return redact_account_identifiers(_governed_decision_trace(conn, decision_id=decision_id))


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
            (
                run
                for run in reversed(runs)
                if _is_proven_post_correction_recommendation(
                    run=run,
                    operation=operation,
                )
            ),
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
        proof_states = _causal_proof_states(
            rejected=rejected,
            corrected=corrected,
            operation=operation,
            operation_effects=effects,
            memories=memories,
            seed=seed,
            compromised=compromised,
        )
        controlled_pair_checks = _controlled_pair_checks(
            rejected=rejected,
            corrected=corrected,
            operation=operation,
            operation_effects=effects,
            memory_correction_proven=(
                proof_states["memory_correction_proven"]["status"] == "proven"
            ),
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
        public_runs = [_public_run_projection(run) for run in runs]
        public_rejected = _public_run_projection(rejected) if rejected is not None else None
        public_corrected = _public_run_projection(corrected) if corrected is not None else None
        scenario = redact_account_identifiers(
            {
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
                "runs": public_runs,
                "operation": dict(operation) if operation is not None else None,
                "operation_events": events,
                "operation_effects": effects,
                "memories": memories,
                "stages": stages,
                "action_comparison": action_comparison,
                "causal_evidence": {
                    "schema_version": CAUSAL_EVIDENCE_SCHEMA_VERSION,
                    "canonicalization": CANONICALIZATION_ID,
                    "scope": "recommendation_only",
                    "proof_states": proof_states,
                    "controlled_pair_checks": controlled_pair_checks,
                    "before_envelope_sha256": _envelope_digest(public_rejected),
                    "after_envelope_sha256": _envelope_digest(public_corrected),
                },
            }
        )
        evidence = causal_evidence_document(scenario)
        scenario["causal_evidence"]["download"] = {
            "url": f"/v1/signature-scenarios/{session['id']}/evidence",
            "protected_url": f"/v2/signature-scenarios/{session['id']}/evidence",
            "sha256": canonical_sha256(evidence),
            "media_type": "application/json",
        }
        return scenario


def signature_scenario_evidence(
    *,
    scenario_id: str,
    db_url: str | None = None,
) -> dict[str, Any] | None:
    """Return the canonical, redacted evidence download for one scenario."""

    scenario = signature_scenario_trace(scenario_id=scenario_id, db_url=db_url)
    if scenario is None:
        return None
    return causal_evidence_document(scenario)


def causal_evidence_document(scenario: dict[str, Any]) -> dict[str, Any]:
    """Project one trace into the stable download contract and bind its digest."""

    stages = scenario.get("stages") if isinstance(scenario.get("stages"), dict) else {}
    runs = scenario.get("runs") if isinstance(scenario.get("runs"), list) else []
    before = next(
        (
            run
            for run in runs
            if isinstance(run, dict)
            and run.get("decision_id") == stages.get("influenced_decision_id")
        ),
        None,
    )
    after = next(
        (
            run
            for run in runs
            if isinstance(run, dict)
            and run.get("decision_id") == stages.get("corrected_decision_id")
        ),
        None,
    )
    summary = (
        scenario.get("causal_evidence") if isinstance(scenario.get("causal_evidence"), dict) else {}
    )
    unsigned = json_contract_value(
        {
            "schema_version": CAUSAL_EVIDENCE_SCHEMA_VERSION,
            "canonicalization": CANONICALIZATION_ID,
            "scope": "recommendation_only",
            "scenario": {
                "scenario_id": scenario.get("scenario_id"),
                "namespace": "[redacted-namespace]",
                "rewind_anchor": scenario.get("rewind_anchor"),
                "created_at": scenario.get("created_at"),
                "completed_at": scenario.get("completed_at"),
            },
            "proof_states": summary.get("proof_states") or {},
            "controlled_pair_checks": summary.get("controlled_pair_checks") or [],
            "correction": {
                "stages": _public_receipt_stages(stages),
                "operation": _public_receipt_operation(scenario.get("operation")),
                "events": [
                    _public_receipt_operation_event(item)
                    for item in scenario.get("operation_events") or []
                    if isinstance(item, dict)
                ],
                "effects": [
                    _public_receipt_operation_effect(item)
                    for item in scenario.get("operation_effects") or []
                    if isinstance(item, dict)
                ],
                "memory_versions": [
                    _public_receipt_memory(item)
                    for item in scenario.get("memories") or []
                    if isinstance(item, dict)
                ],
            },
            "declared_intervention": {
                "before": _evidence_intervention(before),
                "after": _evidence_intervention(after),
                "correction_operation_id": _public_receipt_identifier(
                    stages.get("rewind_operation_id")
                ),
                "operation_effects": [
                    _public_receipt_operation_effect(item)
                    for item in scenario.get("operation_effects") or []
                    if isinstance(item, dict)
                ],
                "invalidated_memory_fingerprints": [
                    canonical_sha256(str(memory_id))
                    for memory_id in (
                        (
                            (scenario.get("operation") or {}).get("invalidated_memory_ids")
                            if isinstance(scenario.get("operation"), dict)
                            else []
                        )
                        or []
                    )
                ],
                "restored_memory_fingerprints": [
                    canonical_sha256(str(memory_id))
                    for memory_id in (
                        (
                            (scenario.get("operation") or {}).get("restored_memory_ids")
                            if isinstance(scenario.get("operation"), dict)
                            else []
                        )
                        or []
                    )
                ],
            },
            "recommendations": {
                "before": _evidence_recommendation(before),
                "after": _evidence_recommendation(after),
            },
        }
    )
    return unsigned


def _public_receipt_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    try:
        return str(UUID(text))
    except ValueError:
        return canonical_sha256(text)


def _public_receipt_stages(stages: dict[str, Any]) -> dict[str, Any]:
    return {key: _public_receipt_identifier(value) for key, value in stages.items()}


def _public_receipt_operation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "id": _public_receipt_identifier(value.get("id")),
        "operation_type": value.get("operation_type"),
        "status": value.get("status"),
        "target_timestamp": value.get("target_timestamp"),
        "invalidated_memory_ids": [
            _public_receipt_identifier(item)
            for item in value.get("invalidated_memory_ids") or []
        ],
        "restored_memory_ids": [
            _public_receipt_identifier(item)
            for item in value.get("restored_memory_ids") or []
        ],
        "attempt_count": value.get("attempt_count"),
        "created_at": value.get("created_at"),
        "completed_at": value.get("completed_at"),
        "failure_code": value.get("failure_code"),
    }


def _public_receipt_operation_event(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _public_receipt_identifier(value.get("id")),
        "operation_id": _public_receipt_identifier(value.get("operation_id")),
        "sequence": value.get("sequence"),
        "status": value.get("status"),
        "created_at": value.get("created_at"),
    }


def _public_receipt_operation_effect(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": _public_receipt_identifier(value.get("operation_id")),
        "sequence": value.get("sequence"),
        "effect_type": value.get("effect_type"),
        "source_memory_id": _public_receipt_identifier(value.get("source_memory_id")),
        "result_memory_id": _public_receipt_identifier(value.get("result_memory_id")),
        "belief_id": _public_receipt_identifier(value.get("belief_id")),
        "namespace": "[redacted-namespace]",
        "created_at": value.get("created_at"),
    }


def _public_receipt_memory(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _public_receipt_identifier(value.get("id")),
        "namespace": "[redacted-namespace]",
        "belief_id": _public_receipt_identifier(value.get("belief_id")),
        "version_number": value.get("version_number"),
        "previous_version_id": _public_receipt_identifier(value.get("previous_version_id")),
        "producer_decision_id": _public_receipt_identifier(
            value.get("producer_decision_id")
        ),
        "transition_kind": value.get("transition_kind"),
        "content_schema": value.get("content_schema"),
        "lineage_status": value.get("lineage_status"),
        "trust_status": value.get("trust_status"),
        "created_by_operation_id": _public_receipt_identifier(
            value.get("created_by_operation_id")
        ),
        "t_valid": value.get("t_valid"),
        "t_invalid": value.get("t_invalid"),
        "written_at": value.get("written_at"),
        "invalidated_at": value.get("invalidated_at"),
    }


def _evidence_intervention(run: dict[str, Any] | None) -> dict[str, Any] | None:
    envelope = _causal_envelope(run)
    if envelope is None:
        return None
    return envelope["permitted_intervention"]


def _public_run_projection(run: dict[str, Any]) -> dict[str, Any]:
    projected = _public_run_response(deepcopy(run))
    trace = projected.get("action_trace")
    if not isinstance(trace, dict):
        return projected
    candidate = trace.get("causal_envelope")
    validated = validated_causal_envelope(candidate)
    if validated is not None and _valid_controlled_envelope(validated):
        trace["causal_envelope"] = validated
    else:
        trace.pop("causal_envelope", None)
    return projected


def _evidence_recommendation(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    trace = run.get("action_trace")
    if not isinstance(trace, dict):
        return None
    recommendation = trace.get("recommendation")
    if not isinstance(recommendation, dict):
        return None
    action = _operational_action(run)
    envelope = _causal_envelope(run)
    projected_action = (
        {key: value for key, value in action.items() if key != "decision_id"}
        if action is not None
        else None
    )
    return {
        "decision_id": run.get("decision_id"),
        "mode": trace.get("mode") if action is not None else None,
        "summary": action.get("directive") if action is not None else None,
        "operational_action": projected_action,
        "causal_envelope": envelope,
        "execution": trace.get("execution") if action is not None else None,
    }


def _causal_envelope(run: dict[str, Any] | None) -> dict[str, Any] | None:
    trace = run.get("action_trace") if isinstance(run, dict) else None
    envelope = trace.get("causal_envelope") if isinstance(trace, dict) else None
    validated = validated_causal_envelope(envelope)
    return validated if validated is not None and _valid_controlled_envelope(validated) else None


def _valid_controlled_envelope(envelope: dict[str, Any]) -> bool:
    identity = envelope["identity"]
    invariants = envelope["invariant_inputs"]
    intervention = envelope["permitted_intervention"]
    actual = envelope["actual_decision_inputs"]
    if set(identity) != {
        "scenario_id",
        "namespace",
        "replay_anchor",
        "scenario_routing_key",
        "run_id",
        "decision_id",
        "release_revision",
    }:
        return False
    if any(
        not isinstance(identity.get(key), str) or not identity[key]
        for key in (
            "scenario_id",
            "namespace",
            "replay_anchor",
            "scenario_routing_key",
            "run_id",
            "decision_id",
            "release_revision",
        )
    ):
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", str(identity.get("release_revision") or "")):
        return False
    if _normalized_timestamp_text(identity.get("replay_anchor")) is None:
        return False
    required_invariants = {
        "normalized_user_incident",
        "prompt_templates",
        "triage_result",
        "ordered_tool_calls",
        "ordered_observations",
        "ordered_model_request_configuration",
        "tool_contract",
        "embedding_profile",
        "release_revision",
        "action_catalog",
        "tenant_id",
        "namespace",
        "scenario_id",
        "replay_anchor",
        "retrieval_policy",
        "retrieval_policy_version",
    }
    if set(invariants) != required_invariants:
        return False
    templates = invariants.get("prompt_templates")
    if (
        not isinstance(templates, dict)
        or set(templates) != {"triage", "decision", "system"}
        or any(
            not isinstance(template, dict)
            or set(template) != {"id", "sha256"}
            or not template.get("id")
            or not _is_sha256_label(template.get("sha256"))
            for template in templates.values()
        )
    ):
        return False
    if (
        invariants.get("release_revision") != identity["release_revision"]
        or invariants.get("namespace") != identity.get("namespace")
        or invariants.get("scenario_id") != identity.get("scenario_id")
        or invariants.get("replay_anchor") != identity.get("replay_anchor")
        or not invariants.get("tenant_id")
        or invariants.get("action_catalog")
        != operational_action_catalog(PAYMENTS_OPERATIONAL_ACTION_CONTRACT)
        or invariants.get("retrieval_policy") not in {"semantic_strict", "semantic_then_keyword"}
        or invariants.get("retrieval_policy_version") != 1
    ):
        return False
    tool_calls = invariants.get("ordered_tool_calls")
    observations = invariants.get("ordered_observations")
    request_configurations = invariants.get("ordered_model_request_configuration")
    tool_contract = invariants.get("tool_contract")
    embedding_profile = invariants.get("embedding_profile")
    if (
        not isinstance(tool_calls, list)
        or not tool_calls
        or any(
            not isinstance(call, dict) or set(call) != {"id", "tool", "query_key", "status"}
            for call in tool_calls
        )
        or not isinstance(observations, list)
        or not observations
        or any(not _complete_controlled_observation(item) for item in observations)
        or not isinstance(request_configurations, list)
        or not request_configurations
        or any(not _complete_request_configuration(item) for item in request_configurations)
        or tool_contract
        != {
            "schema_version": 1,
            "diagnostic_tool": "aws_cloudwatch_diagnostics",
            "observation_schema_version": 1,
            "allowed_query_keys": ["payments.retry_fanout"],
            "max_diagnostic_calls": MAX_DIAGNOSTIC_CALLS,
        }
        or not _complete_embedding_profile(embedding_profile)
    ):
        return False
    if (
        len(tool_calls) != len(observations)
        or len({call.get("id") for call in tool_calls}) != len(tool_calls)
        or len({observation.get("id") for observation in observations}) != len(observations)
        or any(
            not call.get("id")
            or call.get("tool") != "aws_cloudwatch_diagnostics"
            or call.get("query_key") not in tool_contract["allowed_query_keys"]
            or call.get("status") != "completed"
            or observation.get("tool_call_id") != call.get("id")
            or observation.get("query_key") != call.get("query_key")
            for call, observation in zip(tool_calls, observations, strict=True)
        )
    ):
        return False
    if set(intervention) != {
        "kind",
        "ordered_memory_versions",
        "selection_fingerprint",
        "expected_changed_prompt_fragments",
        "correction_operation_id",
        "correction_target_timestamp",
        "operation_effects",
        "invalidated_memory_fingerprints",
        "restored_memory_fingerprints",
    }:
        return False
    memories = intervention.get("ordered_memory_versions")
    if (
        intervention.get("kind") != "governed_memory_version_selection.v1"
        or not isinstance(intervention.get("selection_fingerprint"), str)
        or not intervention["selection_fingerprint"]
        or not isinstance(memories, list)
        or any(
            not _complete_memory_intervention(memory, ordinal=ordinal)
            for ordinal, memory in enumerate(memories, start=1)
        )
    ):
        return False
    if intervention.get("expected_changed_prompt_fragments") != [
        memory["prompt_fragment_sha256"] for memory in memories
    ]:
        return False
    correction_operation_id = intervention.get("correction_operation_id")
    correction_target_timestamp = intervention.get("correction_target_timestamp")
    operation_effects = intervention.get("operation_effects")
    invalidated_fingerprints = intervention.get("invalidated_memory_fingerprints")
    restored_fingerprints = intervention.get("restored_memory_fingerprints")
    if correction_operation_id is not None and (
        not isinstance(correction_operation_id, str) or not correction_operation_id
    ):
        return False
    if (
        correction_target_timestamp is not None
        and _normalized_timestamp_text(correction_target_timestamp) is None
    ):
        return False
    if (
        not isinstance(operation_effects, list)
        or any(
            not _complete_operation_effect(effect, sequence=sequence)
            for sequence, effect in enumerate(operation_effects, start=1)
        )
        or not isinstance(invalidated_fingerprints, list)
        or any(not _is_sha256_label(value) for value in invalidated_fingerprints)
        or not isinstance(restored_fingerprints, list)
        or any(not _is_sha256_label(value) for value in restored_fingerprints)
    ):
        return False
    if correction_operation_id is None and any(
        (
            correction_target_timestamp,
            operation_effects,
            invalidated_fingerprints,
            restored_fingerprints,
        )
    ):
        return False
    if correction_operation_id is not None and (
        correction_target_timestamp is None or not operation_effects or not invalidated_fingerprints
    ):
        return False
    if set(actual) != {
        "incident",
        "triage",
        "retrieval_policy",
        "embedding_profile",
        "ordered_governed_memories",
        "ordered_tool_calls",
        "ordered_observations",
        "ordered_model_requests",
        "tool_contract",
        "action_catalog",
    }:
        return False
    if (
        not _canonical_equal(actual.get("ordered_governed_memories"), memories)
        or not _canonical_equal(
            actual.get("ordered_tool_calls"), invariants.get("ordered_tool_calls")
        )
        or not _canonical_equal(
            actual.get("ordered_observations"), invariants.get("ordered_observations")
        )
        or not _canonical_equal(actual.get("action_catalog"), invariants.get("action_catalog"))
        or not _canonical_equal(actual.get("triage"), invariants.get("triage_result"))
        or actual.get("retrieval_policy") != invariants.get("retrieval_policy")
        or not _canonical_equal(
            actual.get("embedding_profile"), invariants.get("embedding_profile")
        )
        or not _canonical_equal(actual.get("tool_contract"), tool_contract)
    ):
        return False
    actual_requests = actual.get("ordered_model_requests")
    normalized_actual_requests = (
        [_request_invariant_from_actual(item) for item in actual_requests]
        if isinstance(actual_requests, list)
        and all(isinstance(item, dict) for item in actual_requests)
        else None
    )
    if (
        not isinstance(actual_requests, list)
        or len(actual_requests) != len(request_configurations)
        or any(not _complete_actual_request(item, memories=memories) for item in actual_requests)
        or not _canonical_equal(normalized_actual_requests, request_configurations)
        or any(
            request.get("routing_key")
            != f"{identity['scenario_routing_key']}:turn:{request.get('logical_turn')}"
            for request in actual_requests
        )
    ):
        return False
    incident = actual.get("incident")
    triage = invariants.get("triage_result")
    if (
        not isinstance(incident, dict)
        or set(incident)
        != {
            "incident_id",
            "namespace",
            "service_slug",
            "severity",
            "title",
            "normalized_user_incident",
        }
        or incident.get("namespace") != invariants.get("namespace")
        or incident.get("normalized_user_incident") != invariants.get("normalized_user_incident")
        or not isinstance(triage, dict)
        or set(triage)
        != {
            "incident_id",
            "namespace",
            "service_slug",
            "severity",
            "title",
            "summary",
            "prior_chat_messages",
        }
        or incident.get("incident_id") != triage.get("incident_id")
        or incident.get("namespace") != triage.get("namespace")
        or incident.get("service_slug") != triage.get("service_slug")
        or incident.get("severity") != triage.get("severity")
        or incident.get("title") != triage.get("title")
        or incident.get("normalized_user_incident") != triage.get("summary")
        or type(triage.get("prior_chat_messages")) is not int
        or triage["prior_chat_messages"] < 0
    ):
        return False
    try:
        selection = controlled_action_selection_from_payload(envelope.get("decision_output"))
        canonicalize_operational_action(
            selection,
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )
    except (AgentDecisionError, ValidationError, TypeError, ValueError):
        return False
    return _valid_controlled_request_sequence(
        actual_requests,
        memories=memories,
        tool_calls=tool_calls,
        tool_contract=tool_contract,
    )


def _is_sha256_label(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value))


def _complete_memory_intervention(value: Any, *, ordinal: int) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"ordinal", "memory", "memory_sha256", "prompt_fragment_sha256"}
        or value.get("ordinal") != ordinal
        or not isinstance(value.get("memory"), dict)
    ):
        return False
    memory = value["memory"]
    prompt_fragment = f"{ordinal}. {json.dumps(memory, sort_keys=True)}"
    return bool(
        value.get("memory_sha256") == canonical_sha256(memory)
        and value.get("prompt_fragment_sha256") == text_sha256(prompt_fragment)
    )


def _complete_operation_effect(value: Any, *, sequence: int) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value)
        == {
            "sequence",
            "effect_type",
            "source_memory_id",
            "result_memory_id",
            "belief_id",
            "namespace",
        }
        and value.get("sequence") == sequence
        and isinstance(value.get("effect_type"), str)
        and value["effect_type"]
        and isinstance(value.get("namespace"), str)
        and value["namespace"]
        and all(
            item is None or (isinstance(item, str) and item)
            for item in (
                value.get("source_memory_id"),
                value.get("result_memory_id"),
                value.get("belief_id"),
            )
        )
    )


def _complete_embedding_profile(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "profile_id",
            "provider",
            "model",
            "dimensions",
            "capability",
            "encoder_revision",
            "configuration",
            "max_distance",
        }
        or any(
            not isinstance(value.get(key), str) or not value[key]
            for key in ("profile_id", "provider", "model", "capability", "encoder_revision")
        )
        or type(value.get("dimensions")) is not int
        or value["dimensions"] < 1
        or not isinstance(value.get("configuration"), dict)
    ):
        return False
    max_distance = value.get("max_distance")
    return bool(
        max_distance is None
        or (
            not isinstance(max_distance, bool)
            and isinstance(max_distance, (int, float))
            and math.isfinite(float(max_distance))
        )
    )


def _complete_request_configuration(value: Any) -> bool:
    required = {
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
        "response_json_schema",
    }
    return bool(
        isinstance(value, dict)
        and set(value) == required
        and _valid_request_metadata(value)
        and value.get("schema_version") == 1
        and value.get("provider")
        and value.get("model")
        and value.get("system")
        and isinstance(value.get("prompt_invariant"), str)
        and value["prompt_invariant"].count(GOVERNED_MEMORY_PROMPT_MARKER) == 1
        and value.get("prompt_invariant_sha256") == text_sha256(value["prompt_invariant"])
        and _is_sha256_label(value.get("prompt_invariant_sha256"))
        and value.get("routing_key")
        and _valid_controlled_request_contract(value)
        and isinstance(value.get("response_json_schema"), dict)
    )


def _complete_actual_request(value: Any, *, memories: list[dict[str, Any]]) -> bool:
    required = {
        "schema_version",
        "attempt",
        "repair_reason",
        "logical_turn",
        "provider",
        "model",
        "system",
        "prompt",
        "prompt_invariant",
        "prompt_invariant_sha256",
        "temperature",
        "max_output_tokens",
        "routing_key",
        "decision_contract",
        "response_schema_version",
        "response_json_schema",
    }
    if not (
        isinstance(value, dict)
        and set(value) == required
        and _valid_request_metadata(value)
        and isinstance(value.get("prompt"), str)
        and value.get("prompt")
        and isinstance(value.get("prompt_invariant"), str)
        and value["prompt_invariant"].count(GOVERNED_MEMORY_PROMPT_MARKER) == 1
        and value.get("prompt_invariant_sha256") == text_sha256(value["prompt_invariant"])
        and _valid_controlled_request_contract(value)
        and isinstance(value.get("response_json_schema"), dict)
    ):
        return False
    memory_block = _rendered_memory_block(memories)
    expected_prompt = value["prompt_invariant"].replace(
        GOVERNED_MEMORY_PROMPT_MARKER,
        memory_block,
    )
    return value["prompt"] == expected_prompt


def _valid_controlled_request_contract(value: dict[str, Any]) -> bool:
    contract = value.get("decision_contract")
    schema_version = value.get("response_schema_version")
    return bool(
        (contract == "AgentDecisionV3" and schema_version == 3)
        or (contract == "ControlledActionSelectionV1" and schema_version == 1)
    )


def _valid_controlled_request_sequence(
    requests: list[dict[str, Any]],
    *,
    memories: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    tool_contract: dict[str, Any],
) -> bool:
    """Bind request order and every provider schema to the runtime contract."""

    if [request.get("logical_turn") for request in requests] != list(
        range(1, len(requests) + 1)
    ):
        return False
    groups: list[list[dict[str, Any]]] = []
    for request in requests:
        attempt = request.get("attempt")
        if attempt == 1:
            groups.append([request])
        elif (
            attempt == 2
            and groups
            and len(groups[-1]) == 1
            and groups[-1][0].get("attempt") == 1
            and groups[-1][0].get("decision_contract")
            == request.get("decision_contract")
        ):
            groups[-1].append(request)
        else:
            return False
    contracts = [group[0].get("decision_contract") for group in groups]
    try:
        first_selection = contracts.index("ControlledActionSelectionV1")
    except ValueError:
        return False
    if (
        first_selection == 0
        or first_selection != len(tool_calls)
        or contracts[first_selection:] != ["ControlledActionSelectionV1"]
        or any(contract != "AgentDecisionV3" for contract in contracts[:first_selection])
    ):
        return False
    recalled_memory_ids = {
        str(memory["memory"].get("memory_id") or memory["memory"].get("id"))
        for memory in memories
        if isinstance(memory.get("memory"), dict)
        and (memory["memory"].get("memory_id") or memory["memory"].get("id"))
    }
    allowed_query_keys = set(tool_contract["allowed_query_keys"])
    for group_index, group in enumerate(groups):
        for request in group:
            try:
                expected_schema = (
                    agent_decision_provider_schema(
                        recalled_memory_ids=recalled_memory_ids,
                        allowed_query_keys=allowed_query_keys,
                        diagnostic_calls_used=group_index,
                        diagnostic_observation_available=False,
                        model_turn=request["logical_turn"],
                        operational_action_contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
                    )
                    if group_index < first_selection
                    else controlled_action_selection_provider_schema(
                        contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT
                    )
                )
            except (KeyError, TypeError, ValueError):
                return False
            if not _canonical_equal(request.get("response_json_schema"), expected_schema):
                return False
    return True


def _rendered_memory_block(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "No prior memories were recalled."
    return "\n".join(
        f"{ordinal}. {json.dumps(item['memory'], sort_keys=True)}"
        for ordinal, item in enumerate(memories, start=1)
    )


def _valid_request_metadata(value: dict[str, Any]) -> bool:
    attempt = value.get("attempt")
    repair_reason = value.get("repair_reason")
    logical_turn = value.get("logical_turn")
    routing_key = value.get("routing_key")
    return bool(
        type(attempt) is int
        and attempt in {1, 2}
        and (
            (attempt == 1 and repair_reason is None)
            or (attempt == 2 and isinstance(repair_reason, str) and repair_reason)
        )
        and type(logical_turn) is int
        and 1 <= logical_turn <= MAX_MODEL_TURNS
        and type(value.get("temperature")) is int
        and value["temperature"] == 0
        and type(value.get("max_output_tokens")) is int
        and value["max_output_tokens"] == 1_024
        and isinstance(routing_key, str)
        and routing_key.startswith("signature:")
        and routing_key.endswith(f":turn:{logical_turn}")
    )


def _request_invariant_from_actual(value: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads(json.dumps(value["response_json_schema"], sort_keys=True))
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        definitions["MemoryCitation"] = {
            "bound_to": "permitted_intervention.ordered_memory_versions"
        }
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties["recalled_memory_citations"] = {
            "bound_to": "permitted_intervention.ordered_memory_versions"
        }
    return {
        key: item
        for key, item in value.items()
        if key != "prompt" and key != "response_json_schema"
    } | {"response_json_schema": schema}


def _complete_controlled_observation(value: Any) -> bool:
    required = {
        "id",
        "tool_call_id",
        "schema_version",
        "tool",
        "query_key",
        "query_fingerprint",
        "status",
        "region",
        "metric",
        "window",
        "datapoints",
        "datapoint_count",
        "truncated",
    }
    if not isinstance(value, dict) or set(value) != required:
        return False
    metric = value.get("metric")
    window = value.get("window")
    datapoints = value.get("datapoints")
    if (
        any(
            not isinstance(value.get(key), str) or not value[key]
            for key in ("id", "tool_call_id", "query_key", "region")
        )
        or value.get("schema_version") != 1
        or value.get("tool") != "aws_cloudwatch_diagnostics"
        or not isinstance(value.get("query_fingerprint"), str)
        or re.fullmatch(r"cloudwatch_query:[0-9a-f]{64}", value["query_fingerprint"]) is None
        or value.get("status") != "available"
        or not isinstance(metric, dict)
        or set(metric) != {"namespace", "name", "dimensions", "statistic", "unit", "period_seconds"}
        or not isinstance(window, dict)
        or set(window) != {"start", "end", "seconds"}
        or not isinstance(datapoints, list)
        or not datapoints
        or value.get("datapoint_count") != len(datapoints)
        or not isinstance(value.get("truncated"), bool)
    ):
        return False
    dimensions = metric.get("dimensions")
    period_seconds = metric.get("period_seconds")
    if (
        any(
            not isinstance(metric.get(key), str) or not metric[key]
            for key in ("namespace", "name", "statistic", "unit")
        )
        or metric["statistic"] not in {"Average", "Maximum", "Minimum", "SampleCount", "Sum"}
        or type(period_seconds) is not int
        or not 60 <= period_seconds <= 300
        or period_seconds % 60 != 0
        or not isinstance(dimensions, list)
        or len(dimensions) > 10
        or any(
            not isinstance(dimension, dict)
            or set(dimension) != {"name", "value"}
            or not isinstance(dimension.get("name"), str)
            or not dimension["name"]
            or not isinstance(dimension.get("value"), str)
            or not dimension["value"]
            for dimension in dimensions
        )
    ):
        return False
    dimension_pairs = [(item["name"], item["value"]) for item in dimensions]
    if len({name for name, _value in dimension_pairs}) != len(
        dimension_pairs
    ) or dimension_pairs != sorted(dimension_pairs):
        return False
    start = _parse_utc_timestamp(window.get("start"))
    end = _parse_utc_timestamp(window.get("end"))
    window_seconds = window.get("seconds")
    if (
        start is None
        or end is None
        or type(window_seconds) is not int
        or not 60 <= window_seconds <= 900
        or window_seconds % period_seconds != 0
        or end <= start
        or int((end - start).total_seconds()) != window_seconds
        or len(datapoints) > window_seconds // period_seconds
    ):
        return False
    normalized_points: list[tuple[datetime, float]] = []
    for point in datapoints:
        if (
            not isinstance(point, dict)
            or set(point) != {"timestamp", "value"}
            or isinstance(point.get("value"), bool)
            or not isinstance(point.get("value"), (int, float))
            or not math.isfinite(float(point["value"]))
        ):
            return False
        timestamp = _parse_utc_timestamp(point.get("timestamp"))
        if timestamp is None or not start <= timestamp < end:
            return False
        normalized_points.append((timestamp, float(point["value"])))
    return normalized_points == sorted(normalized_points)


def _parse_utc_timestamp(value: Any) -> datetime | None:
    parsed = _parse_aware_timestamp(value)
    if parsed is None:
        return None
    normalized = parsed.astimezone(UTC)
    if normalized.microsecond != 0:
        return None
    if value != normalized.strftime("%Y-%m-%dT%H:%M:%SZ"):
        return None
    return normalized


def _parse_aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_sha256(left) == canonical_sha256(right)
    except (TypeError, ValueError):
        return False


def _envelope_digest(run: dict[str, Any] | None) -> str | None:
    envelope = _causal_envelope(run)
    return str(envelope["envelope_sha256"]) if envelope is not None else None


def _proof_state(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _controlled_pair_checks(
    *,
    rejected: dict[str, Any] | None,
    corrected: dict[str, Any] | None,
    operation: Any | None,
    operation_effects: list[dict[str, Any]],
    memory_correction_proven: bool,
) -> list[dict[str, str]]:
    before = _causal_envelope(rejected)
    after = _causal_envelope(corrected)
    if before is None or after is None:
        return [
            {
                "field": "causal_envelope",
                "status": "unavailable",
                "reason": "causal_envelope_incomplete_or_invalid",
            }
        ]

    checks: list[dict[str, str]] = []

    def equality_check(field: str, before_value: Any, after_value: Any) -> None:
        matched = _canonical_equal(before_value, after_value)
        checks.append(
            {
                "field": field,
                "status": "matched" if matched else "mismatched",
                "reason": f"{field.replace('.', '_')}_{'matched' if matched else 'mismatch'}",
            }
        )

    for field in (
        "scenario_id",
        "namespace",
        "replay_anchor",
        "scenario_routing_key",
        "release_revision",
    ):
        equality_check(
            f"identity.{field}",
            before["identity"][field],
            after["identity"][field],
        )
    for field in _CONTROLLED_INVARIANT_FIELDS:
        equality_check(
            f"invariant_inputs.{field}",
            before["invariant_inputs"][field],
            after["invariant_inputs"][field],
        )

    before_intervention = before["permitted_intervention"]
    after_intervention = after["permitted_intervention"]
    memory_delta_valid = _memory_delta_matches_operation(
        before=before_intervention,
        after=after_intervention,
        operation=operation,
    )
    checks.append(
        {
            "field": "permitted_intervention.ordered_memory_versions",
            "status": "matched" if memory_delta_valid else "mismatched",
            "reason": (
                "declared_memory_intervention_delta_verified"
                if memory_delta_valid
                else "declared_memory_intervention_delta_mismatch"
            ),
        }
    )
    prompt_changed = before["rendered_prompt_sha256"] != after["rendered_prompt_sha256"]
    checks.append(
        {
            "field": "rendered_prompt_sha256",
            "status": "matched" if prompt_changed else "mismatched",
            "reason": (
                "rendered_prompt_delta_declared"
                if prompt_changed
                else "rendered_prompt_delta_missing"
            ),
        }
    )
    operation_bound = _interventions_bind_operation(
        before=before_intervention,
        after=after_intervention,
        operation=operation,
        operation_effects=operation_effects,
        replay_anchor=after["identity"]["replay_anchor"],
    )
    checks.append(
        {
            "field": "permitted_intervention.correction_operation",
            "status": "matched" if operation_bound else "mismatched",
            "reason": (
                "correction_operation_delta_verified"
                if operation_bound
                else "correction_operation_delta_mismatch"
            ),
        }
    )
    checks.append(
        {
            "field": "memory_correction",
            "status": "matched" if memory_correction_proven else "mismatched",
            "reason": (
                "memory_correction_proven"
                if memory_correction_proven
                else "memory_correction_not_proven"
            ),
        }
    )
    return checks


def _interventions_bind_operation(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    operation: Any | None,
    operation_effects: list[dict[str, Any]],
    replay_anchor: str,
) -> bool:
    if not isinstance(operation, dict) or not operation.get("id"):
        return False
    expected_effects = [
        {
            "sequence": effect.get("sequence"),
            "effect_type": effect.get("effect_type"),
            "source_memory_id": (
                str(effect["source_memory_id"])
                if effect.get("source_memory_id") is not None
                else None
            ),
            "result_memory_id": (
                str(effect["result_memory_id"])
                if effect.get("result_memory_id") is not None
                else None
            ),
            "belief_id": (
                str(effect["belief_id"]) if effect.get("belief_id") is not None else None
            ),
            "namespace": effect.get("namespace"),
        }
        for effect in operation_effects
        if isinstance(effect, dict)
    ]
    expected_invalidated = [
        canonical_sha256(str(memory_id))
        for memory_id in (operation.get("invalidated_memory_ids") or [])
    ]
    expected_restored = [
        canonical_sha256(str(memory_id))
        for memory_id in (operation.get("restored_memory_ids") or [])
    ]
    target_timestamp = _normalized_timestamp_text(operation.get("target_timestamp"))
    return bool(
        _memory_delta_matches_operation(before=before, after=after, operation=operation)
        and before.get("correction_operation_id") is None
        and before.get("correction_target_timestamp") is None
        and before.get("operation_effects") == []
        and before.get("invalidated_memory_fingerprints") == []
        and before.get("restored_memory_fingerprints") == []
        and after.get("correction_operation_id") == str(operation["id"])
        and target_timestamp is not None
        and after.get("correction_target_timestamp") == target_timestamp
        and target_timestamp == _normalized_timestamp_text(replay_anchor)
        and after.get("operation_effects") == expected_effects
        and after.get("invalidated_memory_fingerprints") == expected_invalidated
        and after.get("restored_memory_fingerprints") == expected_restored
    )


def _memory_delta_matches_operation(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    operation: Any | None,
) -> bool:
    if not isinstance(operation, dict):
        return False
    before_items = before.get("ordered_memory_versions")
    after_items = after.get("ordered_memory_versions")
    invalidated = [str(value) for value in operation.get("invalidated_memory_ids") or []]
    restored = [str(value) for value in operation.get("restored_memory_ids") or []]
    if (
        not isinstance(before_items, list)
        or not isinstance(after_items, list)
        or not invalidated
        or not restored
    ):
        return False

    def indexed(items: list[dict[str, Any]]) -> tuple[list[str], dict[str, dict[str, Any]]] | None:
        identifiers: list[str] = []
        mapped: dict[str, dict[str, Any]] = {}
        for item in items:
            memory = item.get("memory") if isinstance(item, dict) else None
            memory_id = memory.get("memory_id") if isinstance(memory, dict) else None
            if not isinstance(memory_id, str) or not memory_id or memory_id in mapped:
                return None
            identifiers.append(memory_id)
            mapped[memory_id] = item
        return identifiers, mapped

    before_index = indexed(before_items)
    after_index = indexed(after_items)
    if before_index is None or after_index is None:
        return False
    before_ids, before_by_id = before_index
    after_ids, after_by_id = after_index
    common_before = [memory_id for memory_id in before_ids if memory_id in after_by_id]
    common_after = [memory_id for memory_id in after_ids if memory_id in before_by_id]
    removed = [memory_id for memory_id in before_ids if memory_id not in after_by_id]
    added = [memory_id for memory_id in after_ids if memory_id not in before_by_id]
    return bool(
        removed
        and added
        and common_before == common_after
        and all(
            _canonical_equal(before_by_id[memory_id], after_by_id[memory_id])
            for memory_id in common_before
        )
        and all(memory_id in invalidated for memory_id in removed)
        and all(memory_id in restored for memory_id in added)
        and all(memory_id not in invalidated for memory_id in common_before)
        and all(memory_id not in restored for memory_id in common_after)
        and before.get("selection_fingerprint") != after.get("selection_fingerprint")
    )


def _normalized_timestamp_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    parsed = _parse_aware_timestamp(value)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") if parsed is not None else None


def _causal_proof_states(
    *,
    rejected: dict[str, Any] | None,
    corrected: dict[str, Any] | None,
    operation: Any | None,
    operation_effects: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    seed: dict[str, Any] | None,
    compromised: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    controlled_envelopes_available = bool(
        _causal_envelope(rejected) is not None and _causal_envelope(corrected) is not None
    )
    memory_inputs_available = all(
        (
            isinstance(rejected, dict),
            isinstance(corrected, dict),
            isinstance(operation, dict),
            isinstance(seed, dict),
            isinstance(compromised, dict),
        )
    )
    memory_proven = _memory_correction_proven(
        rejected=rejected,
        corrected=corrected,
        operation=operation,
        operation_effects=operation_effects,
        memories=memories,
        seed=seed,
        compromised=compromised,
    )
    if not controlled_envelopes_available:
        memory_state = _proof_state("unavailable", "causal_envelope_incomplete_or_invalid")
    elif not memory_inputs_available:
        memory_state = _proof_state("unavailable", "memory_correction_evidence_incomplete")
    elif memory_proven:
        memory_state = _proof_state("proven", "rewind_lineage_and_reads_verified")
    else:
        memory_state = _proof_state("not_proven", "memory_correction_checks_failed")

    before_action = _operational_action(rejected)
    after_action = _operational_action(corrected)
    if not controlled_envelopes_available:
        action_state = _proof_state("unavailable", "causal_envelope_incomplete_or_invalid")
    elif before_action is None or after_action is None:
        action_state = _proof_state("unavailable", "catalog_actions_incomplete_or_invalid")
    elif before_action["fingerprint"] != after_action["fingerprint"]:
        action_state = _proof_state("proven", "catalog_action_changed")
    else:
        action_state = _proof_state("not_proven", "catalog_action_unchanged")

    pair_checks = _controlled_pair_checks(
        rejected=rejected,
        corrected=corrected,
        operation=operation,
        operation_effects=operation_effects,
        memory_correction_proven=memory_proven,
    )
    if any(check["status"] == "unavailable" for check in pair_checks):
        pair_state = _proof_state("unavailable", "causal_envelope_incomplete_or_invalid")
    elif all(check["status"] == "matched" for check in pair_checks):
        pair_state = _proof_state("proven", "fixed_context_and_memory_delta_verified")
    else:
        first_mismatch = next(
            check["reason"] for check in pair_checks if check["status"] == "mismatched"
        )
        pair_state = _proof_state("not_proven", first_mismatch)

    return {
        "memory_correction_proven": memory_state,
        "action_delta_proven": action_state,
        "controlled_pair_eligible": pair_state,
        "repeatable_causal_effect_supported": _proof_state(
            "unavailable",
            "repeated_trials_not_measured",
        ),
        "service_recovery_proven": _proof_state(
            "unavailable",
            "service_recovery_not_measured",
        ),
    }


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
        status = "changed" if before["fingerprint"] != after["fingerprint"] else "unchanged"
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
        before_telemetry and after_telemetry and before_telemetry == after_telemetry
    )
    memory_correction_proven = bool(
        _causal_envelope(rejected) is not None
        and _causal_envelope(corrected) is not None
        and _memory_correction_proven(
            rejected=rejected,
            corrected=corrected,
            operation=operation,
            operation_effects=operation_effects,
            memories=memories,
            seed=seed,
            compromised=compromised,
        )
    )
    proof_states = _causal_proof_states(
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
        and proof_states["controlled_pair_eligible"]["status"] == "proven"
        and proof_states["action_delta_proven"]["status"] == "proven"
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
    if (
        not isinstance(trace, dict)
        or trace.get("schema_version") != 4
        or trace.get("mode") != "recommendation_only"
    ):
        return None
    recommendation = trace.get("recommendation") if isinstance(trace, dict) else None
    action = recommendation.get("operational_action") if isinstance(recommendation, dict) else None
    if not isinstance(action, dict) or set(action) != {
        "catalog_id",
        "contract",
        "action_id",
        "disposition",
        "parameters",
        "primary_action",
        "directive",
        "consistency_status",
        "fingerprint",
    }:
        return None
    payload = {
        "catalog_id": action.get("catalog_id"),
        "contract": action.get("contract"),
        "action_id": action.get("action_id"),
        "disposition": action.get("disposition"),
        "parameters": action.get("parameters"),
    }
    try:
        expected = operational_action_fingerprint(payload)
    except (TypeError, ValueError):
        return None
    if (
        action.get("fingerprint") != expected
        or action.get("primary_action") != action.get("action_id")
        or action.get("directive") != operational_action_directive(payload)
        or action.get("consistency_status") != "consistent"
    ):
        return None
    decision_id = run.get("decision_id")
    if not decision_id:
        return None
    envelope = _causal_envelope(run)
    if envelope is None or envelope["identity"].get("decision_id") != decision_id:
        return None
    try:
        selection = controlled_action_selection_from_payload(envelope["decision_output"])
        envelope_action = canonicalize_operational_action(
            selection,
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        ).model_dump(mode="json")
    except (AgentDecisionError, ValidationError, TypeError, ValueError):
        return None
    if envelope_action != payload:
        return None
    expected_directive = operational_action_directive(payload)
    if (
        recommendation.get("summary") != expected_directive
        or recommendation.get("rationale") != selection.rationale
    ):
        return None
    return {
        "decision_id": decision_id,
        **payload,
        "primary_action": action["action_id"],
        "directive": action["directive"],
        "consistency_status": action["consistency_status"],
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
            normalized_dimensions.append({"name": dimension["name"], "value": dimension["value"]})
        finite_points: list[tuple[str, float]] = []
        for datapoint in datapoints:
            if not isinstance(datapoint, dict) or not isinstance(datapoint.get("timestamp"), str):
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
    rejected_target = _read_for_memory(rejected, compromised_id)
    corrected_target = _read_for_memory(corrected, reasserted_id)
    return bool(
        effect_proven
        and _reads_valid_at_use(rejected)
        and _reads_valid_at_use(corrected)
        and rejected_target is not None
        and corrected_target is not None
        and rejected_target.get("incoming_lineage_edge_ids")
        and corrected_target.get("incoming_lineage_edge_ids")
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


def _read_for_memory(run: dict[str, Any], memory_id: str) -> dict[str, Any] | None:
    trace = run.get("trace")
    reads = trace.get("reads") if isinstance(trace, dict) else None
    if not isinstance(reads, list):
        return None
    return next(
        (
            read
            for read in reads
            if isinstance(read, dict) and str(read.get("memory_id") or "") == memory_id
        ),
        None,
    )


def _reads_valid_at_use(run: dict[str, Any]) -> bool:
    trace = run.get("trace")
    reads = trace.get("reads") if isinstance(trace, dict) else None
    if not isinstance(reads, list) or not reads:
        return False
    for read in reads:
        if (
            not isinstance(read, dict)
            or not read.get("memory_id")
            or read.get("memory_lineage_status") != "complete"
        ):
            return False
        read_at = _aware_timestamp(read.get("read_at"))
        valid_at = _aware_timestamp(read.get("t_valid"))
        invalid_at = _aware_timestamp(read.get("t_invalid"))
        if read_at is None or valid_at is None or valid_at > read_at:
            return False
        if invalid_at is not None and read_at >= invalid_at:
            return False
    return True


def _aware_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        resolved = value
    elif isinstance(value, str):
        try:
            resolved = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        return None
    return resolved


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


def _is_proven_post_correction_recommendation(
    *,
    run: dict[str, Any],
    operation: Any | None,
) -> bool:
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
