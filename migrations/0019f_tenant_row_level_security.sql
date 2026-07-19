-- Missing context evaluates to NULL and malformed context raises a cast error.
-- A restrictive fence prevents a later permissive policy from widening access.

CREATE OR REPLACE FUNCTION current_hindsight_tenant_id()
RETURNS UUID
LANGUAGE SQL
STABLE
AS $$
    SELECT nullif(current_setting('hindsight.tenant_id', true), '')::UUID
$$;

CREATE POLICY tenants_self_permissive ON tenants
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (id = current_hindsight_tenant_id())
    WITH CHECK (id = current_hindsight_tenant_id());
CREATE POLICY tenants_self_fence ON tenants
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (id = current_hindsight_tenant_id())
    WITH CHECK (id = current_hindsight_tenant_id());
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

CREATE POLICY episodic_memories_tenant_permissive ON episodic_memories
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY episodic_memories_tenant_fence ON episodic_memories
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE episodic_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE episodic_memories FORCE ROW LEVEL SECURITY;

CREATE POLICY semantic_memories_tenant_permissive ON semantic_memories
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY semantic_memories_tenant_fence ON semantic_memories
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE semantic_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_memories FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_reads_tenant_permissive ON memory_reads
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_reads_tenant_fence ON memory_reads
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_reads ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_reads FORCE ROW LEVEL SECURITY;

CREATE POLICY semantic_memory_embeddings_tenant_permissive ON semantic_memory_embeddings
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY semantic_memory_embeddings_tenant_fence ON semantic_memory_embeddings
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE semantic_memory_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_memory_embeddings FORCE ROW LEVEL SECURITY;

CREATE POLICY services_tenant_permissive ON services
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY services_tenant_fence ON services
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE services ENABLE ROW LEVEL SECURITY;
ALTER TABLE services FORCE ROW LEVEL SECURITY;

CREATE POLICY incidents_tenant_permissive ON incidents
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY incidents_tenant_fence ON incidents
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE incidents FORCE ROW LEVEL SECURITY;

CREATE POLICY incident_services_tenant_permissive ON incident_services
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY incident_services_tenant_fence ON incident_services
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE incident_services ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_services FORCE ROW LEVEL SECURITY;

CREATE POLICY incident_events_tenant_permissive ON incident_events
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY incident_events_tenant_fence ON incident_events
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE incident_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_events FORCE ROW LEVEL SECURITY;

CREATE POLICY runbooks_tenant_permissive ON runbooks
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY runbooks_tenant_fence ON runbooks
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE runbooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE runbooks FORCE ROW LEVEL SECURITY;

CREATE POLICY incident_runbooks_tenant_permissive ON incident_runbooks
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY incident_runbooks_tenant_fence ON incident_runbooks
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE incident_runbooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_runbooks FORCE ROW LEVEL SECURITY;

CREATE POLICY incident_semantic_memories_tenant_permissive ON incident_semantic_memories
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY incident_semantic_memories_tenant_fence ON incident_semantic_memories
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE incident_semantic_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_semantic_memories FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_operations_tenant_permissive ON memory_operations
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_operations_tenant_fence ON memory_operations
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_operations FORCE ROW LEVEL SECURITY;

CREATE POLICY mcp_audit_events_tenant_permissive ON mcp_audit_events
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY mcp_audit_events_tenant_fence ON mcp_audit_events
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE mcp_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE mcp_audit_events FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_runs_tenant_permissive ON agent_runs
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY agent_runs_tenant_fence ON agent_runs
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_run_events_tenant_permissive ON agent_run_events
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY agent_run_events_tenant_fence ON agent_run_events
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE agent_run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_run_events FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_decisions_tenant_permissive ON memory_decisions
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_decisions_tenant_fence ON memory_decisions
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_decisions FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_namespaces_tenant_permissive ON memory_namespaces
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_namespaces_tenant_fence ON memory_namespaces
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_namespaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_namespaces FORCE ROW LEVEL SECURITY;

CREATE POLICY semantic_beliefs_tenant_permissive ON semantic_beliefs
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY semantic_beliefs_tenant_fence ON semantic_beliefs
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE semantic_beliefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_beliefs FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_external_evidence_tenant_permissive ON memory_external_evidence
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_external_evidence_tenant_fence ON memory_external_evidence
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_external_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_external_evidence FORCE ROW LEVEL SECURITY;

CREATE POLICY incident_semantic_beliefs_tenant_permissive ON incident_semantic_beliefs
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY incident_semantic_beliefs_tenant_fence ON incident_semantic_beliefs
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE incident_semantic_beliefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_semantic_beliefs FORCE ROW LEVEL SECURITY;

CREATE POLICY semantic_memory_vectors_tenant_permissive ON semantic_memory_vectors
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY semantic_memory_vectors_tenant_fence ON semantic_memory_vectors
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE semantic_memory_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_memory_vectors FORCE ROW LEVEL SECURITY;

CREATE POLICY embedding_backfill_tasks_tenant_permissive ON embedding_backfill_tasks
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY embedding_backfill_tasks_tenant_fence ON embedding_backfill_tasks
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE embedding_backfill_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_backfill_tasks FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_retrievals_tenant_permissive ON memory_retrievals
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_retrievals_tenant_fence ON memory_retrievals
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_retrievals ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_retrievals FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_lineage_edges_tenant_permissive ON memory_lineage_edges
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_lineage_edges_tenant_fence ON memory_lineage_edges
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_lineage_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_lineage_edges FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_reflections_tenant_permissive ON agent_reflections
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY agent_reflections_tenant_fence ON agent_reflections
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE agent_reflections ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_reflections FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_operation_previews_tenant_permissive ON memory_operation_previews
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_operation_previews_tenant_fence ON memory_operation_previews
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_operation_previews ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_operation_previews FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_operation_events_tenant_permissive ON memory_operation_events
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_operation_events_tenant_fence ON memory_operation_events
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_operation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_operation_events FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_operation_effects_tenant_permissive ON memory_operation_effects
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_operation_effects_tenant_fence ON memory_operation_effects
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_operation_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_operation_effects FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_review_items_tenant_permissive ON memory_review_items
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY memory_review_items_tenant_fence ON memory_review_items
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE memory_review_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_review_items FORCE ROW LEVEL SECURITY;

CREATE POLICY consolidation_jobs_tenant_permissive ON consolidation_jobs
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY consolidation_jobs_tenant_fence ON consolidation_jobs
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE consolidation_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE consolidation_jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY demo_sessions_tenant_permissive ON demo_sessions
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY demo_sessions_tenant_fence ON demo_sessions
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE demo_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_sessions FORCE ROW LEVEL SECURITY;

CREATE POLICY benchmark_experiments_tenant_permissive ON benchmark_experiments
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY benchmark_experiments_tenant_fence ON benchmark_experiments
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE benchmark_experiments ENABLE ROW LEVEL SECURITY;
ALTER TABLE benchmark_experiments FORCE ROW LEVEL SECURITY;

CREATE POLICY benchmark_trials_tenant_permissive ON benchmark_trials
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY benchmark_trials_tenant_fence ON benchmark_trials
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE benchmark_trials ENABLE ROW LEVEL SECURITY;
ALTER TABLE benchmark_trials FORCE ROW LEVEL SECURITY;

CREATE POLICY benchmark_actions_tenant_permissive ON benchmark_actions
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY benchmark_actions_tenant_fence ON benchmark_actions
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE benchmark_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE benchmark_actions FORCE ROW LEVEL SECURITY;

CREATE POLICY benchmark_confirmation_preregistrations_tenant_permissive ON benchmark_confirmation_preregistrations
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY benchmark_confirmation_preregistrations_tenant_fence ON benchmark_confirmation_preregistrations
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE benchmark_confirmation_preregistrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE benchmark_confirmation_preregistrations FORCE ROW LEVEL SECURITY;

CREATE POLICY benchmark_confirmation_bindings_tenant_permissive ON benchmark_confirmation_bindings
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY benchmark_confirmation_bindings_tenant_fence ON benchmark_confirmation_bindings
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE benchmark_confirmation_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE benchmark_confirmation_bindings FORCE ROW LEVEL SECURITY;

CREATE POLICY benchmark_variant_preparations_tenant_permissive ON benchmark_variant_preparations
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY benchmark_variant_preparations_tenant_fence ON benchmark_variant_preparations
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE benchmark_variant_preparations ENABLE ROW LEVEL SECURITY;
ALTER TABLE benchmark_variant_preparations FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_run_dispatches_tenant_permissive ON agent_run_dispatches
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY agent_run_dispatches_tenant_fence ON agent_run_dispatches
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE agent_run_dispatches ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_run_dispatches FORCE ROW LEVEL SECURITY;

CREATE POLICY checkpoints_tenant_permissive ON checkpoints
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY checkpoints_tenant_fence ON checkpoints
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoints FORCE ROW LEVEL SECURITY;

CREATE POLICY checkpoint_blobs_tenant_permissive ON checkpoint_blobs
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY checkpoint_blobs_tenant_fence ON checkpoint_blobs
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE checkpoint_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_blobs FORCE ROW LEVEL SECURITY;

CREATE POLICY checkpoint_writes_tenant_permissive ON checkpoint_writes
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY checkpoint_writes_tenant_fence ON checkpoint_writes
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE checkpoint_writes ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_writes FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_chat_messages_tenant_permissive ON agent_chat_messages
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY agent_chat_messages_tenant_fence ON agent_chat_messages
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE agent_chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_chat_messages FORCE ROW LEVEL SECURITY;
