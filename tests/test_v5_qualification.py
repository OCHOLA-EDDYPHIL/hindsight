from __future__ import annotations

import copy
import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import SerializationFailure

from hindsight import v5_qualification as qualification
from hindsight.tenant import current_tenant_id
from hindsight.v5_corpus import EMBEDDING_PROFILE_ID, sha256_hex


CODE_SHA = "a" * 40
SCENARIO_ID = f"v5s-{'1' * 24}"
TARGET_ID = f"v5m-{'1' * 24}"
DECOY_ID = f"v5m-{'2' * 24}"
LEARNING_RETRIEVAL_ID = "00000000-0000-0000-0000-000000000001"
ALTERNATE_RETRIEVAL_ID = "00000000-0000-0000-0000-000000000002"
DATABASE_CLUSTER_ID = "00000000-0000-0000-0000-000000000003"


def _unit_vector(index: int) -> list[float]:
    values = [0.0] * qualification.EMBEDDING_DIMENSIONS
    values[index] = 1.0
    return values


class FakeGeminiEmbeddingProvider:
    provider_name = "gemini"
    model_name = "gemini-embedding-2"
    dimensions = 1024
    capability = "semantic"
    encoder_revision = "gemini-retrieval-task-v1"
    representation = "raw_control"

    def __init__(self) -> None:
        self.document_calls: list[str] = []
        self.query_calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        return self.embed_document(text)

    def embed_document(self, text: str) -> list[float]:
        self.document_calls.append(text)
        return _unit_vector(0 if text == "alpha guidance" else 1)

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return _unit_vector(0)


class FakeAttestor:
    key_id = "arn:aws:kms:us-east-1:111122223333:key/v5-qualification-test"

    def __init__(self, *, secret: str = "test-only-hmac-authority") -> None:
        self.secret = secret
        self.calls: list[tuple[str, str]] = []

    def token(self, *, kind: str, raw_id: str) -> str:
        self.calls.append((kind, raw_id))
        return hashlib.sha256(f"{self.secret}\0{kind}\0{raw_id}".encode()).hexdigest()


class SummaryCheckpoint:
    delegate_identity = {
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1024,
        "capability": "semantic",
        "encoder_revision": "gemini-retrieval-task-v1",
        "representation": "raw_control",
    }
    checkpoint_sha256 = "b" * 64
    attestation_key_id_sha256 = "c" * 64

    @property
    def entry_counts(self) -> dict[str, int]:
        return {
            qualification.DOCUMENT_TASK: qualification.EXPECTED_UNIQUE_DOCUMENTS,
            qualification.QUERY_TASK: qualification.EXPECTED_SCENARIO_COUNT,
        }

    @property
    def delegate_call_counts(self) -> dict[str, int]:
        return {
            qualification.DOCUMENT_TASK: qualification.EXPECTED_UNIQUE_DOCUMENTS,
            qualification.QUERY_TASK: qualification.EXPECTED_SCENARIO_COUNT,
        }

    @property
    def cache_hit_counts(self) -> dict[str, int]:
        return {qualification.DOCUMENT_TASK: 0, qualification.QUERY_TASK: 0}


def _selected_scenarios(count: int = 600) -> list[dict[str, Any]]:
    return [
        {
            "scenario_id": f"v5s-{index:024x}",
            "oracle": {"positive_lesson_id": TARGET_ID},
        }
        for index in range(count)
    ]


def _qualified_results(count: int = 600) -> list[dict[str, Any]]:
    return [
        {
            "scenario_id": f"v5s-{index:024x}",
            "status": "qualified",
            "candidate_count": 4,
            "policy": "semantic_strict",
            "fallback_reason": None,
            "retrieval_id": f"00000000-0000-0000-0000-{index + 1:012x}",
            "direct_candidate_ids": [TARGET_ID],
            "indexed_candidate_ids": [TARGET_ID],
            "target_rank": 1,
            "indexed_target_rank": 1,
            "target_distance": 0.1,
            "target_margin": 0.05,
            "membership_parity": True,
            "order_parity": True,
            "index_parity": True,
            "max_distance_delta": 0.0,
            "alternate_tenant_visible": False,
            "alternate_retrieval_visible": False,
            "alternate_current_semantic_visible": False,
            "alternate_audit_visible": False,
            "alternate_learning_reads_visible": False,
            "learning_decision_sealed": True,
            "alternate_decision_sealed": True,
        }
        for index in range(count)
    ]


def _structural_receipt() -> dict[str, Any]:
    body = {
        "status": "qualified",
        "code_sha": CODE_SHA,
        "protocol_sha256": qualification.development_protocol()["protocol_sha256"],
        "scenario_count": 6_000,
        "corpus_sha256": "f" * 64,
    }
    return {**body, "receipt_sha256": sha256_hex(body)}


def _database_evidence() -> dict[str, str]:
    return {
        "database_name": "hindsight_v5_development_unit",
        "engine": "cockroachdb",
        "engine_version": "CockroachDB CCL v25.2.3",
        "build_version": "v25.2.3",
        "build_description": "CockroachDB CCL v25.2.3 test build",
        "cluster_id": DATABASE_CLUSTER_ID,
    }


def _database_identities() -> dict[str, str]:
    return {"deploy_identity": "root", "runtime_identity": "runtime"}


def _scenario() -> dict[str, Any]:
    memories = [
        {
            "memory_id": TARGET_ID,
            "content": "alpha guidance",
            "kind": "procedural_lesson",
            "status": "review_required",
            "operator_disposition": "unreviewed",
            "usage_instruction": "unassigned",
            "applicability": {"conditions": [], "status": "unassessed"},
        },
        *[
            {
                "memory_id": f"v5m-{index:024x}",
                "content": f"decoy guidance {index}",
                "kind": "procedural_lesson",
                "status": "review_required",
                "operator_disposition": "unreviewed",
                "usage_instruction": "unassigned",
                "applicability": {"conditions": [], "status": "unassessed"},
            }
            for index in range(2, 5)
        ],
    ]
    return {
        "scenario_id": SCENARIO_ID,
        "agent_view": {
            "recurrence": {
                "incident": "A visible service symptom needs diagnosis.",
                "initial_observation": {"z_metric": 2, "a_metric": 1},
            },
            "memories": memories,
        },
        "oracle": {
            "positive_lesson_id": TARGET_ID,
            "hidden_causal_mechanism": "must never enter a query or database payload",
        },
    }


def _active_profile() -> dict[str, Any]:
    return {
        "id": EMBEDDING_PROFILE_ID,
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1024,
        "capability": "semantic",
        "encoder_revision": "gemini-retrieval-task-v1",
        "configuration": {},
        "max_distance": 0.35,
    }


def test_qualification_contract_binds_profile_query_selection_and_hash():
    contract = qualification.development_qualification_contract()

    assert contract == qualification.development_qualification_contract()
    assert contract["revision"] == "v5-development-live-qualification-v2"
    assert contract["sample"] == {
        "source_scenario_count": 6_000,
        "selected_scenario_count": 600,
        "cases_per_family": 100,
        "selection_sha256": "1c5638eac9fcfa62e57147759fd29a82d168ec9351e6d9a9ac4a97d347824008",
    }
    assert contract["query"] == {
        "renderer_revision": "v5-recurrence-visible-observations-v1",
        "inputs": ["recurrence.incident", "recurrence.initial_observation"],
        "observation_order": "ascending-key",
        "oracle_inputs": False,
        "task_type": qualification.QUERY_TASK,
    }
    assert contract["embedding"] == {
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1024,
        "capability": "semantic",
        "encoder_revision": "gemini-retrieval-task-v1",
        "provider_representation": "raw_control",
        "profile_id": EMBEDDING_PROFILE_ID,
        "max_distance": 0.35,
        "document_task_type": qualification.DOCUMENT_TASK,
        "query_task_type": qualification.QUERY_TASK,
    }
    assert contract["retrieval"] == {
        "policy": "semantic_strict",
        "limit": 4,
        "rank_requirement": 1,
        "fallback": False,
        "distance_parity_tolerance": 1e-6,
        "positive_margin_required": True,
    }
    assert contract["checkpoint"]["document_identity"] != contract["checkpoint"]["query_identity"]
    assert contract["checkpoint"]["attestation"] == {
        "algorithm": qualification.CHECKPOINT_ATTESTATION_ALGORITHM,
        "kind": qualification.CHECKPOINT_ATTESTATION_KIND,
        "scope": "canonical-entry-sha256",
        "key_identity": "sha256-of-kms-key-id",
    }
    assert contract["database"]["engine"] == "cockroachdb"
    assert contract["database"]["separate_deploy_and_runtime_identities"] is True
    assert contract["database"]["candidate_write_retry"] == {
        "outer_attempts": 3,
        "delays_seconds": [0.25, 0.5],
        "retryable_error": "psycopg.errors.SerializationFailure",
        "provider_reinvocation": False,
    }
    assert contract["qualification_contract_sha256"] == sha256_hex(
        {key: value for key, value in contract.items() if key != "qualification_contract_sha256"}
    )


def test_query_renderer_uses_only_sorted_visible_recurrence_inputs():
    scenario = _scenario()

    rendered = qualification.render_retrieval_query(scenario)

    assert rendered == (
        "Incident:\n"
        "A visible service symptom needs diagnosis.\n\n"
        "Visible observations:\n"
        "- a_metric: 1\n"
        "- z_metric: 2"
    )
    assert "hidden_causal_mechanism" not in rendered
    assert "must never enter" not in rendered
    altered = copy.deepcopy(scenario)
    altered["oracle"] = {"positive_lesson_id": "changed", "secret": "changed"}
    assert qualification.render_retrieval_query(altered) == rendered


def test_private_path_rejects_repository_symlinks_and_open_permissions(tmp_path: Path):
    with pytest.raises(ValueError, match="outside the repository"):
        qualification.require_private_path(qualification.REPO_ROOT / "build" / "checkpoint.json")

    link = tmp_path / "checkpoint-link.json"
    link.symlink_to(tmp_path / "checkpoint-target.json")
    with pytest.raises(ValueError, match="symbolic links"):
        qualification.require_private_path(link)

    open_file = tmp_path / "open.json"
    open_file.write_text("{}", encoding="utf-8")
    open_file.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        qualification.require_private_path(open_file)
    open_file.chmod(0o600)
    assert qualification.require_private_path(open_file) == open_file.resolve()


def test_run_rejects_receipt_anywhere_inside_checkpoint(tmp_path: Path):
    checkpoint_path = tmp_path / "checkpoint"

    with pytest.raises(ValueError, match="receipt cannot be inside the checkpoint"):
        qualification.run_development_qualification(
            code_sha=CODE_SHA,
            database_url=(
                "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            runtime_database_url=(
                "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            embedding_provider=FakeGeminiEmbeddingProvider(),
            checkpoint_attestor=FakeAttestor(),
            checkpoint_path=checkpoint_path,
            receipt_path=checkpoint_path / "nested" / "receipt.json",
            diagnostic_path=tmp_path / "diagnostic.json",
        )


class _Rows:
    def __init__(self, *, one: Any = None, many: list[Any] | None = None) -> None:
        self._one = one
        self._many = many or []

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._many


class _FreshConnection:
    def __init__(
        self,
        *,
        table_counts: dict[tuple[str | None, str], int],
        engine_row: tuple[str, str, str, str, str, str, str] = (
            "hindsight_v5_development_unit",
            "CockroachDB CCL v25.2.3",
            DATABASE_CLUSTER_ID,
            "CockroachDB",
            "v25.2.3",
            "CockroachDB CCL v25.2.3 test build",
            DATABASE_CLUSTER_ID,
        ),
    ) -> None:
        self.table_counts = table_counts
        self.engine_row = engine_row

    def __enter__(self) -> _FreshConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str) -> _Rows:
        normalized = " ".join(query.split())
        if normalized.startswith("WITH local_build AS"):
            return _Rows(one=self.engine_row)
        if normalized == "SELECT filename FROM schema_migrations":
            migrations = sorted(
                path.name for path in qualification.MIGRATIONS_DIR.glob("[0-9]*.sql")
            )
            return _Rows(many=[(name,) for name in migrations])
        if normalized.startswith("SELECT active_profile_id, building_profile_id, generation"):
            return _Rows(one=(None, None, 0))
        if normalized == "SELECT count(*) FROM embedding_profiles":
            return _Rows(one=(0,))
        if normalized.startswith("SELECT count(*) FROM "):
            table = normalized.rsplit(" ", 1)[-1]
            return _Rows(one=(self.table_counts.get((current_tenant_id(), table), 0),))
        raise AssertionError(normalized)


def test_disposable_database_fence_checks_exact_schema_freshness_and_all_tenants():
    observed_tenants: list[str | None] = []

    def connect_fn(_url: str, **_kwargs: Any) -> _FreshConnection:
        observed_tenants.append(current_tenant_id())
        return _FreshConnection(table_counts={})

    assert (
        qualification.require_fresh_development_database(
            "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable",
            connect_fn=connect_fn,
        )
        == _database_evidence()
    )
    assert observed_tenants == [None, *qualification._KNOWN_TENANT_IDS]


@pytest.mark.parametrize(
    ("engine_row", "message"),
    [
        (
            (
                "hindsight_v5_development_unit",
                "PostgreSQL 17.0",
                DATABASE_CLUSTER_ID,
                "PostgreSQL",
                "17.0",
                "PostgreSQL test build",
                DATABASE_CLUSTER_ID,
            ),
            "not CockroachDB",
        ),
        (
            (
                "hindsight_v5_development_unit",
                "CockroachDB CCL v25.2.3",
                "not-a-cluster-uuid",
                "CockroachDB",
                "v25.2.3",
                "CockroachDB CCL v25.2.3 test build",
                DATABASE_CLUSTER_ID,
            ),
            "cluster identity is invalid",
        ),
        (
            (
                "hindsight_v5_development_unit",
                "CockroachDB CCL v25.2.3",
                DATABASE_CLUSTER_ID,
                "CockroachDB",
                "v25.2.3",
                "CockroachDB CCL v25.2.3 test build",
                "00000000-0000-0000-0000-000000000004",
            ),
            "cluster identities differ",
        ),
    ],
)
def test_disposable_database_fence_requires_cockroach_engine_evidence(
    engine_row: tuple[str, str, str, str, str, str, str],
    message: str,
):
    with pytest.raises(RuntimeError, match=message):
        qualification.require_fresh_development_database(
            "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable",
            connect_fn=lambda *_args, **_kwargs: _FreshConnection(
                table_counts={},
                engine_row=engine_row,
            ),
        )


def test_disposable_database_fence_rejects_name_before_connecting():
    called = False

    def forbidden_connect(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="hindsight_v5_development"):
        qualification.require_fresh_development_database(
            "postgresql://root@localhost:26257/defaultdb?sslmode=disable",
            connect_fn=forbidden_connect,
        )
    assert called is False


def test_disposable_database_fence_detects_alternate_tenant_rows():
    counts = {(qualification.ACCEPTANCE_TENANT_ID, "semantic_memories"): 1}

    with pytest.raises(RuntimeError, match="semantic_memories"):
        qualification.require_fresh_development_database(
            "postgresql://root@localhost:26257/hindsight_v5_development_stale",
            connect_fn=lambda *_args, **_kwargs: _FreshConnection(
                table_counts=counts,
                engine_row=(
                    "hindsight_v5_development_stale",
                    "CockroachDB CCL v25.2.3",
                    DATABASE_CLUSTER_ID,
                    "CockroachDB",
                    "v25.2.3",
                    "CockroachDB CCL v25.2.3 test build",
                    DATABASE_CLUSTER_ID,
                ),
            ),
        )


class _RuntimeConnection:
    def __init__(
        self,
        *,
        identity: str,
        database_name: str = "hindsight_v5_development_unit",
        cluster_id: str = DATABASE_CLUSTER_ID,
        session_identity: str | None = None,
        superuser: bool = False,
        bypass_rls: bool = False,
        admin: bool = False,
        memory_worker: bool = True,
    ) -> None:
        self.identity = identity
        self.database_name = database_name
        self.cluster_id = cluster_id
        self.runtime_row = (
            database_name,
            cluster_id,
            identity,
            session_identity or identity,
            superuser,
            bypass_rls,
            admin,
            memory_worker,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str):
        normalized = " ".join(query.split())
        if normalized == (
            "SELECT current_database(), crdb_internal.cluster_id()::STRING, current_user"
        ):
            return _Rows(one=(self.database_name, self.cluster_id, self.identity))
        if normalized.startswith(
            "SELECT current_database(), crdb_internal.cluster_id()::STRING, current_user"
        ):
            return _Rows(one=self.runtime_row)
        raise AssertionError(normalized)


def _runtime_connect(
    _url: str,
    *,
    application_name: str,
    deploy_identity: str = "root",
    runtime_identity: str = "runtime",
    deploy_database: str = "hindsight_v5_development_unit",
    runtime_database: str = "hindsight_v5_development_unit",
    deploy_cluster_id: str = DATABASE_CLUSTER_ID,
    runtime_cluster_id: str = DATABASE_CLUSTER_ID,
    session_identity: str | None = None,
    superuser: bool = False,
    bypass_rls: bool = False,
    admin: bool = False,
    memory_worker: bool = True,
) -> _RuntimeConnection:
    if application_name.endswith("deploy-identity"):
        return _RuntimeConnection(
            identity=deploy_identity,
            database_name=deploy_database,
            cluster_id=deploy_cluster_id,
        )
    return _RuntimeConnection(
        identity=runtime_identity,
        database_name=runtime_database,
        cluster_id=runtime_cluster_id,
        session_identity=session_identity,
        superuser=superuser,
        bypass_rls=bypass_rls,
        admin=admin,
        memory_worker=memory_worker,
    )


def test_runtime_database_requires_same_name_and_non_bypass_identity():
    deploy = "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
    runtime = "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
    assert (
        qualification.require_restricted_runtime_database(
            deploy,
            runtime,
            connect_fn=_runtime_connect,
        )
        == _database_identities()
    )
    with pytest.raises(ValueError, match="same hindsight_v5_development"):
        qualification.require_restricted_runtime_database(
            deploy,
            runtime.replace("development_unit", "development_other"),
            connect_fn=_runtime_connect,
        )
    with pytest.raises(RuntimeError, match="bypass tenant isolation"):
        qualification.require_restricted_runtime_database(
            deploy,
            runtime,
            connect_fn=lambda url, **kwargs: _runtime_connect(
                url,
                **kwargs,
                bypass_rls=True,
            ),
        )
    with pytest.raises(RuntimeError, match="hindsight_memory_worker"):
        qualification.require_restricted_runtime_database(
            deploy,
            runtime,
            connect_fn=lambda url, **kwargs: _runtime_connect(
                url,
                **kwargs,
                memory_worker=False,
            ),
        )


def test_runtime_database_requires_distinct_deploy_and_runtime_identities():
    deploy = "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
    runtime = "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"

    with pytest.raises(RuntimeError, match="identities must differ"):
        qualification.require_restricted_runtime_database(
            deploy,
            runtime,
            connect_fn=lambda url, **kwargs: _runtime_connect(
                url,
                **kwargs,
                deploy_identity="shared-role",
                runtime_identity="shared-role",
            ),
        )


@pytest.mark.parametrize(
    ("runtime_overrides", "message"),
    [
        (
            {"runtime_cluster_id": "00000000-0000-0000-0000-000000000004"},
            "database identities differ",
        ),
        ({"session_identity": "proxied-runtime"}, "session identity is indirect"),
        ({"admin": True}, "bypass tenant isolation"),
    ],
)
def test_runtime_database_rejects_cluster_session_or_admin_mismatch(
    runtime_overrides: dict[str, Any],
    message: str,
):
    deploy = "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
    runtime = "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"

    with pytest.raises(RuntimeError, match=message):
        qualification.require_restricted_runtime_database(
            deploy,
            runtime,
            connect_fn=lambda url, **kwargs: _runtime_connect(
                url,
                **kwargs,
                **runtime_overrides,
            ),
        )


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("provider_name", "deterministic"),
        ("model_name", "gemini-embedding-old"),
        ("dimensions", 768),
        ("capability", "lexical_hash"),
        ("encoder_revision", "different-revision"),
        ("representation", "generic_title"),
    ],
)
def test_checkpoint_provider_rejects_every_frozen_provider_identity_mismatch(
    tmp_path: Path,
    attribute: str,
    value: object,
):
    provider = FakeGeminiEmbeddingProvider()
    setattr(provider, attribute, value)

    with pytest.raises(ValueError, match="exact frozen Gemini"):
        qualification.CheckpointedEmbeddingProvider(
            provider,
            tmp_path / "checkpoint",
            code_sha=CODE_SHA,
            attestor=FakeAttestor(),
        )


def test_embedding_cache_deduplicates_resumes_and_separates_tasks(tmp_path: Path):
    checkpoint_path = tmp_path / "checkpoint"
    provider = FakeGeminiEmbeddingProvider()
    cache = qualification.CheckpointedEmbeddingProvider(
        provider,
        checkpoint_path,
        code_sha=CODE_SHA,
        attestor=FakeAttestor(),
    )

    document = cache.embed_document("shared text")
    assert cache.embed_document("shared text") == document
    with pytest.raises(RuntimeError, match="query scope"):
        cache.embed_query("shared text")
    with cache.query_scope(scenario_id=SCENARIO_ID, query="shared text"):
        query = cache.embed_query("shared text")
        assert cache.embed_query("shared text") == query
    other_scenario = f"v5s-{'2' * 24}"
    with cache.query_scope(scenario_id=other_scenario, query="shared text"):
        cache.embed_query("shared text")
    with cache.query_scope(scenario_id=SCENARIO_ID, query="expected"):
        with pytest.raises(ValueError, match="differs from its bound"):
            cache.embed_query("different")

    assert cache.entry_counts == {
        qualification.DOCUMENT_TASK: 1,
        qualification.QUERY_TASK: 2,
    }
    assert cache.delegate_call_counts == {
        qualification.DOCUMENT_TASK: 1,
        qualification.QUERY_TASK: 2,
    }
    assert cache.cache_hit_counts == {
        qualification.DOCUMENT_TASK: 1,
        qualification.QUERY_TASK: 1,
    }
    assert stat.S_IMODE(checkpoint_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((checkpoint_path / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((checkpoint_path / "entries").stat().st_mode) == 0o700
    entry_paths = list((checkpoint_path / "entries").glob("*.json"))
    assert len(entry_paths) == 3
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in entry_paths)
    first_sha256 = cache.checkpoint_sha256

    resumed_delegate = FakeGeminiEmbeddingProvider()
    resumed = qualification.CheckpointedEmbeddingProvider(
        resumed_delegate,
        checkpoint_path,
        code_sha=CODE_SHA,
        attestor=FakeAttestor(),
    )
    assert resumed.embed_document("shared text") == document
    with resumed.query_scope(scenario_id=SCENARIO_ID, query="shared text"):
        assert resumed.embed_query("shared text") == query
    assert resumed.delegate_call_counts == {
        qualification.DOCUMENT_TASK: 0,
        qualification.QUERY_TASK: 0,
    }
    assert resumed.cache_hit_counts == {
        qualification.DOCUMENT_TASK: 1,
        qualification.QUERY_TASK: 1,
    }
    assert resumed.checkpoint_sha256 == first_sha256
    assert (
        json.loads((checkpoint_path / "manifest.json").read_text(encoding="utf-8"))["code_sha"]
        == CODE_SHA
    )
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["code_sha"] == CODE_SHA for path in entry_paths
    )
    with pytest.raises(ValueError, match="manifest identity differs"):
        qualification.CheckpointedEmbeddingProvider(
            FakeGeminiEmbeddingProvider(),
            checkpoint_path,
            code_sha="b" * 40,
            attestor=FakeAttestor(),
        )


def test_embedding_cache_fails_closed_on_integrity_or_contract_tampering(tmp_path: Path):
    checkpoint_path = tmp_path / "checkpoint"
    cache = qualification.CheckpointedEmbeddingProvider(
        FakeGeminiEmbeddingProvider(),
        checkpoint_path,
        code_sha=CODE_SHA,
        attestor=FakeAttestor(),
    )
    cache.embed_document("one document")
    entry_path = next((checkpoint_path / "entries").glob("*.json"))
    payload = json.loads(entry_path.read_text(encoding="utf-8"))
    entry = payload
    entry["vector"][0] = 0.5
    entry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="vector digest differs"):
        qualification.CheckpointedEmbeddingProvider(
            FakeGeminiEmbeddingProvider(),
            checkpoint_path,
            code_sha=CODE_SHA,
            attestor=FakeAttestor(),
        )

    payload = json.loads(entry_path.read_text(encoding="utf-8"))
    entry = payload
    entry["vector_sha256"] = sha256_hex(entry["vector"])
    entry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="attestation differs"):
        qualification.CheckpointedEmbeddingProvider(
            FakeGeminiEmbeddingProvider(),
            checkpoint_path,
            code_sha=CODE_SHA,
            attestor=FakeAttestor(),
        )

    with pytest.raises(ValueError, match="manifest identity differs"):
        qualification.CheckpointedEmbeddingProvider(
            FakeGeminiEmbeddingProvider(),
            checkpoint_path,
            code_sha=CODE_SHA,
            attestor=FakeAttestor(),
            qualification_contract_sha256="c" * 64,
        )


def test_embedding_cache_resume_removes_only_recognized_private_atomic_orphans(
    tmp_path: Path,
):
    checkpoint_path = tmp_path / "checkpoint"
    cache = qualification.CheckpointedEmbeddingProvider(
        FakeGeminiEmbeddingProvider(),
        checkpoint_path,
        code_sha=CODE_SHA,
        attestor=FakeAttestor(),
    )
    cache.embed_document("one document")
    root_orphan = checkpoint_path / ".manifest.json.killed_write_1"
    entry_orphan = checkpoint_path / "entries" / f".{'f' * 64}.json.killed_write_2"
    for path in (root_orphan, entry_orphan):
        path.write_text('{"partial":', encoding="utf-8")
        path.chmod(0o600)

    resumed = qualification.CheckpointedEmbeddingProvider(
        FakeGeminiEmbeddingProvider(),
        checkpoint_path,
        code_sha=CODE_SHA,
        attestor=FakeAttestor(),
    )

    assert resumed.entry_counts == {
        qualification.DOCUMENT_TASK: 1,
        qualification.QUERY_TASK: 0,
    }
    assert not root_orphan.exists()
    assert not entry_orphan.exists()

    junk = checkpoint_path / "arbitrary-junk.tmp"
    junk.write_text("unrecognized", encoding="utf-8")
    junk.chmod(0o600)
    with pytest.raises(ValueError, match="unexpected files"):
        qualification.CheckpointedEmbeddingProvider(
            FakeGeminiEmbeddingProvider(),
            checkpoint_path,
            code_sha=CODE_SHA,
            attestor=FakeAttestor(),
        )


def test_candidate_database_records_are_uniform_opaque_and_role_free():
    digest = "d" * 64
    contract_digest = "e" * 64

    payload = qualification.candidate_database_payload(
        scenario_id=SCENARIO_ID,
        candidate_id=TARGET_ID,
        content_sha256=digest,
        qualification_contract_sha256=contract_digest,
    )
    first_metadata = qualification.candidate_database_metadata()
    second_metadata = qualification.candidate_database_metadata()

    assert payload == {
        "schema_version": 1,
        "scenario_id": SCENARIO_ID,
        "candidate_id": TARGET_ID,
        "content_sha256": digest,
        "qualification_contract_sha256": contract_digest,
    }
    assert (
        first_metadata
        == second_metadata
        == {
            "kind": "procedural_lesson",
            "operator_disposition": "unreviewed",
            "usage_instruction": "unassigned",
            "applicability": {"conditions": [], "status": "unassessed"},
        }
    )
    first_metadata["applicability"]["status"] = "changed"
    assert second_metadata["applicability"]["status"] == "unassessed"
    serialized = json.dumps({"payload": payload, "metadata": second_metadata}, sort_keys=True)
    assert "oracle" not in serialized
    assert "target" not in serialized
    assert "positive_lesson" not in serialized


def test_candidate_write_retry_uses_exact_budget_delays_and_precomputed_vector():
    vector = _unit_vector(7)
    remember_kwargs = {
        "memory_kind": "semantic",
        "namespace": "v5-development-retry",
        "content": "retry candidate",
        "precomputed_embedding": vector,
    }
    calls: list[dict[str, Any]] = []
    delays: list[float] = []

    class Store:
        def remember(self, **kwargs: Any) -> dict[str, str]:
            calls.append(kwargs)
            if len(calls) < qualification.DATABASE_WRITE_ATTEMPTS:
                raise SerializationFailure("restart transaction")
            return {"id": "database-memory"}

    row = qualification._remember_candidate_with_retry(
        store=Store(),
        sleep_fn=delays.append,
        **remember_kwargs,
    )

    assert row == {"id": "database-memory"}
    assert len(calls) == qualification.DATABASE_WRITE_ATTEMPTS
    assert calls == [remember_kwargs] * qualification.DATABASE_WRITE_ATTEMPTS
    assert all(call["precomputed_embedding"] is vector for call in calls)
    assert delays == list(qualification.DATABASE_WRITE_RETRY_DELAYS_SECONDS)


def test_candidate_write_retry_reraises_persistent_serialization_failure():
    calls = 0
    delays: list[float] = []

    class Store:
        def remember(self, **_kwargs: Any) -> dict[str, str]:
            nonlocal calls
            calls += 1
            raise SerializationFailure("persistent restart transaction")

    with pytest.raises(SerializationFailure, match="persistent restart transaction"):
        qualification._remember_candidate_with_retry(
            store=Store(),
            sleep_fn=delays.append,
            memory_kind="semantic",
            content="persistent retry candidate",
            precomputed_embedding=_unit_vector(8),
        )

    assert calls == qualification.DATABASE_WRITE_ATTEMPTS
    assert delays == list(qualification.DATABASE_WRITE_RETRY_DELAYS_SECONDS)


def test_candidate_write_retry_does_not_retry_non_serialization_failure():
    calls = 0
    delays: list[float] = []

    class Store:
        def remember(self, **_kwargs: Any) -> dict[str, str]:
            nonlocal calls
            calls += 1
            raise RuntimeError("candidate is invalid")

    with pytest.raises(RuntimeError, match="candidate is invalid"):
        qualification._remember_candidate_with_retry(
            store=Store(),
            sleep_fn=delays.append,
            memory_kind="semantic",
            content="invalid candidate",
            precomputed_embedding=_unit_vector(9),
        )

    assert calls == 1
    assert delays == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"policy": "semantic_then_keyword"}, "fallback retrieval policy"),
        ({"fallback_reason": "semantic_vector_empty"}, "fallback retrieval policy"),
        ({"target_rank": 2}, "not uniquely rank one"),
        ({"indexed_target_rank": 2}, "indexed target is not rank one"),
        ({"target_distance": 0.3500001}, "exceeds the frozen cutoff"),
        ({"target_margin": 0.0}, "margin is not positive"),
        ({"target_margin": -0.01}, "margin is not positive"),
        ({"membership_parity": False}, "membership differs"),
        ({"order_parity": False}, "order differs"),
        ({"index_parity": False}, "direct and indexed results differ"),
        ({"max_distance_delta": 1.1e-6}, "distance parity exceeds tolerance"),
        ({"alternate_tenant_visible": True}, "visible to the alternate tenant"),
        ({"learning_decision_sealed": False}, "learning retrieval decision is not sealed"),
        ({"alternate_decision_sealed": False}, "alternate-tenant retrieval decision is not sealed"),
    ],
)
def test_summary_fails_closed_on_each_strict_retrieval_condition(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    message: str,
):
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: _selected_scenarios(),
    )
    results = _qualified_results()
    results[0].update(changes)

    with pytest.raises(ValueError, match=message):
        qualification.summarize_qualification_results(
            code_sha=CODE_SHA,
            database_name="hindsight_v5_development_unit",
            results=results,
            checkpoint=SummaryCheckpoint(),
            structural_receipt=_structural_receipt(),
        )


@pytest.mark.parametrize(
    "missing_field",
    [
        "retrieval_id",
        "direct_candidate_ids",
        "indexed_candidate_ids",
        "membership_parity",
        "order_parity",
        "alternate_retrieval_visible",
        "alternate_current_semantic_visible",
        "alternate_audit_visible",
        "alternate_learning_reads_visible",
    ],
)
def test_summary_rejects_missing_retrieval_parity_or_isolation_evidence(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
):
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: _selected_scenarios(),
    )
    results = _qualified_results()
    del results[0][missing_field]

    with pytest.raises(ValueError):
        qualification.summarize_qualification_results(
            code_sha=CODE_SHA,
            database_name="hindsight_v5_development_unit",
            results=results,
            checkpoint=SummaryCheckpoint(),
            structural_receipt=_structural_receipt(),
        )


def test_summary_rejects_consistent_decoy_first_arrays_with_claimed_target_rank_one(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: _selected_scenarios(),
    )
    results = _qualified_results()
    results[0]["direct_candidate_ids"] = [DECOY_ID, TARGET_ID]
    results[0]["indexed_candidate_ids"] = [DECOY_ID, TARGET_ID]

    with pytest.raises(ValueError, match="rank-one target identity differs"):
        qualification.summarize_qualification_results(
            code_sha=CODE_SHA,
            database_name="hindsight_v5_development_unit",
            results=results,
            checkpoint=SummaryCheckpoint(),
            structural_receipt=_structural_receipt(),
        )


def test_summary_rejects_incomplete_600_and_hashes_a_complete_receipt(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: _selected_scenarios(),
    )
    results = _qualified_results()

    with pytest.raises(ValueError, match="incomplete or out of protocol order"):
        qualification.summarize_qualification_results(
            code_sha=CODE_SHA,
            database_name="hindsight_v5_development_unit",
            results=results[:-1],
            checkpoint=SummaryCheckpoint(),
            structural_receipt=_structural_receipt(),
        )

    tampered_structure = _structural_receipt()
    tampered_structure["corpus_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="structural receipt digest differs"):
        qualification.summarize_qualification_results(
            code_sha=CODE_SHA,
            database_name="hindsight_v5_development_unit",
            results=results,
            checkpoint=SummaryCheckpoint(),
            structural_receipt=tampered_structure,
        )

    receipt = qualification.summarize_qualification_results(
        code_sha=CODE_SHA,
        database_name="hindsight_v5_development_unit",
        results=results,
        checkpoint=SummaryCheckpoint(),
        structural_receipt=_structural_receipt(),
    )
    assert receipt["status"] == "qualified"
    assert receipt["scenario_count"] == 600
    assert receipt["alternate_tenant_invisible"] is True
    assert receipt["structural_receipt_sha256"] == _structural_receipt()["receipt_sha256"]
    assert receipt["structural_corpus_sha256"] == "f" * 64
    assert receipt["receipt_sha256"] == sha256_hex(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


class _SharedStoreState:
    def __init__(
        self,
        *,
        corrupt_reads: bool = False,
        duplicate_read_ranks: bool = False,
        reverse_reads: bool = False,
        seal_status: str = "sealed",
        alternate_retrieval_visible: bool = False,
        alternate_current_visible: bool = False,
        alternate_audit_visible: bool = False,
        alternate_learning_reads_visible: bool = False,
        database_stage_cache_miss: bool = False,
    ) -> None:
        self.writes: list[dict[str, Any]] = []
        self.database_ids: dict[str, str] = {}
        self.retrieval_tenants: list[str | None] = []
        self.retrievals: dict[str, dict[str, Any]] = {}
        self.sealed_decision_ids: list[str] = []
        self.corrupt_reads = corrupt_reads
        self.duplicate_read_ranks = duplicate_read_ranks
        self.reverse_reads = reverse_reads
        self.seal_status = seal_status
        self.alternate_retrieval_visible = alternate_retrieval_visible
        self.alternate_current_visible = alternate_current_visible
        self.alternate_audit_visible = alternate_audit_visible
        self.alternate_learning_reads_visible = alternate_learning_reads_visible
        self.database_stage_cache_miss = database_stage_cache_miss
        self.database_stage_cache_miss_triggered = False


class _TraceStore:
    def __init__(self, reads: list[dict[str, Any]]) -> None:
        self.reads = reads

    def reads_for_decision(self, *, decision_id: str) -> list[dict[str, Any]]:
        assert decision_id == "decision"
        return copy.deepcopy(self.reads)


def test_retrieval_trace_orders_reads_by_rank_and_rejects_duplicate_ranks():
    retrieval = {
        "retrieval_id": LEARNING_RETRIEVAL_ID,
        "hits": [
            {"id": "database-memory-1", "distance": 0.1},
            {"id": "database-memory-2", "distance": 0.2},
        ],
    }
    shuffled = [
        {
            "retrieval_id": LEARNING_RETRIEVAL_ID,
            "rank": 2,
            "memory_id": "database-memory-2",
            "distance": 0.2,
        },
        {
            "retrieval_id": LEARNING_RETRIEVAL_ID,
            "rank": 1,
            "memory_id": "database-memory-1",
            "distance": 0.1,
        },
    ]

    qualification._verify_retrieval_trace(
        store=_TraceStore(shuffled),
        retrieval=retrieval,
        decision_id="decision",
    )

    duplicate_ranks = copy.deepcopy(shuffled)
    duplicate_ranks[0]["rank"] = 1
    with pytest.raises(RuntimeError, match="rank"):
        qualification._verify_retrieval_trace(
            store=_TraceStore(duplicate_ranks),
            retrieval=retrieval,
            decision_id="decision",
        )


class _FakeStore:
    def __init__(
        self,
        state: _SharedStoreState,
        *,
        embedding_provider: Any | None = None,
    ) -> None:
        self.state = state
        self.embedding_provider = embedding_provider

    def __enter__(self) -> _FakeStore:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def remember(self, **kwargs: Any) -> dict[str, str]:
        if (
            self.state.database_stage_cache_miss
            and not self.state.database_stage_cache_miss_triggered
        ):
            self.state.database_stage_cache_miss_triggered = True
            assert self.embedding_provider is not None
            self.embedding_provider.embed_document("unexpected database-stage text")
        captured = copy.deepcopy(kwargs)
        self.state.writes.append(captured)
        candidate_id = str(captured["structured_payload"]["candidate_id"])
        database_id = f"database-{candidate_id}"
        self.state.database_ids[candidate_id] = database_id
        return {"id": database_id}

    def retrieve_semantic(self, **kwargs: Any) -> dict[str, Any]:
        tenant_id = current_tenant_id()
        self.state.retrieval_tenants.append(tenant_id)
        if tenant_id == qualification.ACCEPTANCE_TENANT_ID:
            hits = (
                [{"id": self.state.database_ids[TARGET_ID], "distance": 0.0}]
                if self.state.alternate_retrieval_visible
                else []
            )
            result = {
                "retrieval_id": ALTERNATE_RETRIEVAL_ID,
                "status": "succeeded" if hits else "empty",
                "policy": kwargs["policy"],
                "fallback_reason": None,
                "hits": hits,
            }
        else:
            result = {
                "retrieval_id": LEARNING_RETRIEVAL_ID,
                "status": "succeeded",
                "policy": kwargs["policy"],
                "fallback_reason": None,
                "hits": [
                    {
                        "id": self.state.database_ids[TARGET_ID],
                        "distance": 0.0,
                    }
                ],
            }
        self.state.retrievals[str(kwargs["decision_id"])] = result
        return result

    def reads_for_decision(self, *, decision_id: str) -> list[dict[str, Any]]:
        if (
            current_tenant_id() == qualification.ACCEPTANCE_TENANT_ID
            and decision_id.startswith("v5-development-retrieval:")
            and not self.state.alternate_learning_reads_visible
        ):
            return []
        retrieval = self.state.retrievals[decision_id]
        if self.state.corrupt_reads and retrieval["hits"]:
            return []
        reads = [
            {
                "retrieval_id": retrieval["retrieval_id"],
                "rank": rank,
                "memory_id": hit["id"],
                "distance": hit["distance"],
            }
            for rank, hit in enumerate(retrieval["hits"], start=1)
        ]
        if self.state.duplicate_read_ranks and len(reads) > 1:
            reads[-1]["rank"] = reads[0]["rank"]
        if self.state.reverse_reads:
            reads.reverse()
        return reads

    def current_semantic(self, **_kwargs: Any) -> list[dict[str, str]]:
        if (
            current_tenant_id() == qualification.ACCEPTANCE_TENANT_ID
            and self.state.alternate_current_visible
        ):
            return [{"id": self.state.database_ids[TARGET_ID]}]
        return []

    def audit_memory(self, **_kwargs: Any) -> dict[str, str] | None:
        if (
            current_tenant_id() == qualification.ACCEPTANCE_TENANT_ID
            and self.state.alternate_audit_visible
        ):
            return {"id": self.state.database_ids[TARGET_ID]}
        return None

    def seal_decision(self, *, decision_id: str, failed: bool = False) -> dict[str, str]:
        assert failed is False
        self.state.sealed_decision_ids.append(decision_id)
        return {"status": self.state.seal_status}


def test_full_fake_run_writes_role_free_payloads_checks_other_tenant_and_atomic_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(qualification, "EXPECTED_SCENARIO_COUNT", 1)
    monkeypatch.setattr(qualification, "EXPECTED_UNIQUE_DOCUMENTS", 4)
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: [_scenario()],
    )
    state = _SharedStoreState()
    checkpoint_path = tmp_path / "checkpoint"
    receipt_path = tmp_path / "receipt.json"
    diagnostic_path = tmp_path / "diagnostic.json"
    receipt_path.write_text('{"stale": true}\n', encoding="utf-8")
    receipt_path.chmod(0o600)
    diagnostic_path.write_text('{"stale": true}\n', encoding="utf-8")
    diagnostic_path.chmod(0o600)
    monkeypatch.setattr(
        qualification,
        "qualify_development_structure",
        lambda *, code_sha: _structural_receipt(),
    )

    receipt = qualification.run_development_qualification(
        code_sha=CODE_SHA,
        database_url=(
            "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
        ),
        runtime_database_url=(
            "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
        ),
        embedding_provider=FakeGeminiEmbeddingProvider(),
        checkpoint_attestor=FakeAttestor(),
        checkpoint_path=checkpoint_path,
        receipt_path=receipt_path,
        diagnostic_path=diagnostic_path,
        database_validator_fn=lambda _url: _database_evidence(),
        runtime_database_validator_fn=lambda _deploy, _runtime: _database_identities(),
        profile_initializer_fn=lambda **_kwargs: _active_profile(),
        store_factory=lambda **kwargs: _FakeStore(
            state,
            embedding_provider=kwargs.get("embedding_provider"),
        ),
    )

    assert receipt["status"] == "qualified"
    assert receipt["scenario_count"] == 1
    assert receipt["deploy_database_identity_sha256"] == sha256_hex(b"root")
    assert receipt["runtime_database_identity_sha256"] == sha256_hex(b"runtime")
    assert receipt["database_engine"] == "cockroachdb"
    assert receipt["database_build_version_sha256"] == sha256_hex(b"v25.2.3")
    assert receipt["database_build_description_sha256"] == sha256_hex(
        b"CockroachDB CCL v25.2.3 test build"
    )
    assert receipt["checkpoint_entry_counts"] == {
        qualification.DOCUMENT_TASK: 4,
        qualification.QUERY_TASK: 1,
    }
    assert receipt["receipt_sha256"] == sha256_hex(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["status"] == "diagnostic_only"
    assert diagnostic["qualification_claim"] is False
    assert diagnostic["scenario_count"] == 1
    assert diagnostic["database_build_version_sha256"] == sha256_hex(b"v25.2.3")
    assert diagnostic["database_build_description_sha256"] == sha256_hex(
        b"CockroachDB CCL v25.2.3 test build"
    )
    assert diagnostic["diagnostic_sha256"] == sha256_hex(
        {key: value for key, value in diagnostic.items() if key != "diagnostic_sha256"}
    )
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(f".{receipt_path.name}.*"))
    assert state.retrieval_tenants == [
        qualification.learning_tenant_id(),
        qualification.ACCEPTANCE_TENANT_ID,
    ]
    assert state.sealed_decision_ids == [
        f"v5-development-retrieval:{SCENARIO_ID}",
        f"v5-development-isolation:{SCENARIO_ID}",
    ]
    assert len(state.writes) == 4
    assert all(write["metadata"] == state.writes[0]["metadata"] for write in state.writes)
    assert all(
        set(write["structured_payload"])
        == {
            "schema_version",
            "scenario_id",
            "candidate_id",
            "content_sha256",
            "qualification_contract_sha256",
        }
        for write in state.writes
    )
    persisted = json.dumps(
        [
            {
                "metadata": write["metadata"],
                "content_schema": write["content_schema"],
                "structured_payload": write["structured_payload"],
            }
            for write in state.writes
        ],
        sort_keys=True,
    )
    assert "oracle" not in persisted
    assert "target" not in persisted
    assert "positive_lesson" not in persisted


def test_runtime_identity_preflight_happens_before_any_provider_call(tmp_path: Path):
    provider = FakeGeminiEmbeddingProvider()

    with pytest.raises(RuntimeError, match="restricted runtime unavailable"):
        qualification.run_development_qualification(
            code_sha=CODE_SHA,
            database_url=(
                "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            runtime_database_url=(
                "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            embedding_provider=provider,
            checkpoint_attestor=FakeAttestor(),
            checkpoint_path=tmp_path / "checkpoint",
            receipt_path=tmp_path / "receipt.json",
            diagnostic_path=tmp_path / "diagnostic.json",
            database_validator_fn=lambda _url: _database_evidence(),
            runtime_database_validator_fn=lambda _deploy, _runtime: (_ for _ in ()).throw(
                RuntimeError("restricted runtime unavailable")
            ),
        )

    assert provider.document_calls == []
    assert provider.query_calls == []


def test_full_fake_run_fails_if_database_stage_causes_embedding_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(qualification, "EXPECTED_SCENARIO_COUNT", 1)
    monkeypatch.setattr(qualification, "EXPECTED_UNIQUE_DOCUMENTS", 4)
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: [_scenario()],
    )
    monkeypatch.setattr(
        qualification,
        "qualify_development_structure",
        lambda *, code_sha: _structural_receipt(),
    )
    state = _SharedStoreState(database_stage_cache_miss=True)

    with pytest.raises(RuntimeError, match="database stage unexpectedly invoked"):
        qualification.run_development_qualification(
            code_sha=CODE_SHA,
            database_url=(
                "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            runtime_database_url=(
                "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            embedding_provider=FakeGeminiEmbeddingProvider(),
            checkpoint_attestor=FakeAttestor(),
            checkpoint_path=tmp_path / "checkpoint",
            receipt_path=tmp_path / "receipt.json",
            diagnostic_path=tmp_path / "diagnostic.json",
            database_validator_fn=lambda _url: _database_evidence(),
            runtime_database_validator_fn=lambda _deploy, _runtime: _database_identities(),
            profile_initializer_fn=lambda **_kwargs: _active_profile(),
            store_factory=lambda **kwargs: _FakeStore(
                state,
                embedding_provider=kwargs.get("embedding_provider"),
            ),
        )


def test_full_fake_run_records_serialization_exhaustion_without_provider_reinvocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(qualification, "EXPECTED_SCENARIO_COUNT", 1)
    monkeypatch.setattr(qualification, "EXPECTED_UNIQUE_DOCUMENTS", 4)
    scenario = _scenario()
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: [scenario],
    )
    monkeypatch.setattr(
        qualification,
        "qualify_development_structure",
        lambda *, code_sha: _structural_receipt(),
    )
    retry_delays: list[float] = []
    retry_candidate = qualification._remember_candidate_with_retry
    monkeypatch.setattr(
        qualification,
        "_remember_candidate_with_retry",
        lambda **kwargs: retry_candidate(sleep_fn=retry_delays.append, **kwargs),
    )
    state = _SharedStoreState()
    write_attempts = 0
    failure_message = "persistent candidate serialization failure"

    class SerializationFailureStore(_FakeStore):
        def remember(self, **_kwargs: Any) -> dict[str, str]:
            nonlocal write_attempts
            write_attempts += 1
            raise SerializationFailure(failure_message)

    provider = FakeGeminiEmbeddingProvider()
    receipt_path = tmp_path / "receipt.json"
    diagnostic_path = tmp_path / "diagnostic.json"
    receipt_path.write_text('{"stale": true}\n', encoding="utf-8")
    receipt_path.chmod(0o600)
    diagnostic_path.write_text('{"stale": true}\n', encoding="utf-8")
    diagnostic_path.chmod(0o600)

    with pytest.raises(SerializationFailure, match=failure_message):
        qualification.run_development_qualification(
            code_sha=CODE_SHA,
            database_url=(
                "postgresql://root@localhost:26257/"
                "hindsight_v5_development_unit?sslmode=disable"
            ),
            runtime_database_url=(
                "postgresql://runtime@localhost:26257/"
                "hindsight_v5_development_unit?sslmode=disable"
            ),
            embedding_provider=provider,
            checkpoint_attestor=FakeAttestor(),
            checkpoint_path=tmp_path / "checkpoint",
            receipt_path=receipt_path,
            diagnostic_path=diagnostic_path,
            database_validator_fn=lambda _url: _database_evidence(),
            runtime_database_validator_fn=lambda _deploy, _runtime: _database_identities(),
            profile_initializer_fn=lambda **_kwargs: _active_profile(),
            store_factory=lambda **kwargs: SerializationFailureStore(
                state,
                embedding_provider=kwargs.get("embedding_provider"),
            ),
        )

    assert write_attempts == qualification.DATABASE_WRITE_ATTEMPTS
    assert retry_delays == list(qualification.DATABASE_WRITE_RETRY_DELAYS_SECONDS)
    assert provider.document_calls == [
        str(memory["content"]) for memory in scenario["agent_view"]["memories"]
    ]
    assert provider.query_calls == [qualification.render_retrieval_query(scenario)]
    assert not receipt_path.exists()
    assert diagnostic_path.exists()
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o600
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["status"] == "diagnostic_only"
    assert diagnostic["qualification_claim"] is False
    assert diagnostic["scenario_count"] == 0
    assert diagnostic["qualified_row_count"] == 0
    assert diagnostic["results"] == []
    assert diagnostic["failure"] == {
        "stage": "database_population_or_retrieval",
        "code": "SerializationFailure",
        "message_sha256": sha256_hex(failure_message.encode("utf-8")),
    }
    assert diagnostic["delegate_call_counts"] == {
        qualification.DOCUMENT_TASK: 4,
        qualification.QUERY_TASK: 1,
    }
    assert diagnostic["cache_hit_counts"] == {
        qualification.DOCUMENT_TASK: 1,
        qualification.QUERY_TASK: 0,
    }
    assert diagnostic["diagnostic_sha256"] == sha256_hex(
        {key: value for key, value in diagnostic.items() if key != "diagnostic_sha256"}
    )


def test_full_fake_run_fails_closed_on_alternate_tenant_retrieval_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(qualification, "EXPECTED_SCENARIO_COUNT", 1)
    monkeypatch.setattr(qualification, "EXPECTED_UNIQUE_DOCUMENTS", 4)
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: [_scenario()],
    )
    monkeypatch.setattr(
        qualification,
        "qualify_development_structure",
        lambda *, code_sha: _structural_receipt(),
    )
    state = _SharedStoreState(alternate_retrieval_visible=True)
    receipt_path = tmp_path / "receipt.json"
    diagnostic_path = tmp_path / "diagnostic.json"

    with pytest.raises(ValueError, match="non-qualified scenario|alternate tenant"):
        qualification.run_development_qualification(
            code_sha=CODE_SHA,
            database_url=(
                "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            runtime_database_url=(
                "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            embedding_provider=FakeGeminiEmbeddingProvider(),
            checkpoint_attestor=FakeAttestor(),
            checkpoint_path=tmp_path / "checkpoint",
            receipt_path=receipt_path,
            diagnostic_path=diagnostic_path,
            database_validator_fn=lambda _url: _database_evidence(),
            runtime_database_validator_fn=lambda _deploy, _runtime: _database_identities(),
            profile_initializer_fn=lambda **_kwargs: _active_profile(),
            store_factory=lambda **kwargs: _FakeStore(
                state,
                embedding_provider=kwargs.get("embedding_provider"),
            ),
        )

    assert not receipt_path.exists()
    assert diagnostic_path.exists()
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o600
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["status"] == "diagnostic_only"
    assert diagnostic["qualification_claim"] is False
    assert diagnostic["results"][0]["alternate_retrieval_visible"] is True
    assert diagnostic["diagnostic_sha256"] == sha256_hex(
        {key: value for key, value in diagnostic.items() if key != "diagnostic_sha256"}
    )


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (_SharedStoreState(corrupt_reads=True), "trace count differs"),
        (_SharedStoreState(seal_status="open"), "did not reach sealed status"),
    ],
)
def test_full_fake_run_fails_closed_on_missing_read_trace_or_unsealed_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: _SharedStoreState,
    message: str,
):
    monkeypatch.setattr(qualification, "EXPECTED_SCENARIO_COUNT", 1)
    monkeypatch.setattr(qualification, "EXPECTED_UNIQUE_DOCUMENTS", 4)
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: [_scenario()],
    )
    monkeypatch.setattr(
        qualification,
        "qualify_development_structure",
        lambda *, code_sha: _structural_receipt(),
    )

    with pytest.raises(RuntimeError, match=message):
        qualification.run_development_qualification(
            code_sha=CODE_SHA,
            database_url=(
                "postgresql://root@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            runtime_database_url=(
                "postgresql://runtime@localhost:26257/hindsight_v5_development_unit?sslmode=disable"
            ),
            embedding_provider=FakeGeminiEmbeddingProvider(),
            checkpoint_attestor=FakeAttestor(),
            checkpoint_path=tmp_path / "checkpoint",
            receipt_path=tmp_path / "receipt.json",
            diagnostic_path=tmp_path / "diagnostic.json",
            database_validator_fn=lambda _url: _database_evidence(),
            runtime_database_validator_fn=lambda _deploy, _runtime: _database_identities(),
            profile_initializer_fn=lambda **_kwargs: _active_profile(),
            store_factory=lambda **kwargs: _FakeStore(
                state,
                embedding_provider=kwargs.get("embedding_provider"),
            ),
        )
