"""Versioned governance over immutable v5 development qualification evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import struct
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from hindsight.db import connect
from hindsight.embedding_index import activate_profile, begin_profile_build
from hindsight.memory import MemoryStore
from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, learning_tenant_id
from hindsight.tenant import tenant_scope
from hindsight.v5_corpus import (
    EMBEDDING_CAPABILITY,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_ENCODER_REVISION,
    EMBEDDING_MAX_DISTANCE,
    EMBEDDING_MODEL,
    EMBEDDING_PROFILE_ID,
    EMBEDDING_PROVIDER,
    EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256,
    GEMINI_PROVIDER_REPRESENTATION,
    qualify_development_structure,
    sha256_hex,
)
from hindsight.v5_qualification import (
    DOCUMENT_TASK,
    EXPECTED_SCENARIO_COUNT,
    EXPECTED_UNIQUE_DOCUMENTS,
    OPAQUE_MEMORY_RE,
    QUERY_TASK,
    CheckpointAttestor,
    CheckpointedEmbeddingProvider,
    QualificationStore,
    _finite_float,
    _initialize_exact_profile,
    _load_database_case,
    _require_database_evidence,
    _require_database_identities,
    _require_uuid_identity,
    _result_hits,
    _result_value,
    _retrieve_database_case,
    _seal_retrieval_decision,
    _verify_retrieval_trace,
    render_retrieval_query,
    require_fresh_development_database,
    require_restricted_runtime_database,
    select_embedding_scenarios,
)


POLICY_SCHEMA_VERSION = 1
POLICY_REVISION = "v5-development-governance-v2"
AUTHORIZATION_SCHEMA_VERSION = 1
REHEARSAL_SCHEMA_VERSION = 1
PROTECTED_AUTHORIZATION_SCHEMA_VERSION = 1
V1_QUALIFICATION_CONTRACT_SHA256 = (
    "33b676b5680b74cfac7fe5cdc6dad2927d1bf8fa1305485c03d971852aef5d4c"
)
V2_MINIMUM_SEMANTIC_NUMERATOR = 9
V2_MINIMUM_SEMANTIC_DENOMINATOR = 10
V2_MAXIMUM_DISTANCE_DELTA = 2e-6
EXPECTED_REHEARSAL_COUNT = 60
V2_RESULT_KIND = "v5-governance-v2-result"
V2_REHEARSAL_KIND = "v5-governance-v2-rehearsal"
V2_PROTECTED_AUTHORIZATION_KIND = "v5-governance-v2-protected"
SIGNATURE_ALGORITHM = "AWS_KMS_HMAC_SHA_256"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CODE_SHA_RE = re.compile(r"[0-9a-f]{40}")


class GovernanceSigner(Protocol):
    key_id: str

    def token(self, *, kind: str, raw_id: str) -> str: ...


class CacheOnlyEmbeddingProvider:
    """Exact provider identity whose only behavior is to reject cache misses."""

    provider_name = EMBEDDING_PROVIDER
    model_name = EMBEDDING_MODEL
    dimensions = EMBEDDING_DIMENSIONS
    capability = EMBEDDING_CAPABILITY
    encoder_revision = EMBEDDING_ENCODER_REVISION
    representation = GEMINI_PROVIDER_REPRESENTATION

    def __init__(self) -> None:
        self.miss_count = 0

    def embed(self, _text: str) -> list[float]:
        return self.embed_document(_text)

    def embed_document(self, _text: str) -> list[float]:
        self.miss_count += 1
        raise RuntimeError("v5 cache-only execution encountered a document embedding miss")

    def embed_query(self, _text: str) -> list[float]:
        self.miss_count += 1
        raise RuntimeError("v5 cache-only execution encountered a query embedding miss")


def governance_v2_policy() -> dict[str, Any]:
    """Return the immutable V2 governance policy and its content identity."""

    body = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "revision": POLICY_REVISION,
        "source_qualification_contract_sha256": V1_QUALIFICATION_CONTRACT_SHA256,
        "evidence": {
            "scenario_count": EXPECTED_SCENARIO_COUNT,
            "individual_exclusions_allowed": False,
            "diagnostic_content_hash_required": True,
            "checkpoint_attestations_required": True,
        },
        "semantic_rank_one_accuracy": {
            "minimum_numerator": V2_MINIMUM_SEMANTIC_NUMERATOR,
            "minimum_denominator": V2_MINIMUM_SEMANTIC_DENOMINATOR,
        },
        "hard_gates": {
            "strict_retrieval_policy": "semantic_strict",
            "fallback": None,
            "unique_intrinsic_match": True,
            "rank_one_within_cutoff": EMBEDDING_MAX_DISTANCE,
            "candidate_membership_parity": True,
            "candidate_ordering_parity": True,
            "maximum_distance_delta": V2_MAXIMUM_DISTANCE_DELTA,
            "ineligible_candidate_absent": True,
            "ineligible_read_absent": True,
            "audit_only_visible": True,
            "alternate_tenant_invisible": True,
            "learning_decision_sealed": True,
            "alternate_decision_sealed": True,
        },
        "stored_vector_precision": {
            "component_format": "IEEE-754-binary32",
            "direct_evaluator_format": "IEEE-754-binary64",
            "dimensions": EMBEDDING_DIMENSIONS,
            "characterization": "direct-binary64-vs-stored-binary32-cosine-distance",
        },
        "rehearsals": {
            "scenario_count": EXPECTED_REHEARSAL_COUNT,
            "selection_sha256": EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256,
            "cache_only": True,
            "complete_final_index_required": True,
            "provider_calls_allowed": False,
        },
        "protected_learning": {
            "requires_status": "rehearsals_passed",
            "monitoring_required": True,
            "append_only_audit_required": True,
            "rollback_required": True,
        },
    }
    return {**body, "policy_sha256": sha256_hex(body)}


def governance_v2_policy_artifact(
    *,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
) -> dict[str, Any]:
    """Bind the versioned policy to its tested subject and evaluator revision."""

    _require_code_sha(tested_subject_sha, "tested subject")
    _require_code_sha(policy_evaluator_sha, "policy evaluator")
    policy = governance_v2_policy()
    body = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "status": "policy_frozen",
        "revision": POLICY_REVISION,
        "policy_sha256": policy["policy_sha256"],
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "policy": policy,
    }
    return {**body, "policy_artifact_sha256": sha256_hex(body)}


def binary32_cosine_distance(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Characterize Cockroach VECTOR component precision deterministically."""

    left32 = _binary32_vector(left)
    right32 = _binary32_vector(right)
    dot = left_norm = right_norm = _binary32(0.0)
    for left_value, right_value in zip(left32, right32, strict=True):
        dot = _binary32(dot + _binary32(left_value * right_value))
        left_norm = _binary32(left_norm + _binary32(left_value * left_value))
        right_norm = _binary32(right_norm + _binary32(right_value * right_value))
    denominator = _binary32(_binary32(math.sqrt(left_norm)) * _binary32(math.sqrt(right_norm)))
    if not denominator:
        raise ValueError("v5 stored-vector characterization requires nonzero vectors")
    return _binary32(1.0 - _binary32(dot / denominator))


def evaluate_governance_v2(
    *,
    diagnostic: Mapping[str, Any],
    expected_diagnostic_sha256: str,
    diagnostic_file_sha256: str,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    signer: GovernanceSigner,
) -> dict[str, Any]:
    """Evaluate and sign one immutable diagnostic under V2."""

    evaluation = _evaluate_diagnostic(
        diagnostic=diagnostic,
        expected_diagnostic_sha256=expected_diagnostic_sha256,
        diagnostic_file_sha256=diagnostic_file_sha256,
        tested_subject_sha=tested_subject_sha,
        policy_evaluator_sha=policy_evaluator_sha,
    )
    if evaluation["status"] != "v2_passed":
        raise ValueError("v5 diagnostic does not satisfy governance V2")
    return _sign_artifact(evaluation, signer=signer, kind=V2_RESULT_KIND)


def verify_governance_v2(
    *,
    authorization: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    expected_diagnostic_sha256: str,
    diagnostic_file_sha256: str,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    signer: GovernanceSigner,
) -> dict[str, Any]:
    """Verify signature, identities, and a fresh deterministic V2 evaluation."""

    _verify_signed_artifact(authorization, signer=signer, kind=V2_RESULT_KIND)
    expected = _evaluate_diagnostic(
        diagnostic=diagnostic,
        expected_diagnostic_sha256=expected_diagnostic_sha256,
        diagnostic_file_sha256=diagnostic_file_sha256,
        tested_subject_sha=tested_subject_sha,
        policy_evaluator_sha=policy_evaluator_sha,
    )
    observed = _unsigned_artifact_body(authorization)
    if observed != expected or expected["status"] != "v2_passed":
        raise ValueError("v5 governance V2 authorization differs from its evidence")
    return dict(authorization)


def run_cache_only_rehearsals(
    *,
    authorization: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    expected_diagnostic_sha256: str,
    diagnostic_file_sha256: str,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    signer: GovernanceSigner,
    checkpoint_attestor: CheckpointAttestor,
    checkpoint_path: str | os.PathLike[str],
    execution_manifest: Mapping[str, Any],
    database_url: str,
    runtime_database_url: str,
    connect_fn: Callable[..., Any] = connect,
    store_factory: Callable[..., QualificationStore] = MemoryStore,
    begin_profile_build_fn: Callable[..., Mapping[str, Any]] = begin_profile_build,
    activate_profile_fn: Callable[..., Mapping[str, Any]] = activate_profile,
    database_validator_fn: Callable[[str], Mapping[str, str]] | None = None,
    runtime_database_validator_fn: Callable[[str, str], Mapping[str, str]] | None = None,
    profile_initializer_fn: Callable[..., Mapping[str, Any]] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Run the predetermined 60 cases against a complete cache-built index."""

    verify_governance_v2(
        authorization=authorization,
        diagnostic=diagnostic,
        expected_diagnostic_sha256=expected_diagnostic_sha256,
        diagnostic_file_sha256=diagnostic_file_sha256,
        tested_subject_sha=tested_subject_sha,
        policy_evaluator_sha=policy_evaluator_sha,
        signer=signer,
    )
    _require_execution_manifest(
        execution_manifest,
        diagnostic=diagnostic,
        tested_subject_sha=tested_subject_sha,
    )
    database_evidence = (
        database_validator_fn(database_url)
        if database_validator_fn is not None
        else require_fresh_development_database(database_url, connect_fn=connect_fn)
    )
    _require_database_evidence(database_evidence)
    database_identities = (
        runtime_database_validator_fn(database_url, runtime_database_url)
        if runtime_database_validator_fn is not None
        else require_restricted_runtime_database(
            database_url,
            runtime_database_url,
            connect_fn=connect_fn,
        )
    )
    _require_database_identities(database_identities)
    delegate = CacheOnlyEmbeddingProvider()
    checkpoint = CheckpointedEmbeddingProvider(
        delegate,
        checkpoint_path,
        code_sha=tested_subject_sha,
        attestor=checkpoint_attestor,
        execution_manifest_sha256=str(execution_manifest["execution_manifest_sha256"]),
        qualification_contract_sha256=V1_QUALIFICATION_CONTRACT_SHA256,
    )
    if checkpoint.checkpoint_sha256 != diagnostic.get("checkpoint_sha256"):
        raise ValueError("v5 rehearsal checkpoint differs from the diagnostic")
    if checkpoint.entry_counts != {
        DOCUMENT_TASK: EXPECTED_UNIQUE_DOCUMENTS,
        QUERY_TASK: EXPECTED_SCENARIO_COUNT,
    }:
        raise ValueError("v5 rehearsal checkpoint coverage is incomplete")
    selected = select_embedding_scenarios(code_sha=tested_subject_sha)
    structural = qualify_development_structure(code_sha=tested_subject_sha)
    rehearsal_ids = list(structural["rehearsal_scenario_ids"])
    if (
        len(rehearsal_ids) != EXPECTED_REHEARSAL_COUNT
        or sha256_hex(rehearsal_ids) != EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256
    ):
        raise ValueError("v5 rehearsal selection differs from the frozen protocol")
    profile = (
        profile_initializer_fn(provider=checkpoint, db_url=database_url)
        if profile_initializer_fn is not None
        else _initialize_exact_profile(
            provider=checkpoint,
            db_url=database_url,
            store_factory=store_factory,
            begin_profile_build_fn=begin_profile_build_fn,
            activate_profile_fn=activate_profile_fn,
        )
    )
    if str(profile.get("id") or "") != EMBEDDING_PROFILE_ID:
        raise ValueError("v5 rehearsal profile differs from the frozen profile")
    results = _run_rehearsal_cases(
        selected=selected,
        rehearsal_ids=rehearsal_ids,
        db_url=runtime_database_url,
        provider=checkpoint,
        store_factory=store_factory,
        progress_callback=progress_callback,
    )
    if delegate.miss_count or any(checkpoint.delegate_call_counts.values()):
        raise RuntimeError("v5 rehearsal attempted to invoke an embedding provider")
    metrics = _evaluate_rows(results, expected_ids=rehearsal_ids)
    status = "rehearsals_passed" if metrics["passed"] else "rehearsals_failed"
    body = {
        "schema_version": REHEARSAL_SCHEMA_VERSION,
        "status": status,
        "policy_revision": POLICY_REVISION,
        "policy_sha256": governance_v2_policy()["policy_sha256"],
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "source_authorization_sha256": authorization["artifact_sha256"],
        "source_diagnostic_sha256": expected_diagnostic_sha256,
        "source_diagnostic_file_sha256": diagnostic_file_sha256,
        "database_name": database_evidence["database_name"],
        "database_engine": database_evidence["engine"],
        "rehearsal_selection_sha256": EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256,
        "scenario_count": len(results),
        "semantic_rank_one_count": metrics["semantic_rank_one_count"],
        "semantic_rank_one_accuracy_basis_points": metrics[
            "semantic_rank_one_accuracy_basis_points"
        ],
        "maximum_distance_delta": metrics["maximum_distance_delta"],
        "all_hard_gates_passed": metrics["all_hard_gates_passed"],
        "checkpoint_sha256": checkpoint.checkpoint_sha256,
        "checkpoint_entry_counts": checkpoint.entry_counts,
        "embedding_cache_miss_count": delegate.miss_count,
        "embedding_delegate_call_counts": checkpoint.delegate_call_counts,
        "monitoring": {
            "phase": "development_rehearsal",
            "progress_records": len(results),
            "fail_closed": True,
        },
        "audit": {
            "retrieval_decisions_sealed": all(
                row.get("learning_decision_sealed") is True for row in results
            ),
            "alternate_decisions_sealed": all(
                row.get("alternate_decision_sealed") is True for row in results
            ),
            "result_sha256": sha256_hex(results),
        },
    }
    signed = _sign_artifact(body, signer=signer, kind=V2_REHEARSAL_KIND)
    if status != "rehearsals_passed":
        raise ValueError("v5 cache-only rehearsals did not satisfy governance V2")
    return signed


def authorize_protected_learning(
    *,
    authorization: Mapping[str, Any],
    rehearsal_result: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    expected_diagnostic_sha256: str,
    diagnostic_file_sha256: str,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    signer: GovernanceSigner,
) -> dict[str, Any]:
    """Issue the content-addressed protected-learning enablement control."""

    verify_governance_v2(
        authorization=authorization,
        diagnostic=diagnostic,
        expected_diagnostic_sha256=expected_diagnostic_sha256,
        diagnostic_file_sha256=diagnostic_file_sha256,
        tested_subject_sha=tested_subject_sha,
        policy_evaluator_sha=policy_evaluator_sha,
        signer=signer,
    )
    verify_rehearsal_result(rehearsal_result=rehearsal_result, signer=signer)
    if (
        rehearsal_result.get("status") != "rehearsals_passed"
        or rehearsal_result.get("tested_subject_sha") != tested_subject_sha
        or rehearsal_result.get("policy_evaluator_sha") != policy_evaluator_sha
        or rehearsal_result.get("source_authorization_sha256")
        != authorization.get("artifact_sha256")
    ):
        raise ValueError("v5 protected learning requires a passing cache-only rehearsal")
    controls = protected_learning_controls()
    body = {
        "schema_version": PROTECTED_AUTHORIZATION_SCHEMA_VERSION,
        "status": "protected_learning_enabled",
        "policy_revision": POLICY_REVISION,
        "policy_sha256": governance_v2_policy()["policy_sha256"],
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "source_authorization_sha256": authorization["artifact_sha256"],
        "source_rehearsal_sha256": rehearsal_result["artifact_sha256"],
        "source_diagnostic_sha256": expected_diagnostic_sha256,
        "controls": controls,
        "controls_sha256": sha256_hex(controls),
        "audit_record": {
            "event": "protected_learning_authorized",
            "append_only": True,
            "rollback_state": "armed",
            "monitoring_state": "required",
        },
    }
    return _sign_artifact(
        body,
        signer=signer,
        kind=V2_PROTECTED_AUTHORIZATION_KIND,
    )


def verify_rehearsal_result(
    *,
    rehearsal_result: Mapping[str, Any],
    signer: GovernanceSigner,
) -> dict[str, Any]:
    """Verify all cache-only and V2 gates before protected authorization."""

    _verify_signed_artifact(rehearsal_result, signer=signer, kind=V2_REHEARSAL_KIND)
    delegate_counts = rehearsal_result.get("embedding_delegate_call_counts")
    if (
        rehearsal_result.get("schema_version") != REHEARSAL_SCHEMA_VERSION
        or rehearsal_result.get("status") != "rehearsals_passed"
        or rehearsal_result.get("policy_revision") != POLICY_REVISION
        or rehearsal_result.get("policy_sha256") != governance_v2_policy()["policy_sha256"]
        or rehearsal_result.get("scenario_count") != EXPECTED_REHEARSAL_COUNT
        or rehearsal_result.get("rehearsal_selection_sha256")
        != EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256
        or rehearsal_result.get("embedding_cache_miss_count") != 0
        or delegate_counts != {DOCUMENT_TASK: 0, QUERY_TASK: 0}
        or rehearsal_result.get("all_hard_gates_passed") is not True
        or _finite_float(
            rehearsal_result.get("maximum_distance_delta"),
            "rehearsal distance delta",
        )
        > V2_MAXIMUM_DISTANCE_DELTA
        or int(rehearsal_result.get("semantic_rank_one_count") or 0)
        * V2_MINIMUM_SEMANTIC_DENOMINATOR
        < EXPECTED_REHEARSAL_COUNT * V2_MINIMUM_SEMANTIC_NUMERATOR
    ):
        raise ValueError("v5 rehearsal result does not satisfy cache-only governance V2")
    _require_code_sha(str(rehearsal_result.get("tested_subject_sha") or ""), "tested subject")
    _require_code_sha(
        str(rehearsal_result.get("policy_evaluator_sha") or ""),
        "policy evaluator",
    )
    for field in (
        "source_authorization_sha256",
        "source_diagnostic_sha256",
        "source_diagnostic_file_sha256",
        "checkpoint_sha256",
    ):
        _require_sha256(str(rehearsal_result.get(field) or ""), field.replace("_", " "))
    return dict(rehearsal_result)


def verify_protected_learning_authorization(
    *,
    protected_authorization: Mapping[str, Any],
    signer: GovernanceSigner,
) -> dict[str, Any]:
    """Fail closed unless the enablement artifact carries every fixed control."""

    _verify_signed_artifact(
        protected_authorization,
        signer=signer,
        kind=V2_PROTECTED_AUTHORIZATION_KIND,
    )
    controls = protected_learning_controls()
    if (
        protected_authorization.get("status") != "protected_learning_enabled"
        or protected_authorization.get("policy_sha256") != governance_v2_policy()["policy_sha256"]
        or protected_authorization.get("controls") != controls
        or protected_authorization.get("controls_sha256") != sha256_hex(controls)
    ):
        raise ValueError("v5 protected-learning controls are incomplete")
    _require_code_sha(
        str(protected_authorization.get("tested_subject_sha") or ""),
        "tested subject",
    )
    _require_code_sha(
        str(protected_authorization.get("policy_evaluator_sha") or ""),
        "policy evaluator",
    )
    for field in (
        "source_authorization_sha256",
        "source_rehearsal_sha256",
        "source_diagnostic_sha256",
        "controls_sha256",
    ):
        _require_sha256(str(protected_authorization.get(field) or ""), field.replace("_", " "))
    return dict(protected_authorization)


def protected_learning_controls() -> dict[str, Any]:
    """Return mandatory runtime controls bound into enablement authority."""

    return {
        "monitoring": {
            "enabled": True,
            "signals": [
                "semantic_rank_one_accuracy",
                "hard_gate_failures",
                "embedding_cache_misses",
                "tenant_isolation_failures",
            ],
            "stop_on_hard_gate_failure": True,
        },
        "audit": {
            "enabled": True,
            "append_only": True,
            "required_records": [
                "authorization",
                "retrieval_decision",
                "memory_read",
                "outcome",
                "rollback",
            ],
        },
        "rollback": {
            "enabled": True,
            "default_state": "armed",
            "action": "disable-protected-learning-and-revoke-authorization",
            "triggers": [
                "hard_gate_failure",
                "integrity_failure",
                "monitoring_unavailable",
            ],
        },
    }


def write_private_json(path: str | os.PathLike[str], value: Mapping[str, Any]) -> pathlib.Path:
    """Atomically write one private governance artifact outside the repository."""

    target = pathlib.Path(path).expanduser().resolve(strict=False)
    repository = pathlib.Path(__file__).resolve().parents[2]
    if target == repository or repository in target.parents:
        raise ValueError("v5 governance artifacts must remain outside the repository")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def _evaluate_diagnostic(
    *,
    diagnostic: Mapping[str, Any],
    expected_diagnostic_sha256: str,
    diagnostic_file_sha256: str,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
) -> dict[str, Any]:
    _require_code_sha(tested_subject_sha, "tested subject")
    _require_code_sha(policy_evaluator_sha, "policy evaluator")
    _require_sha256(expected_diagnostic_sha256, "expected diagnostic")
    _require_sha256(diagnostic_file_sha256, "diagnostic file")
    body = {key: value for key, value in diagnostic.items() if key != "diagnostic_sha256"}
    observed_diagnostic_sha256 = diagnostic.get("diagnostic_sha256")
    if (
        observed_diagnostic_sha256 != expected_diagnostic_sha256
        or sha256_hex(body) != expected_diagnostic_sha256
    ):
        raise ValueError("v5 diagnostic content identity differs")
    if (
        diagnostic.get("schema_version") != 2
        or diagnostic.get("status") != "diagnostic_only"
        or diagnostic.get("qualification_claim") is not False
        or diagnostic.get("code_sha") != tested_subject_sha
        or diagnostic.get("qualification_contract_sha256") != V1_QUALIFICATION_CONTRACT_SHA256
        or diagnostic.get("database_engine") != "cockroachdb"
        or "failure" in diagnostic
    ):
        raise ValueError("v5 diagnostic envelope is not eligible for V2")
    expected_provider = {
        "provider": EMBEDDING_PROVIDER,
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "capability": EMBEDDING_CAPABILITY,
        "encoder_revision": EMBEDDING_ENCODER_REVISION,
        "representation": GEMINI_PROVIDER_REPRESENTATION,
    }
    if diagnostic.get("provider_identity") != expected_provider:
        raise ValueError("v5 diagnostic provider identity differs")
    if diagnostic.get("checkpoint_entry_counts") != {
        DOCUMENT_TASK: EXPECTED_UNIQUE_DOCUMENTS,
        QUERY_TASK: EXPECTED_SCENARIO_COUNT,
    }:
        raise ValueError("v5 diagnostic checkpoint coverage is incomplete")
    _require_sha256(str(diagnostic.get("checkpoint_sha256") or ""), "checkpoint")
    _require_sha256(
        str(diagnostic.get("checkpoint_attestation_key_id_sha256") or ""),
        "checkpoint attestation key",
    )
    _require_sha256(
        str(diagnostic.get("execution_manifest_sha256") or ""),
        "execution manifest",
    )
    results = diagnostic.get("results")
    if not isinstance(results, list) or len(results) != EXPECTED_SCENARIO_COUNT:
        raise ValueError("v5 diagnostic must contain all 600 scenarios")
    expected_ids = [
        str(item["scenario_id"]) for item in select_embedding_scenarios(code_sha=tested_subject_sha)
    ]
    observed_ids = [str(row.get("scenario_id") or "") for row in results if isinstance(row, dict)]
    if len(observed_ids) != len(results) or observed_ids != expected_ids:
        raise ValueError("v5 diagnostic scenarios are incomplete or reordered")
    for row in results:
        if not isinstance(row, dict):
            raise ValueError("v5 diagnostic result shape is invalid")
        expected_v1_status = "qualified" if _v1_row_qualified(row) else "failed"
        if row.get("status") != expected_v1_status:
            raise ValueError("v5 diagnostic result differs from V1 semantics")
    if diagnostic.get("qualified_row_count") != sum(
        row.get("status") == "qualified" for row in results
    ):
        raise ValueError("v5 diagnostic V1 count differs from its rows")
    metrics = _evaluate_rows(results, expected_ids=expected_ids)
    status = "v2_passed" if metrics["passed"] else "v2_rejected"
    policy = governance_v2_policy()
    policy_artifact = governance_v2_policy_artifact(
        tested_subject_sha=tested_subject_sha,
        policy_evaluator_sha=policy_evaluator_sha,
    )
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "status": status,
        "policy_revision": POLICY_REVISION,
        "policy_sha256": policy["policy_sha256"],
        "policy_artifact_sha256": policy_artifact["policy_artifact_sha256"],
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "source_qualification_contract_sha256": V1_QUALIFICATION_CONTRACT_SHA256,
        "source_execution_manifest_sha256": diagnostic["execution_manifest_sha256"],
        "source_diagnostic_sha256": expected_diagnostic_sha256,
        "source_diagnostic_file_sha256": diagnostic_file_sha256,
        "source_checkpoint_sha256": diagnostic["checkpoint_sha256"],
        "scenario_count": EXPECTED_SCENARIO_COUNT,
        "semantic_rank_one_count": metrics["semantic_rank_one_count"],
        "semantic_rank_one_accuracy_basis_points": metrics[
            "semantic_rank_one_accuracy_basis_points"
        ],
        "semantic_rank_one_accuracy_display": metrics["semantic_rank_one_accuracy_display"],
        "minimum_semantic_rank_one_accuracy_basis_points": 9000,
        "maximum_distance_delta": metrics["maximum_distance_delta"],
        "maximum_allowed_distance_delta": V2_MAXIMUM_DISTANCE_DELTA,
        "all_hard_gates_passed": metrics["all_hard_gates_passed"],
        "individual_scenario_exclusions": 0,
        "v1_qualified_row_count": diagnostic["qualified_row_count"],
        "protected_learning_enabled": False,
        "development_rehearsals_authorized": status == "v2_passed",
    }


def _evaluate_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_ids: Sequence[str],
) -> dict[str, Any]:
    if len(rows) != len(expected_ids):
        raise ValueError("v5 governance evaluation cannot exclude scenarios")
    if [str(row.get("scenario_id") or "") for row in rows] != list(expected_ids):
        raise ValueError("v5 governance evaluation scenario order differs")
    hard_gates = [_v2_hard_gates_pass(row) for row in rows]
    semantic = [_semantic_rank_one(row) for row in rows]
    count = sum(semantic)
    total = len(rows)
    accuracy_passed = (
        count * V2_MINIMUM_SEMANTIC_DENOMINATOR >= total * V2_MINIMUM_SEMANTIC_NUMERATOR
    )
    basis_points = (count * 10_000 + total // 2) // total
    maximum_distance_delta = max(
        _finite_float(row.get("max_distance_delta"), "distance delta") for row in rows
    )
    return {
        "passed": all(hard_gates) and accuracy_passed,
        "all_hard_gates_passed": all(hard_gates),
        "semantic_rank_one_count": count,
        "semantic_rank_one_accuracy_basis_points": basis_points,
        "semantic_rank_one_accuracy_display": f"{count * 100 / total:.1f}%",
        "maximum_distance_delta": maximum_distance_delta,
    }


def _v1_row_qualified(row: Mapping[str, Any]) -> bool:
    return (
        _base_row_shape_valid(row)
        and row.get("membership_parity") is True
        and row.get("order_parity") is True
        and row.get("direct_candidate_ids") == row.get("indexed_candidate_ids")
        and row.get("matching_rank") == 1
        and row.get("indexed_matching_rank") == 1
        and _finite_float(row.get("rank_one_margin"), "rank-one margin") > 0
        and row.get("index_parity") is True
        and _finite_float(row.get("max_distance_delta"), "distance delta") <= 1e-6
    )


def _v2_hard_gates_pass(row: Mapping[str, Any]) -> bool:
    if not _base_row_shape_valid(row):
        return False
    direct = row.get("direct_candidate_ids")
    indexed = row.get("indexed_candidate_ids")
    return (
        row.get("membership_parity") is True
        and row.get("order_parity") is True
        and direct == indexed
        and _finite_float(row.get("max_distance_delta"), "distance delta")
        <= V2_MAXIMUM_DISTANCE_DELTA
    )


def _base_row_shape_valid(row: Mapping[str, Any]) -> bool:
    direct = row.get("direct_candidate_ids")
    indexed = row.get("indexed_candidate_ids")
    try:
        _require_uuid_identity(row.get("retrieval_id"), label="retrieval")
        rank_distance = _finite_float(row.get("rank_one_distance"), "rank-one distance")
    except ValueError:
        return False
    candidate_ids_valid = all(
        isinstance(values, list)
        and bool(values)
        and len(values) <= 3
        and len(set(values)) == len(values)
        and all(isinstance(item, str) and OPAQUE_MEMORY_RE.fullmatch(item) for item in values)
        for values in (direct, indexed)
    )
    return (
        row.get("candidate_count") == 4
        and row.get("policy") == "semantic_strict"
        and row.get("fallback_reason") is None
        and row.get("intrinsic_match_count") == 1
        and rank_distance <= EMBEDDING_MAX_DISTANCE
        and candidate_ids_valid
        and row.get("ineligible_candidate_absent") is True
        and row.get("ineligible_read_absent") is True
        and row.get("audit_only_visible") is True
        and row.get("alternate_tenant_visible") is False
        and row.get("alternate_retrieval_visible") is False
        and row.get("alternate_current_semantic_visible") is False
        and row.get("alternate_audit_visible") is False
        and row.get("alternate_learning_reads_visible") is False
        and row.get("learning_decision_sealed") is True
        and row.get("alternate_decision_sealed") is True
    )


def _semantic_rank_one(row: Mapping[str, Any]) -> bool:
    return (
        row.get("matching_rank") == 1
        and row.get("indexed_matching_rank") == 1
        and _finite_float(row.get("rank_one_distance"), "rank-one distance")
        <= EMBEDDING_MAX_DISTANCE
        and _finite_float(row.get("rank_one_margin"), "rank-one margin") > 0
    )


def _run_rehearsal_cases(
    *,
    selected: Sequence[Mapping[str, Any]],
    rehearsal_ids: Sequence[str],
    db_url: str,
    provider: CheckpointedEmbeddingProvider,
    store_factory: Callable[..., QualificationStore],
    progress_callback: Callable[[str, int, int], None] | None,
) -> list[dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    with tenant_scope(learning_tenant_id()):
        with store_factory(url=db_url, embedding_provider=provider) as store:
            for index, scenario in enumerate(selected, start=1):
                case = _load_database_case(
                    scenario=scenario,
                    store=store,
                    provider=provider,
                    contract_sha256=V1_QUALIFICATION_CONTRACT_SHA256,
                )
                loaded[str(case["scenario_id"])] = case
                if progress_callback is not None:
                    progress_callback("rehearsal_index_population", index * 4, len(selected) * 4)
            results = []
            for index, scenario_id in enumerate(rehearsal_ids, start=1):
                results.append(
                    _retrieve_database_case(
                        loaded_case=loaded[scenario_id],
                        store=store,
                        provider=provider,
                    )
                )
                if progress_callback is not None:
                    progress_callback("cache_only_rehearsal", index, len(rehearsal_ids))
    _append_alternate_tenant_evidence(
        loaded=loaded,
        rehearsal_ids=rehearsal_ids,
        results=results,
        db_url=db_url,
        provider=provider,
        store_factory=store_factory,
    )
    return results


def _append_alternate_tenant_evidence(
    *,
    loaded: Mapping[str, Mapping[str, Any]],
    rehearsal_ids: Sequence[str],
    results: list[dict[str, Any]],
    db_url: str,
    provider: CheckpointedEmbeddingProvider,
    store_factory: Callable[..., QualificationStore],
    retrieval_decision_prefix: str = "v5-development-retrieval",
    isolation_decision_prefix: str = "v5-development-isolation",
    isolation_reader: str = "v5.development.isolation",
    isolation_purpose: str = "Verify alternate-tenant invisibility",
) -> None:
    with tenant_scope(ACCEPTANCE_TENANT_ID):
        with store_factory(url=db_url, embedding_provider=provider) as store:
            for scenario_id, result in zip(rehearsal_ids, results, strict=True):
                case = loaded[scenario_id]
                scenario = case["scenario"]
                query = render_retrieval_query(scenario)
                namespace = str(case["namespace"])
                decision_id = f"{isolation_decision_prefix}:{scenario_id}"
                with provider.query_scope(scenario_id=scenario_id, query=query):
                    alternate = store.retrieve_semantic(
                        namespace=namespace,
                        query=query,
                        decision_id=decision_id,
                        reader=isolation_reader,
                        purpose=isolation_purpose,
                        policy="semantic_strict",
                        limit=4,
                        positive_guidance_only=True,
                    )
                _verify_retrieval_trace(
                    store=store,
                    retrieval=alternate,
                    decision_id=decision_id,
                )
                _seal_retrieval_decision(store=store, decision_id=decision_id)
                retrieval_visible = (
                    bool(_result_hits(alternate)) or _result_value(alternate, "status") != "empty"
                )
                current_visible = bool(store.current_semantic(namespace=namespace, limit=4))
                audit_visible = any(
                    store.audit_memory(memory_kind="semantic", memory_id=database_id) is not None
                    for database_id in case["database_ids"]
                )
                learning_reads_visible = bool(
                    store.reads_for_decision(
                        decision_id=f"{retrieval_decision_prefix}:{scenario_id}"
                    )
                )
                result.update(
                    {
                        "alternate_retrieval_visible": retrieval_visible,
                        "alternate_current_semantic_visible": current_visible,
                        "alternate_audit_visible": audit_visible,
                        "alternate_learning_reads_visible": learning_reads_visible,
                        "alternate_tenant_visible": any(
                            (
                                retrieval_visible,
                                current_visible,
                                audit_visible,
                                learning_reads_visible,
                            )
                        ),
                        "alternate_decision_sealed": True,
                    }
                )


def _require_execution_manifest(
    manifest: Mapping[str, Any],
    *,
    diagnostic: Mapping[str, Any],
    tested_subject_sha: str,
) -> None:
    body = {key: value for key, value in manifest.items() if key != "execution_manifest_sha256"}
    if (
        manifest.get("schema_version") != 1
        or manifest.get("code_sha") != tested_subject_sha
        or manifest.get("qualification_contract_sha256") != V1_QUALIFICATION_CONTRACT_SHA256
        or manifest.get("execution_manifest_sha256") != sha256_hex(body)
        or manifest.get("execution_manifest_sha256") != diagnostic.get("execution_manifest_sha256")
    ):
        raise ValueError("v5 rehearsal execution manifest identity differs")


def _sign_artifact(
    body: Mapping[str, Any],
    *,
    signer: GovernanceSigner,
    kind: str,
) -> dict[str, Any]:
    key_id = str(getattr(signer, "key_id", "")).strip()
    if not key_id:
        raise ValueError("v5 governance signing key identity is required")
    artifact_sha256 = sha256_hex(dict(body))
    mac = signer.token(kind=kind, raw_id=artifact_sha256)
    _require_sha256(mac, "governance signature")
    return {
        **dict(body),
        "artifact_sha256": artifact_sha256,
        "signature": {
            "algorithm": SIGNATURE_ALGORITHM,
            "kind": kind,
            "key_id_sha256": hashlib.sha256(key_id.encode("utf-8")).hexdigest(),
            "mac": mac,
        },
    }


def _verify_signed_artifact(
    artifact: Mapping[str, Any],
    *,
    signer: GovernanceSigner,
    kind: str,
) -> None:
    artifact_sha256 = str(artifact.get("artifact_sha256") or "")
    _require_sha256(artifact_sha256, "artifact")
    body = _unsigned_artifact_body(artifact)
    if sha256_hex(body) != artifact_sha256:
        raise ValueError("v5 governance artifact content identity differs")
    signature = artifact.get("signature")
    key_id = str(getattr(signer, "key_id", "")).strip()
    expected_key_id_sha256 = hashlib.sha256(key_id.encode("utf-8")).hexdigest()
    if (
        not isinstance(signature, Mapping)
        or signature.get("algorithm") != SIGNATURE_ALGORITHM
        or signature.get("kind") != kind
        or signature.get("key_id_sha256") != expected_key_id_sha256
    ):
        raise ValueError("v5 governance artifact signature envelope differs")
    expected_mac = signer.token(kind=kind, raw_id=artifact_sha256)
    if not hmac.compare_digest(str(signature.get("mac") or ""), expected_mac):
        raise ValueError("v5 governance artifact signature is invalid")


def _unsigned_artifact_body(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in artifact.items() if key not in {"artifact_sha256", "signature"}
    }


def _binary32_vector(values: Sequence[float]) -> list[float]:
    if len(values) != EMBEDDING_DIMENSIONS:
        raise ValueError("v5 stored-vector characterization requires exact dimensions")
    converted = [_binary32(float(value)) for value in values]
    if not all(math.isfinite(value) for value in converted):
        raise ValueError("v5 stored-vector characterization requires finite values")
    return converted


def _binary32(value: float) -> float:
    return struct.unpack("!f", struct.pack("!f", value))[0]


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("v5 stored-vector characterization shape differs")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        raise ValueError("v5 stored-vector characterization requires nonzero vectors")
    return 1.0 - dot / (left_norm * right_norm)


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"v5 governance {label} identity must be SHA-256")


def _require_code_sha(value: str, label: str) -> None:
    if not _CODE_SHA_RE.fullmatch(value):
        raise ValueError(f"v5 governance {label} identity must be an exact commit SHA")
