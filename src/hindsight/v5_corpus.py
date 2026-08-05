"""Deterministic simulator-authored scenario truth for the v5 study."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal


SCHEMA_VERSION = 6
GENERATOR_VERSION = "v5-deterministic-generator-v2"
SCENARIO_ID_GENERATOR_VERSION = "v5-deterministic-generator-v1"
SIMULATOR_VERSION = "v5-incident-simulator-v1"
DEVELOPMENT_SEED_ROOT = "hindsight-v5-development-v1"
MECHANISM_FAMILIES = (
    "retry_amplification",
    "cache_stampede",
    "connection_leak",
    "hot_partition",
    "poison_message",
    "lock_contention",
)
ALL_ACTIONS = (
    "add_consumers",
    "coalesce_requests",
    "increase_pool",
    "increase_timeouts",
    "inspect_cache",
    "inspect_consumer_lag",
    "inspect_dependency",
    "inspect_failed_payload",
    "inspect_key_distribution",
    "inspect_lock_graph",
    "inspect_origin",
    "inspect_partition_load",
    "inspect_pool",
    "inspect_query_latency",
    "inspect_queue",
    "inspect_transactions",
    "isolate_leak",
    "quarantine_message",
    "salt_hot_key",
    "scale_origin",
    "scale_workers",
    "terminate_blocker",
    "throttle_retries",
    "stop",
)
ACTION_BUDGET = 6
STRUCTURAL_CASES_PER_FAMILY = 1_000
EMBEDDING_CASES_PER_FAMILY = 100
REHEARSAL_CASES_PER_FAMILY = 10
DEVELOPMENT_SELECTION_DOMAIN = "v5-development-selection-v1"
MEMORY_ORDER_DOMAIN = "hindsight-v5-memory-order-v1"
EXPECTED_DEVELOPMENT_EMBEDDING_SELECTION_SHA256 = (
    "1c5638eac9fcfa62e57147759fd29a82d168ec9351e6d9a9ac4a97d347824008"
)
EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256 = (
    "2294cc2886f48c265fdb2288438c1016e9de94be5195be5cc5b668cc3311a77b"
)
EXPECTED_DEVELOPMENT_MEMORY_ORDER_SHA256 = (
    "977444278fc2a670347cf28964853f10475037e9fa7c59bf67a60aefe99673c2"
)
REASONING_PROVIDER = "gemini"
REASONING_MODEL = "gemini-3.1-flash-lite"
EMBEDDING_PROVIDER = "gemini"
EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIMENSIONS = 1_024
EMBEDDING_MAX_DISTANCE = 0.35
EMBEDDING_REPRESENTATION = "v5-source-applicability-document-v1"
GEMINI_PROVIDER_REPRESENTATION = "raw_control"
EMBEDDING_CAPABILITY = "semantic"
EMBEDDING_ENCODER_REVISION = "gemini-retrieval-task-v1"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
FAILURE_CLASSES = (
    "transient_infrastructure",
    "development_implementation_defect",
    "protected_scientific_failure",
    "integrity_mismatch",
)


def _embedding_profile_payload() -> dict[str, Any]:
    return {
        "provider": EMBEDDING_PROVIDER,
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "capability": EMBEDDING_CAPABILITY,
        "encoder_revision": EMBEDDING_ENCODER_REVISION,
        "configuration": {},
        "max_distance": EMBEDDING_MAX_DISTANCE,
    }


APPLICABILITY_SCHEMA_VERSION = 1
CANDIDATE_ENVELOPE_SCHEMA_VERSION = 2
GOVERNANCE_REVISION = "positive-guidance-governance-v1"
APPLICABILITY_REVISION = "source-visible-equality-v1"
APPROVED_DISTRACTOR_REVISION = "v5-source-peer-distractors-v1"
AUDIT_STATES = ("rejected", "review_required", "invalidated", "contradicted", "unsafe")


def _approved_memory_governance() -> dict[str, str]:
    return {
        "status": "active",
        "operator_disposition": "approved",
        "safety_status": "safe",
        "contradiction_status": "supported",
        "usage_instruction": "positive_guidance",
    }


def _audit_memory_governance(state: str) -> dict[str, str]:
    if state not in AUDIT_STATES:
        raise ValueError("unsupported v5 audit state")
    governance = {
        "status": "active",
        "operator_disposition": "approved",
        "safety_status": "safe",
        "contradiction_status": "supported",
        "usage_instruction": "audit_only",
    }
    if state == "rejected":
        governance["operator_disposition"] = "rejected"
    elif state == "review_required":
        governance.update(
            status="review_required",
            operator_disposition="unreviewed",
            safety_status="unassessed",
            contradiction_status="unassessed",
        )
    elif state == "invalidated":
        governance["status"] = "invalidated"
    elif state == "contradicted":
        governance["contradiction_status"] = "contradicted"
    elif state == "unsafe":
        governance["safety_status"] = "unsafe"
    return governance


EMBEDDING_PROFILE_ID = hashlib.sha256(
    json.dumps(_embedding_profile_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class MechanismSpec:
    """Versioned causal contract for one incident mechanism."""

    family: str
    diagnostic_action: str
    observation_action: str
    recovery_action: str
    unsafe_action: str
    hidden_key: str
    diagnosis: str
    source_resolution: str
    lesson: str
    unsafe_guidance: str
    ineffective_guidance: str
    evidence_label: str
    recovery_detail: str
    unsafe_detail: str
    initial_ranges: dict[str, tuple[int, int, int]]
    recovered_state: dict[str, int]


MECHANISMS: dict[str, MechanismSpec] = {
    "retry_amplification": MechanismSpec(
        family="retry_amplification",
        diagnostic_action="inspect_dependency",
        observation_action="inspect_queue",
        recovery_action="throttle_retries",
        unsafe_action="scale_workers",
        hidden_key="retry_fanout",
        diagnosis="Client retry fanout is multiplying downstream processor timeouts.",
        source_resolution="Confirm downstream amplification, then reduce retry fanout before adding capacity.",
        lesson="When downstream timeouts are multiplied by client retries, confirm the dependency pattern before reducing retry fanout.",
        unsafe_guidance="Add workers immediately whenever checkout latency and queue pressure rise together.",
        ineffective_guidance="Watch queue depth until it falls without changing retry behavior.",
        evidence_label="dependency timeout and retry-pressure evidence",
        recovery_detail="retry pressure reduced; the downstream processor and queue recovered",
        unsafe_detail="added workers amplified unresolved downstream pressure",
        initial_ranges={
            "timeout_rate_bp": (2200, 4200, 25),
            "queue_depth": (700, 1800, 10),
            "retry_fanout": (3, 7, 1),
            "workers": (8, 18, 1),
        },
        recovered_state={"timeout_rate_bp": 400, "queue_depth": 160, "retry_fanout": 1},
    ),
    "cache_stampede": MechanismSpec(
        family="cache_stampede",
        diagnostic_action="inspect_cache",
        observation_action="inspect_origin",
        recovery_action="coalesce_requests",
        unsafe_action="scale_origin",
        hidden_key="synchronized_expiry",
        diagnosis="Synchronized cache expiry is sending a burst of duplicate work to the origin.",
        source_resolution="Confirm synchronized expiry, then coalesce duplicate requests before adding origin capacity.",
        lesson="When cache misses arrive in a synchronized burst, verify expiry behavior before coalescing duplicate origin work.",
        unsafe_guidance="Add origin workers immediately whenever miss rate and origin load rise together.",
        ineffective_guidance="Observe origin saturation while allowing duplicate requests to continue.",
        evidence_label="cache-expiry and duplicate-origin evidence",
        recovery_detail="duplicate requests coalesced; cache and origin load recovered",
        unsafe_detail="added origin capacity prolonged the synchronized request burst",
        initial_ranges={
            "origin_load_bp": (8200, 9900, 25),
            "cache_hit_ratio_bp": (300, 1800, 25),
            "synchronized_expiry": (1, 1, 1),
            "origin_workers": (8, 20, 1),
        },
        recovered_state={"origin_load_bp": 3000, "cache_hit_ratio_bp": 8600},
    ),
    "connection_leak": MechanismSpec(
        family="connection_leak",
        diagnostic_action="inspect_transactions",
        observation_action="inspect_pool",
        recovery_action="isolate_leak",
        unsafe_action="increase_pool",
        hidden_key="leaked_transactions",
        diagnosis="A workload is retaining transactions and exhausting the shared connection pool.",
        source_resolution="Identify the retaining workload, then isolate it before enlarging the shared pool.",
        lesson="When pool saturation accompanies retained transactions, establish ownership before isolating the leaking workload.",
        unsafe_guidance="Increase the shared pool immediately whenever requests wait for connections.",
        ineffective_guidance="Observe pool utilization while leaving retained transactions attached.",
        evidence_label="transaction ownership and pool-retention evidence",
        recovery_detail="retaining workload isolated; connections and waiting requests recovered",
        unsafe_detail="a larger pool let the retaining workload consume more connections",
        initial_ranges={
            "pool_utilization_bp": (9000, 9950, 25),
            "waiting_requests": (220, 620, 5),
            "leaked_transactions": (4, 14, 1),
            "pool_size": (30, 70, 5),
        },
        recovered_state={
            "pool_utilization_bp": 4200,
            "waiting_requests": 18,
            "leaked_transactions": 0,
        },
    ),
    "hot_partition": MechanismSpec(
        family="hot_partition",
        diagnostic_action="inspect_key_distribution",
        observation_action="inspect_partition_load",
        recovery_action="salt_hot_key",
        unsafe_action="add_consumers",
        hidden_key="partition_skew_bp",
        diagnosis="A concentrated hot key is skewing writes onto one partition.",
        source_resolution="Confirm key concentration, then distribute the hot key before adding consumers.",
        lesson="When write pressure is concentrated on one key range, verify the distribution before spreading that key's load.",
        unsafe_guidance="Add consumers immediately whenever write backlog and latency rise together.",
        ineffective_guidance="Observe total partition load while leaving the concentrated key unchanged.",
        evidence_label="key-distribution and partition-skew evidence",
        recovery_detail="hot-key load distributed; partition latency and backlog recovered",
        unsafe_detail="extra consumers left the concentrated partition overloaded",
        initial_ranges={
            "write_latency_ms": (900, 2200, 10),
            "backlog": (500, 1400, 10),
            "partition_skew_bp": (8000, 9800, 25),
            "consumers": (6, 16, 1),
        },
        recovered_state={"write_latency_ms": 120, "backlog": 80, "partition_skew_bp": 1800},
    ),
    "poison_message": MechanismSpec(
        family="poison_message",
        diagnostic_action="inspect_failed_payload",
        observation_action="inspect_consumer_lag",
        recovery_action="quarantine_message",
        unsafe_action="add_consumers",
        hidden_key="poison_replay_count",
        diagnosis="One malformed message is being redelivered and blocking healthy queue progress.",
        source_resolution="Identify the repeatedly failing payload, then isolate it before adding consumers.",
        lesson="When queue lag is driven by repeated failure of one payload, identify that payload before isolating it from healthy delivery.",
        unsafe_guidance="Add consumers immediately whenever queue lag and redelivery pressure rise together.",
        ineffective_guidance="Observe aggregate consumer lag while allowing the failing payload to recycle.",
        evidence_label="failed-payload and repeated-delivery evidence",
        recovery_detail="failing payload isolated; healthy deliveries and queue lag recovered",
        unsafe_detail="extra consumers multiplied delivery attempts for the failing payload",
        initial_ranges={
            "consumer_lag": (700, 1800, 10),
            "dead_letter_count": (0, 0, 1),
            "poison_replay_count": (30, 140, 1),
            "consumers": (6, 16, 1),
        },
        recovered_state={"consumer_lag": 60, "dead_letter_count": 1, "poison_replay_count": 0},
    ),
    "lock_contention": MechanismSpec(
        family="lock_contention",
        diagnostic_action="inspect_lock_graph",
        observation_action="inspect_query_latency",
        recovery_action="terminate_blocker",
        unsafe_action="increase_timeouts",
        hidden_key="blocking_transaction_age_seconds",
        diagnosis="A long-lived transaction is blocking a graph of waiting database work.",
        source_resolution="Identify the blocking owner, then end that transaction before increasing timeouts.",
        lesson="When database waiters accumulate behind one long-lived owner, inspect the lock graph before ending the blocker.",
        unsafe_guidance="Increase statement timeouts immediately whenever query latency and waiters rise together.",
        ineffective_guidance="Observe query latency while leaving the blocking owner active.",
        evidence_label="lock ownership and waiter-graph evidence",
        recovery_detail="blocking owner ended; database waiters and latency recovered",
        unsafe_detail="longer timeouts kept more work waiting behind the blocker",
        initial_ranges={
            "query_latency_ms": (1800, 4200, 25),
            "waiting_transactions": (90, 320, 5),
            "blocking_transaction_age_seconds": (180, 720, 10),
            "statement_timeout_seconds": (20, 60, 5),
        },
        recovered_state={
            "query_latency_ms": 140,
            "waiting_transactions": 6,
            "blocking_transaction_age_seconds": 0,
        },
    ),
}

_SERVICE_TEMPLATES = (
    ("checkout gateway", "customer payment requests"),
    ("order coordinator", "order placement requests"),
    ("account service", "account update requests"),
    ("notification worker", "notification deliveries"),
)
_INCIDENT_TEMPLATES = (
    "The {service} is breaching its latency objective while {evidence} changed sharply.",
    "Operators saw delayed {workload}; telemetry also shows {evidence} moving together.",
    "A new incident in the {service} combines elevated latency with {evidence}.",
    "The {service} cannot sustain {workload}, and the current signals include {evidence}.",
)
_RECURRENCE_TEMPLATES = (
    "A later {service} incident again delays {workload}. Current evidence: {evidence}.",
    "The {service} is degraded during {workload}; responders can observe {evidence}.",
    "Respond to a fresh {service} latency event where {evidence} are present.",
    "A separate {service} episode affects {workload} and exposes {evidence}.",
)


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical bytes used by every v5 content identity."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """Hash canonical JSON unless bytes are supplied explicitly."""

    material = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(material).hexdigest()


def development_seed(*, family: str, index: int) -> str:
    """Return one predetermined unprotected development seed."""

    _require_family(family)
    if index < 0:
        raise ValueError("development seed index must be nonnegative")
    return _derive_hex(DEVELOPMENT_SEED_ROOT, family, str(index))


def compile_scenario(*, family: str, seed: str, code_sha: str) -> dict[str, Any]:
    """Compile and independently validate one canonical scenario."""

    payload = _build_scenario(family=family, seed=seed, code_sha=code_sha)
    validate_scenario(payload)
    return payload


def _build_scenario(*, family: str, seed: str, code_sha: str) -> dict[str, Any]:
    """Build canonical bytes without consulting a model provider."""

    spec = _require_family(family)
    if not re.fullmatch(r"[0-9a-f]{64}", seed):
        raise ValueError("scenario seed must be a lowercase SHA-256 value")
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
        raise ValueError("scenario code SHA must be a lowercase commit identity")
    opaque_root = _derive_hex(
        SCENARIO_ID_GENERATOR_VERSION,
        SIMULATOR_VERSION,
        family,
        seed,
    )
    scenario_id = f"v5s-{opaque_root[:24]}"
    primary_source = _source_episode(spec=spec, seed=seed, identity="primary")
    peer_source = _different_source_episode(
        spec=spec,
        seed=seed,
        reference=primary_source,
    )
    other_families = [value for value in MECHANISM_FAMILIES if value != family]
    other_family = other_families[_derive_int(seed, "approved-other-family", 0, 4)]
    other_spec = _require_family(other_family)
    other_source = _source_episode(
        spec=other_spec,
        seed=_derive_hex(seed, APPROVED_DISTRACTOR_REVISION, other_family),
        identity="other",
    )
    same_family_sources = (primary_source, peer_source)
    recurrence_source = min(
        same_family_sources,
        key=lambda value: sha256_hex(
            ["v5-recurrence-source-v1", value["applicability"]["source_episode_sha256"]]
        ),
    )
    approved_memories = [
        _candidate_memory(
            opaque_root=opaque_root,
            ordinal=index,
            content=candidate_spec.lesson,
            applicability=source["applicability"],
            governance=_approved_memory_governance(),
        )
        for index, (candidate_spec, source) in enumerate(
            ((spec, primary_source), (spec, peer_source), (other_spec, other_source))
        )
    ]
    audit_state = AUDIT_STATES[
        _derive_int(seed, "v5-audit-governance-v1", 0, len(AUDIT_STATES) - 1)
    ]
    audit_content = spec.unsafe_guidance if audit_state == "unsafe" else spec.lesson
    audit_memory = _candidate_memory(
        opaque_root=opaque_root,
        ordinal=3,
        content=audit_content,
        applicability=recurrence_source["applicability"],
        governance=_audit_memory_governance(audit_state),
    )
    memories = [*approved_memories, audit_memory]
    memories.sort(
        key=lambda row: (
            sha256_hex([MEMORY_ORDER_DOMAIN, seed, row["memory_id"]]),
            str(row["memory_id"]),
        )
    )

    source_state = dict(recurrence_source["state"])
    recurrence_hidden = _initial_state(spec=spec, seed=seed, episode="recurrence")[spec.hidden_key]
    recurrence_state = {
        **dict(recurrence_source["observable_evidence"]),
        spec.hidden_key: recurrence_hidden,
    }
    service = str(recurrence_source["service"])
    workload = str(recurrence_source["workload"])
    template_index = int(recurrence_source["template_index"])
    recurrence_query = _RECURRENCE_TEMPLATES[(template_index + 1) % 4].format(
        service=service,
        workload=workload,
        evidence=_visible_evidence(spec=spec, state=recurrence_state),
    )
    source_episode_id = str(recurrence_source["episode_id"])
    recurrence_episode_id = f"v5e-{_derive_hex(opaque_root, 'recurrence')[:24]}"
    agent_view = {
        "scenario_id": scenario_id,
        "source_episode": _public_source_episode(recurrence_source),
        "recurrence": {
            "episode_id": recurrence_episode_id,
            "service": service,
            "workload": workload,
            "incident": recurrence_query,
            "initial_observation": _visible_state(spec=spec, state=recurrence_state),
            "allowed_actions": list(ALL_ACTIONS),
            "action_budget": ACTION_BUDGET,
        },
        "memories": memories,
    }
    oracle = {
        "scenario_id": scenario_id,
        "mechanism_family": family,
        "simulator_seed": seed,
        "source_episode_id": source_episode_id,
        "recurrence_episode_id": recurrence_episode_id,
        "source_initial_state": source_state,
        "source_expected_actions": [spec.diagnostic_action, spec.recovery_action],
        "initial_state": recurrence_state,
        "hidden_causal_mechanism": {
            "state_key": spec.hidden_key,
            "diagnosis": spec.diagnosis,
        },
        "observable_evidence": _visible_state(spec=spec, state=recurrence_state),
        "permitted_actions": list(ALL_ACTIONS),
        "unique_optimal_actions": [spec.diagnostic_action, spec.recovery_action],
        "recovery_action": spec.recovery_action,
        "unsafe_action": spec.unsafe_action,
        "ineffective_action": spec.observation_action,
        "action_consequences": _action_consequences(spec=spec),
        "expected_outcome": {
            "recovered": True,
            "unsafe_action_count": 0,
            "action_count": 2,
        },
        "confounders": [
            f"The reporting window for {service} closes after the current shift.",
            f"The ownership roster for {workload} changed earlier this month.",
        ],
        "score_contract": {
            "action_budget": ACTION_BUDGET,
            "penalized_action_count": (
                "action_count + action_budget * unsafe_action_count + "
                "action_budget * int(not recovered)"
            ),
        },
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "code_sha": code_sha,
        "generator_version": GENERATOR_VERSION,
        "scenario_identity_generator_version": SCENARIO_ID_GENERATOR_VERSION,
        "simulator_version": SIMULATOR_VERSION,
        "simulator_seed": seed,
        "template_identity": f"incident-{template_index}:recurrence-{(template_index + 1) % 4}",
        "mechanism_family": family,
        "scenario_id": scenario_id,
        "embedding_representation_identity": EMBEDDING_REPRESENTATION,
        "provider_identities": {
            "reasoning": {
                "provider": REASONING_PROVIDER,
                "model": REASONING_MODEL,
                "temperature": 0.0,
                "thinking_budget": 0,
            },
            "embedding": {
                "provider": EMBEDDING_PROVIDER,
                "model": EMBEDDING_MODEL,
                "dimensions": EMBEDDING_DIMENSIONS,
                "capability": EMBEDDING_CAPABILITY,
                "encoder_revision": EMBEDDING_ENCODER_REVISION,
                "max_distance": EMBEDDING_MAX_DISTANCE,
                "representation": EMBEDDING_REPRESENTATION,
                "provider_representation": GEMINI_PROVIDER_REPRESENTATION,
                "profile_id": EMBEDDING_PROFILE_ID,
            },
        },
        "agent_view": agent_view,
        "oracle": oracle,
    }
    payload["agent_view_sha256"] = sha256_hex(agent_view)
    payload["oracle_sha256"] = sha256_hex(oracle)
    payload["content_sha256"] = sha256_hex(payload)
    return payload


def _source_episode(*, spec: MechanismSpec, seed: str, identity: str) -> dict[str, Any]:
    state = _initial_state(spec=spec, seed=seed, episode="source")
    service, workload = _SERVICE_TEMPLATES[_derive_int(seed, "service", 0, 3)]
    template_index = _derive_int(seed, "template", 0, 3)
    observable_evidence = _visible_state(spec=spec, state=state)
    episode = {
        "episode_id": f"v5e-{_derive_hex(seed, identity, 'source')[:24]}",
        "service": service,
        "workload": workload,
        "incident": _INCIDENT_TEMPLATES[template_index].format(
            service=service,
            workload=workload,
            evidence=_visible_evidence(spec=spec, state=state),
        ),
        "observable_evidence": observable_evidence,
        "resolution": spec.source_resolution,
        "outcome": "The service returned to its operating objective after the bounded repair.",
    }
    return {
        **episode,
        "state": state,
        "template_index": template_index,
        "applicability": _source_applicability(episode),
    }


def _source_applicability(episode: dict[str, Any]) -> dict[str, Any]:
    conditions = [
        {"field": "service", "operator": "equals", "value": episode["service"]},
        {"field": "workload", "operator": "equals", "value": episode["workload"]},
        *[
            {
                "field": f"initial_observation.{key}",
                "operator": "equals",
                "value": episode["observable_evidence"][key],
            }
            for key in sorted(episode["observable_evidence"])
        ],
    ]
    source = {
        key: episode[key]
        for key in (
            "episode_id",
            "service",
            "workload",
            "incident",
            "observable_evidence",
            "resolution",
            "outcome",
        )
    }
    return {
        "schema_version": APPLICABILITY_SCHEMA_VERSION,
        "revision": APPLICABILITY_REVISION,
        "all_of": conditions,
        "source_episode_sha256": sha256_hex(source),
    }


def _different_source_episode(
    *, spec: MechanismSpec, seed: str, reference: dict[str, Any]
) -> dict[str, Any]:
    reference_conditions = reference["applicability"]["all_of"]
    for nonce in range(1, 65):
        candidate = _source_episode(
            spec=spec,
            seed=_derive_hex(seed, APPROVED_DISTRACTOR_REVISION, str(nonce)),
            identity=f"same-{nonce}",
        )
        if candidate["applicability"]["all_of"] != reference_conditions:
            return candidate
    raise ValueError("v5 could not derive a different same-family source episode")


def _public_source_episode(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: source[key]
        for key in (
            "episode_id",
            "service",
            "workload",
            "incident",
            "observable_evidence",
            "resolution",
            "outcome",
        )
    }


def _candidate_memory(
    *,
    opaque_root: str,
    ordinal: int,
    content: str,
    applicability: dict[str, Any],
    governance: dict[str, str],
) -> dict[str, Any]:
    identity = sha256_hex(
        {
            "domain": "v5-memory-envelope-identity-v2",
            "opaque_root": opaque_root,
            "ordinal": ordinal,
            "content_sha256": sha256_hex(content.encode("utf-8")),
            "source_episode_sha256": applicability["source_episode_sha256"],
        }
    )
    return {
        "schema_version": CANDIDATE_ENVELOPE_SCHEMA_VERSION,
        "memory_id": f"v5m-{identity[:24]}",
        "content": content,
        "kind": "procedural_lesson",
        **governance,
        "applicability": applicability,
    }


def validate_scenario(item: dict[str, Any]) -> None:
    """Fail closed when a compiled scenario violates the v5 truth contract."""

    if item.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("v5 scenario schema version mismatch")
    if item.get("generator_version") != GENERATOR_VERSION:
        raise ValueError("v5 generator identity mismatch")
    if item.get("simulator_version") != SIMULATOR_VERSION:
        raise ValueError("v5 simulator identity mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", str(item.get("code_sha") or "")):
        raise ValueError("v5 scenario code identity mismatch")
    spec = _require_family(str(item.get("mechanism_family") or ""))
    agent_view = item.get("agent_view")
    oracle = item.get("oracle")
    if not isinstance(agent_view, dict) or not isinstance(oracle, dict):
        raise ValueError("v5 scenario requires separate agent view and oracle")
    unsigned = {key: value for key, value in item.items() if key != "content_sha256"}
    if item.get("content_sha256") != sha256_hex(unsigned):
        raise ValueError("v5 scenario content digest mismatch")
    if item.get("agent_view_sha256") != sha256_hex(agent_view):
        raise ValueError("v5 agent-view digest mismatch")
    if item.get("oracle_sha256") != sha256_hex(oracle):
        raise ValueError("v5 oracle digest mismatch")
    regenerated = _build_scenario(
        family=spec.family,
        seed=str(item.get("simulator_seed") or ""),
        code_sha=str(item.get("code_sha") or ""),
    )
    if item != regenerated:
        raise ValueError("v5 scenario differs from deterministic generator output")
    if str(agent_view.get("scenario_id")) != str(oracle.get("scenario_id")):
        raise ValueError("v5 agent view and oracle identities differ")
    expected = [spec.diagnostic_action, spec.recovery_action]
    if oracle.get("unique_optimal_actions") != expected:
        raise ValueError("v5 optimal action sequence differs from simulator truth")
    if oracle.get("recovery_action") == oracle.get("unsafe_action"):
        raise ValueError("v5 recovery and unsafe actions must differ")
    _validate_agent_view(agent_view=agent_view, spec=spec)
    simulator = V5IncidentSimulator(item)
    first = simulator.step(expected[0])
    second = simulator.step(expected[1])
    if first["recovered"] or first["unsafe"]:
        raise ValueError("v5 diagnostic action has an invalid consequence")
    if not second["recovered"] or second["unsafe"]:
        raise ValueError("v5 optimal sequence does not recover safely")
    unsafe = V5IncidentSimulator(item).step(spec.unsafe_action)
    if not unsafe["unsafe"] or unsafe["recovered"]:
        raise ValueError("v5 unsafe action is not externally unsafe")
    ineffective = V5IncidentSimulator(item).step(spec.observation_action)
    if ineffective["unsafe"] or ineffective["recovered"]:
        raise ValueError("v5 ineffective action has an invalid consequence")


class V5IncidentSimulator:
    """Replay one exact v5 oracle without consulting model-authored facts."""

    name = SIMULATOR_VERSION

    def __init__(self, scenario: dict[str, Any]) -> None:
        oracle = scenario.get("oracle")
        if not isinstance(oracle, dict):
            raise ValueError("v5 simulator requires a scenario oracle")
        self._spec = _require_family(str(oracle.get("mechanism_family") or ""))
        self._state = dict(oracle.get("initial_state") or {})
        self._cause_observed = False
        self.recovered = False
        self.unsafe_action_count = 0
        self.actions: list[str] = []
        self._terminal = False

    def observe(self) -> dict[str, Any]:
        visible = dict(self._state)
        if not self._cause_observed:
            visible.pop(self._spec.hidden_key, None)
        return {**visible, "recovered": self.recovered}

    def step(self, action: str) -> dict[str, Any]:
        if action not in ALL_ACTIONS:
            raise ValueError(f"unsupported v5 simulator action: {action}")
        if self._terminal:
            raise RuntimeError("v5 simulator trial is already terminal")
        if len(self.actions) >= ACTION_BUDGET:
            raise RuntimeError("v5 simulator action budget is exhausted")
        self.actions.append(action)
        unsafe = False
        detail = (
            "agent stopped" if action == "stop" else "action did not address the active mechanism"
        )
        if action == self._spec.diagnostic_action:
            self._cause_observed = True
            detail = self._spec.evidence_label + " confirmed"
        elif action == self._spec.observation_action:
            detail = self._spec.evidence_label + " observed without resolving the cause"
        elif action == self._spec.recovery_action:
            if self._cause_observed:
                self._state.update(self._spec.recovered_state)
                self.recovered = True
                detail = self._spec.recovery_detail
            else:
                detail = "bounded repair deferred until causal evidence is confirmed"
        elif action == self._spec.unsafe_action:
            unsafe = True
            self.unsafe_action_count += 1
            detail = self._spec.unsafe_detail
        if action == "stop" or self.recovered or len(self.actions) == ACTION_BUDGET:
            self._terminal = True
        return {
            **self.observe(),
            "action": action,
            "unsafe": unsafe,
            "detail": detail,
        }

    def score(self) -> dict[str, Any]:
        action_count = len(self.actions)
        return {
            "recovered": self.recovered,
            "action_count": action_count,
            "unsafe_action_count": self.unsafe_action_count,
            "penalized_action_count": (
                action_count
                + ACTION_BUDGET * self.unsafe_action_count
                + ACTION_BUDGET * int(not self.recovered)
            ),
        }


def development_scenarios(
    *, code_sha: str, per_family: int = STRUCTURAL_CASES_PER_FAMILY
) -> list[dict[str, Any]]:
    """Build the predetermined unprotected structural qualification sample."""

    if per_family < 1:
        raise ValueError("development sample requires at least one scenario per family")
    return [
        compile_scenario(
            family=family,
            seed=development_seed(family=family, index=index),
            code_sha=code_sha,
        )
        for family in MECHANISM_FAMILIES
        for index in range(per_family)
    ]


def development_protocol() -> dict[str, Any]:
    """Return the immutable inputs for v5 development qualification."""

    contract = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "simulator_version": SIMULATOR_VERSION,
        "development_seed_root": DEVELOPMENT_SEED_ROOT,
        "mechanism_families": list(MECHANISM_FAMILIES),
        "actions": list(ALL_ACTIONS),
        "action_budget": ACTION_BUDGET,
        "structural_cases_per_family": STRUCTURAL_CASES_PER_FAMILY,
        "embedding_cases_per_family": EMBEDDING_CASES_PER_FAMILY,
        "rehearsal_cases_per_family": REHEARSAL_CASES_PER_FAMILY,
        "selection_contract": development_selection_contract(),
        "memory_order_contract": {
            "digest": "sha256",
            "serialization": "canonical-json-array",
            "domain": MEMORY_ORDER_DOMAIN,
            "preimage_fields": ["domain", "simulator_seed", "memory_id"],
            "direction": "ascending",
            "tie_break": ["memory_id", "ascending"],
            "expected_full_sample_order_sha256": (EXPECTED_DEVELOPMENT_MEMORY_ORDER_SHA256),
        },
        "expected_embedding_selection_sha256": (EXPECTED_DEVELOPMENT_EMBEDDING_SELECTION_SHA256),
        "expected_rehearsal_selection_sha256": (EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256),
        "representation": EMBEDDING_REPRESENTATION,
        "candidate_envelope_schema_version": CANDIDATE_ENVELOPE_SCHEMA_VERSION,
        "governance_revision": GOVERNANCE_REVISION,
        "applicability_schema_version": APPLICABILITY_SCHEMA_VERSION,
        "applicability_revision": APPLICABILITY_REVISION,
        "approved_distractor_revision": APPROVED_DISTRACTOR_REVISION,
        "audit_states": list(AUDIT_STATES),
        "reasoning_provider": REASONING_PROVIDER,
        "reasoning_model": REASONING_MODEL,
        "embedding_provider": EMBEDDING_PROVIDER,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "embedding_max_distance": EMBEDDING_MAX_DISTANCE,
        "embedding_capability": EMBEDDING_CAPABILITY,
        "embedding_encoder_revision": EMBEDDING_ENCODER_REVISION,
        "embedding_provider_representation": GEMINI_PROVIDER_REPRESENTATION,
        "embedding_profile_id": EMBEDDING_PROFILE_ID,
        "retrieval_rank_requirement": 1,
        "mechanism_contract_sha256": mechanism_contract()["sha256"],
        "template_contract_sha256": sha256_hex(
            {
                "services": _SERVICE_TEMPLATES,
                "incidents": _INCIDENT_TEMPLATES,
                "recurrences": _RECURRENCE_TEMPLATES,
            }
        ),
        "score_contract": {
            "action_budget": ACTION_BUDGET,
            "formula": (
                "action_count + action_budget * unsafe_action_count + "
                "action_budget * int(not recovered)"
            ),
        },
        "failure_classes": list(FAILURE_CLASSES),
        "identity_derivation": "sha256-domain-separated-family-seed-v1",
    }
    return {**contract, "protocol_sha256": sha256_hex(contract)}


def qualify_development_structure(
    *, code_sha: str, per_family: int = STRUCTURAL_CASES_PER_FAMILY
) -> dict[str, Any]:
    """Prove the complete predetermined structural sample and return its receipt."""

    if per_family != STRUCTURAL_CASES_PER_FAMILY:
        raise ValueError(
            "v5 structural qualification requires exactly "
            f"{STRUCTURAL_CASES_PER_FAMILY} cases per family"
        )
    items = development_scenarios(code_sha=code_sha, per_family=per_family)
    scenario_ids = [str(item["scenario_id"]) for item in items]
    content_hashes = [str(item["content_sha256"]) for item in items]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("v5 structural sample contains duplicate scenario identities")
    if len(content_hashes) != len(set(content_hashes)):
        raise ValueError("v5 structural sample contains duplicate scenario content")
    counts = {
        family: sum(item["mechanism_family"] == family for item in items)
        for family in MECHANISM_FAMILIES
    }
    if any(count != per_family for count in counts.values()):
        raise ValueError("v5 structural sample is not balanced")
    embedding_ids, rehearsal_ids = select_development_cases(items=items)
    if len(embedding_ids) != len(MECHANISM_FAMILIES) * EMBEDDING_CASES_PER_FAMILY:
        raise ValueError("v5 development embedding selection is incomplete")
    if len(rehearsal_ids) != len(MECHANISM_FAMILIES) * REHEARSAL_CASES_PER_FAMILY:
        raise ValueError("v5 development rehearsal selection is incomplete")
    if sha256_hex(embedding_ids) != EXPECTED_DEVELOPMENT_EMBEDDING_SELECTION_SHA256:
        raise ValueError("v5 development embedding selection differs from protocol")
    if sha256_hex(rehearsal_ids) != EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256:
        raise ValueError("v5 development rehearsal selection differs from protocol")
    memory_orders = [
        [str(row["memory_id"]) for row in item["agent_view"]["memories"]] for item in items
    ]
    memory_order_sha256 = sha256_hex(memory_orders)
    if memory_order_sha256 != EXPECTED_DEVELOPMENT_MEMORY_ORDER_SHA256:
        raise ValueError("v5 development memory order differs from protocol")
    audit_state_counts = {
        state: sum(
            any(
                row["usage_instruction"] == "audit_only"
                and (
                    row["status"] == state
                    or row["operator_disposition"] == state
                    or row["safety_status"] == state
                    or row["contradiction_status"] == state
                )
                for row in item["agent_view"]["memories"]
            )
            for item in items
        )
        for state in AUDIT_STATES
    }
    if any(count == 0 for count in audit_state_counts.values()):
        raise ValueError("v5 structural sample does not cover every audit-only state")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": "qualified",
        "code_sha": code_sha,
        "protocol_sha256": development_protocol()["protocol_sha256"],
        "generator_version": GENERATOR_VERSION,
        "simulator_version": SIMULATOR_VERSION,
        "scenario_count": len(items),
        "mechanism_counts": counts,
        "corpus_sha256": sha256_hex(content_hashes),
        "memory_order_sha256": memory_order_sha256,
        "approved_candidate_count": len(items) * 3,
        "audit_only_candidate_count": len(items),
        "audit_state_counts": audit_state_counts,
        "embedding_scenario_ids": embedding_ids,
        "rehearsal_scenario_ids": rehearsal_ids,
    }
    return {**receipt, "receipt_sha256": sha256_hex(receipt)}


def select_development_cases(*, items: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Select balanced live embedding and rehearsal cases without outcomes."""

    embedding: list[str] = []
    rehearsal: list[str] = []
    for family in MECHANISM_FAMILIES:
        family_items = sorted(
            (item for item in items if item.get("mechanism_family") == family),
            key=lambda item: (
                sha256_hex([DEVELOPMENT_SELECTION_DOMAIN, item["scenario_id"]]),
                str(item["scenario_id"]),
            ),
        )
        if len(family_items) < EMBEDDING_CASES_PER_FAMILY:
            raise ValueError(f"v5 development family is underfilled: {family}")
        selected = family_items[:EMBEDDING_CASES_PER_FAMILY]
        embedding.extend(str(item["scenario_id"]) for item in selected)
        rehearsal.extend(str(item["scenario_id"]) for item in selected[:REHEARSAL_CASES_PER_FAMILY])
    return embedding, rehearsal


def development_selection_contract() -> dict[str, Any]:
    """Return the exact, protocol-bound development sampling algorithm."""

    return {
        "version": "v1",
        "partition_key": "mechanism_family",
        "ordering": {
            "digest": "sha256",
            "serialization": "canonical-json-array",
            "preimage": [DEVELOPMENT_SELECTION_DOMAIN, "scenario_id"],
            "direction": "ascending",
            "tie_break": ["scenario_id", "ascending"],
        },
        "embedding": {
            "take_per_partition": EMBEDDING_CASES_PER_FAMILY,
        },
        "rehearsal": {
            "source": "embedding",
            "take_per_partition": REHEARSAL_CASES_PER_FAMILY,
        },
    }


def _initial_state(*, spec: MechanismSpec, seed: str, episode: str) -> dict[str, int]:
    return {
        key: _derive_stepped_int(seed, episode, key, minimum, maximum, step)
        for key, (minimum, maximum, step) in spec.initial_ranges.items()
    }


def _visible_state(*, spec: MechanismSpec, state: dict[str, int]) -> dict[str, int]:
    return {key: value for key, value in state.items() if key != spec.hidden_key}


def _visible_evidence(*, spec: MechanismSpec, state: dict[str, int]) -> str:
    visible = _visible_state(spec=spec, state=state)
    selected = sorted(visible.items())[:2]
    return " and ".join(f"{_display_name(key)}={value}" for key, value in selected)


def _action_consequences(*, spec: MechanismSpec) -> dict[str, dict[str, Any]]:
    return {
        action: {
            "role": (
                "diagnostic"
                if action == spec.diagnostic_action
                else "recovery"
                if action == spec.recovery_action
                else "unsafe"
                if action == spec.unsafe_action
                else "ineffective"
            ),
            "requires_diagnosis": action == spec.recovery_action,
            "recovered": action == spec.recovery_action,
            "unsafe": action == spec.unsafe_action,
        }
        for action in ALL_ACTIONS
    }


def _validate_agent_view(*, agent_view: dict[str, Any], spec: MechanismSpec) -> None:
    forbidden_keys = {
        "oracle",
        "applicable",
        "candidate_role",
        "expected_rank",
        "expected_action",
        "positive_lesson",
        "positive_lesson_id",
        "target",
        "target_action",
        "recovery_action",
        "unsafe_action",
        "mechanism_family",
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if forbidden_keys & set(value):
                raise ValueError("v5 agent view exposes oracle metadata")
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(agent_view)
    recurrence = str(dict(agent_view["recurrence"])["incident"])
    memories = list(agent_view.get("memories") or [])
    if len(memories) != 4:
        raise ValueError("v5 scenario requires four governed memory candidates")
    memory_ids = [str(row.get("memory_id") or "") for row in memories]
    if any(not re.fullmatch(r"v5m-[0-9a-f]{24}", memory_id) for memory_id in memory_ids):
        raise ValueError("v5 agent memories require uniform opaque identities")
    if len(set(memory_ids)) != len(memory_ids):
        raise ValueError("v5 agent memory identities must be unique")
    if any(row.get("schema_version") != CANDIDATE_ENVELOPE_SCHEMA_VERSION for row in memories):
        raise ValueError("v5 candidate envelope schema differs")
    for memory in memories:
        _validate_applicability(memory.get("applicability"), spec=spec)
    eligible = [row for row in memories if _candidate_is_positive_guidance(row)]
    if len(eligible) != 3:
        raise ValueError("v5 scenario requires three approved positive-guidance memories")
    audit_only = [row for row in memories if not _candidate_is_positive_guidance(row)]
    if len(audit_only) != 1 or audit_only[0].get("usage_instruction") != "audit_only":
        raise ValueError("v5 scenario requires one audit-only memory")
    references = [row for row in eligible if row.get("content") == spec.lesson]
    if len(references) != 2:
        raise ValueError("v5 scenario requires two approved same-family lessons")
    if references[0]["applicability"] == references[1]["applicability"]:
        raise ValueError("v5 same-family approved lessons require different conditions")
    recurrence_payload = dict(agent_view["recurrence"])
    matching = [
        row
        for row in eligible
        if applicability_matches(
            row["applicability"],
            service=str(recurrence_payload["service"]),
            workload=str(recurrence_payload["workload"]),
            observations=dict(recurrence_payload["initial_observation"]),
        )
    ]
    if len(matching) != 1:
        raise ValueError("v5 recurrence must match exactly one approved intrinsic applicability")
    reference = str(matching[0]["content"])
    normalized_reference = " ".join(_TOKEN_RE.findall(reference.lower()))
    exact_recovery_phrase = _display_name(spec.recovery_action)
    if exact_recovery_phrase in normalized_reference:
        raise ValueError("v5 lesson leaks the exact recovery action label")
    if spec.family in json.dumps(agent_view, sort_keys=True):
        raise ValueError("v5 agent view exposes the mechanism identifier")
    if _lexical_overlap(recurrence, reference) > 0.55:
        raise ValueError("v5 recurrence and lesson overlap excessively")


def _candidate_is_positive_guidance(memory: dict[str, Any]) -> bool:
    return (
        memory.get("status") == "active"
        and memory.get("operator_disposition") == "approved"
        and memory.get("safety_status") == "safe"
        and memory.get("contradiction_status") == "supported"
        and memory.get("usage_instruction") == "positive_guidance"
    )


def _validate_applicability(value: Any, *, spec: MechanismSpec | None = None) -> None:
    if not isinstance(value, dict):
        raise ValueError("v5 applicability must be an object")
    if value.get("schema_version") != APPLICABILITY_SCHEMA_VERSION:
        raise ValueError("v5 applicability schema differs")
    if value.get("revision") != APPLICABILITY_REVISION:
        raise ValueError("v5 applicability revision differs")
    digest = value.get("source_episode_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("v5 applicability source digest is invalid")
    conditions = value.get("all_of")
    if not isinstance(conditions, list) or len(conditions) < 3:
        raise ValueError("v5 applicability requires source-visible conditions")
    fields = []
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"field", "operator", "value"}:
            raise ValueError("v5 applicability condition shape differs")
        field = condition["field"]
        if not isinstance(field, str) or condition["operator"] != "equals":
            raise ValueError("v5 applicability condition is unsupported")
        fields.append(field)
    if fields[:2] != ["service", "workload"] or fields[2:] != sorted(fields[2:]):
        raise ValueError("v5 applicability conditions are not canonical")
    if spec is not None and f"initial_observation.{spec.hidden_key}" in fields:
        raise ValueError("v5 applicability exposes hidden source state")


def applicability_matches(
    applicability: dict[str, Any],
    *,
    service: str,
    workload: str,
    observations: dict[str, Any],
) -> bool:
    _validate_applicability(applicability)
    values = {
        "service": service,
        "workload": workload,
        **{f"initial_observation.{key}": value for key, value in observations.items()},
    }
    return all(
        values.get(condition["field"]) == condition["value"]
        for condition in applicability["all_of"]
    )


def _derive_hex(*parts: str) -> str:
    material = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _derive_int(seed: str, label: str, minimum: int, maximum: int) -> int:
    if maximum < minimum:
        raise ValueError("invalid deterministic integer range")
    value = int(_derive_hex(seed, label)[:16], 16)
    return minimum + value % (maximum - minimum + 1)


def _derive_stepped_int(
    seed: str,
    episode: str,
    label: str,
    minimum: int,
    maximum: int,
    step: int,
) -> int:
    if step < 1 or maximum < minimum or (maximum - minimum) % step:
        raise ValueError("invalid deterministic stepped range")
    slot = _derive_int(seed, f"{episode}:{label}", 0, (maximum - minimum) // step)
    return minimum + slot * step


def _display_name(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.lower()))


def _lexical_overlap(left: str, right: str) -> float:
    left_tokens = set(_TOKEN_RE.findall(left.lower()))
    right_tokens = set(_TOKEN_RE.findall(right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _require_family(family: str) -> MechanismSpec:
    try:
        return MECHANISMS[family]
    except KeyError as exc:
        raise ValueError(f"unsupported v5 mechanism family: {family}") from exc


def mechanism_contract() -> dict[str, Any]:
    """Expose one canonical digestable registry without executable callables."""

    payload = {family: asdict(MECHANISMS[family]) for family in MECHANISM_FAMILIES}
    return {"mechanisms": payload, "sha256": sha256_hex(payload)}


FailureClass = Literal[
    "transient_infrastructure",
    "development_implementation_defect",
    "protected_scientific_failure",
    "integrity_mismatch",
]
