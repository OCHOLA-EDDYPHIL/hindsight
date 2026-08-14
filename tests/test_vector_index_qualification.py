from pathlib import Path

import pytest

from hindsight.vector_index_qualification import (
    TENANT_VECTOR_INDEX,
    VectorIndexQualificationError,
    finalize_dvi_receipt,
    qualify_semantic_vector_observation,
    qualify_semantic_vector_plan,
    redact_vector_plan,
    semantic_vector_explain_sql,
    semantic_vector_search_sql,
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
    normalized_explain = " ".join(query.replace("EXPLAIN", "", 1).split())
    normalized_search = " ".join(semantic_vector_search_sql().split())
    assert normalized_search == normalized_explain


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


def test_dvi_observation_redacts_scope_and_requires_known_neighbor_first():
    plan = f"""
        • vector search
          table: semantic_memory_vectors@{TENANT_VECTOR_INDEX}
          target count: 5
          prefix spans: [/'tenant-a'/'namespace-a'/'profile-a']
    """

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self, result_id="memory-target"):
            self.result_id = result_id
            self.calls = 0

        def execute(self, _statement, _params):
            self.calls += 1
            return Result([(plan,)] if self.calls == 1 else [(self.result_id,)])

    receipt = qualify_semantic_vector_observation(
        Connection(),
        tenant_id="tenant-a",
        namespace="namespace-a",
        profile_id="profile-a",
        query_vector=[1.0, *([0.0] * 1023)],
        expected_memory_id="memory-target",
        same_prefix_cardinality=1024,
        source_revision="a" * 40,
        migration_sha256="b" * 64,
    )

    assert receipt["status"] == "PASS"
    assert receipt["known_neighbor_rank"] == 1
    assert receipt["index"] == TENANT_VECTOR_INDEX
    assert "tenant-a" not in receipt["redacted_plan"]
    assert "namespace-a" not in receipt["redacted_plan"]
    assert "profile-a" not in receipt["redacted_plan"]
    assert "<tenant>" in receipt["redacted_plan"]

    with pytest.raises(VectorIndexQualificationError, match="did not rank first"):
        qualify_semantic_vector_observation(
            Connection("wrong-memory"),
            tenant_id="tenant-a",
            namespace="namespace-a",
            profile_id="profile-a",
            query_vector=[1.0, *([0.0] * 1023)],
            expected_memory_id="memory-target",
            same_prefix_cardinality=1024,
            source_revision="a" * 40,
            migration_sha256="b" * 64,
        )


def test_dvi_receipt_fails_closed_without_verified_cleanup():
    observation = {
        "schema_version": "hindsight.dvi-qualification.v1",
        "status": "PASS",
    }

    receipt = finalize_dvi_receipt(
        observation,
        cleanup_verified=True,
        database_name_sha256="c" * 64,
    )
    assert receipt["cleanup"] == {"database_absent": True}
    assert len(receipt["receipt_sha256"]) == 64

    with pytest.raises(VectorIndexQualificationError, match="cleanup"):
        finalize_dvi_receipt(
            observation,
            cleanup_verified=False,
            database_name_sha256="c" * 64,
        )


def test_dvi_script_has_exact_disposable_database_boundary():
    source = (ROOT / "scripts/run_dvi_qualification.py").read_text()

    assert "hindsight_dvi_" in source
    assert "DROP DATABASE IF EXISTS" in source
    assert "DATABASE_PATTERN.fullmatch" in source
    assert "0030_tenant_vector_cosine_index.sql" in source
    assert "DVI_CARDINALITIES" in source
    assert "semantic_vector_explain_sql" not in source


def test_vector_plan_redaction_removes_uuid_and_query_values():
    redacted = redact_vector_plan(
        "prefix /00000000-0000-0000-0000-000000000401/ns/profile",
        tenant_id="00000000-0000-0000-0000-000000000401",
        namespace="ns",
        profile_id="profile",
    )

    assert redacted == "prefix /<tenant>/<namespace>/<profile>"
