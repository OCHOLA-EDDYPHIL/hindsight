from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TENANT_TABLES = {
    "episodic_memories",
    "semantic_memories",
    "memory_reads",
    "semantic_memory_embeddings",
    "services",
    "incidents",
    "incident_services",
    "incident_events",
    "runbooks",
    "incident_runbooks",
    "incident_semantic_memories",
    "memory_operations",
    "mcp_audit_events",
    "agent_runs",
    "agent_run_events",
    "memory_decisions",
    "memory_namespaces",
    "semantic_beliefs",
    "memory_external_evidence",
    "incident_semantic_beliefs",
    "semantic_memory_vectors",
    "embedding_backfill_tasks",
    "memory_retrievals",
    "memory_lineage_edges",
    "agent_reflections",
    "memory_operation_previews",
    "memory_operation_events",
    "memory_operation_effects",
    "memory_review_items",
    "consolidation_jobs",
    "demo_sessions",
    "benchmark_experiments",
    "benchmark_trials",
    "benchmark_actions",
    "benchmark_confirmation_preregistrations",
    "benchmark_confirmation_bindings",
    "benchmark_variant_preparations",
    "agent_run_dispatches",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "agent_chat_messages",
}


def test_tenant_migrations_cover_every_authoritative_product_table():
    additive = (ROOT / "migrations/0019a_tenant_columns_and_outbox.sql").read_text()
    backfill = (ROOT / "migrations/0019b_tenant_backfill_and_keys.sql").read_text()

    for table in TENANT_TABLES:
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id UUID" in additive
        assert f"UPDATE {table} SET tenant_id" in backfill
        assert f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL" in backfill
        assert (
            f"ALTER TABLE {table} ALTER COLUMN tenant_id SET DEFAULT "
            "nullif(current_setting('hindsight.tenant_id', true), '')::UUID"
        ) in backfill


def test_outbox_contains_only_tenant_routing_and_sanitized_payload_fields():
    migration = (ROOT / "migrations/0019a_tenant_columns_and_outbox.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS tenant_event_outbox" in migration
    assert "tenant_id UUID NOT NULL" in migration
    assert "event_type STRING NOT NULL" in migration
    assert "aggregate_id STRING NOT NULL" in migration
    assert "topics JSONB NOT NULL" in migration
    assert "payload JSONB NOT NULL" in migration
    assert "content STRING" not in migration
    assert "embedding VECTOR" not in migration


def test_tenant_foreign_keys_rls_and_cdc_roles_are_explicit():
    catalog = (ROOT / "migrations/0019c_tenant_catalog_foreign_keys.sql").read_text()
    relationships = (ROOT / "migrations/0019d_tenant_relationship_foreign_keys.sql").read_text()
    policies = (ROOT / "migrations/0019f_tenant_row_level_security.sql").read_text()
    roles = (ROOT / "infra/db/roles.sql").read_text()

    for table in TENANT_TABLES:
        assert f"ALTER TABLE {table}" in catalog
        assert f"CREATE POLICY {table}_tenant_permissive" in policies
        assert f"CREATE POLICY {table}_tenant_fence" in policies
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in policies
    assert "FOREIGN KEY (tenant_id, incident_id)" in relationships
    assert "FOREIGN KEY (tenant_id, memory_id)" in relationships
    assert "ALTER ROLE hindsight_agent_writer NOBYPASSRLS" in roles
    assert "ALTER ROLE hindsight_memory_worker NOBYPASSRLS" in roles
    assert "GRANT INSERT ON TABLE tenant_event_outbox" in roles
    assert "GRANT SELECT, CHANGEFEED ON TABLE tenant_event_outbox TO hindsight_cdc" in roles
