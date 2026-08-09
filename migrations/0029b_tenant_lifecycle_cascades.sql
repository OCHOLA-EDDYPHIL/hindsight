-- Add a lifecycle cascade alongside every existing tenant catalog reference.
-- Existing NO ACTION constraints remain valid while these schema changes build.

ALTER TABLE episodic_memories
    ADD CONSTRAINT IF NOT EXISTS episodic_memories_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE semantic_memories
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_reads
    ADD CONSTRAINT IF NOT EXISTS memory_reads_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE semantic_memory_embeddings
    ADD CONSTRAINT IF NOT EXISTS semantic_memory_embeddings_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE services
    ADD CONSTRAINT IF NOT EXISTS services_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE incidents
    ADD CONSTRAINT IF NOT EXISTS incidents_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE incident_services
    ADD CONSTRAINT IF NOT EXISTS incident_services_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE incident_events
    ADD CONSTRAINT IF NOT EXISTS incident_events_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE runbooks
    ADD CONSTRAINT IF NOT EXISTS runbooks_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE incident_runbooks
    ADD CONSTRAINT IF NOT EXISTS incident_runbooks_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE incident_semantic_memories
    ADD CONSTRAINT IF NOT EXISTS incident_semantic_memories_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_operations
    ADD CONSTRAINT IF NOT EXISTS memory_operations_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE mcp_audit_events
    ADD CONSTRAINT IF NOT EXISTS mcp_audit_events_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE agent_runs
    ADD CONSTRAINT IF NOT EXISTS agent_runs_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE agent_run_events
    ADD CONSTRAINT IF NOT EXISTS agent_run_events_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_decisions
    ADD CONSTRAINT IF NOT EXISTS memory_decisions_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_namespaces
    ADD CONSTRAINT IF NOT EXISTS memory_namespaces_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE semantic_beliefs
    ADD CONSTRAINT IF NOT EXISTS semantic_beliefs_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_external_evidence
    ADD CONSTRAINT IF NOT EXISTS memory_external_evidence_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE incident_semantic_beliefs
    ADD CONSTRAINT IF NOT EXISTS incident_semantic_beliefs_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE semantic_memory_vectors
    ADD CONSTRAINT IF NOT EXISTS semantic_memory_vectors_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE embedding_backfill_tasks
    ADD CONSTRAINT IF NOT EXISTS embedding_backfill_tasks_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_retrievals
    ADD CONSTRAINT IF NOT EXISTS memory_retrievals_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_lineage_edges
    ADD CONSTRAINT IF NOT EXISTS memory_lineage_edges_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE agent_reflections
    ADD CONSTRAINT IF NOT EXISTS agent_reflections_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_operation_previews
    ADD CONSTRAINT IF NOT EXISTS memory_operation_previews_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_operation_events
    ADD CONSTRAINT IF NOT EXISTS memory_operation_events_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_operation_effects
    ADD CONSTRAINT IF NOT EXISTS memory_operation_effects_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE memory_review_items
    ADD CONSTRAINT IF NOT EXISTS memory_review_items_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE consolidation_jobs
    ADD CONSTRAINT IF NOT EXISTS consolidation_jobs_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE demo_sessions
    ADD CONSTRAINT IF NOT EXISTS demo_sessions_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE benchmark_experiments
    ADD CONSTRAINT IF NOT EXISTS benchmark_experiments_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE benchmark_trials
    ADD CONSTRAINT IF NOT EXISTS benchmark_trials_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE benchmark_actions
    ADD CONSTRAINT IF NOT EXISTS benchmark_actions_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE benchmark_confirmation_preregistrations
    ADD CONSTRAINT IF NOT EXISTS benchmark_preregistrations_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE benchmark_confirmation_bindings
    ADD CONSTRAINT IF NOT EXISTS benchmark_bindings_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE benchmark_variant_preparations
    ADD CONSTRAINT IF NOT EXISTS benchmark_preparations_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE agent_run_dispatches
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatches_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE checkpoints
    ADD CONSTRAINT IF NOT EXISTS checkpoints_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE checkpoint_blobs
    ADD CONSTRAINT IF NOT EXISTS checkpoint_blobs_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE checkpoint_writes
    ADD CONSTRAINT IF NOT EXISTS checkpoint_writes_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE agent_chat_messages
    ADD CONSTRAINT IF NOT EXISTS agent_chat_messages_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE tenant_event_outbox
    ADD CONSTRAINT IF NOT EXISTS tenant_event_outbox_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE learning_protocol_authorizations
    ADD CONSTRAINT IF NOT EXISTS learning_protocol_authorizations_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE learning_execution_authorizations
    ADD CONSTRAINT IF NOT EXISTS learning_execution_authorizations_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE learning_evidence_records
    ADD CONSTRAINT IF NOT EXISTS learning_evidence_records_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE learning_qualification_attempts
    ADD CONSTRAINT IF NOT EXISTS learning_qualification_attempts_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE learning_qualification_family_terminals
    ADD CONSTRAINT IF NOT EXISTS learning_qualification_terminals_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE agent_run_dispatch_attempts
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatch_attempts_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
ALTER TABLE product_principal_roles
    ADD CONSTRAINT IF NOT EXISTS product_principal_roles_lifecycle_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE;
