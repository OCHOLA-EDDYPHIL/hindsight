-- The lifecycle cascades are now committed. Remove the superseded direct
-- tenant catalog references so each table has one authoritative delete rule.

ALTER TABLE episodic_memories DROP CONSTRAINT IF EXISTS episodic_memories_tenant_fk;
ALTER TABLE semantic_memories DROP CONSTRAINT IF EXISTS semantic_memories_tenant_fk;
ALTER TABLE memory_reads DROP CONSTRAINT IF EXISTS memory_reads_tenant_fk;
ALTER TABLE semantic_memory_embeddings
    DROP CONSTRAINT IF EXISTS semantic_memory_embeddings_tenant_fk;
ALTER TABLE services DROP CONSTRAINT IF EXISTS services_tenant_fk;
ALTER TABLE incidents DROP CONSTRAINT IF EXISTS incidents_tenant_fk;
ALTER TABLE incident_services DROP CONSTRAINT IF EXISTS incident_services_tenant_fk;
ALTER TABLE incident_events DROP CONSTRAINT IF EXISTS incident_events_tenant_fk;
ALTER TABLE runbooks DROP CONSTRAINT IF EXISTS runbooks_tenant_fk;
ALTER TABLE incident_runbooks DROP CONSTRAINT IF EXISTS incident_runbooks_tenant_fk;
ALTER TABLE incident_semantic_memories
    DROP CONSTRAINT IF EXISTS incident_semantic_memories_tenant_fk;
ALTER TABLE memory_operations DROP CONSTRAINT IF EXISTS memory_operations_tenant_fk;
ALTER TABLE mcp_audit_events DROP CONSTRAINT IF EXISTS mcp_audit_events_tenant_fk;
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_tenant_fk;
ALTER TABLE agent_run_events DROP CONSTRAINT IF EXISTS agent_run_events_tenant_fk;
ALTER TABLE memory_decisions DROP CONSTRAINT IF EXISTS memory_decisions_tenant_fk;
ALTER TABLE memory_namespaces DROP CONSTRAINT IF EXISTS memory_namespaces_tenant_fk;
ALTER TABLE semantic_beliefs DROP CONSTRAINT IF EXISTS semantic_beliefs_tenant_fk;
ALTER TABLE memory_external_evidence
    DROP CONSTRAINT IF EXISTS memory_external_evidence_tenant_fk;
ALTER TABLE incident_semantic_beliefs
    DROP CONSTRAINT IF EXISTS incident_semantic_beliefs_tenant_fk;
ALTER TABLE semantic_memory_vectors
    DROP CONSTRAINT IF EXISTS semantic_memory_vectors_tenant_fk;
ALTER TABLE embedding_backfill_tasks
    DROP CONSTRAINT IF EXISTS embedding_backfill_tasks_tenant_fk;
ALTER TABLE memory_retrievals DROP CONSTRAINT IF EXISTS memory_retrievals_tenant_fk;
ALTER TABLE memory_lineage_edges
    DROP CONSTRAINT IF EXISTS memory_lineage_edges_tenant_fk;
ALTER TABLE agent_reflections DROP CONSTRAINT IF EXISTS agent_reflections_tenant_fk;
ALTER TABLE memory_operation_previews
    DROP CONSTRAINT IF EXISTS memory_operation_previews_tenant_fk;
ALTER TABLE memory_operation_events
    DROP CONSTRAINT IF EXISTS memory_operation_events_tenant_fk;
ALTER TABLE memory_operation_effects
    DROP CONSTRAINT IF EXISTS memory_operation_effects_tenant_fk;
ALTER TABLE memory_review_items DROP CONSTRAINT IF EXISTS memory_review_items_tenant_fk;
ALTER TABLE consolidation_jobs DROP CONSTRAINT IF EXISTS consolidation_jobs_tenant_fk;
ALTER TABLE demo_sessions DROP CONSTRAINT IF EXISTS demo_sessions_tenant_fk;
ALTER TABLE benchmark_experiments
    DROP CONSTRAINT IF EXISTS benchmark_experiments_tenant_fk;
ALTER TABLE benchmark_trials DROP CONSTRAINT IF EXISTS benchmark_trials_tenant_fk;
ALTER TABLE benchmark_actions DROP CONSTRAINT IF EXISTS benchmark_actions_tenant_fk;
ALTER TABLE benchmark_confirmation_preregistrations
    DROP CONSTRAINT IF EXISTS benchmark_preregistrations_tenant_fk;
ALTER TABLE benchmark_confirmation_bindings
    DROP CONSTRAINT IF EXISTS benchmark_bindings_tenant_fk;
ALTER TABLE benchmark_variant_preparations
    DROP CONSTRAINT IF EXISTS benchmark_preparations_tenant_fk;
ALTER TABLE agent_run_dispatches
    DROP CONSTRAINT IF EXISTS agent_run_dispatches_tenant_fk;
ALTER TABLE checkpoints DROP CONSTRAINT IF EXISTS checkpoints_tenant_fk;
ALTER TABLE checkpoint_blobs DROP CONSTRAINT IF EXISTS checkpoint_blobs_tenant_fk;
ALTER TABLE checkpoint_writes DROP CONSTRAINT IF EXISTS checkpoint_writes_tenant_fk;
ALTER TABLE agent_chat_messages DROP CONSTRAINT IF EXISTS agent_chat_messages_tenant_fk;
ALTER TABLE tenant_event_outbox
    DROP CONSTRAINT IF EXISTS tenant_event_outbox_tenant_id_fkey;
ALTER TABLE agent_run_dispatch_attempts
    DROP CONSTRAINT IF EXISTS agent_run_dispatch_attempts_tenant_fk;
ALTER TABLE product_principal_roles
    DROP CONSTRAINT IF EXISTS product_principal_roles_tenant_fk;
