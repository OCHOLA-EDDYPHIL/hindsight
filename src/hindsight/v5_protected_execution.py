"""Embedding preparation and outcome-free retrieval qualification for V5 protected study."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hindsight.db import connect
from hindsight.embedding_index import activate_profile, begin_profile_build
from hindsight.memory import MemoryStore
from hindsight.server_tenants import learning_tenant_id
from hindsight.tenant import tenant_scope
from hindsight.v5_corpus import sha256_hex
from hindsight.v5_governance import (
    CacheOnlyEmbeddingProvider,
    V2_MINIMUM_SEMANTIC_DENOMINATOR,
    V2_MINIMUM_SEMANTIC_NUMERATOR,
    _append_alternate_tenant_evidence,
    _semantic_rank_one,
    _v2_hard_gates_pass,
)
from hindsight.v5_protected import (
    EMBEDDING_CHECKPOINT_KIND,
    RETRIEVAL_RESULT_KIND,
    ProtectedStudySigner,
    protected_study_protocol,
    sign_protected_artifact,
)
from hindsight.v5_qualification import (
    DOCUMENT_TASK,
    QUERY_TASK,
    CheckpointAttestor,
    CheckpointedEmbeddingProvider,
    EmbeddingProvider,
    QualificationStore,
    _initialize_exact_profile,
    _load_database_case,
    _require_database_identities,
    _retrieve_database_case,
    render_retrieval_document,
    render_retrieval_query,
    require_fresh_development_database,
    require_restricted_runtime_database,
)


PROTECTED_DATABASE_RE = re.compile(r"hindsight_v5_protected_[a-z0-9_]+")


def _semantic_accuracy_passes(*, rank_one_count: int, scenario_count: int) -> bool:
    return (
        scenario_count > 0
        and rank_one_count * V2_MINIMUM_SEMANTIC_DENOMINATOR
        >= scenario_count * V2_MINIMUM_SEMANTIC_NUMERATOR
    )


class DevelopmentCacheThenEmbeddingProvider:
    """Reuse development document vectors and call Gemini only on genuine misses."""

    def __init__(
        self,
        *,
        development_checkpoint: CheckpointedEmbeddingProvider,
        development_cache_delegate: CacheOnlyEmbeddingProvider,
        live_provider: EmbeddingProvider,
    ) -> None:
        self._development = development_checkpoint
        self._cache_delegate = development_cache_delegate
        self._live = live_provider
        self.provider_name = live_provider.provider_name
        self.model_name = live_provider.model_name
        self.dimensions = live_provider.dimensions
        self.capability = live_provider.capability
        self.encoder_revision = live_provider.encoder_revision
        self.representation = str(getattr(live_provider, "representation", ""))
        self.development_document_hits = 0
        self.live_document_calls = 0
        self.live_query_calls = 0

    def embed(self, text: str) -> list[float]:
        return self.embed_document(text)

    def embed_document(self, text: str) -> list[float]:
        misses_before = self._cache_delegate.miss_count
        try:
            vector = self._development.embed_document(text)
        except RuntimeError:
            if self._cache_delegate.miss_count != misses_before + 1:
                raise
            self.live_document_calls += 1
            return self._live.embed_document(text)
        self.development_document_hits += 1
        return vector

    def embed_query(self, text: str) -> list[float]:
        self.live_query_calls += 1
        return self._live.embed_query(text)

    @property
    def source_counts(self) -> dict[str, int]:
        return {
            "development_document_cache_hits": self.development_document_hits,
            "live_document_provider_calls": self.live_document_calls,
            "live_query_provider_calls": self.live_query_calls,
        }


def build_protected_embedding_checkpoint(
    *,
    scenarios: Sequence[Mapping[str, Any]],
    delegate: EmbeddingProvider,
    checkpoint_path: str | os.PathLike[str],
    attestor: CheckpointAttestor,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    protected_runner_sha: str,
    final_freeze_sha256: str,
    sealed_corpus_sha256: str,
    signer: ProtectedStudySigner,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[CheckpointedEmbeddingProvider, dict[str, Any]]:
    """Create or resume the separate protected checkpoint and seal exact coverage."""

    checkpoint = CheckpointedEmbeddingProvider(
        delegate,
        checkpoint_path,
        code_sha=protected_runner_sha,
        attestor=attestor,
        execution_manifest_sha256=final_freeze_sha256,
        qualification_contract_sha256=protected_study_protocol()["protocol_sha256"],
    )
    total = len(scenarios) * 5
    current = 0
    for scenario in scenarios:
        for memory in scenario["agent_view"]["memories"]:
            checkpoint.embed_document(render_retrieval_document(memory))
            current += 1
            if progress_callback is not None:
                progress_callback("protected_embedding_checkpoint", current, total)
        query = render_retrieval_query(scenario)
        with checkpoint.query_scope(scenario_id=str(scenario["scenario_id"]), query=query):
            checkpoint.embed_query(query)
        current += 1
        if progress_callback is not None:
            progress_callback("protected_embedding_checkpoint", current, total)
    coverage = verify_protected_checkpoint_coverage(
        checkpoint=checkpoint,
        scenarios=scenarios,
    )
    body = {
        "schema_version": 1,
        "status": "protected_embeddings_ready",
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "protected_runner_sha": protected_runner_sha,
        "final_freeze_sha256": final_freeze_sha256,
        "sealed_corpus_sha256": sealed_corpus_sha256,
        "protocol_sha256": protected_study_protocol()["protocol_sha256"],
        "checkpoint_sha256": checkpoint.checkpoint_sha256,
        "checkpoint_entry_counts": checkpoint.entry_counts,
        "expected_input_set_sha256": coverage["expected_input_set_sha256"],
        "provider_identity": checkpoint.delegate_identity,
        "delegate_call_counts": checkpoint.delegate_call_counts,
        "embedding_source_counts": dict(getattr(delegate, "source_counts", {})),
        "exact_cache_coverage": True,
    }
    return checkpoint, sign_protected_artifact(
        body,
        signer=signer,
        kind=EMBEDDING_CHECKPOINT_KIND,
    )


def verify_protected_checkpoint_coverage(
    *,
    checkpoint: CheckpointedEmbeddingProvider,
    scenarios: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Require the checkpoint to contain exactly the final protected input set."""

    expected_documents = {
        hashlib.sha256(render_retrieval_document(memory).encode("utf-8")).hexdigest()
        for scenario in scenarios
        for memory in scenario["agent_view"]["memories"]
    }
    expected_queries = {
        (
            str(scenario["scenario_id"]),
            hashlib.sha256(render_retrieval_query(scenario).encode("utf-8")).hexdigest(),
        )
        for scenario in scenarios
    }
    entries_path = checkpoint.checkpoint_path / "entries"
    observed_documents: set[str] = set()
    observed_queries: set[tuple[str, str]] = set()
    for path in sorted(entries_path.glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry["task_type"] == DOCUMENT_TASK:
            observed_documents.add(str(entry["input_sha256"]))
        elif entry["task_type"] == QUERY_TASK:
            observed_queries.add((str(entry["scenario_id"]), str(entry["input_sha256"])))
        else:
            raise ValueError("v5 protected checkpoint task type differs")
    if observed_documents != expected_documents or observed_queries != expected_queries:
        raise ValueError("v5 protected embedding checkpoint coverage differs")
    input_set = {
        "documents": sorted(expected_documents),
        "queries": [list(item) for item in sorted(expected_queries)],
    }
    return {
        "document_count": len(expected_documents),
        "query_count": len(expected_queries),
        "expected_input_set_sha256": sha256_hex(input_set),
    }


def open_cache_only_protected_checkpoint(
    *,
    scenarios: Sequence[Mapping[str, Any]],
    checkpoint_path: str | os.PathLike[str],
    attestor: CheckpointAttestor,
    protected_runner_sha: str,
    final_freeze_sha256: str,
) -> tuple[CheckpointedEmbeddingProvider, CacheOnlyEmbeddingProvider]:
    """Open and completely exercise the protected cache without provider access."""

    delegate = CacheOnlyEmbeddingProvider()
    checkpoint = CheckpointedEmbeddingProvider(
        delegate,
        checkpoint_path,
        code_sha=protected_runner_sha,
        attestor=attestor,
        execution_manifest_sha256=final_freeze_sha256,
        qualification_contract_sha256=protected_study_protocol()["protocol_sha256"],
    )
    verify_protected_checkpoint_coverage(checkpoint=checkpoint, scenarios=scenarios)
    for scenario in scenarios:
        for memory in scenario["agent_view"]["memories"]:
            checkpoint.embed_document(render_retrieval_document(memory))
        query = render_retrieval_query(scenario)
        with checkpoint.query_scope(scenario_id=str(scenario["scenario_id"]), query=query):
            checkpoint.embed_query(query)
    if delegate.miss_count or any(checkpoint.delegate_call_counts.values()):
        raise RuntimeError("v5 protected cache-only preflight attempted a provider call")
    return checkpoint, delegate


def qualify_protected_retrieval(
    *,
    scenarios: Sequence[Mapping[str, Any]],
    checkpoint: CheckpointedEmbeddingProvider,
    cache_only_delegate: CacheOnlyEmbeddingProvider,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    protected_runner_sha: str,
    final_freeze_sha256: str,
    sealed_corpus_sha256: str,
    embedding_checkpoint_sha256: str,
    database_url: str,
    runtime_database_url: str,
    signer: ProtectedStudySigner,
    connect_fn: Callable[..., Any] = connect,
    store_factory: Callable[..., QualificationStore] = MemoryStore,
    begin_profile_build_fn: Callable[..., Mapping[str, Any]] = begin_profile_build,
    activate_profile_fn: Callable[..., Mapping[str, Any]] = activate_profile,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Require 90% rank-one accuracy and 100% protected hard gates."""

    database_evidence = require_fresh_development_database(
        database_url,
        connect_fn=connect_fn,
        database_name_pattern=PROTECTED_DATABASE_RE,
    )
    if (
        database_evidence.get("engine") != "cockroachdb"
        or not PROTECTED_DATABASE_RE.fullmatch(str(database_evidence.get("database_name") or ""))
    ):
        raise RuntimeError("v5 protected database identity evidence is invalid")
    identities = require_restricted_runtime_database(
        database_url,
        runtime_database_url,
        connect_fn=connect_fn,
        database_name_pattern=PROTECTED_DATABASE_RE,
    )
    _require_database_identities(identities)
    profile = _initialize_exact_profile(
        provider=checkpoint,
        db_url=database_url,
        store_factory=store_factory,
        begin_profile_build_fn=begin_profile_build_fn,
        activate_profile_fn=activate_profile_fn,
    )
    loaded: dict[str, dict[str, Any]] = {}
    with tenant_scope(learning_tenant_id()):
        with store_factory(url=runtime_database_url, embedding_provider=checkpoint) as store:
            for index, scenario in enumerate(scenarios, start=1):
                case = _load_database_case(
                    scenario=scenario,
                    store=store,
                    provider=checkpoint,
                    contract_sha256=protected_study_protocol()["protocol_sha256"],
                    namespace_prefix="v5-protected-qualification",
                    writer="v5.protected.qualification",
                    source_prefix="v5-protected-qualification",
                    justification="Store one governed protected retrieval candidate",
                    content_schema="v5_protected_candidate.v1",
                )
                loaded[str(case["scenario_id"])] = case
                if progress_callback is not None:
                    progress_callback("protected_index_population", index, len(scenarios))
            rows = []
            for index, scenario in enumerate(scenarios, start=1):
                rows.append(
                    _retrieve_database_case(
                        loaded_case=loaded[str(scenario["scenario_id"])],
                        store=store,
                        provider=checkpoint,
                        decision_prefix="v5-protected-retrieval",
                        reader="v5.protected.qualification",
                        purpose="Qualify strict rank-one protected retrieval",
                    )
                )
                if progress_callback is not None:
                    progress_callback("protected_retrieval_qualification", index, len(scenarios))
    _append_alternate_tenant_evidence(
        loaded=loaded,
        rehearsal_ids=[str(row["scenario_id"]) for row in scenarios],
        results=rows,
        db_url=runtime_database_url,
        provider=checkpoint,
        store_factory=store_factory,
        retrieval_decision_prefix="v5-protected-retrieval",
        isolation_decision_prefix="v5-protected-isolation",
        isolation_reader="v5.protected.isolation",
        isolation_purpose="Verify protected alternate-tenant invisibility",
    )
    hard_gate_rows = [
        _v2_hard_gates_pass(row)
        and row.get("alternate_tenant_visible") is False
        and row.get("alternate_decision_sealed") is True
        for row in rows
    ]
    rank_one_count = sum(_semantic_rank_one(row) for row in rows)
    semantic_accuracy_passed = _semantic_accuracy_passes(
        rank_one_count=rank_one_count,
        scenario_count=len(rows),
    )
    prepared_cases: dict[str, dict[str, Any]] = {}
    prepared_arm_gate_count = 0
    if (
        all(hard_gate_rows)
        and semantic_accuracy_passed
        and len(rows) == len(scenarios)
    ):
        from hindsight.v5_protected_runtime import (
            DatabaseArmContextProvider,
            prepare_arm_database,
        )

        with tenant_scope(learning_tenant_id()):
            prepared_cases = prepare_arm_database(
                scenarios=scenarios,
                db_url=runtime_database_url,
                provider=checkpoint,
                execution_contract_sha256=protected_study_protocol()["protocol_sha256"],
            )
            context_provider = DatabaseArmContextProvider(
                db_url=runtime_database_url,
                provider=checkpoint,
                prepared_cases=prepared_cases,
                cache_only_delegate=cache_only_delegate,
            )
            for scenario in scenarios:
                for arm in ("no_lesson", "reference_lesson", "consolidated_lesson"):
                    context = context_provider.context(
                        scenario=scenario,
                        arm=arm,
                        trial_id=f"qualification-{scenario['scenario_id']}-{arm}",
                        step=1,
                    )
                    if context.get("hard_gate_passed") is not True:
                        raise RuntimeError("v5 protected prepared arm failed qualification")
                    prepared_arm_gate_count += 1
    cache_clean = not cache_only_delegate.miss_count and not any(
        checkpoint.delegate_call_counts.values()
    )
    passed = (
        all(hard_gate_rows)
        and semantic_accuracy_passed
        and cache_clean
        and len(rows) == len(scenarios)
        and prepared_arm_gate_count == len(scenarios) * 3
    )
    body = {
        "schema_version": 1,
        "status": "protected_retrieval_passed" if passed else "protected_retrieval_failed",
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "protected_runner_sha": protected_runner_sha,
        "final_freeze_sha256": final_freeze_sha256,
        "sealed_corpus_sha256": sealed_corpus_sha256,
        "embedding_checkpoint_sha256": embedding_checkpoint_sha256,
        "database_name": database_evidence["database_name"],
        "database_engine": database_evidence["engine"],
        "database_cluster_id_sha256": hashlib.sha256(
            database_evidence["cluster_id"].encode("utf-8")
        ).hexdigest(),
        "deploy_database_identity_sha256": hashlib.sha256(
            identities["deploy_identity"].encode("utf-8")
        ).hexdigest(),
        "runtime_database_identity_sha256": hashlib.sha256(
            identities["runtime_identity"].encode("utf-8")
        ).hexdigest(),
        "embedding_profile_id": str(profile["id"]),
        "scenario_count": len(rows),
        "rank_one_count": rank_one_count,
        "semantic_accuracy_passed": semantic_accuracy_passed,
        "all_hard_gates_passed": all(hard_gate_rows),
        "embedding_cache_miss_count": cache_only_delegate.miss_count,
        "embedding_delegate_call_counts": checkpoint.delegate_call_counts,
        "prepared_arm_gate_count": prepared_arm_gate_count,
        "prepared_cases": prepared_cases,
        "prepared_cases_sha256": sha256_hex(prepared_cases),
        "results": rows,
        "result_sha256": sha256_hex(rows),
    }
    return sign_protected_artifact(body, signer=signer, kind=RETRIEVAL_RESULT_KIND)


def validate_protected_retrieval_result(
    result: Mapping[str, Any], *, expected_scenario_ids: Sequence[str]
) -> dict[str, Any]:
    """Recompute every protected retrieval gate from the signed diagnostic rows."""

    rows = result.get("results")
    prepared = result.get("prepared_cases")
    if not isinstance(rows, list) or not isinstance(prepared, Mapping):
        raise ValueError("v5 protected retrieval evidence is incomplete")
    observed_ids = [str(row.get("scenario_id") or "") for row in rows]
    hard_gate_rows = [
        _v2_hard_gates_pass(row)
        and row.get("alternate_tenant_visible") is False
        and row.get("alternate_decision_sealed") is True
        for row in rows
    ]
    rank_one_count = sum(_semantic_rank_one(row) for row in rows)
    semantic_accuracy_passed = _semantic_accuracy_passes(
        rank_one_count=rank_one_count,
        scenario_count=len(rows),
    )
    delegate_counts = result.get("embedding_delegate_call_counts")
    if (
        result.get("status") != "protected_retrieval_passed"
        or observed_ids != list(expected_scenario_ids)
        or result.get("scenario_count") != len(expected_scenario_ids)
        or not all(hard_gate_rows)
        or not semantic_accuracy_passed
        or result.get("rank_one_count") != rank_one_count
        or result.get("semantic_accuracy_passed") is not True
        or result.get("all_hard_gates_passed") is not True
        or result.get("embedding_cache_miss_count") != 0
        or not isinstance(delegate_counts, Mapping)
        or any(int(value) != 0 for value in delegate_counts.values())
        or result.get("result_sha256") != sha256_hex(rows)
        or set(prepared) != set(expected_scenario_ids)
        or result.get("prepared_cases_sha256") != sha256_hex(prepared)
        or result.get("prepared_arm_gate_count") != len(expected_scenario_ids) * 3
    ):
        raise ValueError("v5 protected retrieval result fails a frozen gate")
    return dict(result)


__all__ = [
    "DevelopmentCacheThenEmbeddingProvider",
    "build_protected_embedding_checkpoint",
    "open_cache_only_protected_checkpoint",
    "qualify_protected_retrieval",
    "validate_protected_retrieval_result",
    "verify_protected_checkpoint_coverage",
]
