from pathlib import Path

import pytest

from hindsight.vector_index_qualification import (
    TENANT_VECTOR_INDEX,
    VectorIndexQualificationError,
    qualify_semantic_vector_plan,
    semantic_vector_explain_sql,
)

ROOT = Path(__file__).resolve().parents[1]


def test_phase_a_migration_adds_cosine_prefix_index_without_removing_fallback():
    migration = (ROOT / "migrations/0030_tenant_vector_cosine_index.sql").read_text()

    assert "tenant_id," in migration
    assert "namespace," in migration
    assert "profile_id," in migration
    assert "embedding vector_cosine_ops" in migration
    assert TENANT_VECTOR_INDEX in migration
    assert "DROP INDEX" not in migration.upper()


def test_qualification_query_is_natural_and_binds_every_prefix():
    query = semantic_vector_explain_sql()

    assert "tenant_id = %s::UUID" in query
    assert "namespace = %s" in query
    assert "profile_id = %s" in query
    assert "ORDER BY embedding <=>" in query
    assert "@" not in query
    assert "FORCE_INDEX" not in query.upper()


def test_plan_qualifier_requires_natural_search_exact_index_and_prefix_spans():
    plan = f"""
        • vector search
          table: semantic_memory_vectors@{TENANT_VECTOR_INDEX}
          target count: 5
          prefix spans: [/'tenant'/'namespace'/'profile' - /'tenant'/'namespace'/'profile']
    """

    assert qualify_semantic_vector_plan(plan).startswith("[/")

    with pytest.raises(VectorIndexQualificationError, match="natural vector search"):
        qualify_semantic_vector_plan(plan.replace("vector search", "scan"))
    with pytest.raises(VectorIndexQualificationError, match="exact index"):
        qualify_semantic_vector_plan(plan.replace(TENANT_VECTOR_INDEX, "other_idx"))
    with pytest.raises(VectorIndexQualificationError, match="exact index"):
        qualify_semantic_vector_plan(
            plan.replace(TENANT_VECTOR_INDEX, f"{TENANT_VECTOR_INDEX}_old")
        )
    with pytest.raises(VectorIndexQualificationError, match="empty prefix spans"):
        qualify_semantic_vector_plan(
            plan.replace(
                "[/'tenant'/'namespace'/'profile' - /'tenant'/'namespace'/'profile']",
                "[]",
            )
        )


def test_product_recall_sql_qualifies_vector_prefixes_and_tenant_joins():
    source = (ROOT / "src/hindsight/memory.py").read_text()

    assert source.count("vector.tenant_id = current_hindsight_tenant_id()") == 2
    assert source.count("vector.namespace = %s") == 2
    assert source.count("vector.tenant_id = memory.tenant_id") == 1
    assert source.count("vector.tenant_id = m.tenant_id") == 1
    for relationship in (
        "link.tenant_id = memory.tenant_id",
        "incident_service.tenant_id = link.tenant_id",
        "service.tenant_id = incident_service.tenant_id",
        "im.tenant_id = m.tenant_id",
        "i.tenant_id = im.tenant_id",
        "isvc.tenant_id = i.tenant_id",
        "s.tenant_id = isvc.tenant_id",
        "r.tenant_id = ir.tenant_id",
    ):
        assert relationship in source
