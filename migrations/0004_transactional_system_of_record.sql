CREATE TABLE IF NOT EXISTS services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug STRING NOT NULL UNIQUE,
    name STRING NOT NULL,
    owner_team STRING NOT NULL,
    tier STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT services_tier CHECK (tier IN ('critical', 'core', 'supporting'))
);

CREATE TABLE IF NOT EXISTS incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug STRING NOT NULL UNIQUE,
    title STRING NOT NULL,
    severity STRING NOT NULL,
    status STRING NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    summary STRING NOT NULL,
    root_cause STRING,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT incidents_severity CHECK (severity IN ('sev1', 'sev2', 'sev3')),
    CONSTRAINT incidents_status CHECK (status IN ('resolved', 'mitigated', 'open')),
    CONSTRAINT incidents_time_order CHECK (
        resolved_at IS NULL OR resolved_at >= started_at
    )
);

CREATE TABLE IF NOT EXISTS incident_services (
    incident_id UUID NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    service_id UUID NOT NULL REFERENCES services (id),
    impact STRING NOT NULL,
    PRIMARY KEY (incident_id, service_id)
);

CREATE TABLE IF NOT EXISTS incident_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    event_type STRING NOT NULL,
    summary STRING NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB
);

CREATE TABLE IF NOT EXISTS runbooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug STRING NOT NULL UNIQUE,
    service_id UUID REFERENCES services (id),
    title STRING NOT NULL,
    summary STRING NOT NULL,
    steps JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS incident_runbooks (
    incident_id UUID NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    runbook_id UUID NOT NULL REFERENCES runbooks (id),
    usage_note STRING NOT NULL,
    outcome STRING NOT NULL,
    PRIMARY KEY (incident_id, runbook_id)
);

CREATE TABLE IF NOT EXISTS incident_semantic_memories (
    incident_id UUID NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    memory_id UUID NOT NULL REFERENCES semantic_memories (id) ON DELETE CASCADE,
    relationship STRING NOT NULL,
    PRIMARY KEY (incident_id, memory_id),
    CONSTRAINT incident_memory_relationship CHECK (
        relationship IN ('summary', 'root_cause', 'resolution', 'lesson')
    )
);

CREATE INDEX IF NOT EXISTS incident_services_service_idx
    ON incident_services (service_id, incident_id);

CREATE INDEX IF NOT EXISTS incident_events_incident_time_idx
    ON incident_events (incident_id, occurred_at);

CREATE INDEX IF NOT EXISTS runbooks_service_idx
    ON runbooks (service_id, slug);

CREATE INDEX IF NOT EXISTS incident_semantic_memories_memory_idx
    ON incident_semantic_memories (memory_id, incident_id);
