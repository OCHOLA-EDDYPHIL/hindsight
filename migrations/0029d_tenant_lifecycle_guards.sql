-- Bind tenant lifecycle state to row writes, immutable-record deletion, and
-- the single tenant-catalog purge path.

CREATE ROLE IF NOT EXISTS hindsight_lifecycle NOLOGIN;
ALTER ROLE hindsight_lifecycle NOLOGIN;
ALTER ROLE hindsight_lifecycle NOBYPASSRLS;

-- pg_has_role() reports true for CockroachDB's root/admin identity even when
-- that identity was never granted the lifecycle role. Authorization must be
-- based on an explicit role edge so ordinary administration and fixture setup
-- cannot accidentally acquire (or be constrained by) purge semantics.
CREATE OR REPLACE FUNCTION current_hindsight_lifecycle_role_member()
RETURNS BOOL
LANGUAGE SQL
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS granted_role
          ON granted_role.oid = membership.roleid
        JOIN pg_catalog.pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE granted_role.rolname = 'hindsight_lifecycle'
          AND member_role.rolname = session_user
    )
$$;

CREATE OR REPLACE FUNCTION current_hindsight_lifecycle_purge_allowed(
    row_tenant_id UUID
)
RETURNS BOOL
LANGUAGE SQL
STABLE
SECURITY DEFINER
AS $$
    SELECT current_hindsight_lifecycle_role_member()
        AND EXISTS (
            SELECT 1
            FROM public.tenant_lifecycle_operations AS operation
            WHERE operation.id = nullif(
                    current_setting('hindsight.lifecycle_operation_id', true),
                    ''
                )::UUID
              AND operation.lease_owner = nullif(
                    current_setting('hindsight.lifecycle_lease_owner', true),
                    ''
                )::UUID
              AND operation.target_tenant_id = row_tenant_id
              AND operation.status = 'purging'
              AND operation.lease_expires_at > now()
              AND operation.export_verified_at IS NOT NULL
              AND operation.confirmed_export_fingerprint
                    = operation.export_fingerprint
              AND operation.purge_confirmed_at IS NOT NULL
              AND operation.purge_confirmed_at
                    <= operation.export_retention_until
        )
$$;

CREATE OR REPLACE FUNCTION guard_tenant_lifecycle_row_state()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    row_tenant_id UUID;
    tenant_state STRING;
BEGIN
    IF TG_OP = 'DELETE' THEN
        row_tenant_id := (to_jsonb(OLD)->>'tenant_id')::UUID;
        IF current_hindsight_lifecycle_purge_allowed(row_tenant_id) THEN
            RETURN OLD;
        END IF;
        IF current_hindsight_lifecycle_role_member() THEN
            RAISE EXCEPTION 'lifecycle deletes require a verified active purge fence';
        END IF;
        SELECT status INTO tenant_state FROM tenants WHERE id = row_tenant_id;
        IF tenant_state = 'active' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'tenant-owned rows are frozen for lifecycle processing';
    END IF;

    row_tenant_id := (to_jsonb(NEW)->>'tenant_id')::UUID;
    IF TG_OP = 'UPDATE'
        AND (to_jsonb(OLD)->>'tenant_id')::UUID IS DISTINCT FROM row_tenant_id
    THEN
        RAISE EXCEPTION 'tenant-owned row identity is immutable';
    END IF;
    IF current_hindsight_lifecycle_role_member() THEN
        IF TG_OP = 'UPDATE'
            AND current_hindsight_lifecycle_purge_allowed(row_tenant_id)
        THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'lifecycle row mutations require a verified active purge fence';
    END IF;
    SELECT status INTO tenant_state FROM tenants WHERE id = row_tenant_id;
    IF tenant_state IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'tenant-owned rows are frozen for lifecycle processing';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION guard_immutable_tenant_delete()
RETURNS TRIGGER
LANGUAGE PLpgSQL
SECURITY DEFINER
AS $$
BEGIN
    IF current_hindsight_lifecycle_purge_allowed((to_jsonb(OLD)->>'tenant_id')::UUID) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'immutable tenant records can be deleted only by a fenced lifecycle purge';
END
$$;

CREATE OR REPLACE FUNCTION guard_tenant_lifecycle_status_transition()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    operation_tenant UUID;
    operation_status STRING;
    operation_owner UUID;
    operation_lease_expires_at TIMESTAMPTZ;
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id THEN
        RAISE EXCEPTION 'tenant identity is immutable';
    END IF;

    -- Once lifecycle processing starts, the tenant catalog row is itself
    -- frozen. A lifecycle transition may change only status/updated_at; an
    -- operator cannot smuggle a slug, kind, or creation-time rewrite into the
    -- same statement that archives or purges the tenant.
    IF (
        (NEW).slug IS DISTINCT FROM (OLD).slug
        OR (NEW).tenant_kind IS DISTINCT FROM (OLD).tenant_kind
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
    ) AND (
        (OLD).status != 'active'
        OR (NEW).status IS DISTINCT FROM (OLD).status
    ) THEN
        RAISE EXCEPTION 'tenant root fields are frozen during lifecycle processing';
    END IF;

    IF (NEW).status IS NOT DISTINCT FROM (OLD).status
        AND (OLD).status = 'active'
        AND NOT current_hindsight_lifecycle_role_member()
    THEN
        RETURN NEW;
    END IF;
    IF NOT current_hindsight_lifecycle_role_member() THEN
        RAISE EXCEPTION 'tenant lifecycle status requires the lifecycle role';
    END IF;
    SELECT target_tenant_id, status, lease_owner, lease_expires_at
    INTO operation_tenant, operation_status, operation_owner,
        operation_lease_expires_at
    FROM tenant_lifecycle_operations
    WHERE id = nullif(
        current_setting('hindsight.lifecycle_operation_id', true), ''
    )::UUID;
    IF operation_tenant IS DISTINCT FROM (OLD).id THEN
        RAISE EXCEPTION 'tenant lifecycle operation does not own this tenant';
    END IF;
    IF operation_owner IS DISTINCT FROM nullif(
            current_setting('hindsight.lifecycle_lease_owner', true), ''
        )::UUID
        OR operation_lease_expires_at IS NULL
        OR operation_lease_expires_at <= now()
    THEN
        RAISE EXCEPTION 'tenant lifecycle status requires the active lease';
    END IF;
    IF (NEW).status IS NOT DISTINCT FROM (OLD).status THEN
        IF (
            ((OLD).status = 'archived' AND operation_status = 'exporting')
            OR ((OLD).status IN ('purge_pending', 'purging')
                AND operation_status = 'purging')
        ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'invalid tenant lifecycle status transition';
    END IF;
    IF NOT (
        ((OLD).status = 'active' AND (NEW).status = 'archived'
            AND operation_status = 'exporting')
        OR ((OLD).status = 'archived' AND (NEW).status = 'active'
            AND operation_status IN (
                'pending_export', 'exporting', 'exported', 'verified',
                'failed', 'aborted'
            ))
        OR ((OLD).status = 'archived' AND (NEW).status = 'purge_pending'
            AND operation_status = 'purging')
        OR ((OLD).status = 'purge_pending' AND (NEW).status = 'purging'
            AND operation_status = 'purging')
    ) THEN
        RAISE EXCEPTION 'invalid tenant lifecycle status transition';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS tenants_lifecycle_status_guard ON tenants;
CREATE TRIGGER tenants_lifecycle_status_guard
BEFORE UPDATE ON tenants
FOR EACH ROW
EXECUTE FUNCTION guard_tenant_lifecycle_status_transition();

CREATE OR REPLACE FUNCTION guard_tenant_catalog_delete()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (OLD).status != 'purging'
        OR NOT current_hindsight_lifecycle_purge_allowed((OLD).id)
    THEN
        RAISE EXCEPTION 'tenant deletion requires a verified fenced lifecycle purge';
    END IF;
    RETURN OLD;
END
$$;

DROP TRIGGER IF EXISTS tenants_lifecycle_delete_guard ON tenants;
CREATE TRIGGER tenants_lifecycle_delete_guard
BEFORE DELETE ON tenants
FOR EACH ROW
EXECUTE FUNCTION guard_tenant_catalog_delete();

CREATE OR REPLACE FUNCTION guard_tenant_purge_identity()
RETURNS TRIGGER
LANGUAGE PLpgSQL
SECURITY DEFINER
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM tenant_purge_tombstones
        WHERE tenant_identity_sha256 = sha256(
            decode(
                '68696e6473696768742e74656e616e742d70757267652e763100',
                'hex'
            ) || (NEW).id::STRING::BYTES
        )
    ) THEN
        RAISE EXCEPTION 'purged tenant identities cannot be recreated';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS tenants_purge_identity_guard ON tenants;
CREATE TRIGGER tenants_purge_identity_guard
BEFORE INSERT ON tenants
FOR EACH ROW
EXECUTE FUNCTION guard_tenant_purge_identity();

CREATE OR REPLACE FUNCTION guard_tenant_purge_tombstone_immutable()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    RAISE EXCEPTION 'tenant purge tombstones are immutable';
END
$$;

DROP TRIGGER IF EXISTS tenant_purge_tombstone_immutable ON tenant_purge_tombstones;
CREATE TRIGGER tenant_purge_tombstone_immutable
BEFORE UPDATE OR DELETE ON tenant_purge_tombstones
FOR EACH ROW
EXECUTE FUNCTION guard_tenant_purge_tombstone_immutable();

INSERT INTO tenant_lifecycle_tables (
    table_name, table_class, tenant_column, export_order,
    purge_via_tenant_cascade
) VALUES
    ('tenants', 'tenant_root', 'id', 0, true),
    ('agent_chat_messages', 'tenant_owned', 'tenant_id', 10, true),
    ('agent_reflections', 'tenant_owned', 'tenant_id', 11, true),
    ('agent_run_dispatch_attempts', 'tenant_owned', 'tenant_id', 12, true),
    ('agent_run_dispatches', 'tenant_owned', 'tenant_id', 13, true),
    ('agent_run_events', 'tenant_owned', 'tenant_id', 14, true),
    ('agent_runs', 'tenant_owned', 'tenant_id', 15, true),
    ('benchmark_actions', 'tenant_owned', 'tenant_id', 16, true),
    ('benchmark_confirmation_bindings', 'tenant_owned', 'tenant_id', 17, true),
    ('benchmark_confirmation_preregistrations', 'tenant_owned', 'tenant_id', 18, true),
    ('benchmark_experiments', 'tenant_owned', 'tenant_id', 19, true),
    ('benchmark_trials', 'tenant_owned', 'tenant_id', 20, true),
    ('benchmark_variant_preparations', 'tenant_owned', 'tenant_id', 21, true),
    ('checkpoint_blobs', 'tenant_owned', 'tenant_id', 22, true),
    ('checkpoint_writes', 'tenant_owned', 'tenant_id', 23, true),
    ('checkpoints', 'tenant_owned', 'tenant_id', 24, true),
    ('consolidation_jobs', 'tenant_owned', 'tenant_id', 25, true),
    ('demo_sessions', 'tenant_owned', 'tenant_id', 26, true),
    ('embedding_backfill_tasks', 'tenant_owned', 'tenant_id', 27, true),
    ('episodic_memories', 'tenant_owned', 'tenant_id', 28, true),
    ('incident_events', 'tenant_owned', 'tenant_id', 29, true),
    ('incident_runbooks', 'tenant_owned', 'tenant_id', 30, true),
    ('incident_semantic_beliefs', 'tenant_owned', 'tenant_id', 31, true),
    ('incident_semantic_memories', 'tenant_owned', 'tenant_id', 32, true),
    ('incident_services', 'tenant_owned', 'tenant_id', 33, true),
    ('incidents', 'tenant_owned', 'tenant_id', 34, true),
    ('learning_evidence_records', 'tenant_owned', 'tenant_id', 35, true),
    ('learning_execution_authorizations', 'tenant_owned', 'tenant_id', 36, true),
    ('learning_protocol_authorizations', 'tenant_owned', 'tenant_id', 37, true),
    ('learning_qualification_attempts', 'tenant_owned', 'tenant_id', 38, true),
    ('learning_qualification_family_terminals', 'tenant_owned', 'tenant_id', 39, true),
    ('mcp_audit_events', 'tenant_owned', 'tenant_id', 40, true),
    ('memory_decisions', 'tenant_owned', 'tenant_id', 41, true),
    ('memory_external_evidence', 'tenant_owned', 'tenant_id', 42, true),
    ('memory_lineage_edges', 'tenant_owned', 'tenant_id', 43, true),
    ('memory_namespaces', 'tenant_owned', 'tenant_id', 44, true),
    ('memory_operation_effects', 'tenant_owned', 'tenant_id', 45, true),
    ('memory_operation_events', 'tenant_owned', 'tenant_id', 46, true),
    ('memory_operation_previews', 'tenant_owned', 'tenant_id', 47, true),
    ('memory_operations', 'tenant_owned', 'tenant_id', 48, true),
    ('memory_reads', 'tenant_owned', 'tenant_id', 49, true),
    ('memory_retrievals', 'tenant_owned', 'tenant_id', 50, true),
    ('memory_review_items', 'tenant_owned', 'tenant_id', 51, true),
    ('product_principal_roles', 'tenant_owned', 'tenant_id', 52, true),
    ('runbooks', 'tenant_owned', 'tenant_id', 53, true),
    ('semantic_beliefs', 'tenant_owned', 'tenant_id', 54, true),
    ('semantic_memories', 'tenant_owned', 'tenant_id', 55, true),
    ('semantic_memory_embeddings', 'tenant_owned', 'tenant_id', 56, true),
    ('semantic_memory_vectors', 'tenant_owned', 'tenant_id', 57, true),
    ('services', 'tenant_owned', 'tenant_id', 58, true),
    ('tenant_event_outbox', 'tenant_owned', 'tenant_id', 59, true),
    ('app_meta', 'global', NULL, NULL, false),
    ('checkpoint_migrations', 'global', NULL, NULL, false),
    ('embedding_index_state', 'global', NULL, NULL, false),
    ('embedding_index_write_fence', 'global', NULL, NULL, false),
    ('embedding_profiles', 'global', NULL, NULL, false),
    ('schema_migrations', 'global', NULL, NULL, false),
    ('tenant_lifecycle_operations', 'control', NULL, NULL, false),
    ('tenant_lifecycle_tables', 'control', NULL, NULL, false),
    ('tenant_purge_tombstones', 'control', NULL, NULL, false)
ON CONFLICT (table_name) DO UPDATE SET
    table_class = excluded.table_class,
    tenant_column = excluded.tenant_column,
    export_order = excluded.export_order,
    purge_via_tenant_cascade = excluded.purge_via_tenant_cascade;

DROP TRIGGER IF EXISTS agent_chat_messages_tenant_lifecycle_state ON agent_chat_messages;
CREATE TRIGGER agent_chat_messages_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON agent_chat_messages FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS agent_reflections_tenant_lifecycle_state ON agent_reflections;
CREATE TRIGGER agent_reflections_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON agent_reflections FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS agent_run_dispatch_attempts_tenant_lifecycle_state
ON agent_run_dispatch_attempts;
CREATE TRIGGER agent_run_dispatch_attempts_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON agent_run_dispatch_attempts
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS agent_run_dispatches_tenant_lifecycle_state ON agent_run_dispatches;
CREATE TRIGGER agent_run_dispatches_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON agent_run_dispatches FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS agent_run_events_tenant_lifecycle_state ON agent_run_events;
CREATE TRIGGER agent_run_events_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON agent_run_events FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS agent_runs_tenant_lifecycle_state ON agent_runs;
CREATE TRIGGER agent_runs_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON agent_runs FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS benchmark_actions_tenant_lifecycle_state ON benchmark_actions;
CREATE TRIGGER benchmark_actions_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON benchmark_actions FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS benchmark_confirmation_bindings_tenant_lifecycle_state
ON benchmark_confirmation_bindings;
CREATE TRIGGER benchmark_confirmation_bindings_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON benchmark_confirmation_bindings
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS benchmark_preregistrations_tenant_lifecycle_state
ON benchmark_confirmation_preregistrations;
CREATE TRIGGER benchmark_preregistrations_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON benchmark_confirmation_preregistrations
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS benchmark_experiments_tenant_lifecycle_state ON benchmark_experiments;
CREATE TRIGGER benchmark_experiments_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON benchmark_experiments FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS benchmark_trials_tenant_lifecycle_state ON benchmark_trials;
CREATE TRIGGER benchmark_trials_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON benchmark_trials FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS benchmark_preparations_tenant_lifecycle_state
ON benchmark_variant_preparations;
CREATE TRIGGER benchmark_preparations_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON benchmark_variant_preparations
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS checkpoint_blobs_tenant_lifecycle_state ON checkpoint_blobs;
CREATE TRIGGER checkpoint_blobs_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON checkpoint_blobs FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS checkpoint_writes_tenant_lifecycle_state ON checkpoint_writes;
CREATE TRIGGER checkpoint_writes_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON checkpoint_writes FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS checkpoints_tenant_lifecycle_state ON checkpoints;
CREATE TRIGGER checkpoints_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON checkpoints FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS consolidation_jobs_tenant_lifecycle_state ON consolidation_jobs;
CREATE TRIGGER consolidation_jobs_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON consolidation_jobs FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS demo_sessions_tenant_lifecycle_state ON demo_sessions;
CREATE TRIGGER demo_sessions_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON demo_sessions FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS embedding_backfill_tasks_tenant_lifecycle_state
ON embedding_backfill_tasks;
CREATE TRIGGER embedding_backfill_tasks_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON embedding_backfill_tasks
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS episodic_memories_tenant_lifecycle_state ON episodic_memories;
CREATE TRIGGER episodic_memories_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON episodic_memories FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS incident_events_tenant_lifecycle_state ON incident_events;
CREATE TRIGGER incident_events_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON incident_events FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS incident_runbooks_tenant_lifecycle_state ON incident_runbooks;
CREATE TRIGGER incident_runbooks_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON incident_runbooks FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS incident_semantic_beliefs_tenant_lifecycle_state
ON incident_semantic_beliefs;
CREATE TRIGGER incident_semantic_beliefs_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON incident_semantic_beliefs
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS incident_semantic_memories_tenant_lifecycle_state
ON incident_semantic_memories;
CREATE TRIGGER incident_semantic_memories_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON incident_semantic_memories
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS incident_services_tenant_lifecycle_state ON incident_services;
CREATE TRIGGER incident_services_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON incident_services FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS incidents_tenant_lifecycle_state ON incidents;
CREATE TRIGGER incidents_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON incidents FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS learning_evidence_records_tenant_lifecycle_state
ON learning_evidence_records;
CREATE TRIGGER learning_evidence_records_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON learning_evidence_records
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS learning_execution_authorizations_tenant_lifecycle_state
ON learning_execution_authorizations;
CREATE TRIGGER learning_execution_authorizations_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON learning_execution_authorizations
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS learning_protocol_authorizations_tenant_lifecycle_state
ON learning_protocol_authorizations;
CREATE TRIGGER learning_protocol_authorizations_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON learning_protocol_authorizations
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS learning_qualification_attempts_tenant_lifecycle_state
ON learning_qualification_attempts;
CREATE TRIGGER learning_qualification_attempts_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON learning_qualification_attempts
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS learning_qualification_terminals_tenant_lifecycle_state
ON learning_qualification_family_terminals;
CREATE TRIGGER learning_qualification_terminals_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON learning_qualification_family_terminals
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS mcp_audit_events_tenant_lifecycle_state ON mcp_audit_events;
CREATE TRIGGER mcp_audit_events_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON mcp_audit_events FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_decisions_tenant_lifecycle_state ON memory_decisions;
CREATE TRIGGER memory_decisions_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON memory_decisions FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_external_evidence_tenant_lifecycle_state
ON memory_external_evidence;
CREATE TRIGGER memory_external_evidence_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON memory_external_evidence
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_lineage_edges_tenant_lifecycle_state ON memory_lineage_edges;
CREATE TRIGGER memory_lineage_edges_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON memory_lineage_edges FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_namespaces_tenant_lifecycle_state ON memory_namespaces;
CREATE TRIGGER memory_namespaces_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON memory_namespaces FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_operation_effects_tenant_lifecycle_state
ON memory_operation_effects;
CREATE TRIGGER memory_operation_effects_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON memory_operation_effects
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_operation_events_tenant_lifecycle_state
ON memory_operation_events;
CREATE TRIGGER memory_operation_events_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON memory_operation_events
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_operation_previews_tenant_lifecycle_state
ON memory_operation_previews;
CREATE TRIGGER memory_operation_previews_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON memory_operation_previews
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_operations_tenant_lifecycle_state ON memory_operations;
CREATE TRIGGER memory_operations_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON memory_operations FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_reads_tenant_lifecycle_state ON memory_reads;
CREATE TRIGGER memory_reads_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON memory_reads FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_retrievals_tenant_lifecycle_state ON memory_retrievals;
CREATE TRIGGER memory_retrievals_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON memory_retrievals FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS memory_review_items_tenant_lifecycle_state ON memory_review_items;
CREATE TRIGGER memory_review_items_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON memory_review_items FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS product_principal_roles_tenant_lifecycle_state
ON product_principal_roles;
CREATE TRIGGER product_principal_roles_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON product_principal_roles
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS runbooks_tenant_lifecycle_state ON runbooks;
CREATE TRIGGER runbooks_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON runbooks FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS semantic_beliefs_tenant_lifecycle_state ON semantic_beliefs;
CREATE TRIGGER semantic_beliefs_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON semantic_beliefs FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS semantic_memories_tenant_lifecycle_state ON semantic_memories;
CREATE TRIGGER semantic_memories_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON semantic_memories FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS semantic_memory_embeddings_tenant_lifecycle_state
ON semantic_memory_embeddings;
CREATE TRIGGER semantic_memory_embeddings_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON semantic_memory_embeddings
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS semantic_memory_vectors_tenant_lifecycle_state
ON semantic_memory_vectors;
CREATE TRIGGER semantic_memory_vectors_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON semantic_memory_vectors
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS services_tenant_lifecycle_state ON services;
CREATE TRIGGER services_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON services FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();
DROP TRIGGER IF EXISTS tenant_event_outbox_tenant_lifecycle_state ON tenant_event_outbox;
CREATE TRIGGER tenant_event_outbox_tenant_lifecycle_state BEFORE INSERT OR UPDATE OR DELETE
ON tenant_event_outbox FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();

-- Preserve every update/insert immutability rule while routing DELETE through
-- the verified lifecycle fence.
DROP TRIGGER IF EXISTS benchmark_preregistration_delete_immutable
ON benchmark_confirmation_preregistrations;
CREATE TRIGGER benchmark_preregistration_delete_immutable
BEFORE DELETE ON benchmark_confirmation_preregistrations
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS benchmark_confirmation_binding_delete_immutable
ON benchmark_confirmation_bindings;
CREATE TRIGGER benchmark_confirmation_binding_delete_immutable
BEFORE DELETE ON benchmark_confirmation_bindings
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS benchmark_experiment_delete_immutable ON benchmark_experiments;
CREATE TRIGGER benchmark_experiment_delete_immutable
BEFORE DELETE ON benchmark_experiments
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS benchmark_trial_delete_immutable ON benchmark_trials;
CREATE TRIGGER benchmark_trial_delete_immutable
BEFORE DELETE ON benchmark_trials
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS benchmark_action_delete_immutable ON benchmark_actions;
CREATE TRIGGER benchmark_action_delete_immutable
BEFORE DELETE ON benchmark_actions
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS benchmark_variant_preparation_delete_immutable
ON benchmark_variant_preparations;
CREATE TRIGGER benchmark_variant_preparation_delete_immutable
BEFORE DELETE ON benchmark_variant_preparations
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS learning_protocol_authorization_immutable
ON learning_protocol_authorizations;
CREATE TRIGGER learning_protocol_authorization_immutable
BEFORE UPDATE ON learning_protocol_authorizations
FOR EACH ROW EXECUTE FUNCTION guard_append_only_learning_record();
DROP TRIGGER IF EXISTS learning_protocol_authorization_delete_immutable
ON learning_protocol_authorizations;
CREATE TRIGGER learning_protocol_authorization_delete_immutable
BEFORE DELETE ON learning_protocol_authorizations
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS learning_evidence_record_immutable ON learning_evidence_records;
CREATE TRIGGER learning_evidence_record_immutable
BEFORE UPDATE ON learning_evidence_records
FOR EACH ROW EXECUTE FUNCTION guard_append_only_learning_record();
DROP TRIGGER IF EXISTS learning_evidence_record_delete_immutable
ON learning_evidence_records;
CREATE TRIGGER learning_evidence_record_delete_immutable
BEFORE DELETE ON learning_evidence_records
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS learning_execution_authorization_guarded
ON learning_execution_authorizations;
CREATE TRIGGER learning_execution_authorization_guarded
BEFORE UPDATE ON learning_execution_authorizations
FOR EACH ROW EXECUTE FUNCTION guard_learning_execution_authorization();
DROP TRIGGER IF EXISTS learning_execution_authorization_delete_immutable
ON learning_execution_authorizations;
CREATE TRIGGER learning_execution_authorization_delete_immutable
BEFORE DELETE ON learning_execution_authorizations
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS learning_qualification_attempt_guarded
ON learning_qualification_attempts;
CREATE TRIGGER learning_qualification_attempt_guarded
BEFORE UPDATE ON learning_qualification_attempts
FOR EACH ROW EXECUTE FUNCTION guard_learning_qualification_attempt();
DROP TRIGGER IF EXISTS learning_qualification_attempt_delete_immutable
ON learning_qualification_attempts;
CREATE TRIGGER learning_qualification_attempt_delete_immutable
BEFORE DELETE ON learning_qualification_attempts
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

DROP TRIGGER IF EXISTS learning_qualification_terminal_immutable
ON learning_qualification_family_terminals;
CREATE TRIGGER learning_qualification_terminal_immutable
BEFORE UPDATE ON learning_qualification_family_terminals
FOR EACH ROW EXECUTE FUNCTION guard_append_only_learning_record();
DROP TRIGGER IF EXISTS learning_qualification_terminal_delete_immutable
ON learning_qualification_family_terminals;
CREATE TRIGGER learning_qualification_terminal_delete_immutable
BEFORE DELETE ON learning_qualification_family_terminals
FOR EACH ROW EXECUTE FUNCTION guard_immutable_tenant_delete();

-- A tenant-root cascade must not manufacture realtime events for rows that no
-- longer have a tenant. Normal row deletes retain the existing outbox behavior.
DROP TRIGGER IF EXISTS incidents_tenant_event_outbox ON incidents;
DROP TRIGGER IF EXISTS semantic_memories_tenant_event_outbox ON semantic_memories;
DROP TRIGGER IF EXISTS memory_operations_tenant_event_outbox ON memory_operations;
DROP TRIGGER IF EXISTS agent_runs_tenant_event_outbox ON agent_runs;
DROP TRIGGER IF EXISTS agent_run_events_tenant_event_outbox ON agent_run_events;

CREATE OR REPLACE FUNCTION emit_tenant_event_outbox()
RETURNS TRIGGER
LANGUAGE PLpgSQL
SECURITY DEFINER
AS $$
DECLARE
    row_data JSONB;
    previous_data JSONB;
    event_tenant UUID;
    aggregate_id STRING;
    run_id STRING;
    event_topics JSONB;
    event_payload JSONB;
BEGIN
    IF TG_OP = 'DELETE' THEN
        row_data := to_jsonb(OLD);
    ELSE
        row_data := to_jsonb(NEW);
    END IF;
    IF TG_OP = 'UPDATE' THEN
        previous_data := to_jsonb(OLD);
    ELSE
        previous_data := '{}'::JSONB;
    END IF;

    event_tenant := (row_data->>'tenant_id')::UUID;
    IF TG_OP = 'DELETE'
        AND current_hindsight_lifecycle_purge_allowed(event_tenant)
    THEN
        RETURN OLD;
    END IF;

    aggregate_id := row_data->>'id';
    run_id := CASE
        WHEN TG_TABLE_NAME = 'agent_runs' THEN aggregate_id
        ELSE row_data->>'run_id'
    END;
    event_topics := jsonb_build_array(
        'tenant:' || event_tenant::STRING || ':table:' || TG_TABLE_NAME,
        'tenant:' || event_tenant::STRING || ':aggregate:' || aggregate_id
    );
    IF run_id IS NOT NULL THEN
        event_topics := event_topics || jsonb_build_array(
            'tenant:' || event_tenant::STRING || ':run:' || run_id
        );
    END IF;
    IF row_data->>'namespace' IS NOT NULL THEN
        event_topics := event_topics || jsonb_build_array(
            'tenant:' || event_tenant::STRING || ':namespace:' || (row_data->>'namespace')
        );
    END IF;

    event_payload := jsonb_strip_nulls(jsonb_build_object(
        'id', aggregate_id,
        'run_id', run_id,
        'incident_id', CASE
            WHEN TG_TABLE_NAME = 'incidents' THEN aggregate_id
            ELSE row_data->>'incident_id'
        END,
        'status', row_data->>'status',
        'previous_status', previous_data->>'status',
        'resolution_event_id', row_data->>'resolution_event_id',
        'consolidation_policy', row_data->>'consolidation_policy',
        'operation_type', row_data->>'operation_type',
        'sequence', row_data->>'sequence',
        'updated_at', COALESCE(row_data->>'updated_at', row_data->>'created_at')
    ));

    INSERT INTO public.tenant_event_outbox (
        tenant_id, event_type, aggregate_type, aggregate_id, topics, payload
    ) VALUES (
        event_tenant,
        lower(TG_TABLE_NAME || '.' || TG_OP),
        TG_TABLE_NAME,
        aggregate_id,
        event_topics,
        event_payload
    );
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER incidents_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON incidents
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();
CREATE TRIGGER semantic_memories_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON semantic_memories
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();
CREATE TRIGGER memory_operations_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON memory_operations
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();
CREATE TRIGGER agent_runs_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON agent_runs
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();
CREATE TRIGGER agent_run_events_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON agent_run_events
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();

-- This live view is the fail-closed schema coverage assertion used before
-- every export and immediately before catalog deletion.
CREATE OR REPLACE VIEW tenant_lifecycle_completeness_issues AS
WITH public_tables AS (
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
),
tenant_columns AS (
    SELECT columns.table_name
    FROM information_schema.columns AS columns
    JOIN public_tables ON public_tables.table_name = columns.table_name
    WHERE columns.table_schema = 'public' AND columns.column_name = 'tenant_id'
),
primary_key_tables AS (
    SELECT DISTINCT table_name
    FROM information_schema.table_constraints
    WHERE table_schema = 'public' AND constraint_type = 'PRIMARY KEY'
),
cascade_tables AS (
    SELECT DISTINCT foreign_key.table_name
    FROM information_schema.table_constraints AS foreign_key
    JOIN information_schema.referential_constraints AS reference
      ON reference.constraint_schema = foreign_key.constraint_schema
     AND reference.constraint_name = foreign_key.constraint_name
    JOIN information_schema.key_column_usage AS foreign_column
      ON foreign_column.constraint_schema = foreign_key.constraint_schema
     AND foreign_column.constraint_name = foreign_key.constraint_name
    JOIN information_schema.constraint_column_usage AS target_column
      ON target_column.constraint_schema = reference.unique_constraint_schema
     AND target_column.constraint_name = reference.unique_constraint_name
    WHERE foreign_key.table_schema = 'public'
      AND foreign_key.constraint_type = 'FOREIGN KEY'
      AND foreign_column.column_name = 'tenant_id'
      AND target_column.table_schema = 'public'
      AND target_column.table_name = 'tenants'
      AND target_column.column_name = 'id'
      AND reference.delete_rule = 'CASCADE'
)
SELECT public_tables.table_name, 'unclassified_table'::STRING AS issue_code
FROM public_tables
LEFT JOIN tenant_lifecycle_tables AS catalog
  ON catalog.table_name = public_tables.table_name
WHERE catalog.table_name IS NULL
UNION ALL
SELECT tenant_columns.table_name, 'tenant_column_not_tenant_owned'::STRING
FROM tenant_columns
LEFT JOIN tenant_lifecycle_tables AS catalog
  ON catalog.table_name = tenant_columns.table_name
WHERE catalog.table_class IS DISTINCT FROM 'tenant_owned'
UNION ALL
SELECT catalog.table_name, 'catalog_table_missing'::STRING
FROM tenant_lifecycle_tables AS catalog
LEFT JOIN public_tables ON public_tables.table_name = catalog.table_name
WHERE catalog.table_class IN ('tenant_owned', 'tenant_root')
  AND public_tables.table_name IS NULL
UNION ALL
SELECT catalog.table_name, 'tenant_column_missing'::STRING
FROM tenant_lifecycle_tables AS catalog
LEFT JOIN tenant_columns ON tenant_columns.table_name = catalog.table_name
WHERE catalog.table_class = 'tenant_owned'
  AND tenant_columns.table_name IS NULL
UNION ALL
SELECT catalog.table_name, 'primary_key_missing'::STRING
FROM tenant_lifecycle_tables AS catalog
LEFT JOIN primary_key_tables ON primary_key_tables.table_name = catalog.table_name
WHERE catalog.table_class IN ('tenant_owned', 'tenant_root')
  AND primary_key_tables.table_name IS NULL
UNION ALL
SELECT catalog.table_name, 'tenant_cascade_missing'::STRING
FROM tenant_lifecycle_tables AS catalog
LEFT JOIN cascade_tables ON cascade_tables.table_name = catalog.table_name
WHERE catalog.table_class = 'tenant_owned'
  AND cascade_tables.table_name IS NULL;

-- Schema migrations and deploy preflights can fail closed with:
--   SELECT count(*) FROM tenant_lifecycle_schema_change_blockers;
-- A tenant row is intentionally absent after the database-purged checkpoint,
-- so this reports only operation metadata and no tenant identity.
CREATE OR REPLACE VIEW tenant_lifecycle_schema_change_blockers AS
SELECT
    id AS operation_id,
    status,
    lease_owner IS NOT NULL AND lease_expires_at > now() AS lease_active,
    database_purged_at,
    updated_at
FROM tenant_lifecycle_operations
WHERE status IN ('purging', 'database_purged');

REVOKE ALL ON TABLE tenant_lifecycle_operations FROM PUBLIC;
REVOKE ALL ON TABLE tenant_lifecycle_tables FROM PUBLIC;
REVOKE ALL ON TABLE tenant_purge_tombstones FROM PUBLIC;
REVOKE ALL ON TABLE tenant_lifecycle_completeness_issues FROM PUBLIC;
REVOKE ALL ON TABLE tenant_lifecycle_schema_change_blockers FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO hindsight_lifecycle;
GRANT SELECT ON TABLE
    tenants,
    agent_chat_messages,
    agent_reflections,
    agent_run_dispatch_attempts,
    agent_run_dispatches,
    agent_run_events,
    agent_runs,
    benchmark_actions,
    benchmark_confirmation_bindings,
    benchmark_confirmation_preregistrations,
    benchmark_experiments,
    benchmark_trials,
    benchmark_variant_preparations,
    checkpoint_blobs,
    checkpoint_writes,
    checkpoints,
    consolidation_jobs,
    demo_sessions,
    embedding_backfill_tasks,
    episodic_memories,
    incident_events,
    incident_runbooks,
    incident_semantic_beliefs,
    incident_semantic_memories,
    incident_services,
    incidents,
    learning_evidence_records,
    learning_execution_authorizations,
    learning_protocol_authorizations,
    learning_qualification_attempts,
    learning_qualification_family_terminals,
    mcp_audit_events,
    memory_decisions,
    memory_external_evidence,
    memory_lineage_edges,
    memory_namespaces,
    memory_operation_effects,
    memory_operation_events,
    memory_operation_previews,
    memory_operations,
    memory_reads,
    memory_retrievals,
    memory_review_items,
    product_principal_roles,
    runbooks,
    semantic_beliefs,
    semantic_memories,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    services,
    tenant_event_outbox,
    tenant_lifecycle_tables,
    tenant_lifecycle_completeness_issues,
    tenant_lifecycle_schema_change_blockers
TO hindsight_lifecycle;
-- CockroachDB requires the invoker to hold DELETE on every table in an
-- ON DELETE CASCADE. Row guards above make these privileges usable only for
-- the exact verified tenant operation and live lease.
GRANT DELETE ON TABLE
    agent_chat_messages,
    agent_reflections,
    agent_run_dispatch_attempts,
    agent_run_dispatches,
    agent_run_events,
    agent_runs,
    benchmark_actions,
    benchmark_confirmation_bindings,
    benchmark_confirmation_preregistrations,
    benchmark_experiments,
    benchmark_trials,
    benchmark_variant_preparations,
    checkpoint_blobs,
    checkpoint_writes,
    checkpoints,
    consolidation_jobs,
    demo_sessions,
    embedding_backfill_tasks,
    episodic_memories,
    incident_events,
    incident_runbooks,
    incident_semantic_beliefs,
    incident_semantic_memories,
    incident_services,
    incidents,
    learning_evidence_records,
    learning_execution_authorizations,
    learning_protocol_authorizations,
    learning_qualification_attempts,
    learning_qualification_family_terminals,
    mcp_audit_events,
    memory_decisions,
    memory_external_evidence,
    memory_lineage_edges,
    memory_namespaces,
    memory_operation_effects,
    memory_operation_events,
    memory_operation_previews,
    memory_operations,
    memory_reads,
    memory_retrievals,
    memory_review_items,
    product_principal_roles,
    runbooks,
    semantic_beliefs,
    semantic_memories,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    services,
    tenant_event_outbox
TO hindsight_lifecycle;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE tenant_lifecycle_operations
TO hindsight_lifecycle;
GRANT SELECT, INSERT ON TABLE tenant_purge_tombstones TO hindsight_lifecycle;
GRANT UPDATE, DELETE ON TABLE tenants TO hindsight_lifecycle;
-- The existing outbox trigger body contains an INSERT branch. CockroachDB
-- checks that privilege while building a cascaded DELETE even though the
-- lifecycle branch returns before the INSERT executes.
GRANT INSERT ON TABLE tenant_event_outbox TO hindsight_lifecycle;
GRANT UPDATE ON TABLE
    agent_reflections,
    agent_runs,
    incidents,
    memory_decisions
TO hindsight_lifecycle;

-- No other runtime identity may see lifecycle control state or acquire a
-- catalog-delete capability through the role template.
REVOKE ALL ON TABLE tenant_lifecycle_operations, tenant_lifecycle_tables,
    tenant_purge_tombstones, tenant_lifecycle_completeness_issues,
    tenant_lifecycle_schema_change_blockers
FROM hindsight_agent_writer, hindsight_memory_worker;
REVOKE DELETE ON TABLE tenants FROM hindsight_agent_writer,
    hindsight_memory_worker;
