CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key STRING UNIQUE,
    thread_id STRING NOT NULL,
    incident_id UUID REFERENCES incidents (id) ON DELETE SET NULL,
    incident_slug STRING NOT NULL,
    namespace STRING NOT NULL,
    service_slug STRING,
    user_input STRING NOT NULL,
    status STRING NOT NULL DEFAULT 'queued',
    decision_id STRING NOT NULL UNIQUE,
    plan STRING,
    proposed_action STRING,
    action_approved BOOL,
    provider STRING,
    model STRING,
    usage JSONB NOT NULL DEFAULT '{}'::JSONB,
    reflected_memory_id UUID REFERENCES semantic_memories (id) ON DELETE SET NULL,
    failure_code STRING,
    failure_detail STRING,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT agent_runs_status CHECK (
        status IN (
            'queued',
            'triaging',
            'recalling',
            'planning',
            'awaiting_approval',
            'resuming',
            'reflecting',
            'completed',
            'rejected',
            'failed'
        )
    )
);

CREATE TABLE IF NOT EXISTS agent_run_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs (id) ON DELETE CASCADE,
    sequence INT8 NOT NULL,
    phase STRING NOT NULL,
    status STRING NOT NULL,
    summary STRING NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS agent_runs_incident_idx
    ON agent_runs (incident_slug, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_runs_namespace_idx
    ON agent_runs (namespace, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_runs_status_idx
    ON agent_runs (status, created_at);

CREATE INDEX IF NOT EXISTS agent_run_events_run_idx
    ON agent_run_events (run_id, sequence);
