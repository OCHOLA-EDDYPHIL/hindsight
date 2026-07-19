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


def test_relationship_foreign_keys_are_restart_safe_after_schema_retries():
    relationships = (ROOT / "migrations/0019d_tenant_relationship_foreign_keys.sql").read_text()

    assert "ADD CONSTRAINT " not in relationships.replace(
        "ADD CONSTRAINT IF NOT EXISTS ", ""
    )
    assert relationships.count("ADD CONSTRAINT IF NOT EXISTS") == relationships.count(
        "FOREIGN KEY"
    )


def test_realtime_outbox_upgrade_recreates_active_triggers_around_function_change():
    migration = (ROOT / "migrations/0021_outbox_realtime_payload.sql").read_text()
    trigger_names = {
        "incidents_tenant_event_outbox": "incidents",
        "semantic_memories_tenant_event_outbox": "semantic_memories",
        "memory_operations_tenant_event_outbox": "memory_operations",
        "agent_runs_tenant_event_outbox": "agent_runs",
        "agent_run_events_tenant_event_outbox": "agent_run_events",
    }

    function_position = migration.index("CREATE OR REPLACE FUNCTION")
    for trigger, table in trigger_names.items():
        drop = f"DROP TRIGGER IF EXISTS {trigger} ON {table};"
        create = f"CREATE TRIGGER {trigger}"
        assert migration.index(drop) < function_position
        assert migration.index(create) > function_position


def test_tenant_backfill_restores_benchmark_update_guards_after_legacy_assignment():
    migration = (ROOT / "migrations/0019b_tenant_backfill_and_keys.sql").read_text()
    trigger_tables = {
        "benchmark_experiment_contract_immutable": "benchmark_experiments",
        "benchmark_trial_trace_immutable": "benchmark_trials",
        "benchmark_action_trace_immutable": "benchmark_actions",
        "benchmark_preregistration_update_immutable": (
            "benchmark_confirmation_preregistrations"
        ),
        "benchmark_confirmation_binding_recorded": (
            "benchmark_confirmation_preregistrations"
        ),
        "benchmark_confirmation_binding_update_immutable": (
            "benchmark_confirmation_bindings"
        ),
        "benchmark_variant_preparation_update_immutable": (
            "benchmark_variant_preparations"
        ),
    }

    first_backfill = migration.index("UPDATE episodic_memories SET tenant_id")
    last_backfill = migration.index("UPDATE agent_chat_messages SET tenant_id")
    first_constraint = migration.index(
        "ALTER TABLE episodic_memories ALTER COLUMN tenant_id SET NOT NULL"
    )
    for trigger, table in trigger_tables.items():
        drop = f"DROP TRIGGER IF EXISTS {trigger}"
        create = f"CREATE TRIGGER {trigger}"
        assert migration.index(drop) < first_backfill
        assert last_backfill < migration.index(create) < first_constraint
        assert f"ON {table};" in migration[migration.index(drop) : first_backfill]
