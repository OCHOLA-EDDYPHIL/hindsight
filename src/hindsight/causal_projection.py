"""Canonical public projection for controlled causal evidence envelopes."""

from __future__ import annotations

import json
import re
from typing import Any

from hindsight.agent_decision import (
    CONTROLLED_ACTION_SELECTION_RATIONALES,
    PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
    AgentDecisionError,
    agent_decision_provider_schema,
    controlled_action_selection_from_decision,
    controlled_action_selection_provider_schema,
    controlled_decision_from_selection,
)
from hindsight.causal_evidence import (
    GOVERNED_MEMORY_PROMPT_MARKER,
    build_causal_envelope,
    canonical_sha256,
    text_sha256,
)


def public_causal_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Build the one digest-valid public projection of exact internal evidence."""

    source_identity = envelope["identity"]
    source_invariants = envelope["invariant_inputs"]
    source_intervention = envelope["permitted_intervention"]
    source_actual = envelope["actual_decision_inputs"]
    public_namespace = "[redacted-namespace]"
    public_scenario_id = _public_identifier(
        source_identity.get("scenario_id"),
        kind="scenario",
    )
    public_identity = {
        "scenario_id": public_scenario_id,
        "namespace": public_namespace,
        "replay_anchor": source_identity["replay_anchor"],
        "scenario_routing_key": source_identity["scenario_routing_key"],
        "run_id": _public_identifier(source_identity.get("run_id"), kind="run"),
        "decision_id": _public_identifier(
            source_identity.get("decision_id"),
            kind="decision",
        ),
        "release_revision": source_identity["release_revision"],
    }

    source_memories = source_intervention.get("ordered_memory_versions") or []
    public_memories = [
        _public_memory_intervention(item, ordinal=ordinal)
        for ordinal, item in enumerate(source_memories, start=1)
    ]
    public_tool_calls = [
        {
            "id": f"diagnostic:{index}",
            "tool": call["tool"],
            "query_key": call["query_key"],
            "status": call["status"],
        }
        for index, call in enumerate(
            source_invariants.get("ordered_tool_calls") or [],
            start=1,
        )
    ]
    public_observations = [
        _public_controlled_observation(item, ordinal=ordinal)
        for ordinal, item in enumerate(
            source_invariants.get("ordered_observations") or [],
            start=1,
        )
    ]
    public_requests = []
    diagnostic_groups_seen = 0
    for item in source_actual.get("ordered_model_requests") or []:
        if item.get("decision_contract") == "AgentDecisionV3":
            if item.get("attempt") == 1:
                diagnostic_calls_used = diagnostic_groups_seen
                diagnostic_groups_seen += 1
            else:
                diagnostic_calls_used = max(0, diagnostic_groups_seen - 1)
        else:
            diagnostic_calls_used = diagnostic_groups_seen
        public_requests.append(
            _public_model_request(
                item,
                memories=public_memories,
                diagnostic_calls_used=diagnostic_calls_used,
                allowed_query_keys=set(
                    (source_invariants.get("tool_contract") or {}).get("allowed_query_keys") or []
                ),
            )
        )
    public_request_configurations = [_model_request_invariant(item) for item in public_requests]
    source_triage = source_invariants.get("triage_result") or {}
    public_incident_id = _public_identifier(
        source_triage.get("incident_id"),
        kind="incident",
    )
    public_triage = {
        "incident_id": public_incident_id,
        "namespace": public_namespace,
        "service_slug": _public_contract_label(
            source_triage.get("service_slug"),
            fallback="redacted-service",
        ),
        "severity": _public_contract_label(
            source_triage.get("severity"),
            fallback="redacted-severity",
        ),
        "title": "[redacted-title]",
        "summary": "[redacted-incident]",
        "prior_chat_messages": source_triage.get("prior_chat_messages", 0),
    }
    source_embedding = source_invariants.get("embedding_profile") or {}
    public_embedding = {
        "profile_id": _public_identifier(
            source_embedding.get("profile_id"),
            kind="embedding-profile",
        ),
        "provider": _public_contract_label(
            source_embedding.get("provider"),
            fallback="redacted-provider",
        ),
        "model": _public_contract_label(
            source_embedding.get("model"),
            fallback="redacted-model",
        ),
        "dimensions": source_embedding.get("dimensions"),
        "capability": _public_contract_label(
            source_embedding.get("capability"),
            fallback="redacted-capability",
        ),
        "encoder_revision": _public_contract_label(
            source_embedding.get("encoder_revision"),
            fallback="redacted-encoder",
        ),
        "configuration": {},
        "max_distance": source_embedding.get("max_distance"),
    }
    public_templates = json.loads(
        json.dumps(source_invariants.get("prompt_templates") or {}, sort_keys=True)
    )
    if isinstance(public_templates.get("system"), dict) and public_requests:
        public_templates["system"]["sha256"] = text_sha256(public_requests[-1]["system"])
    public_invariants = {
        "normalized_user_incident": "[redacted-incident]",
        "prompt_templates": public_templates,
        "triage_result": public_triage,
        "ordered_tool_calls": public_tool_calls,
        "ordered_observations": public_observations,
        "ordered_model_request_configuration": public_request_configurations,
        "tool_contract": source_invariants["tool_contract"],
        "embedding_profile": public_embedding,
        "release_revision": source_identity["release_revision"],
        "action_catalog": source_invariants["action_catalog"],
        "tenant_id": "[redacted-tenant]",
        "namespace": public_namespace,
        "scenario_id": public_scenario_id,
        "replay_anchor": source_identity["replay_anchor"],
        "retrieval_policy": source_invariants["retrieval_policy"],
        "retrieval_policy_version": source_invariants["retrieval_policy_version"],
    }
    public_effects = [
        _public_operation_effect(item, ordinal=ordinal)
        for ordinal, item in enumerate(
            source_intervention.get("operation_effects") or [],
            start=1,
        )
    ]
    public_intervention = {
        "kind": source_intervention["kind"],
        "ordered_memory_versions": public_memories,
        "selection_fingerprint": source_intervention["selection_fingerprint"],
        "expected_changed_prompt_fragments": [
            item["prompt_fragment_sha256"] for item in public_memories
        ],
        "correction_operation_id": (
            _public_identifier(
                source_intervention.get("correction_operation_id"),
                kind="operation",
            )
            if source_intervention.get("correction_operation_id") is not None
            else None
        ),
        "correction_target_timestamp": source_intervention.get("correction_target_timestamp"),
        "operation_effects": public_effects,
        "invalidated_memory_fingerprints": list(
            source_intervention.get("invalidated_memory_fingerprints") or []
        ),
        "restored_memory_fingerprints": list(
            source_intervention.get("restored_memory_fingerprints") or []
        ),
    }
    public_actual = {
        "incident": {
            "incident_id": public_incident_id,
            "namespace": public_namespace,
            "service_slug": public_triage["service_slug"],
            "severity": public_triage["severity"],
            "title": public_triage["title"],
            "normalized_user_incident": public_triage["summary"],
        },
        "triage": public_triage,
        "retrieval_policy": public_invariants["retrieval_policy"],
        "embedding_profile": public_embedding,
        "ordered_governed_memories": public_memories,
        "ordered_tool_calls": public_tool_calls,
        "ordered_observations": public_observations,
        "ordered_model_requests": public_requests,
        "tool_contract": public_invariants["tool_contract"],
        "action_catalog": public_invariants["action_catalog"],
    }
    source_selection = controlled_action_selection_from_decision(
        controlled_decision_from_selection(
            envelope["decision_output"],
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )
    )
    public_selection = {
        "action_id": source_selection.action_id,
        "disposition": source_selection.disposition,
        "parameters": source_selection.parameters.model_dump(mode="json"),
        "rationale": CONTROLLED_ACTION_SELECTION_RATIONALES[0],
    }
    return build_causal_envelope(
        identity=public_identity,
        invariant_inputs=public_invariants,
        permitted_intervention=public_intervention,
        actual_decision_inputs=public_actual,
        rendered_prompt_sha256=[text_sha256(request["prompt"]) for request in public_requests],
        decision_output=public_selection,
    )


def _public_identifier(value: Any, *, kind: str) -> str:
    return f"{kind}:{canonical_sha256(str(value)).removeprefix('sha256:')}"


def _public_contract_label(value: Any, *, fallback: str) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", text) else fallback


def _public_memory_intervention(item: dict[str, Any], *, ordinal: int) -> dict[str, Any]:
    source = item.get("memory") if isinstance(item.get("memory"), dict) else {}
    public_memory = {
        "memory_id": _public_identifier(source.get("memory_id"), kind="memory"),
        "belief_id": _public_identifier(source.get("belief_id"), kind="belief"),
        "version": source.get("version"),
        "content": "[redacted-memory-content]",
        "kind": _public_contract_label(source.get("kind"), fallback="redacted-kind"),
        "status": _public_contract_label(source.get("status"), fallback="redacted-status"),
        "trust": _public_contract_label(source.get("trust"), fallback="redacted-trust"),
        "transition": _public_contract_label(
            source.get("transition"),
            fallback="redacted-transition",
        ),
        "operator_disposition": _public_contract_label(
            source.get("operator_disposition"),
            fallback="redacted-disposition",
        ),
        "safety_status": _public_contract_label(
            source.get("safety_status"),
            fallback="redacted-safety",
        ),
        "contradiction_status": _public_contract_label(
            source.get("contradiction_status"),
            fallback="redacted-contradiction",
        ),
        "applicability": None,
        "evidence_quality": _public_contract_label(
            source.get("evidence_quality"),
            fallback="redacted-evidence-quality",
        ),
        "evidence": [
            {
                "writer": "[redacted-writer]",
                "source_ref": "[redacted-source]",
                "justification": "[redacted-justification]",
            }
        ],
        "usage_instruction": _public_contract_label(
            source.get("usage_instruction"),
            fallback="audit_only",
        ),
        "source_memory_sha256": item.get("memory_sha256"),
    }
    prompt_fragment = f"{ordinal}. {json.dumps(public_memory, sort_keys=True)}"
    return {
        "ordinal": ordinal,
        "memory": public_memory,
        "memory_sha256": canonical_sha256(public_memory),
        "prompt_fragment_sha256": text_sha256(prompt_fragment),
    }


def _public_controlled_observation(item: dict[str, Any], *, ordinal: int) -> dict[str, Any]:
    metric = item.get("metric") if isinstance(item.get("metric"), dict) else {}
    dimensions = []
    for index, dimension in enumerate(metric.get("dimensions") or [], start=1):
        name = _public_contract_label(
            dimension.get("name") if isinstance(dimension, dict) else None,
            fallback=f"Dimension{index:02d}",
        )
        source_value = dimension.get("value") if isinstance(dimension, dict) else None
        value = (
            str(source_value)
            if name == "Scenario" and source_value == "payments-checkout-latency"
            else str(source_value)
            if name == "Service" and source_value == "payments-api"
            else "[redacted-dimension]"
        )
        dimensions.append({"name": name, "value": value})
    dimensions.sort(key=lambda dimension: (dimension["name"], dimension["value"]))
    return {
        "id": f"observation:{ordinal}",
        "tool_call_id": f"diagnostic:{ordinal}",
        "schema_version": item.get("schema_version"),
        "tool": item.get("tool"),
        "query_key": item.get("query_key"),
        "query_fingerprint": item.get("query_fingerprint"),
        "status": item.get("status"),
        "region": _public_contract_label(item.get("region"), fallback="redacted-region"),
        "metric": {
            "namespace": _public_contract_label(
                metric.get("namespace"),
                fallback="redacted/metric",
            ),
            "name": _public_contract_label(
                metric.get("name"),
                fallback="redacted-metric",
            ),
            "dimensions": dimensions,
            "statistic": metric.get("statistic"),
            "unit": metric.get("unit"),
            "period_seconds": metric.get("period_seconds"),
        },
        "window": item.get("window"),
        "datapoints": item.get("datapoints"),
        "datapoint_count": item.get("datapoint_count"),
        "truncated": item.get("truncated"),
    }


def _public_model_request(
    item: dict[str, Any],
    *,
    memories: list[dict[str, Any]],
    diagnostic_calls_used: int,
    allowed_query_keys: set[str],
) -> dict[str, Any]:
    prompt_invariant = (
        "Public controlled replay input.\n"
        f"{GOVERNED_MEMORY_PROMPT_MARKER}\n"
        "All non-memory inputs are bound by the invariant envelope digest."
    )
    memory_block = (
        "\n".join(
            f"{ordinal}. {json.dumps(memory['memory'], sort_keys=True)}"
            for ordinal, memory in enumerate(memories, start=1)
        )
        if memories
        else "No prior memories were recalled."
    )
    recalled_memory_ids = {
        str(memory["memory"].get("memory_id") or memory["memory"].get("id"))
        for memory in memories
        if isinstance(memory.get("memory"), dict)
        and (memory["memory"].get("memory_id") or memory["memory"].get("id"))
    }
    decision_contract = item.get("decision_contract")
    response_schema = (
        controlled_action_selection_provider_schema(contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT)
        if decision_contract == "ControlledActionSelectionV1"
        else agent_decision_provider_schema(
            recalled_memory_ids=recalled_memory_ids,
            allowed_query_keys=allowed_query_keys,
            diagnostic_calls_used=diagnostic_calls_used,
            diagnostic_observation_available=False,
            model_turn=int(item.get("logical_turn")),
            operational_action_contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )
    )
    return {
        "schema_version": item.get("schema_version"),
        "attempt": item.get("attempt"),
        "repair_reason": (
            "redacted_repair_reason" if item.get("repair_reason") is not None else None
        ),
        "logical_turn": item.get("logical_turn"),
        "provider": _public_contract_label(item.get("provider"), fallback="redacted-provider"),
        "model": _public_contract_label(item.get("model"), fallback="redacted-model"),
        "system": "Controlled replay system prompt; public text redacted.",
        "prompt": prompt_invariant.replace(GOVERNED_MEMORY_PROMPT_MARKER, memory_block),
        "prompt_invariant": prompt_invariant,
        "prompt_invariant_sha256": text_sha256(prompt_invariant),
        "temperature": item.get("temperature"),
        "max_output_tokens": item.get("max_output_tokens"),
        "routing_key": item.get("routing_key"),
        "decision_contract": decision_contract,
        "response_schema_version": item.get("response_schema_version"),
        "response_json_schema": response_schema,
    }


def _public_operation_effect(item: dict[str, Any], *, ordinal: int) -> dict[str, Any]:
    return {
        "sequence": ordinal,
        "effect_type": _public_contract_label(
            item.get("effect_type"),
            fallback="redacted-effect",
        ),
        "source_memory_id": (
            _public_identifier(item.get("source_memory_id"), kind="memory")
            if item.get("source_memory_id") is not None
            else None
        ),
        "result_memory_id": (
            _public_identifier(item.get("result_memory_id"), kind="memory")
            if item.get("result_memory_id") is not None
            else None
        ),
        "belief_id": (
            _public_identifier(item.get("belief_id"), kind="belief")
            if item.get("belief_id") is not None
            else None
        ),
        "namespace": "[redacted-namespace]",
    }


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
