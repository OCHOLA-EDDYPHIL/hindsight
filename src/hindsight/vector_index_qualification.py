"""Executable qualification for the tenant-prefixed semantic vector index."""

from __future__ import annotations

import re
from typing import Any

from hindsight.embeddings import EMBEDDING_DIMENSIONS, vector_literal

TENANT_VECTOR_INDEX = "semantic_memory_vectors_tenant_namespace_profile_embedding_idx"


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
