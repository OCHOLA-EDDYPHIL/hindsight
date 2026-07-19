-- Stage tenant catalog constraints separately from cross-table relationship keys.

ALTER TABLE episodic_memories ADD CONSTRAINT episodic_memories_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE semantic_memories ADD CONSTRAINT semantic_memories_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_reads ADD CONSTRAINT memory_reads_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE semantic_memory_embeddings
    ADD CONSTRAINT semantic_memory_embeddings_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE services ADD CONSTRAINT services_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE incidents ADD CONSTRAINT incidents_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE incident_services ADD CONSTRAINT incident_services_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE incident_events ADD CONSTRAINT incident_events_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE runbooks ADD CONSTRAINT runbooks_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE incident_runbooks ADD CONSTRAINT incident_runbooks_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE incident_semantic_memories
    ADD CONSTRAINT incident_semantic_memories_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_operations ADD CONSTRAINT memory_operations_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE mcp_audit_events ADD CONSTRAINT mcp_audit_events_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE agent_run_events ADD CONSTRAINT agent_run_events_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_decisions ADD CONSTRAINT memory_decisions_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_namespaces ADD CONSTRAINT memory_namespaces_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE semantic_beliefs ADD CONSTRAINT semantic_beliefs_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_external_evidence
    ADD CONSTRAINT memory_external_evidence_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE incident_semantic_beliefs
    ADD CONSTRAINT incident_semantic_beliefs_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE semantic_memory_vectors
    ADD CONSTRAINT semantic_memory_vectors_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE embedding_backfill_tasks
    ADD CONSTRAINT embedding_backfill_tasks_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_retrievals ADD CONSTRAINT memory_retrievals_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_lineage_edges ADD CONSTRAINT memory_lineage_edges_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE agent_reflections ADD CONSTRAINT agent_reflections_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_operation_previews
    ADD CONSTRAINT memory_operation_previews_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_operation_events
    ADD CONSTRAINT memory_operation_events_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_operation_effects
    ADD CONSTRAINT memory_operation_effects_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE memory_review_items ADD CONSTRAINT memory_review_items_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE consolidation_jobs ADD CONSTRAINT consolidation_jobs_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE demo_sessions ADD CONSTRAINT demo_sessions_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE benchmark_experiments
    ADD CONSTRAINT benchmark_experiments_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE benchmark_trials ADD CONSTRAINT benchmark_trials_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE benchmark_actions ADD CONSTRAINT benchmark_actions_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE benchmark_confirmation_preregistrations
    ADD CONSTRAINT benchmark_preregistrations_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE benchmark_confirmation_bindings
    ADD CONSTRAINT benchmark_bindings_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE benchmark_variant_preparations
    ADD CONSTRAINT benchmark_preparations_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE agent_run_dispatches ADD CONSTRAINT agent_run_dispatches_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE checkpoints ADD CONSTRAINT checkpoints_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE checkpoint_blobs ADD CONSTRAINT checkpoint_blobs_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE checkpoint_writes ADD CONSTRAINT checkpoint_writes_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
ALTER TABLE agent_chat_messages ADD CONSTRAINT agent_chat_messages_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id);
