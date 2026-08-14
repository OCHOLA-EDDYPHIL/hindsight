"""Executable qualification for the tenant-prefixed semantic vector index."""

from __future__ import annotations

from hashlib import sha256
import json
import re
from time import perf_counter
from typing import Any

from hindsight.embeddings import EMBEDDING_DIMENSIONS, vector_literal

TENANT_VECTOR_INDEX = "semantic_memory_vectors_tenant_namespace_profile_embedding_idx"
DVI_SCHEMA_VERSION = "hindsight.dvi-qualification.v1"
DVI_CARDINALITIES = (256, 512, 1024, 2048)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


class VectorIndexQualificationError(RuntimeError):
    """Raised when the optimizer does not choose the qualified ANN path."""


def semantic_vector_explain_sql() -> str:
    """Return the natural (unhinted) query used to qualify the ANN plan."""

    return f"""
        EXPLAIN
        SELECT memory_id
        FROM semantic_memory_vectors
        WHERE tenant_id = %s::UUID
            AND namespace = %s
            AND profile_id = %s
        ORDER BY embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})
        LIMIT %s
    """


def semantic_vector_search_sql() -> str:
    """Return the exact unhinted tenant-scoped semantic search query."""

    return f"""
        SELECT memory_id
        FROM semantic_memory_vectors
        WHERE tenant_id = %s::UUID
            AND namespace = %s
            AND profile_id = %s
        ORDER BY embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})
        LIMIT %s
    """


def explain_semantic_vector_search(
    conn: Any,
    *,
    tenant_id: str,
    namespace: str,
    profile_id: str,
    query_vector: list[float],
    limit: int,
) -> str:
    """Ask CockroachDB to plan the supported semantic-vector query."""

    rows = conn.execute(
        semantic_vector_explain_sql(),
        (
            tenant_id,
            namespace,
            profile_id,
            vector_literal(query_vector),
            limit,
        ),
    ).fetchall()
    return "\n".join(str(row[0]) for row in rows)


def qualify_semantic_vector_plan(plan: str) -> str:
    """Require natural ANN selection, the exact index, and bounded prefixes."""

    normalized = plan.lower()
    if "vector search" not in normalized:
        raise VectorIndexQualificationError("plan does not use natural vector search")

    index_match = re.search(
        r"vector search[\s\S]*?table:\s+semantic_memory_vectors@([^\s]+)",
        plan,
        flags=re.IGNORECASE,
    )
    if index_match is None or index_match.group(1).lower() != TENANT_VECTOR_INDEX.lower():
        raise VectorIndexQualificationError(f"plan does not use exact index {TENANT_VECTOR_INDEX}")

    match = re.search(r"prefix spans:\s*(.+)", plan, flags=re.IGNORECASE)
    if match is None:
        raise VectorIndexQualificationError("plan does not report prefix spans")
    spans = match.group(1).strip()
    if spans.lower() in {"", "[]", "full scan"}:
        raise VectorIndexQualificationError("plan has empty prefix spans")
    return spans


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


def _digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_bytes(value)
    return sha256(payload).hexdigest()


def redact_vector_plan(
    plan: str, *, tenant_id: str, namespace: str, profile_id: str
) -> str:
    """Remove tenant and query values while retaining the optimizer evidence."""

    redacted = plan
    for value, replacement in (
        (tenant_id, "<tenant>"),
        (namespace, "<namespace>"),
        (profile_id, "<profile>"),
    ):
        redacted = redacted.replace(value, replacement)
    redacted = UUID_PATTERN.sub("<uuid>", redacted)
    return redacted[:4_000]


def qualify_semantic_vector_observation(
    conn: Any,
    *,
    tenant_id: str,
    namespace: str,
    profile_id: str,
    query_vector: list[float],
    expected_memory_id: str,
    same_prefix_cardinality: int,
    source_revision: str,
    migration_sha256: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Prove natural index selection and first-neighbor correctness."""

    if SHA_PATTERN.fullmatch(source_revision) is None:
        raise VectorIndexQualificationError("source revision must be an exact commit SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", migration_sha256):
        raise VectorIndexQualificationError("migration digest must be an exact SHA-256")
    if same_prefix_cardinality < 1:
        raise VectorIndexQualificationError("same-prefix cardinality must be positive")

    plan = explain_semantic_vector_search(
        conn,
        tenant_id=tenant_id,
        namespace=namespace,
        profile_id=profile_id,
        query_vector=query_vector,
        limit=limit,
    )
    prefix_spans = qualify_semantic_vector_plan(plan)
    started = perf_counter()
    rows = conn.execute(
        semantic_vector_search_sql(),
        (
            tenant_id,
            namespace,
            profile_id,
            vector_literal(query_vector),
            limit,
        ),
    ).fetchall()
    latency_ms = round((perf_counter() - started) * 1_000, 3)
    result_ids = [str(row[0]) for row in rows]
    if not result_ids or result_ids[0] != expected_memory_id:
        raise VectorIndexQualificationError("known nearest neighbor did not rank first")
    redacted_plan = redact_vector_plan(
        plan,
        tenant_id=tenant_id,
        namespace=namespace,
        profile_id=profile_id,
    )
    if any(value in redacted_plan for value in (tenant_id, namespace, profile_id)):
        raise VectorIndexQualificationError("optimizer plan redaction failed")

    conclusion = {
        "source_revision": source_revision,
        "migration_sha256": migration_sha256,
        "index": TENANT_VECTOR_INDEX,
        "query_sha256": _digest(semantic_vector_search_sql().strip().encode()),
        "same_prefix_cardinality": same_prefix_cardinality,
        "known_neighbor_rank": 1,
        "prefix_spans_present": bool(prefix_spans),
        "status": "PASS",
    }
    return {
        "schema_version": DVI_SCHEMA_VERSION,
        **conclusion,
        "plan_sha256": _digest(plan.encode()),
        "redacted_plan": redacted_plan,
        "result_sha256": _digest(result_ids),
        "latency_ms": latency_ms,
        "conclusion_sha256": _digest(conclusion),
    }


def finalize_dvi_receipt(
    observation: dict[str, Any], *, cleanup_verified: bool, database_name_sha256: str
) -> dict[str, Any]:
    """Bind successful cleanup after the disposable database has been removed."""

    if observation.get("status") != "PASS" or not cleanup_verified:
        raise VectorIndexQualificationError("DVI cleanup was not verified")
    if not re.fullmatch(r"[0-9a-f]{64}", database_name_sha256):
        raise VectorIndexQualificationError("database name digest is malformed")
    receipt = {
        **observation,
        "scope": {
            "kind": "disposable-database",
            "database_name_sha256": database_name_sha256,
        },
        "cleanup": {"database_absent": True},
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt
