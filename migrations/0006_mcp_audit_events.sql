CREATE TABLE IF NOT EXISTS mcp_audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tool_name STRING NOT NULL,
    actor STRING NOT NULL,
    purpose STRING NOT NULL,
    arguments JSONB NOT NULL DEFAULT '{}'::JSONB,
    result_count INT8,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS mcp_audit_events_created_idx
    ON mcp_audit_events (created_at DESC);

CREATE INDEX IF NOT EXISTS mcp_audit_events_tool_idx
    ON mcp_audit_events (tool_name, created_at DESC);
