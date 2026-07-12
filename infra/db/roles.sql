-- Least-privilege role template for deployed Hindsight environments.
--
-- Create credentials outside this file with CockroachDB Cloud, SSO, or a secret
-- manager. Do not commit generated passwords or connection strings.

CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_mcp_readonly LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_dashboard_reader LOGIN;

GRANT SELECT, INSERT, UPDATE ON TABLE
    episodic_memories,
    semantic_memories,
    semantic_memory_embeddings,
    memory_reads,
    services,
    incidents,
    incident_services,
    incident_events,
    runbooks,
    incident_runbooks,
    incident_semantic_memories,
    memory_operations
TO hindsight_agent_writer;

GRANT SELECT ON TABLE
    episodic_memories,
    semantic_memories,
    semantic_memory_embeddings,
    memory_reads,
    current_episodic_memories,
    current_semantic_memories,
    services,
    incidents,
    incident_services,
    incident_events,
    runbooks,
    incident_runbooks,
    incident_semantic_memories,
    memory_operations,
    mcp_audit_events
TO hindsight_mcp_readonly;

GRANT INSERT ON TABLE
    memory_reads,
    mcp_audit_events
TO hindsight_mcp_readonly;

GRANT UPDATE ON TABLE
    mcp_audit_events
TO hindsight_mcp_readonly;

GRANT SELECT ON TABLE
    semantic_memories,
    memory_operations
TO hindsight_dashboard_reader;
