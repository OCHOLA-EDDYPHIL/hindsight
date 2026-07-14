-- Least-privilege role template for deployed Hindsight environments.
-- Credentials are managed outside the repository. No product role receives
-- DELETE, and ordinary agent/API roles cannot UPDATE immutable memory rows.

CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_memory_worker LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_mcp_readonly LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_dashboard_reader LOGIN;

GRANT SELECT ON TABLE
    episodic_memories,
    semantic_memories,
    current_episodic_memories,
    current_semantic_memories,
    semantic_beliefs,
    memory_namespaces,
    embedding_profiles,
    embedding_index_state,
    semantic_memory_embeddings,
    semantic_memory_vectors,
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
    agent_reflections,
    agent_runs,
    agent_run_events,
    consolidation_jobs,
    benchmark_actions
TO hindsight_agent_writer;

GRANT INSERT ON TABLE
    episodic_memories,
    semantic_memories,
    semantic_beliefs,
    memory_namespaces,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    memory_decisions,
    memory_reads,
    memory_retrievals,
    memory_external_evidence,
    memory_lineage_edges,
    agent_reflections,
    agent_runs,
    agent_run_events,
    incidents,
    incident_services,
    incident_events,
    incident_semantic_memories,
    incident_semantic_beliefs
TO hindsight_agent_writer;

GRANT UPDATE ON TABLE
    memory_namespaces,
    memory_decisions,
    agent_runs,
    agent_run_events,
    incidents
TO hindsight_agent_writer;

-- This role is isolated to the queued correction/consolidation worker. It may
-- close versions and manage derivative indexes, but still receives no DELETE.
GRANT SELECT ON TABLE
    episodic_memories,
    semantic_memories,
    semantic_beliefs,
    memory_namespaces,
    embedding_profiles,
    embedding_index_state,
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
    benchmark_actions
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
    incident_semantic_memories,
    incident_semantic_beliefs,
    incident_events
TO hindsight_memory_worker;

GRANT UPDATE ON TABLE
    episodic_memories,
    semantic_memories,
    memory_namespaces,
    embedding_profiles,
    embedding_index_state,
    semantic_memory_embeddings,
    semantic_memory_vectors,
    embedding_backfill_tasks,
    memory_decisions,
    memory_operations,
    memory_review_items,
    consolidation_jobs,
    agent_runs,
    agent_run_events,
    incidents
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
