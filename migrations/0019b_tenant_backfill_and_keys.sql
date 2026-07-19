-- Backfill existing single-tenant rows before enforcing context-derived defaults.

-- Benchmark traces are intentionally immutable after they carry outcomes. Remove
-- only their UPDATE hooks while assigning the one-time legacy tenant, then restore
-- every hook before enforcing the tenant constraints below.
DROP TRIGGER IF EXISTS benchmark_experiment_contract_immutable ON benchmark_experiments;
DROP TRIGGER IF EXISTS benchmark_trial_trace_immutable ON benchmark_trials;
DROP TRIGGER IF EXISTS benchmark_action_trace_immutable ON benchmark_actions;
DROP TRIGGER IF EXISTS benchmark_preregistration_update_immutable
    ON benchmark_confirmation_preregistrations;
DROP TRIGGER IF EXISTS benchmark_confirmation_binding_recorded
    ON benchmark_confirmation_preregistrations;
DROP TRIGGER IF EXISTS benchmark_confirmation_binding_update_immutable
    ON benchmark_confirmation_bindings;
DROP TRIGGER IF EXISTS benchmark_variant_preparation_update_immutable
    ON benchmark_variant_preparations;

INSERT INTO tenants (id, slug, tenant_kind)
VALUES ('00000000-0000-0000-0000-000000000001', 'legacy', 'legacy')
ON CONFLICT (id) DO NOTHING;

UPDATE episodic_memories SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE semantic_memories SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_reads SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE semantic_memory_embeddings SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE services SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE incidents SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE incident_services SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE incident_events SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE runbooks SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE incident_runbooks SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE incident_semantic_memories SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_operations SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE mcp_audit_events SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE agent_runs SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE agent_run_events SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_decisions SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_namespaces SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE semantic_beliefs SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_external_evidence SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE incident_semantic_beliefs SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE semantic_memory_vectors SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE embedding_backfill_tasks SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_retrievals SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_lineage_edges SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE agent_reflections SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_operation_previews SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_operation_events SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_operation_effects SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE memory_review_items SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE consolidation_jobs SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE demo_sessions SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE benchmark_experiments SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE benchmark_trials SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE benchmark_actions SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE benchmark_confirmation_preregistrations SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE benchmark_confirmation_bindings SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE benchmark_variant_preparations SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE agent_run_dispatches SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE checkpoints SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE checkpoint_blobs SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE checkpoint_writes SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE agent_chat_messages SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;

CREATE TRIGGER benchmark_experiment_contract_immutable
BEFORE UPDATE ON benchmark_experiments
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_experiment_contract();

CREATE TRIGGER benchmark_trial_trace_immutable
BEFORE UPDATE ON benchmark_trials
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_trial_trace();

CREATE TRIGGER benchmark_action_trace_immutable
BEFORE UPDATE ON benchmark_actions
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_action_immutable();

CREATE TRIGGER benchmark_preregistration_update_immutable
BEFORE UPDATE ON benchmark_confirmation_preregistrations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_preregistration_mutation();

CREATE TRIGGER benchmark_confirmation_binding_recorded
AFTER UPDATE ON benchmark_confirmation_preregistrations
FOR EACH ROW
EXECUTE FUNCTION record_benchmark_confirmation_binding();

CREATE TRIGGER benchmark_confirmation_binding_update_immutable
BEFORE UPDATE ON benchmark_confirmation_bindings
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_confirmation_binding_history();

CREATE TRIGGER benchmark_variant_preparation_update_immutable
BEFORE UPDATE ON benchmark_variant_preparations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_variant_preparation_mutation();

ALTER TABLE episodic_memories ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_reads ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE semantic_memory_embeddings ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE services ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE incidents ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE incident_services ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE incident_events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE runbooks ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE incident_runbooks ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE incident_semantic_memories ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_operations ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE mcp_audit_events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_runs ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_run_events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_decisions ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_namespaces ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE semantic_beliefs ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_external_evidence ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE incident_semantic_beliefs ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE semantic_memory_vectors ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE embedding_backfill_tasks ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_retrievals ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_lineage_edges ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_reflections ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_operation_previews ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_operation_events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_operation_effects ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memory_review_items ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE consolidation_jobs ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE demo_sessions ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE benchmark_experiments ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE benchmark_trials ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE benchmark_actions ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE benchmark_confirmation_preregistrations ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE benchmark_confirmation_bindings ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE benchmark_variant_preparations ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_run_dispatches ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE checkpoints ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE checkpoint_blobs ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE checkpoint_writes ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_chat_messages ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE episodic_memories ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE semantic_memories ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_reads ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE semantic_memory_embeddings ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE services ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE incidents ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE incident_services ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE incident_events ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE runbooks ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE incident_runbooks ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE incident_semantic_memories ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_operations ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE mcp_audit_events ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE agent_runs ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE agent_run_events ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_decisions ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_namespaces ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE semantic_beliefs ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_external_evidence ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE incident_semantic_beliefs ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE semantic_memory_vectors ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE embedding_backfill_tasks ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_retrievals ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_lineage_edges ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE agent_reflections ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_operation_previews ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_operation_events ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_operation_effects ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE memory_review_items ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE consolidation_jobs ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE demo_sessions ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE benchmark_experiments ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE benchmark_trials ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE benchmark_actions ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE benchmark_confirmation_preregistrations ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE benchmark_confirmation_bindings ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE benchmark_variant_preparations ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE agent_run_dispatches ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE checkpoints ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE checkpoint_blobs ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE checkpoint_writes ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;
ALTER TABLE agent_chat_messages ALTER COLUMN tenant_id SET DEFAULT nullif(current_setting('hindsight.tenant_id', true), '')::UUID;

CREATE UNIQUE INDEX IF NOT EXISTS episodic_memories_tenant_id_idx ON episodic_memories (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS semantic_memories_tenant_id_idx ON semantic_memories (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_reads_tenant_id_idx ON memory_reads (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_reads_tenant_decision_idx
    ON memory_reads (tenant_id, id, decision_id);
CREATE UNIQUE INDEX IF NOT EXISTS semantic_memory_embeddings_tenant_key_idx
    ON semantic_memory_embeddings (tenant_id, memory_id);
CREATE UNIQUE INDEX IF NOT EXISTS services_tenant_id_idx ON services (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS incidents_tenant_id_idx ON incidents (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS incident_services_tenant_key_idx
    ON incident_services (tenant_id, incident_id, service_id);
CREATE UNIQUE INDEX IF NOT EXISTS incident_events_tenant_id_idx ON incident_events (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS runbooks_tenant_id_idx ON runbooks (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS incident_runbooks_tenant_key_idx
    ON incident_runbooks (tenant_id, incident_id, runbook_id);
CREATE UNIQUE INDEX IF NOT EXISTS incident_semantic_memories_tenant_key_idx
    ON incident_semantic_memories (tenant_id, incident_id, memory_id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_operations_tenant_id_idx ON memory_operations (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS mcp_audit_events_tenant_id_idx ON mcp_audit_events (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_tenant_id_idx ON agent_runs (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS agent_run_events_tenant_id_idx ON agent_run_events (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_decisions_tenant_id_idx ON memory_decisions (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_namespaces_tenant_namespace_idx ON memory_namespaces (tenant_id, namespace);
CREATE UNIQUE INDEX IF NOT EXISTS semantic_beliefs_tenant_id_idx ON semantic_beliefs (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_external_evidence_tenant_id_idx ON memory_external_evidence (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS incident_semantic_beliefs_tenant_key_idx
    ON incident_semantic_beliefs (tenant_id, incident_id, belief_id);
CREATE UNIQUE INDEX IF NOT EXISTS semantic_memory_vectors_tenant_key_idx
    ON semantic_memory_vectors (tenant_id, memory_id, profile_id);
CREATE UNIQUE INDEX IF NOT EXISTS embedding_backfill_tasks_tenant_key_idx
    ON embedding_backfill_tasks (tenant_id, memory_id, profile_id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_retrievals_tenant_id_idx ON memory_retrievals (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_lineage_edges_tenant_id_idx ON memory_lineage_edges (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS agent_reflections_tenant_id_idx ON agent_reflections (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_operation_previews_tenant_id_idx ON memory_operation_previews (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_operation_events_tenant_id_idx ON memory_operation_events (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_operation_effects_tenant_key_idx
    ON memory_operation_effects (tenant_id, operation_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS memory_review_items_tenant_id_idx ON memory_review_items (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS consolidation_jobs_tenant_id_idx ON consolidation_jobs (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS demo_sessions_tenant_id_idx ON demo_sessions (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_experiments_tenant_id_idx ON benchmark_experiments (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_trials_tenant_id_idx ON benchmark_trials (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_actions_tenant_key_idx
    ON benchmark_actions (tenant_id, trial_id, step);
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_preregistrations_tenant_key_idx
    ON benchmark_confirmation_preregistrations (tenant_id, pilot_experiment_id);
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_bindings_tenant_key_idx
    ON benchmark_confirmation_bindings (tenant_id, confirmation_experiment_id);
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_preparations_tenant_key_idx
    ON benchmark_variant_preparations (tenant_id, experiment_id, variant_id);
CREATE UNIQUE INDEX IF NOT EXISTS agent_run_dispatches_tenant_id_idx ON agent_run_dispatches (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS semantic_memories_tenant_producer_idx
    ON semantic_memories (tenant_id, id, producer_decision_id);
CREATE UNIQUE INDEX IF NOT EXISTS episodic_memories_tenant_producer_idx
    ON episodic_memories (tenant_id, id, producer_decision_id);
CREATE UNIQUE INDEX IF NOT EXISTS checkpoints_tenant_key_idx
    ON checkpoints (tenant_id, thread_id, checkpoint_ns, checkpoint_id);
CREATE UNIQUE INDEX IF NOT EXISTS checkpoint_blobs_tenant_key_idx
    ON checkpoint_blobs (tenant_id, thread_id, checkpoint_ns, channel, version);
CREATE UNIQUE INDEX IF NOT EXISTS checkpoint_writes_tenant_key_idx
    ON checkpoint_writes (
        tenant_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx
    );
CREATE UNIQUE INDEX IF NOT EXISTS agent_chat_messages_tenant_id_idx
    ON agent_chat_messages (tenant_id, id);
CREATE INDEX IF NOT EXISTS agent_chat_messages_tenant_session_idx
    ON agent_chat_messages (tenant_id, session_id, created_at);

CREATE INDEX IF NOT EXISTS semantic_memories_tenant_namespace_current_idx
    ON semantic_memories (tenant_id, namespace, t_valid DESC) WHERE t_invalid IS NULL;
CREATE INDEX IF NOT EXISTS incidents_tenant_created_idx
    ON incidents (tenant_id, created_at DESC, id);
CREATE INDEX IF NOT EXISTS agent_runs_tenant_status_idx
    ON agent_runs (tenant_id, status, created_at, id);
CREATE INDEX IF NOT EXISTS memory_operations_tenant_status_idx
    ON memory_operations (tenant_id, operation_type, created_at, id);
