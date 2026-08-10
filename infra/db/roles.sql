-- Least-privilege role template for deployed Hindsight environments.
-- Credentials are managed outside the repository. No product role receives
-- DELETE, and ordinary agent/API roles cannot UPDATE immutable memory rows.

CREATE ROLE IF NOT EXISTS hindsight_agent_writer NOLOGIN;
CREATE ROLE IF NOT EXISTS hindsight_memory_worker NOLOGIN;
CREATE ROLE IF NOT EXISTS hindsight_mcp_readonly LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_dashboard_reader LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_archive NOLOGIN;
CREATE ROLE IF NOT EXISTS hindsight_cdc NOLOGIN;
CREATE ROLE IF NOT EXISTS hindsight_lifecycle NOLOGIN;

ALTER ROLE hindsight_agent_writer NOLOGIN;
ALTER ROLE hindsight_memory_worker NOLOGIN;
ALTER ROLE hindsight_archive NOLOGIN;
ALTER ROLE hindsight_cdc NOLOGIN;
ALTER ROLE hindsight_lifecycle NOLOGIN;
ALTER ROLE hindsight_agent_writer NOBYPASSRLS;
ALTER ROLE hindsight_memory_worker NOBYPASSRLS;
ALTER ROLE hindsight_mcp_readonly NOBYPASSRLS;
ALTER ROLE hindsight_dashboard_reader NOBYPASSRLS;
ALTER ROLE hindsight_archive NOBYPASSRLS;
ALTER ROLE hindsight_cdc NOBYPASSRLS;
ALTER ROLE hindsight_lifecycle NOBYPASSRLS;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO
    hindsight_agent_writer,
    hindsight_memory_worker,
    hindsight_archive,
    hindsight_cdc,
    hindsight_lifecycle;

GRANT SELECT ON TABLE
    tenants,
    product_principal_roles,
    episodic_memories,
    semantic_memories,
    current_episodic_memories,
    current_semantic_memories,
    semantic_beliefs,
    memory_namespaces,
    embedding_profiles,
    embedding_index_state,
    embedding_index_write_fence,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    embedding_backfill_tasks,
    services,
    incidents,
    incident_services,
    incident_events,
    runbooks,
    incident_runbooks,
    incident_semantic_memories,
    incident_semantic_beliefs,
    memory_decisions,
    memory_reads,
    memory_retrievals,
    memory_operations,
    memory_operation_previews,
    memory_operation_events,
    memory_operation_effects,
    memory_review_items,
    memory_external_evidence,
    memory_lineage_edges,
    agent_reflections,
    agent_runs,
    agent_run_events,
    agent_run_dispatches,
    agent_run_dispatch_attempts,
    consolidation_jobs,
    benchmark_variant_preparations,
    benchmark_actions,
    demo_sessions
TO hindsight_agent_writer;

GRANT INSERT ON TABLE
    episodic_memories,
    semantic_memories,
    semantic_beliefs,
    memory_namespaces,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    embedding_backfill_tasks,
    memory_decisions,
    memory_reads,
    memory_retrievals,
    memory_operations,
    memory_operation_previews,
    memory_operation_events,
    memory_external_evidence,
    memory_lineage_edges,
    agent_reflections,
    agent_runs,
    agent_run_events,
    agent_run_dispatches,
    agent_run_dispatch_attempts,
    incidents,
    incident_services,
    incident_events,
    incident_semantic_memories,
    incident_semantic_beliefs,
    services,
    demo_sessions
TO hindsight_agent_writer;

GRANT UPDATE ON TABLE
    memory_namespaces,
    embedding_index_write_fence,
    memory_decisions,
    agent_runs,
    agent_run_events,
    agent_run_dispatches,
    agent_run_dispatch_attempts,
    incidents,
    services,
    incident_services,
    demo_sessions
TO hindsight_agent_writer;

-- This role is isolated to the queued correction/consolidation worker. It may
-- close versions and manage derivative indexes, but still receives no DELETE.
GRANT SELECT ON TABLE
    tenants,
    episodic_memories,
    semantic_memories,
    semantic_beliefs,
    memory_namespaces,
    embedding_profiles,
    embedding_index_state,
    embedding_index_write_fence,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    embedding_backfill_tasks,
    memory_decisions,
    memory_reads,
    memory_retrievals,
    memory_external_evidence,
    memory_lineage_edges,
    memory_operations,
    memory_operation_previews,
    memory_operation_events,
    memory_operation_effects,
    memory_review_items,
    consolidation_jobs,
    agent_reflections,
    agent_runs,
    agent_run_events,
    agent_run_dispatches,
    agent_run_dispatch_attempts,
    current_episodic_memories,
    current_semantic_memories,
    services,
    incident_semantic_memories,
    incident_semantic_beliefs,
    incidents,
    incident_services,
    incident_events,
    runbooks,
    incident_runbooks,
    benchmark_experiments,
    benchmark_trials,
    benchmark_actions,
    benchmark_variant_preparations,
    checkpoint_migrations,
    checkpoints,
    checkpoint_blobs,
    checkpoint_writes,
    agent_chat_messages
TO hindsight_memory_worker;

GRANT INSERT ON TABLE
    episodic_memories,
    semantic_memories,
    semantic_beliefs,
    memory_namespaces,
    embedding_profiles,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    embedding_backfill_tasks,
    memory_decisions,
    memory_reads,
    memory_retrievals,
    memory_external_evidence,
    memory_lineage_edges,
    memory_operations,
    memory_operation_previews,
    memory_operation_events,
    memory_operation_effects,
    memory_review_items,
    consolidation_jobs,
    agent_reflections,
    agent_runs,
    agent_run_events,
    agent_run_dispatch_attempts,
    incident_semantic_memories,
    incident_semantic_beliefs,
    incident_events,
    checkpoints,
    checkpoint_blobs,
    checkpoint_writes,
    agent_chat_messages
TO hindsight_memory_worker;

REVOKE DELETE ON ALL TABLES IN SCHEMA public
FROM hindsight_agent_writer, hindsight_memory_worker, hindsight_archive, hindsight_cdc;

GRANT UPDATE ON TABLE
    episodic_memories,
    semantic_memories,
    memory_namespaces,
    embedding_profiles,
    embedding_index_state,
    embedding_index_write_fence,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    embedding_backfill_tasks,
    memory_decisions,
    memory_operations,
    memory_review_items,
    consolidation_jobs,
    agent_runs,
    agent_run_events,
    agent_run_dispatches,
    agent_run_dispatch_attempts,
    incident_semantic_beliefs,
    incidents,
    checkpoints,
    checkpoint_blobs,
    checkpoint_writes
TO hindsight_memory_worker;

GRANT SELECT ON TABLE
    episodic_memories,
    semantic_memories,
    current_episodic_memories,
    current_semantic_memories,
    semantic_beliefs,
    memory_decisions,
    memory_reads,
    memory_retrievals,
    memory_external_evidence,
    memory_lineage_edges,
    memory_operations,
    memory_operation_events,
    memory_operation_effects,
    memory_review_items,
    agent_reflections,
    services,
    incidents,
    incident_services,
    incident_events,
    runbooks,
    incident_runbooks,
    incident_semantic_memories,
    incident_semantic_beliefs,
    mcp_audit_events
TO hindsight_mcp_readonly;

GRANT INSERT ON TABLE memory_decisions, memory_reads, mcp_audit_events
TO hindsight_mcp_readonly;

GRANT UPDATE ON TABLE memory_decisions, mcp_audit_events
TO hindsight_mcp_readonly;

GRANT SELECT ON TABLE
    semantic_memories,
    current_semantic_memories,
    memory_decisions,
    memory_reads,
    memory_retrievals,
    memory_operations,
    memory_operation_events,
    memory_operation_effects,
    memory_review_items,
    agent_reflections,
    benchmark_experiments,
    benchmark_trials,
    benchmark_actions
TO hindsight_dashboard_reader;

GRANT SELECT ON TABLE
    tenants,
    episodic_memories,
    semantic_memories,
    memory_reads,
    semantic_memory_embeddings,
    services,
    incidents,
    incident_services,
    incident_events,
    runbooks,
    incident_runbooks,
    incident_semantic_memories,
    memory_operations,
    mcp_audit_events,
    agent_runs,
    agent_run_events,
    memory_decisions,
    memory_namespaces,
    semantic_beliefs,
    memory_external_evidence,
    incident_semantic_beliefs,
    semantic_memory_vectors,
    embedding_backfill_tasks,
    memory_retrievals,
    memory_lineage_edges,
    agent_reflections,
    memory_operation_previews,
    memory_operation_events,
    memory_operation_effects,
    memory_review_items,
    consolidation_jobs,
    demo_sessions,
    benchmark_experiments,
    benchmark_trials,
    benchmark_actions,
    benchmark_confirmation_preregistrations,
    benchmark_confirmation_bindings,
    benchmark_variant_preparations,
    learning_protocol_authorizations,
    learning_execution_authorizations,
    learning_evidence_records,
    agent_run_dispatches,
    agent_run_dispatch_attempts,
    checkpoints,
    checkpoint_blobs,
    checkpoint_writes,
    agent_chat_messages
TO hindsight_archive;

REVOKE ALL ON TABLE
    learning_protocol_authorizations,
    learning_execution_authorizations,
    learning_evidence_records
FROM hindsight_agent_writer, hindsight_memory_worker,
     hindsight_mcp_readonly, hindsight_dashboard_reader, hindsight_cdc;
GRANT SELECT ON TABLE
    learning_protocol_authorizations,
    learning_execution_authorizations,
    learning_evidence_records
TO hindsight_archive;

GRANT INSERT ON TABLE tenant_event_outbox
TO hindsight_agent_writer, hindsight_memory_worker;
GRANT SELECT, CHANGEFEED ON TABLE tenant_event_outbox TO hindsight_cdc;
REVOKE SELECT, UPDATE, DELETE ON TABLE tenant_event_outbox
FROM hindsight_agent_writer, hindsight_memory_worker, hindsight_archive;
REVOKE INSERT, UPDATE, DELETE ON TABLE tenant_event_outbox
FROM hindsight_archive, hindsight_cdc;

-- The lifecycle role can export exactly one RLS-bound tenant and can delete
-- only through the database lifecycle guards. It is never granted to any
-- application runtime role.
REVOKE ALL ON TABLE tenant_lifecycle_operations FROM PUBLIC;
REVOKE ALL ON TABLE tenant_lifecycle_tables FROM PUBLIC;
REVOKE ALL ON TABLE tenant_purge_tombstones FROM PUBLIC;
REVOKE ALL ON TABLE tenant_lifecycle_completeness_issues FROM PUBLIC;
REVOKE ALL ON TABLE tenant_lifecycle_schema_change_blockers FROM PUBLIC;
REVOKE ALL ON TABLE tenant_lifecycle_operations, tenant_lifecycle_tables,
    tenant_purge_tombstones, tenant_lifecycle_completeness_issues,
    tenant_lifecycle_schema_change_blockers
FROM hindsight_agent_writer, hindsight_memory_worker,
    hindsight_mcp_readonly, hindsight_dashboard_reader,
    hindsight_archive, hindsight_cdc;

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
    product_credential_locators,
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
-- CockroachDB checks DELETE privileges for each table in a tenant cascade;
-- lifecycle row guards make these grants usable only under the verified
-- operation, matching fingerprint, target tenant, and live lease.
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
    product_credential_locators,
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
-- Required only because CockroachDB privilege-checks the existing outbox
-- trigger's non-lifecycle INSERT branch while building a tenant cascade.
GRANT INSERT ON TABLE tenant_event_outbox TO hindsight_lifecycle;
GRANT UPDATE ON TABLE
    agent_reflections,
    agent_runs,
    demo_sessions,
    incidents,
    memory_decisions
TO hindsight_lifecycle;

REVOKE DELETE ON TABLE tenants FROM hindsight_agent_writer,
    hindsight_memory_worker, hindsight_mcp_readonly,
    hindsight_dashboard_reader, hindsight_archive, hindsight_cdc;
