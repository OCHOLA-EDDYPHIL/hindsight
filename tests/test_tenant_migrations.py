import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_future_tenant_natural_key_migration_is_restart_safe():
    migration = (ROOT / "migrations" / "0022_tenant_natural_keys.sql").read_text()

    for index in (
        "services@services_slug_key",
        "incidents@incidents_slug_key",
        "runbooks@runbooks_slug_key",
        "demo_sessions@demo_sessions_namespace_key",
    ):
        assert f"DROP INDEX IF EXISTS {index} CASCADE" in migration
    for index in (
        "services_tenant_slug_idx",
        "incidents_tenant_slug_idx",
        "runbooks_tenant_slug_idx",
        "demo_sessions_tenant_namespace_idx",
    ):
        assert f"CREATE UNIQUE INDEX IF NOT EXISTS {index}" in migration


def test_governed_operation_idempotency_is_tenant_scoped_and_restart_safe():
    migration = (ROOT / "migrations" / "0031_tenant_memory_operation_idempotency.sql").read_text()
    source = (ROOT / "src" / "hindsight" / "operations.py").read_text()

    assert (
        "DROP INDEX IF EXISTS memory_operations@memory_operations_idempotency_idx;"
        in migration
    )
    assert "CASCADE" not in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS memory_operations_tenant_idempotency_idx" in migration
    assert "ON memory_operations (tenant_id, idempotency_key)" in migration
    assert "WHERE idempotency_key IS NOT NULL" in migration
    assert source.count("tenant_id = current_hindsight_tenant_id()") >= 3
    assert "id, tenant_id, operation_type" in source
    assert "VALUES (%s, current_hindsight_tenant_id()" in source
    assert "ON CONFLICT (tenant_id, idempotency_key)" in source


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

LIFECYCLE_TENANT_TABLES = TENANT_TABLES | {
    "agent_run_dispatch_attempts",
    "learning_evidence_records",
    "learning_execution_authorizations",
    "learning_protocol_authorizations",
    "learning_qualification_attempts",
    "learning_qualification_family_terminals",
    "product_principal_roles",
    "tenant_event_outbox",
}

LIFECYCLE_EXTENSION_TABLES = {"product_credential_locators"}
ALL_LIFECYCLE_TENANT_TABLES = LIFECYCLE_TENANT_TABLES | LIFECYCLE_EXTENSION_TABLES


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

    assert "ADD CONSTRAINT " not in relationships.replace("ADD CONSTRAINT IF NOT EXISTS ", "")
    assert relationships.count("ADD CONSTRAINT IF NOT EXISTS") == relationships.count("FOREIGN KEY")


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
        "benchmark_preregistration_update_immutable": ("benchmark_confirmation_preregistrations"),
        "benchmark_confirmation_binding_recorded": ("benchmark_confirmation_preregistrations"),
        "benchmark_confirmation_binding_update_immutable": ("benchmark_confirmation_bindings"),
        "benchmark_variant_preparation_update_immutable": ("benchmark_variant_preparations"),
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


def test_tenant_lifecycle_catalog_cascade_and_freeze_cover_every_tenant_table():
    cascades = (ROOT / "migrations/0029b_tenant_lifecycle_cascades.sql").read_text()
    guards = (ROOT / "migrations/0029d_tenant_lifecycle_guards.sql").read_text()

    cascade_tables = set(
        re.findall(
            r"ALTER TABLE (\w+)\s+ADD CONSTRAINT IF NOT EXISTS "
            r"\w+_lifecycle_tenant_fk",
            cascades,
        )
    )
    catalog_tables = set(
        re.findall(
            r"\('([^']+)', 'tenant_owned', 'tenant_id', \d+, true\)",
            guards,
        )
    )
    guarded_tables = set(
        re.findall(
            r"CREATE TRIGGER \w+_tenant_lifecycle_state\s+"
            r"BEFORE INSERT OR UPDATE OR DELETE\s+ON (\w+)",
            guards,
        )
    )

    assert len(LIFECYCLE_TENANT_TABLES) == 50
    assert cascade_tables == LIFECYCLE_TENANT_TABLES
    assert catalog_tables == LIFECYCLE_TENANT_TABLES
    assert guarded_tables == LIFECYCLE_TENANT_TABLES
    assert cascades.count("ON DELETE CASCADE") == 50


def test_tenant_lifecycle_control_state_is_fenced_and_tombstones_are_non_sensitive():
    control = (ROOT / "migrations/0029a_tenant_lifecycle_control.sql").read_text()
    guards = (ROOT / "migrations/0029d_tenant_lifecycle_guards.sql").read_text()
    roles = (ROOT / "infra/db/roles.sql").read_text()

    assert "updated_at TIMESTAMPTZ\n    NOT NULL DEFAULT now()" in control
    for status in (
        "pending_export",
        "exporting",
        "exported",
        "verified",
        "purging",
        "database_purged",
        "completed",
        "failed",
        "aborted",
    ):
        assert f"'{status}'" in control
    assert "confirmed_export_fingerprint = export_fingerprint" in control
    assert "jsonb_typeof(principal_hashes) = 'array'" in control

    tombstone = control.split("CREATE TABLE IF NOT EXISTS tenant_purge_tombstones", 1)[1]
    tombstone = tombstone.split("CREATE TABLE IF NOT EXISTS tenant_lifecycle_tables", 1)[0]
    assert "tenant_identity_sha256" in tombstone
    assert "export_fingerprint" in tombstone
    assert "target_tenant_id" not in tombstone
    assert "principal_hashes" not in tombstone
    assert "export_bucket" not in tombstone
    assert "export_data_key" not in tombstone

    assert (
        "operation.purge_confirmed_at\n                    <= operation.export_retention_until"
    ) in guards
    assert "operation.confirmed_export_fingerprint" in guards
    assert "operation.lease_expires_at > now()" in guards
    assert "tenant-owned row identity is immutable" in guards
    assert "tenant identity is immutable" in guards
    assert "tenant root fields are frozen during lifecycle processing" in guards
    assert "CREATE OR REPLACE FUNCTION current_hindsight_lifecycle_role_member" in guards
    explicit_role_helper = guards.split(
        "CREATE OR REPLACE FUNCTION current_hindsight_lifecycle_role_member", 1
    )[1].split("$$;", 1)[0]
    assert "pg_catalog.pg_auth_members" in explicit_role_helper
    assert "member_role.rolname = session_user" in explicit_role_helper
    assert "pg_has_role" not in explicit_role_helper
    assert "CREATE OR REPLACE VIEW tenant_lifecycle_schema_change_blockers" in guards
    blocker_view = guards.split(
        "CREATE OR REPLACE VIEW tenant_lifecycle_schema_change_blockers", 1
    )[1].split(";", 1)[0]
    assert "status IN ('purging', 'database_purged')" in blocker_view
    assert "target_tenant_id" not in blocker_view
    assert "tenant_identity_sha256" not in blocker_view
    helper = guards.split(
        "CREATE OR REPLACE FUNCTION current_hindsight_lifecycle_purge_allowed", 1
    )[1].split("$$;", 1)[0]
    assert "SECURITY DEFINER" in helper
    assert "CREATE OR REPLACE VIEW tenant_lifecycle_completeness_issues" in guards
    for issue in (
        "unclassified_table",
        "tenant_column_not_tenant_owned",
        "catalog_table_missing",
        "primary_key_missing",
        "tenant_cascade_missing",
    ):
        assert issue in guards

    assert "CREATE ROLE IF NOT EXISTS hindsight_lifecycle NOLOGIN" in roles
    assert "ALTER ROLE hindsight_lifecycle NOBYPASSRLS" in roles
    assert "GRANT UPDATE, DELETE ON TABLE tenants TO hindsight_lifecycle" in roles
    assert "GRANT SELECT, INSERT ON TABLE tenant_purge_tombstones" in roles
    assert "GRANT INSERT ON TABLE tenant_event_outbox TO hindsight_lifecycle" in roles
    assert "tenant_lifecycle_schema_change_blockers" in roles
    lifecycle_update_grant = roles.split("GRANT UPDATE ON TABLE\n    agent_reflections", 1)[
        1
    ].split("TO hindsight_lifecycle;", 1)[0]
    for table in ("agent_runs", "incidents", "memory_decisions"):
        assert table in lifecycle_update_grant
    assert "GRANT DELETE ON ALL TABLES" not in roles
    lifecycle_role = roles.split("-- The lifecycle role", 1)[1]
    for table in ALL_LIFECYCLE_TENANT_TABLES:
        assert table in lifecycle_role
    lifecycle_deletes = lifecycle_role.split("GRANT DELETE ON TABLE", 1)[1].split(
        "TO hindsight_lifecycle;", 1
    )[0]
    granted_delete_tables = set(re.findall(r"^\s+(\w+),?$", lifecycle_deletes, flags=re.MULTILINE))
    assert granted_delete_tables == ALL_LIFECYCLE_TENANT_TABLES


def test_product_credential_locators_are_private_exported_and_purge_fenced():
    migration = (ROOT / "migrations/0029e_product_credential_locators.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS product_credential_locators" in migration
    assert "cognito_username STRING(128) NOT NULL" in migration
    assert "UNIQUE (user_pool_id, cognito_username)" in migration
    assert "REFERENCES tenants (id) ON DELETE CASCADE" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "product_credential_locators_tenant_lifecycle_state" in migration
    assert "'product_credential_locators', 'tenant_owned', 'tenant_id', 60, true" in migration
    assert "cognito_credential_locators JSONB" in migration
    assert "cleanup_targets_captured_at TIMESTAMPTZ" in migration
    purge_helper = migration.split(
        "CREATE OR REPLACE FUNCTION current_hindsight_lifecycle_purge_allowed", 1
    )[1].split("$$;", 1)[0]
    assert "operation.cleanup_targets_captured_at IS NOT NULL" in purge_helper
    assert "current_hindsight_lifecycle_role_member()" in purge_helper
    assert "pg_has_role" not in purge_helper
    assert "SECURITY DEFINER" in purge_helper
    assert "REVOKE ALL ON TABLE product_credential_locators" in migration
    assert "GRANT SELECT, DELETE ON TABLE product_credential_locators" in migration


def test_tenant_lifecycle_purge_suppresses_orphan_outbox_events_and_preserves_guards():
    guards = (ROOT / "migrations/0029d_tenant_lifecycle_guards.sql").read_text()

    assert "TG_OP = 'DELETE'" in guards
    assert "current_hindsight_lifecycle_purge_allowed(event_tenant)" in guards
    assert guards.count("FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox()") == 5
    assert guards.count("EXECUTE FUNCTION guard_immutable_tenant_delete()") == 11
    assert "immutable tenant records can be deleted only" in guards
    assert "tenant deletion requires a verified fenced lifecycle purge" in guards
    assert "lifecycle deletes require a verified active purge fence" in guards
    assert "lifecycle row mutations require a verified active purge fence" in guards
    assert "CREATE OR REPLACE FUNCTION guard_tenant_purge_identity()" in guards
    assert "purged tenant identities cannot be recreated" in guards
    assert ") || (NEW).id::STRING::BYTES" in guards
