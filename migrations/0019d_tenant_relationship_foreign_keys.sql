-- Every relational reference carries the tenant key so constraints cannot
-- resolve a parent row from another tenant even when RLS is bypassed by DDL.

ALTER TABLE semantic_memory_embeddings
    ADD CONSTRAINT IF NOT EXISTS semantic_memory_embeddings_tenant_memory_fk
    FOREIGN KEY (tenant_id, memory_id)
    REFERENCES semantic_memories (tenant_id, id);

ALTER TABLE incident_services
    ADD CONSTRAINT IF NOT EXISTS incident_services_tenant_incident_fk
        FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incidents (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS incident_services_tenant_service_fk
        FOREIGN KEY (tenant_id, service_id)
        REFERENCES services (tenant_id, id);

ALTER TABLE incident_events
    ADD CONSTRAINT IF NOT EXISTS incident_events_tenant_incident_fk
    FOREIGN KEY (tenant_id, incident_id)
    REFERENCES incidents (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE runbooks
    ADD CONSTRAINT IF NOT EXISTS runbooks_tenant_service_fk
    FOREIGN KEY (tenant_id, service_id)
    REFERENCES services (tenant_id, id);

ALTER TABLE incident_runbooks
    ADD CONSTRAINT IF NOT EXISTS incident_runbooks_tenant_incident_fk
        FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incidents (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS incident_runbooks_tenant_runbook_fk
        FOREIGN KEY (tenant_id, runbook_id)
        REFERENCES runbooks (tenant_id, id);

ALTER TABLE incident_semantic_memories
    ADD CONSTRAINT IF NOT EXISTS incident_semantic_memories_tenant_incident_fk
        FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incidents (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS incident_semantic_memories_tenant_memory_fk
        FOREIGN KEY (tenant_id, memory_id)
        REFERENCES semantic_memories (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE agent_runs
    ADD CONSTRAINT IF NOT EXISTS agent_runs_tenant_incident_fk
        FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incidents (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS agent_runs_tenant_reflected_memory_fk
        FOREIGN KEY (tenant_id, reflected_memory_id)
        REFERENCES semantic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS agent_runs_tenant_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES memory_decisions (tenant_id, id);

ALTER TABLE agent_run_events
    ADD CONSTRAINT IF NOT EXISTS agent_run_events_tenant_run_fk
    FOREIGN KEY (tenant_id, run_id)
    REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE memory_decisions
    ADD CONSTRAINT IF NOT EXISTS memory_decisions_tenant_run_fk
    FOREIGN KEY (tenant_id, run_id)
    REFERENCES agent_runs (tenant_id, id);

ALTER TABLE semantic_memories
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_tenant_belief_fk
        FOREIGN KEY (tenant_id, belief_id)
        REFERENCES semantic_beliefs (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_tenant_previous_fk
        FOREIGN KEY (tenant_id, previous_version_id)
        REFERENCES semantic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_tenant_producer_fk
        FOREIGN KEY (tenant_id, producer_decision_id)
        REFERENCES memory_decisions (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_tenant_operation_fk
        FOREIGN KEY (tenant_id, created_by_operation_id)
        REFERENCES memory_operations (tenant_id, id);

ALTER TABLE episodic_memories
    ADD CONSTRAINT IF NOT EXISTS episodic_memories_tenant_producer_fk
    FOREIGN KEY (tenant_id, producer_decision_id)
    REFERENCES memory_decisions (tenant_id, id);

ALTER TABLE memory_reads
    ADD CONSTRAINT IF NOT EXISTS memory_reads_tenant_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES memory_decisions (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS memory_reads_tenant_semantic_fk
        FOREIGN KEY (tenant_id, semantic_memory_id)
        REFERENCES semantic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS memory_reads_tenant_episodic_fk
        FOREIGN KEY (tenant_id, episodic_memory_id)
        REFERENCES episodic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS memory_reads_tenant_retrieval_fk
        FOREIGN KEY (tenant_id, retrieval_id)
        REFERENCES memory_retrievals (tenant_id, id);

ALTER TABLE memory_external_evidence
    ADD CONSTRAINT IF NOT EXISTS memory_external_evidence_tenant_semantic_fk
        FOREIGN KEY (tenant_id, semantic_memory_id)
        REFERENCES semantic_memories (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS memory_external_evidence_tenant_episodic_fk
        FOREIGN KEY (tenant_id, episodic_memory_id)
        REFERENCES episodic_memories (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE incident_semantic_beliefs
    ADD CONSTRAINT IF NOT EXISTS incident_semantic_beliefs_tenant_incident_fk
        FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incidents (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS incident_semantic_beliefs_tenant_belief_fk
        FOREIGN KEY (tenant_id, belief_id)
        REFERENCES semantic_beliefs (tenant_id, id);

ALTER TABLE semantic_memory_vectors
    ADD CONSTRAINT IF NOT EXISTS semantic_memory_vectors_tenant_memory_fk
    FOREIGN KEY (tenant_id, memory_id)
    REFERENCES semantic_memories (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE embedding_backfill_tasks
    ADD CONSTRAINT IF NOT EXISTS embedding_backfill_tasks_tenant_memory_fk
    FOREIGN KEY (tenant_id, memory_id)
    REFERENCES semantic_memories (tenant_id, id);

ALTER TABLE memory_retrievals
    ADD CONSTRAINT IF NOT EXISTS memory_retrievals_tenant_decision_fk
    FOREIGN KEY (tenant_id, decision_id)
    REFERENCES memory_decisions (tenant_id, id);

ALTER TABLE memory_lineage_edges
    ADD CONSTRAINT IF NOT EXISTS memory_lineage_tenant_semantic_child_fk
        FOREIGN KEY (tenant_id, child_semantic_memory_id, producer_decision_id)
        REFERENCES semantic_memories (tenant_id, id, producer_decision_id),
    ADD CONSTRAINT IF NOT EXISTS memory_lineage_tenant_episodic_child_fk
        FOREIGN KEY (tenant_id, child_episodic_memory_id, producer_decision_id)
        REFERENCES episodic_memories (tenant_id, id, producer_decision_id),
    ADD CONSTRAINT IF NOT EXISTS memory_lineage_tenant_parent_read_fk
        FOREIGN KEY (tenant_id, parent_read_id, producer_decision_id)
        REFERENCES memory_reads (tenant_id, id, decision_id);

ALTER TABLE agent_reflections
    ADD CONSTRAINT IF NOT EXISTS agent_reflections_tenant_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES memory_decisions (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS agent_reflections_tenant_run_fk
        FOREIGN KEY (tenant_id, run_id)
        REFERENCES agent_runs (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS agent_reflections_tenant_memory_fk
        FOREIGN KEY (tenant_id, semantic_memory_id)
        REFERENCES semantic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS agent_reflections_tenant_belief_fk
        FOREIGN KEY (tenant_id, belief_id)
        REFERENCES semantic_beliefs (tenant_id, id);

ALTER TABLE memory_operation_events
    ADD CONSTRAINT IF NOT EXISTS memory_operation_events_tenant_operation_fk
    FOREIGN KEY (tenant_id, operation_id)
    REFERENCES memory_operations (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE memory_operation_effects
    ADD CONSTRAINT IF NOT EXISTS memory_operation_effects_tenant_operation_fk
        FOREIGN KEY (tenant_id, operation_id)
        REFERENCES memory_operations (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS memory_operation_effects_tenant_belief_fk
        FOREIGN KEY (tenant_id, belief_id)
        REFERENCES semantic_beliefs (tenant_id, id);

ALTER TABLE memory_review_items
    ADD CONSTRAINT IF NOT EXISTS memory_review_items_tenant_operation_fk
        FOREIGN KEY (tenant_id, operation_id)
        REFERENCES memory_operations (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS memory_review_items_tenant_memory_fk
        FOREIGN KEY (tenant_id, semantic_memory_id)
        REFERENCES semantic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS memory_review_items_tenant_resolution_fk
        FOREIGN KEY (tenant_id, resolution_operation_id)
        REFERENCES memory_operations (tenant_id, id);

ALTER TABLE incidents
    ADD CONSTRAINT IF NOT EXISTS incidents_tenant_resolution_event_fk
    FOREIGN KEY (tenant_id, resolution_event_id)
    REFERENCES incident_events (tenant_id, id);

ALTER TABLE consolidation_jobs
    ADD CONSTRAINT IF NOT EXISTS consolidation_jobs_tenant_incident_fk
        FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incidents (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS consolidation_jobs_tenant_source_event_fk
        FOREIGN KEY (tenant_id, source_event_id)
        REFERENCES incident_events (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS consolidation_jobs_tenant_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES memory_decisions (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS consolidation_jobs_tenant_belief_fk
        FOREIGN KEY (tenant_id, lesson_belief_id)
        REFERENCES semantic_beliefs (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS consolidation_jobs_tenant_memory_fk
        FOREIGN KEY (tenant_id, lesson_memory_id)
        REFERENCES semantic_memories (tenant_id, id);

ALTER TABLE benchmark_trials
    ADD CONSTRAINT IF NOT EXISTS benchmark_trials_tenant_experiment_fk
        FOREIGN KEY (tenant_id, experiment_id)
        REFERENCES benchmark_experiments (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS benchmark_trials_tenant_memory_fk
        FOREIGN KEY (tenant_id, lesson_memory_id)
        REFERENCES semantic_memories (tenant_id, id);

ALTER TABLE benchmark_actions
    ADD CONSTRAINT IF NOT EXISTS benchmark_actions_tenant_trial_fk
        FOREIGN KEY (tenant_id, trial_id)
        REFERENCES benchmark_trials (tenant_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT IF NOT EXISTS benchmark_actions_tenant_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES memory_decisions (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_actions_tenant_retrieval_fk
        FOREIGN KEY (tenant_id, retrieval_id)
        REFERENCES memory_retrievals (tenant_id, id);

ALTER TABLE benchmark_confirmation_preregistrations
    ADD CONSTRAINT IF NOT EXISTS benchmark_preregistrations_tenant_pilot_fk
        FOREIGN KEY (tenant_id, pilot_experiment_id)
        REFERENCES benchmark_experiments (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_preregistrations_tenant_confirmation_fk
        FOREIGN KEY (tenant_id, confirmation_experiment_id)
        REFERENCES benchmark_experiments (tenant_id, id);

ALTER TABLE benchmark_confirmation_bindings
    ADD CONSTRAINT IF NOT EXISTS benchmark_bindings_tenant_confirmation_fk
        FOREIGN KEY (tenant_id, confirmation_experiment_id)
        REFERENCES benchmark_experiments (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_bindings_tenant_pilot_fk
        FOREIGN KEY (tenant_id, pilot_experiment_id)
        REFERENCES benchmark_experiments (tenant_id, id);

ALTER TABLE benchmark_variant_preparations
    ADD CONSTRAINT IF NOT EXISTS benchmark_preparations_tenant_experiment_fk
        FOREIGN KEY (tenant_id, experiment_id)
        REFERENCES benchmark_experiments (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_preparations_tenant_incident_fk
        FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incidents (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_preparations_tenant_source_memory_fk
        FOREIGN KEY (tenant_id, source_memory_id)
        REFERENCES semantic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_preparations_tenant_reference_memory_fk
        FOREIGN KEY (tenant_id, reference_memory_id)
        REFERENCES semantic_memories (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_preparations_tenant_consolidated_memory_fk
        FOREIGN KEY (tenant_id, consolidated_memory_id)
        REFERENCES semantic_memories (tenant_id, id);

ALTER TABLE agent_run_dispatches
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatches_tenant_run_fk
    FOREIGN KEY (tenant_id, run_id)
    REFERENCES agent_runs (tenant_id, id);
