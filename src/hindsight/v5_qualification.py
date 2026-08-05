"""Live Gemini and CockroachDB qualification for the v5 development corpus."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from psycopg.errors import SerializationFailure

from hindsight.db import connect
from hindsight.embedding_index import activate_profile, begin_profile_build
from hindsight.embeddings import EmbeddingProvider, embedding_profile
from hindsight.memory import MemoryStore, Provenance
from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, learning_tenant_id
from hindsight.tenant import tenant_scope
from hindsight.v5_corpus import (
    EMBEDDING_CAPABILITY,
    EMBEDDING_CASES_PER_FAMILY,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_ENCODER_REVISION,
    EMBEDDING_MAX_DISTANCE,
    EMBEDDING_MODEL,
    EMBEDDING_PROFILE_ID,
    EMBEDDING_PROVIDER,
    EXPECTED_DEVELOPMENT_EMBEDDING_SELECTION_SHA256,
    GEMINI_PROVIDER_REPRESENTATION,
    MECHANISM_FAMILIES,
    STRUCTURAL_CASES_PER_FAMILY,
    development_protocol,
    development_scenarios,
    qualify_development_structure,
    select_development_cases,
    sha256_hex,
)


QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_REVISION = "v5-development-live-qualification-v2"
QUERY_RENDERER_REVISION = "v5-recurrence-visible-observations-v1"
CACHE_SCHEMA_VERSION = 2
RECEIPT_SCHEMA_VERSION = 1
DIAGNOSTIC_SCHEMA_VERSION = 1
DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"
CHECKPOINT_ATTESTATION_ALGORITHM = "AWS_KMS_HMAC_SHA_256"
CHECKPOINT_ATTESTATION_KIND = "v5-embedding-entry"
EXPECTED_SCENARIO_COUNT = len(MECHANISM_FAMILIES) * EMBEDDING_CASES_PER_FAMILY
EXPECTED_UNIQUE_DOCUMENTS = 18
DATABASE_WRITE_ATTEMPTS = 3
DATABASE_WRITE_RETRY_DELAYS_SECONDS = (0.25, 0.5)
DEVELOPMENT_DATABASE_RE = re.compile(r"hindsight_v5_development_[a-z0-9_]+")
OPAQUE_SCENARIO_RE = re.compile(r"v5s-[0-9a-f]{24}")
OPAQUE_MEMORY_RE = re.compile(r"v5m-[0-9a-f]{24}")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
_KNOWN_TENANT_IDS = (
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002",
    ACCEPTANCE_TENANT_ID,
    learning_tenant_id(),
)
_FRESH_TABLES = (
    "semantic_memories",
    "semantic_beliefs",
    "semantic_memory_vectors",
    "semantic_memory_embeddings",
    "memory_decisions",
    "memory_reads",
    "memory_retrievals",
)
_NEUTRAL_METADATA = {
    "kind": "procedural_lesson",
    "operator_disposition": "unreviewed",
    "usage_instruction": "unassigned",
    "applicability": {"conditions": [], "status": "unassessed"},
}


class QualificationStore(Protocol):
    def __enter__(self) -> QualificationStore: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def remember(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def retrieve_semantic(self, **kwargs: Any) -> Any: ...

    def active_embedding_profile(self) -> Any: ...

    def reads_for_decision(self, *, decision_id: str) -> list[Mapping[str, Any]]: ...

    def seal_decision(self, *, decision_id: str, failed: bool = False) -> Mapping[str, Any]: ...

    def current_semantic(self, **kwargs: Any) -> list[Mapping[str, Any]]: ...

    def audit_memory(self, **kwargs: Any) -> Mapping[str, Any] | None: ...


class CheckpointAttestor(Protocol):
    """KMS-backed authority used to authenticate cached provider outputs."""

    key_id: str

    def token(self, *, kind: str, raw_id: str) -> str: ...


def development_qualification_contract() -> dict[str, Any]:
    """Return the complete content-addressed development qualification contract."""

    core = development_protocol()
    contract = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "revision": QUALIFICATION_REVISION,
        "core_protocol_sha256": core["protocol_sha256"],
        "sample": {
            "source_scenario_count": len(MECHANISM_FAMILIES) * STRUCTURAL_CASES_PER_FAMILY,
            "selected_scenario_count": EXPECTED_SCENARIO_COUNT,
            "cases_per_family": EMBEDDING_CASES_PER_FAMILY,
            "selection_sha256": EXPECTED_DEVELOPMENT_EMBEDDING_SELECTION_SHA256,
        },
        "query": {
            "renderer_revision": QUERY_RENDERER_REVISION,
            "inputs": ["recurrence.incident", "recurrence.initial_observation"],
            "observation_order": "ascending-key",
            "oracle_inputs": False,
            "task_type": QUERY_TASK,
        },
        "embedding": {
            "provider": EMBEDDING_PROVIDER,
            "model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMENSIONS,
            "capability": EMBEDDING_CAPABILITY,
            "encoder_revision": EMBEDDING_ENCODER_REVISION,
            "provider_representation": GEMINI_PROVIDER_REPRESENTATION,
            "profile_id": EMBEDDING_PROFILE_ID,
            "max_distance": EMBEDDING_MAX_DISTANCE,
            "document_task_type": DOCUMENT_TASK,
            "query_task_type": QUERY_TASK,
        },
        "database": {
            "engine": "cockroachdb",
            "engine_probe_revision": "v5-crdb-build-and-logical-cluster-v1",
            "database_name_pattern": DEVELOPMENT_DATABASE_RE.pattern,
            "fresh_schema_required": True,
            "separate_deploy_and_runtime_identities": True,
            "same_logical_cluster": True,
            "runtime_session_identity": "direct",
            "runtime_superuser": False,
            "runtime_bypass_rls": False,
            "runtime_admin_member": False,
            "runtime_permission_role": "hindsight_memory_worker",
            "learning_tenant_id": learning_tenant_id(),
            "alternate_tenant_id": ACCEPTANCE_TENANT_ID,
            "candidate_write_retry": {
                "outer_attempts": DATABASE_WRITE_ATTEMPTS,
                "delays_seconds": list(DATABASE_WRITE_RETRY_DELAYS_SECONDS),
                "retryable_error": "psycopg.errors.SerializationFailure",
                "provider_reinvocation": False,
            },
        },
        "retrieval": {
            "policy": "semantic_strict",
            "limit": 4,
            "rank_requirement": 1,
            "fallback": False,
            "distance_parity_tolerance": 1e-6,
            "positive_margin_required": True,
        },
        "checkpoint": {
            "schema_version": CACHE_SCHEMA_VERSION,
            "code_identity": "exact-lowercase-40-character-git-sha",
            "document_identity": "code-profile-task-content-sha256",
            "query_identity": "code-profile-task-scenario-input-sha256",
            "attestation": {
                "algorithm": CHECKPOINT_ATTESTATION_ALGORITHM,
                "kind": CHECKPOINT_ATTESTATION_KIND,
                "scope": "canonical-entry-sha256",
                "key_identity": "sha256-of-kms-key-id",
            },
            "expected_unique_documents": EXPECTED_UNIQUE_DOCUMENTS,
            "expected_queries": EXPECTED_SCENARIO_COUNT,
        },
    }
    return {**contract, "qualification_contract_sha256": sha256_hex(contract)}


def select_embedding_scenarios(*, code_sha: str) -> list[dict[str, Any]]:
    """Regenerate 6,000 scenarios and return the protocol-bound 600 in order."""

    items = development_scenarios(
        code_sha=code_sha,
        per_family=STRUCTURAL_CASES_PER_FAMILY,
    )
    selected_ids, _ = select_development_cases(items=items)
    if len(selected_ids) != EXPECTED_SCENARIO_COUNT:
        raise ValueError("v5 live qualification selection is incomplete")
    if sha256_hex(selected_ids) != EXPECTED_DEVELOPMENT_EMBEDDING_SELECTION_SHA256:
        raise ValueError("v5 live qualification selection differs from the core protocol")
    by_id = {str(item["scenario_id"]): item for item in items}
    if len(by_id) != len(items):
        raise ValueError("v5 development corpus contains duplicate scenario identities")
    try:
        selected = [by_id[scenario_id] for scenario_id in selected_ids]
    except KeyError as exc:
        raise ValueError("v5 selected scenario is absent from the regenerated corpus") from exc
    counts = {
        family: sum(item["mechanism_family"] == family for item in selected)
        for family in MECHANISM_FAMILIES
    }
    if any(count != EMBEDDING_CASES_PER_FAMILY for count in counts.values()):
        raise ValueError("v5 live qualification selection is not family-balanced")
    return selected


def render_retrieval_query(scenario: Mapping[str, Any]) -> str:
    """Render only the recurrence incident and sorted visible observations."""

    agent_view = scenario.get("agent_view")
    if not isinstance(agent_view, Mapping):
        raise ValueError("v5 query rendering requires an agent view")
    recurrence = agent_view.get("recurrence")
    if not isinstance(recurrence, Mapping):
        raise ValueError("v5 query rendering requires a recurrence")
    incident = recurrence.get("incident")
    observations = recurrence.get("initial_observation")
    if not isinstance(incident, str) or not incident.strip():
        raise ValueError("v5 recurrence incident is required")
    if not isinstance(observations, Mapping) or not observations:
        raise ValueError("v5 recurrence observations are required")
    rendered_observations = [
        f"- {key}: {json.dumps(observations[key], ensure_ascii=False, sort_keys=True)}"
        for key in sorted(observations)
    ]
    return "\n".join(
        [
            "Incident:",
            incident.strip(),
            "",
            "Visible observations:",
            *rendered_observations,
        ]
    )


def require_private_path(path: str | os.PathLike[str]) -> pathlib.Path:
    """Resolve a non-symlink path outside the repository with private file mode."""

    candidate = pathlib.Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("v5 qualification private paths cannot be symbolic links")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("v5 qualification private paths must be outside the repository")
    if resolved.exists():
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if not resolved.is_file() or mode & 0o077:
            raise ValueError("v5 qualification private files must use mode 0600 or stricter")
    return resolved


def _require_private_directory(path: str | os.PathLike[str]) -> pathlib.Path:
    candidate = pathlib.Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("v5 qualification checkpoint cannot be a symbolic link")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("v5 qualification checkpoint must be outside the repository")
    if resolved.exists():
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if not resolved.is_dir() or mode & 0o077:
            raise ValueError("v5 qualification checkpoint directory must use mode 0700")
    else:
        resolved.mkdir(mode=0o700, parents=True)
    return resolved


def _reject_checkpoint_descendant(
    path: pathlib.Path,
    checkpoint_directory: pathlib.Path,
    *,
    label: str,
) -> None:
    try:
        path.relative_to(checkpoint_directory)
    except ValueError:
        return
    raise ValueError(f"v5 qualification {label} cannot be inside the checkpoint directory")


def _remove_private_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class CheckpointedEmbeddingProvider:
    """Identity-bound Gemini embedding cache with atomic durable checkpoints."""

    def __init__(
        self,
        delegate: EmbeddingProvider,
        checkpoint_path: str | os.PathLike[str],
        *,
        code_sha: str,
        attestor: CheckpointAttestor,
        qualification_contract_sha256: str | None = None,
    ) -> None:
        contract_sha256 = (
            qualification_contract_sha256
            or development_qualification_contract()["qualification_contract_sha256"]
        )
        if not re.fullmatch(r"[0-9a-f]{64}", contract_sha256):
            raise ValueError("v5 qualification contract identity must be SHA-256")
        if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
            raise ValueError("v5 embedding checkpoint requires an exact lowercase code SHA")
        self._delegate = delegate
        self._directory = _require_private_directory(checkpoint_path)
        self._manifest_path = self._directory / "manifest.json"
        self._entries_directory = self._directory / "entries"
        self._contract_sha256 = contract_sha256
        self._code_sha = code_sha
        self._identity = _require_exact_provider(delegate)
        attestation_key_id = str(getattr(attestor, "key_id", "")).strip()
        if not attestation_key_id:
            raise ValueError("v5 embedding checkpoint requires a KMS HMAC key identity")
        self._attestor = attestor
        self._attestation_key_id_sha256 = hashlib.sha256(
            attestation_key_id.encode("utf-8")
        ).hexdigest()
        self.provider_name = delegate.provider_name
        self.model_name = delegate.model_name
        self.dimensions = delegate.dimensions
        self.capability = delegate.capability
        self.encoder_revision = delegate.encoder_revision
        self.representation = str(getattr(delegate, "representation", ""))
        self._query_context: ContextVar[tuple[str, str] | None] = ContextVar(
            f"v5_embedding_query_{id(self)}",
            default=None,
        )
        self._lock = threading.RLock()
        self._delegate_calls = {DOCUMENT_TASK: 0, QUERY_TASK: 0}
        self._cache_hits = {DOCUMENT_TASK: 0, QUERY_TASK: 0}
        self._entries: dict[str, dict[str, Any]] = {}
        self._initialize_checkpoint()

    @property
    def delegate_identity(self) -> dict[str, Any]:
        return dict(self._identity)

    @property
    def checkpoint_path(self) -> pathlib.Path:
        return self._directory

    @property
    def attestation_key_id_sha256(self) -> str:
        return self._attestation_key_id_sha256

    @property
    def checkpoint_sha256(self) -> str:
        with self._lock:
            persisted_manifest = _load_private_json(
                self._manifest_path,
                label="checkpoint manifest",
            )
            manifest = self._manifest_document()
            if persisted_manifest != manifest:
                raise ValueError("v5 embedding checkpoint manifest changed after loading")
            for key, entry in self._entries.items():
                persisted_entry = _load_private_json(
                    self._entries_directory / f"{key}.json",
                    label="checkpoint entry",
                )
                if persisted_entry != entry:
                    raise ValueError("v5 embedding checkpoint entry changed after loading")
            return sha256_hex(
                {
                    "manifest_sha256": sha256_hex(manifest),
                    "entries": {
                        key: sha256_hex(entry) for key, entry in sorted(self._entries.items())
                    },
                }
            )

    @property
    def delegate_call_counts(self) -> dict[str, int]:
        return dict(self._delegate_calls)

    @property
    def cache_hit_counts(self) -> dict[str, int]:
        return dict(self._cache_hits)

    @property
    def entry_counts(self) -> dict[str, int]:
        return {
            DOCUMENT_TASK: sum(row["task_type"] == DOCUMENT_TASK for row in self._entries.values()),
            QUERY_TASK: sum(row["task_type"] == QUERY_TASK for row in self._entries.values()),
        }

    @contextmanager
    def query_scope(self, *, scenario_id: str, query: str) -> Iterator[None]:
        """Bind one opaque scenario identity to a query embedding operation."""

        if not OPAQUE_SCENARIO_RE.fullmatch(scenario_id):
            raise ValueError("v5 query cache requires an opaque scenario identity")
        input_sha256 = hashlib.sha256(query.encode("utf-8")).hexdigest()
        token = self._query_context.set((scenario_id, input_sha256))
        try:
            yield
        finally:
            self._query_context.reset(token)

    def embed(self, text: str) -> list[float]:
        return self.embed_document(text)

    def embed_document(self, text: str) -> list[float]:
        return self._embed_cached(task_type=DOCUMENT_TASK, text=text, scenario_id=None)

    def embed_query(self, text: str) -> list[float]:
        context = self._query_context.get()
        if context is None:
            raise RuntimeError("v5 query embeddings require an explicit scenario query scope")
        scenario_id, expected_input_sha256 = context
        observed = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if observed != expected_input_sha256:
            raise ValueError("v5 query input differs from its bound query scope")
        return self._embed_cached(
            task_type=QUERY_TASK,
            text=text,
            scenario_id=scenario_id,
        )

    def _manifest_document(self) -> dict[str, Any]:
        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "code_sha": self._code_sha,
            "qualification_contract_sha256": self._contract_sha256,
            "provider_identity": dict(self._identity),
            "profile_id": EMBEDDING_PROFILE_ID,
            "attestation": {
                "algorithm": CHECKPOINT_ATTESTATION_ALGORITHM,
                "kind": CHECKPOINT_ATTESTATION_KIND,
                "key_id_sha256": self._attestation_key_id_sha256,
            },
        }

    def _initialize_checkpoint(self) -> None:
        _remove_orphan_atomic_files(
            self._directory,
            name_pattern=r"\.manifest\.json\.[a-z0-9_]+",
        )
        allowed = {"manifest.json", "entries"}
        unexpected = {path.name for path in self._directory.iterdir()} - allowed
        if unexpected:
            raise ValueError("v5 embedding checkpoint contains unexpected files")
        manifest = self._manifest_document()
        if self._manifest_path.exists():
            observed = _load_private_json(self._manifest_path, label="checkpoint manifest")
            if observed != manifest:
                raise ValueError("v5 embedding checkpoint manifest identity differs")
        else:
            if self._entries_directory.exists():
                raise ValueError("v5 embedding checkpoint entries have no manifest")
            _atomic_write_json(self._manifest_path, manifest)
        if self._entries_directory.is_symlink():
            raise ValueError("v5 embedding checkpoint entries cannot be a symbolic link")
        if self._entries_directory.exists():
            mode = stat.S_IMODE(self._entries_directory.stat().st_mode)
            if not self._entries_directory.is_dir() or mode & 0o077:
                raise ValueError("v5 embedding entry directory must use mode 0700")
        else:
            self._entries_directory.mkdir(mode=0o700)
        _remove_orphan_atomic_files(
            self._entries_directory,
            name_pattern=r"\.[0-9a-f]{64}\.json\.[a-z0-9_]+",
        )
        for path in sorted(self._entries_directory.iterdir()):
            if (
                path.is_symlink()
                or not path.is_file()
                or not re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
            ):
                raise ValueError("v5 embedding checkpoint entry filename is invalid")
            key = path.stem
            entry = _load_private_json(path, label="checkpoint entry")
            self._validate_entry(key=key, entry=entry)
            self._entries[key] = entry

    def _embed_cached(
        self,
        *,
        task_type: str,
        text: str,
        scenario_id: str | None,
    ) -> list[float]:
        if task_type not in {DOCUMENT_TASK, QUERY_TASK}:
            raise ValueError("unsupported v5 embedding task")
        if not isinstance(text, str) or not text:
            raise ValueError("v5 embeddings require nonempty text")
        if task_type == DOCUMENT_TASK and scenario_id is not None:
            raise ValueError("v5 document cache cannot carry a scenario identity")
        if task_type == QUERY_TASK and not scenario_id:
            raise ValueError("v5 query cache requires a scenario identity")
        _require_exact_provider(self._delegate)
        input_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        scenario_input_sha256 = (
            sha256_hex(
                {
                    "scenario_id": scenario_id,
                    "input_sha256": input_sha256,
                }
            )
            if scenario_id is not None
            else None
        )
        key = _cache_key(
            code_sha=self._code_sha,
            task_type=task_type,
            input_sha256=input_sha256,
            scenario_id=scenario_id,
            scenario_input_sha256=scenario_input_sha256,
        )
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                persisted = _load_private_json(
                    self._entries_directory / f"{key}.json",
                    label="checkpoint entry",
                )
                if persisted != existing:
                    raise ValueError("v5 embedding checkpoint entry changed after loading")
                if (
                    existing["input_sha256"] != input_sha256
                    or existing["scenario_id"] != scenario_id
                    or existing["scenario_input_sha256"] != scenario_input_sha256
                ):
                    raise ValueError("v5 embedding checkpoint input identity differs")
                self._cache_hits[task_type] += 1
                return [float(value) for value in existing["vector"]]

            if task_type == DOCUMENT_TASK:
                vector = self._delegate.embed_document(text)
            else:
                vector = self._delegate.embed_query(text)
            validated = _validated_vector(vector)
            self._delegate_calls[task_type] += 1
            unsigned_entry = {
                "code_sha": self._code_sha,
                "task_type": task_type,
                "profile_id": EMBEDDING_PROFILE_ID,
                "provider_identity": dict(self._identity),
                "scenario_id": scenario_id,
                "input_sha256": input_sha256,
                "scenario_input_sha256": scenario_input_sha256,
                "vector": validated,
                "vector_sha256": sha256_hex(validated),
            }
            entry_sha256 = sha256_hex(unsigned_entry)
            attestation = self._attestor.token(
                kind=CHECKPOINT_ATTESTATION_KIND,
                raw_id=entry_sha256,
            )
            entry = {**unsigned_entry, "attestation": attestation}
            self._validate_entry(key=key, entry=entry, verify_attestation=False)
            entry_path = self._entries_directory / f"{key}.json"
            if entry_path.exists():
                raise ValueError("v5 embedding checkpoint entry already exists unexpectedly")
            _atomic_write_json(entry_path, entry)
            self._entries[key] = entry
            return list(validated)

    def _validate_entry(
        self,
        *,
        key: str,
        entry: Any,
        verify_attestation: bool = True,
    ) -> None:
        expected_fields = {
            "code_sha",
            "task_type",
            "profile_id",
            "provider_identity",
            "scenario_id",
            "input_sha256",
            "scenario_input_sha256",
            "vector",
            "vector_sha256",
            "attestation",
        }
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise ValueError("v5 embedding checkpoint entry shape is invalid")
        task_type = entry["task_type"]
        if entry["code_sha"] != self._code_sha:
            raise ValueError("v5 embedding checkpoint entry code identity differs")
        if task_type not in {DOCUMENT_TASK, QUERY_TASK}:
            raise ValueError("v5 embedding checkpoint task type is invalid")
        if entry["profile_id"] != EMBEDDING_PROFILE_ID:
            raise ValueError("v5 embedding checkpoint entry profile differs")
        if entry["provider_identity"] != self._identity:
            raise ValueError("v5 embedding checkpoint entry provider differs")
        input_sha256 = entry["input_sha256"]
        if not isinstance(input_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
            raise ValueError("v5 embedding checkpoint input identity is invalid")
        scenario_id = entry["scenario_id"]
        scenario_input_sha256 = entry["scenario_input_sha256"]
        if task_type == DOCUMENT_TASK:
            if scenario_id is not None or scenario_input_sha256 is not None:
                raise ValueError("v5 document checkpoint entry carries query identity")
        else:
            if not isinstance(scenario_id, str) or not OPAQUE_SCENARIO_RE.fullmatch(scenario_id):
                raise ValueError("v5 query checkpoint scenario identity is invalid")
            expected_scenario_input = sha256_hex(
                {"scenario_id": scenario_id, "input_sha256": input_sha256}
            )
            if scenario_input_sha256 != expected_scenario_input:
                raise ValueError("v5 query checkpoint input binding differs")
        expected_key = _cache_key(
            code_sha=self._code_sha,
            task_type=task_type,
            input_sha256=input_sha256,
            scenario_id=scenario_id,
            scenario_input_sha256=scenario_input_sha256,
        )
        if key != expected_key:
            raise ValueError("v5 embedding checkpoint key differs from its entry")
        vector = _validated_vector(entry["vector"])
        if entry["vector_sha256"] != sha256_hex(vector):
            raise ValueError("v5 embedding checkpoint vector digest differs")
        attestation = entry["attestation"]
        if not isinstance(attestation, str) or not re.fullmatch(r"[0-9a-f]{64}", attestation):
            raise ValueError("v5 embedding checkpoint attestation is invalid")
        if verify_attestation:
            unsigned_entry = {name: value for name, value in entry.items() if name != "attestation"}
            expected_attestation = self._attestor.token(
                kind=CHECKPOINT_ATTESTATION_KIND,
                raw_id=sha256_hex(unsigned_entry),
            )
            if not hmac.compare_digest(attestation, expected_attestation):
                raise ValueError("v5 embedding checkpoint attestation differs")


def candidate_database_payload(
    *,
    scenario_id: str,
    candidate_id: str,
    content_sha256: str,
    qualification_contract_sha256: str,
) -> dict[str, Any]:
    """Return an opaque-only database payload shared by every candidate role."""

    if not OPAQUE_SCENARIO_RE.fullmatch(scenario_id):
        raise ValueError("v5 database payload requires an opaque scenario identity")
    if not OPAQUE_MEMORY_RE.fullmatch(candidate_id):
        raise ValueError("v5 database payload requires an opaque candidate identity")
    for value in (content_sha256, qualification_contract_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("v5 database payload digests must be SHA-256")
    return {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "candidate_id": candidate_id,
        "content_sha256": content_sha256,
        "qualification_contract_sha256": qualification_contract_sha256,
    }


def candidate_database_metadata() -> dict[str, Any]:
    """Return a fresh copy of the identical neutral candidate metadata."""

    return json.loads(json.dumps(_NEUTRAL_METADATA))


def require_fresh_development_database(
    database_url: str,
    *,
    connect_fn: Callable[..., Any] = connect,
) -> dict[str, str]:
    """Require a fully migrated, empty, name-fenced disposable database."""

    database_name = _database_name(database_url)
    if not DEVELOPMENT_DATABASE_RE.fullmatch(database_name):
        raise ValueError("v5 qualification requires a hindsight_v5_development_* database")
    expected_migrations = sorted(path.name for path in MIGRATIONS_DIR.glob("[0-9]*.sql"))
    if not expected_migrations:
        raise RuntimeError("v5 qualification could not resolve repository migrations")
    with connect_fn(
        database_url,
        application_name="hindsight-v5-qualification-preflight",
    ) as conn:
        engine_row = conn.execute(
            """
            WITH local_build AS (
                SELECT
                    max(value) FILTER (WHERE field = 'Name') AS engine_name,
                    max(value) FILTER (WHERE field = 'Version') AS build_version,
                    max(value) FILTER (WHERE field = 'Build') AS build_description,
                    max(value) FILTER (WHERE field = 'ClusterID') AS build_cluster_id
                FROM crdb_internal.node_build_info
            )
            SELECT
                current_database(),
                version(),
                crdb_internal.cluster_id()::STRING,
                engine_name,
                build_version,
                build_description,
                build_cluster_id
            FROM local_build
            """
        ).fetchone()
        if engine_row is None or len(engine_row) != 7:
            raise RuntimeError("v5 qualification could not identify the database engine")
        (
            current_database,
            engine_version,
            cluster_id,
            engine_name,
            build_version,
            build_description,
            build_cluster_id,
        ) = (str(value) for value in engine_row)
        if (
            current_database != database_name
            or engine_name != "CockroachDB"
            or "cockroachdb" not in engine_version.lower()
            or not build_version.strip()
            or not build_description.strip()
        ):
            raise RuntimeError("v5 qualification database is not CockroachDB")
        try:
            normalized_cluster_id = str(uuid.UUID(cluster_id))
            normalized_build_cluster_id = str(uuid.UUID(build_cluster_id))
        except (AttributeError, ValueError) as exc:
            raise RuntimeError("v5 qualification CockroachDB cluster identity is invalid") from exc
        if (
            normalized_cluster_id == str(uuid.UUID(int=0))
            or normalized_build_cluster_id != normalized_cluster_id
        ):
            raise RuntimeError("v5 qualification CockroachDB cluster identities differ")
        observed = sorted(
            str(row[0]) for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        )
        if observed != expected_migrations:
            raise RuntimeError("v5 qualification database is not at the exact migrated schema")
        state = conn.execute(
            "SELECT active_profile_id, building_profile_id, generation "
            "FROM embedding_index_state WHERE singleton = true"
        ).fetchone()
        if state is None or tuple(state) != (None, None, 0):
            raise RuntimeError("v5 qualification database embedding state is not fresh")
        profile_count = int(conn.execute("SELECT count(*) FROM embedding_profiles").fetchone()[0])
        if profile_count != 0:
            raise RuntimeError("v5 qualification database already contains embedding profiles")
    for tenant_id in _KNOWN_TENANT_IDS:
        with tenant_scope(tenant_id):
            with connect_fn(
                database_url,
                application_name="hindsight-v5-qualification-tenant-preflight",
            ) as conn:
                for table in _FRESH_TABLES:
                    count = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                    if count:
                        raise RuntimeError(
                            f"v5 qualification database is not fresh for table {table}"
                        )
    return {
        "database_name": database_name,
        "engine": "cockroachdb",
        "engine_version": engine_version,
        "build_version": build_version,
        "build_description": build_description,
        "cluster_id": normalized_cluster_id,
    }


def _database_name(database_url: str) -> str:
    return unquote(urlsplit(database_url).path.lstrip("/")).split("/", 1)[0]


def _require_database_evidence(evidence: Mapping[str, str]) -> None:
    required = {
        "database_name",
        "engine",
        "engine_version",
        "build_version",
        "build_description",
        "cluster_id",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != required:
        raise RuntimeError("v5 qualification database identity evidence is incomplete")
    if not DEVELOPMENT_DATABASE_RE.fullmatch(str(evidence["database_name"])):
        raise RuntimeError("v5 qualification database identity is outside the development fence")
    if (
        evidence["engine"] != "cockroachdb"
        or "cockroachdb" not in str(evidence["engine_version"]).lower()
    ):
        raise RuntimeError("v5 qualification database identity is not CockroachDB")
    if not evidence["build_version"].strip() or not evidence["build_description"].strip():
        raise RuntimeError("v5 qualification CockroachDB build identity is incomplete")
    try:
        normalized_cluster_id = str(uuid.UUID(str(evidence["cluster_id"])))
    except (AttributeError, ValueError) as exc:
        raise RuntimeError("v5 qualification CockroachDB cluster identity is invalid") from exc
    if normalized_cluster_id != evidence["cluster_id"]:
        raise RuntimeError("v5 qualification CockroachDB cluster identity is not canonical")
    if normalized_cluster_id == str(uuid.UUID(int=0)):
        raise RuntimeError("v5 qualification CockroachDB cluster identity is zero")


def _require_database_identities(evidence: Mapping[str, str]) -> None:
    if not isinstance(evidence, Mapping) or set(evidence) != {
        "deploy_identity",
        "runtime_identity",
    }:
        raise RuntimeError("v5 qualification database identity evidence is incomplete")
    deploy_identity = evidence["deploy_identity"]
    runtime_identity = evidence["runtime_identity"]
    if (
        not isinstance(deploy_identity, str)
        or not deploy_identity.strip()
        or not isinstance(runtime_identity, str)
        or not runtime_identity.strip()
    ):
        raise RuntimeError("v5 qualification database identity evidence is invalid")
    if deploy_identity == runtime_identity:
        raise RuntimeError("v5 qualification deploy and runtime identities must differ")


def require_restricted_runtime_database(
    deploy_database_url: str,
    runtime_database_url: str,
    *,
    connect_fn: Callable[..., Any] = connect,
) -> dict[str, str]:
    """Require a non-bypass runtime identity on the same disposable database."""

    deploy_name = _database_name(deploy_database_url)
    runtime_name = _database_name(runtime_database_url)
    if deploy_name != runtime_name or not DEVELOPMENT_DATABASE_RE.fullmatch(runtime_name):
        raise ValueError(
            "v5 qualification deploy and runtime URLs must target the same "
            "hindsight_v5_development_* database"
        )
    with connect_fn(
        deploy_database_url,
        application_name="hindsight-v5-qualification-deploy-identity",
    ) as conn:
        deploy_row = conn.execute(
            "SELECT current_database(), crdb_internal.cluster_id()::STRING, current_user"
        ).fetchone()
        if deploy_row is None or len(deploy_row) != 3:
            raise RuntimeError("v5 qualification deploy database identity is incomplete")
        deploy_database, deploy_cluster_id, deploy_identity = deploy_row
    with connect_fn(
        runtime_database_url,
        application_name="hindsight-v5-qualification-runtime-preflight",
    ) as conn:
        runtime_row = conn.execute(
            "SELECT current_database(), crdb_internal.cluster_id()::STRING, "
            "current_user, session_user, rolsuper, rolbypassrls, "
            "pg_has_role(current_user, 'admin', 'member'), "
            "pg_has_role(current_user, 'hindsight_memory_worker', 'member') "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if runtime_row is None or len(runtime_row) != 8:
            raise RuntimeError("v5 qualification runtime database identity is incomplete")
        (
            runtime_database,
            runtime_cluster_id,
            identity,
            session_identity,
            superuser,
            bypass_rls,
            admin,
            memory_worker,
        ) = runtime_row
    try:
        normalized_deploy_cluster = str(uuid.UUID(str(deploy_cluster_id)))
        normalized_runtime_cluster = str(uuid.UUID(str(runtime_cluster_id)))
    except (AttributeError, ValueError) as exc:
        raise RuntimeError("v5 qualification runtime cluster identity is invalid") from exc
    if (
        str(deploy_database) != deploy_name
        or str(runtime_database) != runtime_name
        or normalized_deploy_cluster != normalized_runtime_cluster
        or normalized_deploy_cluster == str(uuid.UUID(int=0))
    ):
        raise RuntimeError("v5 qualification deploy and runtime database identities differ")
    if str(identity) != str(session_identity):
        raise RuntimeError("v5 qualification runtime session identity is indirect")
    if bool(superuser) or bool(bypass_rls) or bool(admin):
        raise RuntimeError("v5 qualification runtime identity can bypass tenant isolation")
    if not str(identity).strip():
        raise RuntimeError("v5 qualification runtime identity is missing")
    if str(deploy_identity) == str(identity):
        raise RuntimeError("v5 qualification deploy and runtime identities must differ")
    if not bool(memory_worker):
        raise RuntimeError("v5 qualification runtime identity must inherit hindsight_memory_worker")
    return {
        "deploy_identity": str(deploy_identity),
        "runtime_identity": str(identity),
    }


def summarize_qualification_results(
    *,
    code_sha: str,
    database_name: str,
    results: Sequence[Mapping[str, Any]],
    checkpoint: CheckpointedEmbeddingProvider,
    structural_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed over all 600 case records and return a hashed receipt body."""

    structural = dict(structural_receipt or qualify_development_structure(code_sha=code_sha))
    _require_structural_receipt(structural, code_sha=code_sha)
    selected = select_embedding_scenarios(code_sha=code_sha)
    expected_ids = [str(item["scenario_id"]) for item in selected]
    observed_ids = [str(row.get("scenario_id") or "") for row in results]
    if len(results) != EXPECTED_SCENARIO_COUNT or observed_ids != expected_ids:
        raise ValueError("v5 qualification results are incomplete or out of protocol order")
    if len(set(observed_ids)) != EXPECTED_SCENARIO_COUNT:
        raise ValueError("v5 qualification results contain duplicate scenarios")
    for scenario, row in zip(selected, results, strict=True):
        oracle = scenario.get("oracle")
        expected_target_id = (
            str(oracle.get("positive_lesson_id") or "") if isinstance(oracle, Mapping) else ""
        )
        if not OPAQUE_MEMORY_RE.fullmatch(expected_target_id):
            raise ValueError("v5 qualification selected target identity is invalid")
        if row.get("status") != "qualified":
            raise ValueError("v5 qualification contains a non-qualified scenario")
        if row.get("candidate_count") != 4:
            raise ValueError("v5 qualification scenario does not contain four candidates")
        if row.get("policy") != "semantic_strict" or row.get("fallback_reason") is not None:
            raise ValueError("v5 qualification used a fallback retrieval policy")
        if row.get("target_rank") != 1:
            raise ValueError("v5 qualification target is not uniquely rank one")
        _require_uuid_identity(row.get("retrieval_id"), label="retrieval")
        direct_ids = row.get("direct_candidate_ids")
        indexed_ids = row.get("indexed_candidate_ids")
        if (
            not isinstance(direct_ids, list)
            or not isinstance(indexed_ids, list)
            or not direct_ids
            or len(direct_ids) > 4
            or len(indexed_ids) > 4
            or any(
                not isinstance(item, str) or not OPAQUE_MEMORY_RE.fullmatch(item)
                for item in direct_ids
            )
            or any(
                not isinstance(item, str) or not OPAQUE_MEMORY_RE.fullmatch(item)
                for item in indexed_ids
            )
            or len(set(direct_ids)) != len(direct_ids)
            or len(set(indexed_ids)) != len(indexed_ids)
        ):
            raise ValueError("v5 qualification candidate identity evidence is invalid")
        if row.get("membership_parity") is not True:
            raise ValueError("v5 qualification direct and indexed membership differs")
        if row.get("order_parity") is not True or direct_ids != indexed_ids:
            raise ValueError("v5 qualification direct and indexed order differs")
        if direct_ids[0] != expected_target_id or indexed_ids[0] != expected_target_id:
            raise ValueError("v5 qualification rank-one target identity differs")
        target_distance = _finite_float(row.get("target_distance"), "target distance")
        if target_distance > EMBEDDING_MAX_DISTANCE:
            raise ValueError("v5 qualification target exceeds the frozen cutoff")
        if _finite_float(row.get("target_margin"), "target margin") <= 0:
            raise ValueError("v5 qualification target margin is not positive")
        if row.get("index_parity") is not True:
            raise ValueError("v5 qualification direct and indexed results differ")
        if _finite_float(row.get("max_distance_delta"), "distance delta") > 1e-6:
            raise ValueError("v5 qualification indexed distance parity exceeds tolerance")
        if row.get("alternate_tenant_visible") is not False:
            raise ValueError("v5 qualification data is visible to the alternate tenant")
        for field in (
            "alternate_retrieval_visible",
            "alternate_current_semantic_visible",
            "alternate_audit_visible",
            "alternate_learning_reads_visible",
        ):
            if row.get(field) is not False:
                raise ValueError("v5 qualification tenant-isolation evidence is incomplete")
        if row.get("indexed_target_rank") != 1:
            raise ValueError("v5 indexed target is not rank one")
        if row.get("learning_decision_sealed") is not True:
            raise ValueError("v5 learning retrieval decision is not sealed")
        if row.get("alternate_decision_sealed") is not True:
            raise ValueError("v5 alternate-tenant retrieval decision is not sealed")
    counts = checkpoint.entry_counts
    if counts != {
        DOCUMENT_TASK: EXPECTED_UNIQUE_DOCUMENTS,
        QUERY_TASK: EXPECTED_SCENARIO_COUNT,
    }:
        raise ValueError("v5 embedding checkpoint coverage is incomplete")
    if checkpoint.delegate_call_counts[DOCUMENT_TASK] > EXPECTED_UNIQUE_DOCUMENTS:
        raise ValueError("v5 document embedding cache exceeded its unique-input bound")
    if checkpoint.delegate_call_counts[QUERY_TASK] > EXPECTED_SCENARIO_COUNT:
        raise ValueError("v5 query embedding cache exceeded its scenario-input bound")
    contract = development_qualification_contract()
    summary = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "qualified",
        "code_sha": code_sha,
        "qualification_contract_sha256": contract["qualification_contract_sha256"],
        "core_protocol_sha256": contract["core_protocol_sha256"],
        "structural_receipt_sha256": structural["receipt_sha256"],
        "structural_corpus_sha256": structural["corpus_sha256"],
        "structural_protocol_sha256": structural["protocol_sha256"],
        "database_name": database_name,
        "provider_identity": checkpoint.delegate_identity,
        "embedding_profile_id": EMBEDDING_PROFILE_ID,
        "embedding_max_distance": EMBEDDING_MAX_DISTANCE,
        "selection_sha256": EXPECTED_DEVELOPMENT_EMBEDDING_SELECTION_SHA256,
        "scenario_count": len(results),
        "all_target_rank_one": True,
        "all_index_parity": True,
        "alternate_tenant_invisible": True,
        "minimum_target_margin": min(float(row["target_margin"]) for row in results),
        "maximum_target_distance": max(float(row["target_distance"]) for row in results),
        "maximum_distance_delta": max(float(row["max_distance_delta"]) for row in results),
        "checkpoint_sha256": checkpoint.checkpoint_sha256,
        "checkpoint_attestation_key_id_sha256": checkpoint.attestation_key_id_sha256,
        "checkpoint_entry_counts": counts,
        "delegate_call_counts": checkpoint.delegate_call_counts,
        "cache_hit_counts": checkpoint.cache_hit_counts,
        "results": [dict(row) for row in results],
    }
    return {**summary, "receipt_sha256": sha256_hex(summary)}


def _qualification_diagnostic(
    *,
    code_sha: str,
    database_evidence: Mapping[str, str],
    database_identities: Mapping[str, str],
    structural_receipt: Mapping[str, Any],
    checkpoint: CheckpointedEmbeddingProvider,
    results: Sequence[Mapping[str, Any]],
    failure: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a non-claim diagnostic artifact before fail-closed summarization."""

    contract = development_qualification_contract()
    body = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": "diagnostic_only",
        "qualification_claim": False,
        "code_sha": code_sha,
        "qualification_contract_sha256": contract["qualification_contract_sha256"],
        "structural_receipt_sha256": structural_receipt["receipt_sha256"],
        "database_name": database_evidence["database_name"],
        "database_engine": database_evidence["engine"],
        "database_engine_version_sha256": hashlib.sha256(
            database_evidence["engine_version"].encode("utf-8")
        ).hexdigest(),
        "database_build_version_sha256": hashlib.sha256(
            database_evidence["build_version"].encode("utf-8")
        ).hexdigest(),
        "database_build_description_sha256": hashlib.sha256(
            database_evidence["build_description"].encode("utf-8")
        ).hexdigest(),
        "database_cluster_id_sha256": hashlib.sha256(
            database_evidence["cluster_id"].encode("utf-8")
        ).hexdigest(),
        "deploy_database_identity_sha256": hashlib.sha256(
            database_identities["deploy_identity"].encode("utf-8")
        ).hexdigest(),
        "runtime_database_identity_sha256": hashlib.sha256(
            database_identities["runtime_identity"].encode("utf-8")
        ).hexdigest(),
        "provider_identity": checkpoint.delegate_identity,
        "checkpoint_sha256": checkpoint.checkpoint_sha256,
        "checkpoint_attestation_key_id_sha256": checkpoint.attestation_key_id_sha256,
        "checkpoint_entry_counts": checkpoint.entry_counts,
        "delegate_call_counts": checkpoint.delegate_call_counts,
        "cache_hit_counts": checkpoint.cache_hit_counts,
        "scenario_count": len(results),
        "qualified_row_count": sum(row.get("status") == "qualified" for row in results),
        "results": [dict(row) for row in results],
    }
    if failure is not None:
        body["failure"] = dict(failure)
    return {**body, "diagnostic_sha256": sha256_hex(body)}


def run_development_qualification(
    *,
    code_sha: str,
    database_url: str,
    runtime_database_url: str,
    embedding_provider: EmbeddingProvider,
    checkpoint_attestor: CheckpointAttestor,
    checkpoint_path: str | os.PathLike[str],
    receipt_path: str | os.PathLike[str],
    diagnostic_path: str | os.PathLike[str],
    connect_fn: Callable[..., Any] = connect,
    store_factory: Callable[..., QualificationStore] = MemoryStore,
    begin_profile_build_fn: Callable[..., Mapping[str, Any]] = begin_profile_build,
    activate_profile_fn: Callable[..., Mapping[str, Any]] = activate_profile,
    database_validator_fn: Callable[[str], Mapping[str, str]] | None = None,
    runtime_database_validator_fn: Callable[[str, str], Mapping[str, str]] | None = None,
    profile_initializer_fn: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the complete provider and strict CockroachDB development qualification."""

    if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
        raise ValueError("v5 qualification requires an exact lowercase code SHA")
    receipt = require_private_path(receipt_path)
    diagnostic = require_private_path(diagnostic_path)
    if diagnostic == receipt:
        raise ValueError("v5 qualification receipt and diagnostic paths must differ")
    checkpoint_directory = _require_private_directory(checkpoint_path)
    _reject_checkpoint_descendant(receipt, checkpoint_directory, label="receipt")
    _reject_checkpoint_descendant(diagnostic, checkpoint_directory, label="diagnostic")
    _remove_private_file(receipt)
    _remove_private_file(diagnostic)
    checkpoint = CheckpointedEmbeddingProvider(
        embedding_provider,
        checkpoint_directory,
        code_sha=code_sha,
        attestor=checkpoint_attestor,
    )
    database_evidence = (
        database_validator_fn(database_url)
        if database_validator_fn is not None
        else require_fresh_development_database(database_url, connect_fn=connect_fn)
    )
    _require_database_evidence(database_evidence)
    database_name = database_evidence["database_name"]
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
    structural_receipt = qualify_development_structure(code_sha=code_sha)
    _require_structural_receipt(structural_receipt, code_sha=code_sha)
    selected = select_embedding_scenarios(code_sha=code_sha)
    _prewarm_embeddings(selected=selected, provider=checkpoint)
    if checkpoint.entry_counts != {
        DOCUMENT_TASK: EXPECTED_UNIQUE_DOCUMENTS,
        QUERY_TASK: EXPECTED_SCENARIO_COUNT,
    }:
        raise RuntimeError("v5 provider qualification did not produce exact cache coverage")
    calls_before_database = checkpoint.delegate_call_counts
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
    _require_profile_mapping(_profile_mapping(profile))
    try:
        results = _run_database_cases(
            selected=selected,
            db_url=runtime_database_url,
            provider=checkpoint,
            store_factory=store_factory,
        )
    except Exception as exc:
        if checkpoint.delegate_call_counts != calls_before_database:
            raise RuntimeError(
                "v5 database stage unexpectedly invoked the embedding provider"
            ) from exc
        failure_diagnostic = _qualification_diagnostic(
            code_sha=code_sha,
            database_evidence=database_evidence,
            database_identities=database_identities,
            structural_receipt=structural_receipt,
            checkpoint=checkpoint,
            results=[],
            failure={
                "stage": "database_population_or_retrieval",
                "code": type(exc).__name__,
                "message_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            },
        )
        _atomic_write_json(diagnostic, failure_diagnostic)
        raise
    diagnostic_value = _qualification_diagnostic(
        code_sha=code_sha,
        database_evidence=database_evidence,
        database_identities=database_identities,
        structural_receipt=structural_receipt,
        checkpoint=checkpoint,
        results=results,
    )
    _atomic_write_json(diagnostic, diagnostic_value)
    if checkpoint.delegate_call_counts != calls_before_database:
        raise RuntimeError("v5 database stage unexpectedly invoked the embedding provider")
    value = summarize_qualification_results(
        code_sha=code_sha,
        database_name=database_name,
        results=results,
        checkpoint=checkpoint,
        structural_receipt=structural_receipt,
    )
    value["deploy_database_identity_sha256"] = hashlib.sha256(
        database_identities["deploy_identity"].encode("utf-8")
    ).hexdigest()
    value["runtime_database_identity_sha256"] = hashlib.sha256(
        database_identities["runtime_identity"].encode("utf-8")
    ).hexdigest()
    value["database_engine"] = database_evidence["engine"]
    value["database_engine_version_sha256"] = hashlib.sha256(
        database_evidence["engine_version"].encode("utf-8")
    ).hexdigest()
    value["database_build_version_sha256"] = hashlib.sha256(
        database_evidence["build_version"].encode("utf-8")
    ).hexdigest()
    value["database_build_description_sha256"] = hashlib.sha256(
        database_evidence["build_description"].encode("utf-8")
    ).hexdigest()
    value["database_cluster_id_sha256"] = hashlib.sha256(
        database_evidence["cluster_id"].encode("utf-8")
    ).hexdigest()
    value["checkpoint_attestation_key_id_sha256"] = checkpoint.attestation_key_id_sha256
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    value["receipt_sha256"] = sha256_hex(unsigned)
    _atomic_write_json(receipt, value)
    return value


def _prewarm_embeddings(
    *,
    selected: Sequence[Mapping[str, Any]],
    provider: CheckpointedEmbeddingProvider,
) -> None:
    for scenario in selected:
        scenario_id = str(scenario["scenario_id"])
        memories = _candidate_memories(scenario)
        for memory in memories:
            provider.embed_document(str(memory["content"]))
        query = render_retrieval_query(scenario)
        with provider.query_scope(scenario_id=scenario_id, query=query):
            provider.embed_query(query)


def _initialize_exact_profile(
    *,
    provider: CheckpointedEmbeddingProvider,
    db_url: str,
    store_factory: Callable[..., QualificationStore],
    begin_profile_build_fn: Callable[..., Mapping[str, Any]],
    activate_profile_fn: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    with tenant_scope(learning_tenant_id()):
        building = begin_profile_build_fn(
            provider=provider,
            max_distance=EMBEDDING_MAX_DISTANCE,
            db_url=db_url,
        )
        if str(building.get("id") or "") != EMBEDDING_PROFILE_ID:
            raise RuntimeError("v5 building profile differs from the frozen profile")
        activate_profile_fn(profile_id=EMBEDDING_PROFILE_ID, db_url=db_url)
        with store_factory(url=db_url, embedding_provider=provider) as store:
            active = store.active_embedding_profile()
    return _profile_mapping(active)


def _run_database_cases(
    *,
    selected: Sequence[Mapping[str, Any]],
    db_url: str,
    provider: CheckpointedEmbeddingProvider,
    store_factory: Callable[..., QualificationStore],
) -> list[dict[str, Any]]:
    contract_sha256 = development_qualification_contract()["qualification_contract_sha256"]
    loaded_cases: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    with tenant_scope(learning_tenant_id()):
        with store_factory(url=db_url, embedding_provider=provider) as store:
            # Populate the complete 2,400-candidate corpus before any query so every
            # scenario is qualified against the same final CockroachDB index state.
            for scenario in selected:
                loaded_cases.append(
                    _load_database_case(
                        scenario=scenario,
                        store=store,
                        provider=provider,
                        contract_sha256=contract_sha256,
                    )
                )
            for loaded_case in loaded_cases:
                results.append(
                    _retrieve_database_case(
                        loaded_case=loaded_case,
                        store=store,
                        provider=provider,
                    )
                )
    with tenant_scope(ACCEPTANCE_TENANT_ID):
        with store_factory(url=db_url, embedding_provider=provider) as store:
            for loaded_case, result in zip(loaded_cases, results, strict=True):
                scenario = loaded_case["scenario"]
                query = render_retrieval_query(scenario)
                scenario_id = str(loaded_case["scenario_id"])
                namespace = str(loaded_case["namespace"])
                decision_id = f"v5-development-isolation:{scenario_id}"
                with provider.query_scope(scenario_id=scenario_id, query=query):
                    alternate = store.retrieve_semantic(
                        namespace=namespace,
                        query=query,
                        decision_id=decision_id,
                        reader="v5.development.isolation",
                        purpose="Verify alternate-tenant invisibility",
                        policy="semantic_strict",
                        limit=4,
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
                    for database_id in loaded_case["database_ids"]
                )
                learning_reads_visible = bool(
                    store.reads_for_decision(decision_id=f"v5-development-retrieval:{scenario_id}")
                )
                result["alternate_retrieval_visible"] = retrieval_visible
                result["alternate_current_semantic_visible"] = current_visible
                result["alternate_audit_visible"] = audit_visible
                result["alternate_learning_reads_visible"] = learning_reads_visible
                visible = any(
                    (
                        retrieval_visible,
                        current_visible,
                        audit_visible,
                        learning_reads_visible,
                    )
                )
                result["alternate_tenant_visible"] = visible
                result["alternate_decision_sealed"] = True
                if visible:
                    result["status"] = "failed"
    return results


def _load_database_case(
    *,
    scenario: Mapping[str, Any],
    store: QualificationStore,
    provider: CheckpointedEmbeddingProvider,
    contract_sha256: str,
) -> dict[str, Any]:
    scenario_id = str(scenario["scenario_id"])
    namespace = _scenario_namespace(scenario_id)
    memories = _candidate_memories(scenario)
    expected_target_id = str(dict(scenario["oracle"])["positive_lesson_id"])
    vectors: dict[str, list[float]] = {}
    database_ids: dict[str, str] = {}
    for memory in memories:
        candidate_id = str(memory["memory_id"])
        content = str(memory["content"])
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        vector = provider.embed_document(content)
        vectors[candidate_id] = vector
        row = _remember_candidate_with_retry(
            store=store,
            memory_kind="semantic",
            namespace=namespace,
            content=content,
            provenance=Provenance(
                writer="v5.development.qualification",
                source_ref=f"v5-development:{scenario_id}:{candidate_id}",
                justification="Store one neutral development retrieval candidate",
            ),
            metadata=candidate_database_metadata(),
            content_schema="v5_development_candidate.v1",
            structured_payload=candidate_database_payload(
                scenario_id=scenario_id,
                candidate_id=candidate_id,
                content_sha256=content_sha256,
                qualification_contract_sha256=contract_sha256,
            ),
            precomputed_embedding=vector,
        )
        database_ids[str(row["id"])] = candidate_id
    return {
        "scenario": scenario,
        "scenario_id": scenario_id,
        "namespace": namespace,
        "memories": memories,
        "expected_target_id": expected_target_id,
        "vectors": vectors,
        "database_ids": database_ids,
    }


def _remember_candidate_with_retry(
    *,
    store: QualificationStore,
    sleep_fn: Callable[[float], None] = time.sleep,
    **remember_kwargs: Any,
) -> Mapping[str, Any]:
    """Retry one fully rolled-back CockroachDB candidate write with backoff."""

    for attempt in range(1, DATABASE_WRITE_ATTEMPTS + 1):
        try:
            return store.remember(**remember_kwargs)
        except SerializationFailure:
            if attempt == DATABASE_WRITE_ATTEMPTS:
                raise
            sleep_fn(DATABASE_WRITE_RETRY_DELAYS_SECONDS[attempt - 1])
    raise RuntimeError("v5 candidate write retry loop exited without a result")


def _retrieve_database_case(
    *,
    loaded_case: Mapping[str, Any],
    store: QualificationStore,
    provider: CheckpointedEmbeddingProvider,
) -> dict[str, Any]:
    scenario = loaded_case["scenario"]
    scenario_id = str(loaded_case["scenario_id"])
    namespace = str(loaded_case["namespace"])
    memories = loaded_case["memories"]
    expected_target_id = str(loaded_case["expected_target_id"])
    vectors = loaded_case["vectors"]
    database_ids = loaded_case["database_ids"]
    if (
        not isinstance(scenario, Mapping)
        or not isinstance(memories, list)
        or not isinstance(vectors, Mapping)
        or not isinstance(database_ids, Mapping)
    ):
        raise RuntimeError("v5 loaded database case is invalid")
    query = render_retrieval_query(scenario)
    decision_id = f"v5-development-retrieval:{scenario_id}"
    with provider.query_scope(scenario_id=scenario_id, query=query):
        query_vector = provider.embed_query(query)
        retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=query,
            decision_id=decision_id,
            reader="v5.development.qualification",
            purpose="Qualify strict rank-one development retrieval",
            policy="semantic_strict",
            limit=4,
        )
    _verify_retrieval_trace(store=store, retrieval=retrieval, decision_id=decision_id)
    _seal_retrieval_decision(store=store, decision_id=decision_id)
    direct_all = sorted(
        (
            {
                "candidate_id": candidate_id,
                "distance": _cosine_distance(query_vector, vector),
            }
            for candidate_id, vector in vectors.items()
        ),
        key=lambda row: (row["distance"], row["candidate_id"]),
    )
    direct = [row for row in direct_all if row["distance"] <= EMBEDDING_MAX_DISTANCE]
    indexed = []
    for hit in _result_hits(retrieval):
        database_id = str(hit["id"])
        if database_id not in database_ids:
            raise RuntimeError("v5 indexed retrieval returned an unknown database memory")
        indexed.append(
            {
                "candidate_id": database_ids[database_id],
                "distance": _finite_float(hit.get("distance"), "indexed distance"),
            }
        )
    direct_ids = [str(row["candidate_id"]) for row in direct]
    indexed_ids = [str(row["candidate_id"]) for row in indexed]
    membership_parity = set(direct_ids) == set(indexed_ids)
    order_parity = direct_ids == indexed_ids
    deltas = [
        abs(float(direct_row["distance"]) - float(indexed_row["distance"]))
        for direct_row, indexed_row in zip(direct, indexed, strict=False)
        if direct_row["candidate_id"] == indexed_row["candidate_id"]
    ]
    max_distance_delta = max(deltas, default=math.inf if direct_ids or indexed_ids else 0.0)
    target_rank = next(
        (
            index
            for index, row in enumerate(direct_all, start=1)
            if row["candidate_id"] == expected_target_id
        ),
        None,
    )
    target_distance = next(
        (float(row["distance"]) for row in direct_all if row["candidate_id"] == expected_target_id),
        math.inf,
    )
    competitor_distance = min(
        float(row["distance"]) for row in direct_all if row["candidate_id"] != expected_target_id
    )
    indexed_target_rank = next(
        (
            index
            for index, row in enumerate(indexed, start=1)
            if row["candidate_id"] == expected_target_id
        ),
        None,
    )
    policy = _result_value(retrieval, "policy")
    fallback_reason = _result_value(retrieval, "fallback_reason")
    index_parity = (
        membership_parity
        and order_parity
        and len(deltas) == len(direct) == len(indexed)
        and max_distance_delta <= 1e-6
    )
    qualified = (
        _result_value(retrieval, "status") == "succeeded"
        and policy == "semantic_strict"
        and fallback_reason is None
        and target_rank == 1
        and indexed_target_rank == 1
        and target_distance <= EMBEDDING_MAX_DISTANCE
        and competitor_distance - target_distance > 0
        and index_parity
    )
    return {
        "scenario_id": scenario_id,
        "status": "qualified" if qualified else "failed",
        "candidate_count": len(memories),
        "policy": policy,
        "fallback_reason": fallback_reason,
        "retrieval_id": str(_result_value(retrieval, "retrieval_id") or ""),
        "direct_candidate_ids": direct_ids,
        "indexed_candidate_ids": indexed_ids,
        "target_rank": target_rank,
        "indexed_target_rank": indexed_target_rank,
        "target_distance": target_distance,
        "target_margin": competitor_distance - target_distance,
        "membership_parity": membership_parity,
        "order_parity": order_parity,
        "max_distance_delta": max_distance_delta,
        "index_parity": index_parity,
        "alternate_tenant_visible": False,
        "alternate_retrieval_visible": False,
        "alternate_current_semantic_visible": False,
        "alternate_audit_visible": False,
        "alternate_learning_reads_visible": False,
        "learning_decision_sealed": True,
        "alternate_decision_sealed": False,
    }


def _candidate_memories(scenario: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    agent_view = scenario.get("agent_view")
    if not isinstance(agent_view, Mapping):
        raise ValueError("v5 qualification scenario has no agent view")
    memories = agent_view.get("memories")
    if not isinstance(memories, list) or len(memories) != 4:
        raise ValueError("v5 qualification scenario requires four candidates")
    governance = []
    for memory in memories:
        if not isinstance(memory, Mapping):
            raise ValueError("v5 qualification candidate is invalid")
        memory_id = str(memory.get("memory_id") or "")
        if not OPAQUE_MEMORY_RE.fullmatch(memory_id):
            raise ValueError("v5 qualification candidate identity is not opaque")
        if not isinstance(memory.get("content"), str) or not memory["content"]:
            raise ValueError("v5 qualification candidate content is required")
        governance.append(
            {key: value for key, value in memory.items() if key not in {"memory_id", "content"}}
        )
    if any(value != governance[0] for value in governance[1:]):
        raise ValueError("v5 qualification candidates do not have neutral uniform governance")
    return memories


def _require_exact_provider(provider: EmbeddingProvider) -> dict[str, Any]:
    identity = {
        "provider": str(getattr(provider, "provider_name", "")),
        "model": str(getattr(provider, "model_name", "")),
        "dimensions": getattr(provider, "dimensions", None),
        "capability": str(getattr(provider, "capability", "")),
        "encoder_revision": str(getattr(provider, "encoder_revision", "")),
        "representation": str(getattr(provider, "representation", "")),
    }
    expected = {
        "provider": EMBEDDING_PROVIDER,
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "capability": EMBEDDING_CAPABILITY,
        "encoder_revision": EMBEDDING_ENCODER_REVISION,
        "representation": GEMINI_PROVIDER_REPRESENTATION,
    }
    if identity != expected:
        raise ValueError("v5 qualification requires the exact frozen Gemini embedding provider")
    profile = embedding_profile(provider, max_distance=EMBEDDING_MAX_DISTANCE)
    if profile.profile_id != EMBEDDING_PROFILE_ID:
        raise ValueError("v5 qualification provider differs from the frozen embedding profile")
    return identity


def _require_structural_receipt(receipt: Mapping[str, Any], *, code_sha: str) -> None:
    expected_protocol = development_protocol()["protocol_sha256"]
    required = {
        "status": "qualified",
        "code_sha": code_sha,
        "protocol_sha256": expected_protocol,
        "scenario_count": len(MECHANISM_FAMILIES) * STRUCTURAL_CASES_PER_FAMILY,
    }
    if any(receipt.get(key) != value for key, value in required.items()):
        raise ValueError("v5 live qualification structural receipt is invalid")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != sha256_hex(unsigned):
        raise ValueError("v5 live qualification structural receipt digest differs")
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("corpus_sha256") or "")):
        raise ValueError("v5 live qualification structural corpus identity is invalid")


def _require_profile_mapping(profile: Mapping[str, Any]) -> None:
    expected = {
        "id": EMBEDDING_PROFILE_ID,
        "provider": EMBEDDING_PROVIDER,
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "capability": EMBEDDING_CAPABILITY,
        "encoder_revision": EMBEDDING_ENCODER_REVISION,
        "configuration": {},
        "max_distance": EMBEDDING_MAX_DISTANCE,
    }
    observed = {key: profile.get(key) for key in expected}
    if observed != expected:
        raise RuntimeError("v5 database active profile differs from the frozen profile")


def _profile_mapping(profile: Any) -> dict[str, Any]:
    if isinstance(profile, Mapping):
        value = dict(profile)
    else:
        value = {
            "id": getattr(profile, "profile_id", None),
            "provider": getattr(profile, "provider", None),
            "model": getattr(profile, "model", None),
            "dimensions": getattr(profile, "dimensions", None),
            "capability": getattr(profile, "capability", None),
            "encoder_revision": getattr(profile, "encoder_revision", None),
            "configuration": dict(getattr(profile, "configuration", {}) or {}),
            "max_distance": getattr(profile, "max_distance", None),
        }
    if "profile_id" in value and "id" not in value:
        value["id"] = value["profile_id"]
    return value


def _cache_key(
    *,
    code_sha: str,
    task_type: str,
    input_sha256: str,
    scenario_id: str | None,
    scenario_input_sha256: str | None,
) -> str:
    return sha256_hex(
        {
            "code_sha": code_sha,
            "profile_id": EMBEDDING_PROFILE_ID,
            "task_type": task_type,
            "input_sha256": input_sha256 if task_type == DOCUMENT_TASK else None,
            "scenario_id": scenario_id,
            "scenario_input_sha256": scenario_input_sha256,
        }
    )


def _validated_vector(value: Any) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("v5 embedding vector is not a numeric sequence")
    if len(value) != EMBEDDING_DIMENSIONS:
        raise ValueError("v5 embedding vector has unexpected dimensions")
    vector = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("v5 embedding vector contains a nonnumeric value")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError("v5 embedding vector contains a nonfinite value")
        vector.append(number)
    if not any(number != 0 for number in vector):
        raise ValueError("v5 embedding vector cannot be all zero")
    return vector


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != EMBEDDING_DIMENSIONS or len(right) != EMBEDDING_DIMENSIONS:
        raise ValueError("v5 cosine distance requires exact embedding dimensions")
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("v5 cosine distance requires nonzero vectors")
    distance = 1.0 - dot / (left_norm * right_norm)
    if not math.isfinite(distance):
        raise ValueError("v5 cosine distance is nonfinite")
    return distance


def _scenario_namespace(scenario_id: str) -> str:
    if not OPAQUE_SCENARIO_RE.fullmatch(scenario_id):
        raise ValueError("v5 qualification namespace requires an opaque scenario identity")
    return f"v5-development:{scenario_id}"


def _verify_retrieval_trace(
    *,
    store: QualificationStore,
    retrieval: Any,
    decision_id: str,
) -> None:
    retrieval_id = str(_result_value(retrieval, "retrieval_id") or "")
    if not retrieval_id:
        raise RuntimeError("v5 audited retrieval has no retrieval identity")
    hits = _result_hits(retrieval)
    reads = store.reads_for_decision(decision_id=decision_id)
    if len(reads) != len(hits):
        raise RuntimeError("v5 decision-to-memory trace count differs from retrieval hits")
    try:
        ranked_reads = sorted(reads, key=lambda read: int(read.get("rank") or 0))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("v5 decision read rank is invalid") from exc
    observed_ranks = [int(read.get("rank") or 0) for read in ranked_reads]
    if observed_ranks != list(range(1, len(hits) + 1)):
        raise RuntimeError("v5 decision read ranks are not unique and contiguous")
    for expected_rank, (hit, read) in enumerate(zip(hits, ranked_reads, strict=True), start=1):
        if str(read.get("retrieval_id") or "") != retrieval_id:
            raise RuntimeError("v5 decision read has a different retrieval identity")
        if int(read.get("rank") or 0) != expected_rank:
            raise RuntimeError("v5 decision read rank differs from retrieval order")
        if str(read.get("memory_id") or "") != str(hit.get("id") or ""):
            raise RuntimeError("v5 decision read memory differs from retrieval hit")
        read_distance = _finite_float(read.get("distance"), "trace distance")
        hit_distance = _finite_float(hit.get("distance"), "retrieval distance")
        if abs(read_distance - hit_distance) > 1e-6:
            raise RuntimeError("v5 decision read distance differs from retrieval hit")


def _seal_retrieval_decision(*, store: QualificationStore, decision_id: str) -> None:
    sealed = store.seal_decision(decision_id=decision_id)
    if not isinstance(sealed, Mapping) or sealed.get("status") != "sealed":
        raise RuntimeError("v5 retrieval decision did not reach sealed status")
    connection = getattr(store, "_conn", None)
    if connection is not None:
        connection.commit()


def _result_hits(result: Any) -> list[Mapping[str, Any]]:
    hits = _result_value(result, "hits")
    if not isinstance(hits, (list, tuple)):
        raise ValueError("v5 qualification retrieval returned invalid hits")
    if any(not isinstance(hit, Mapping) for hit in hits):
        raise ValueError("v5 qualification retrieval hit is invalid")
    return list(hits)


def _result_value(result: Any, name: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"v5 qualification {label} is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"v5 qualification {label} is invalid") from exc
    if not math.isfinite(number):
        raise ValueError(f"v5 qualification {label} is nonfinite")
    return number


def _require_uuid_identity(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"v5 qualification {label} identity is invalid")
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"v5 qualification {label} identity is invalid") from exc
    if value != normalized:
        raise ValueError(f"v5 qualification {label} identity is not canonical")
    return normalized


def _load_private_json(path: pathlib.Path, *, label: str) -> dict[str, Any]:
    private_path = require_private_path(path)
    try:
        value = json.loads(private_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"v5 embedding {label} is unreadable or corrupt") from exc
    if not isinstance(value, dict):
        raise ValueError(f"v5 embedding {label} must be one JSON object")
    return value


def _remove_orphan_atomic_files(
    directory: pathlib.Path,
    *,
    name_pattern: str,
) -> None:
    removed = False
    for path in directory.iterdir():
        if re.fullmatch(name_pattern, path.name) is None:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("v5 embedding checkpoint orphan is not a private regular file")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("v5 embedding checkpoint orphan is not private")
        path.unlink()
        removed = True
    if removed:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _atomic_write_json(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path = require_private_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            os.chmod(temporary_name, 0o600)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            pathlib.Path(temporary_name).unlink(missing_ok=True)


__all__ = [
    "CheckpointedEmbeddingProvider",
    "DOCUMENT_TASK",
    "EXPECTED_SCENARIO_COUNT",
    "EXPECTED_UNIQUE_DOCUMENTS",
    "QUERY_TASK",
    "candidate_database_metadata",
    "candidate_database_payload",
    "development_qualification_contract",
    "render_retrieval_query",
    "require_fresh_development_database",
    "require_private_path",
    "require_restricted_runtime_database",
    "run_development_qualification",
    "select_embedding_scenarios",
    "summarize_qualification_results",
]
