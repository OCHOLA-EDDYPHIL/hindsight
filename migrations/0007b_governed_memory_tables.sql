-- Establish the governed identity/evidence tables before 0008 backfills them.
-- Tables with foreign keys may remain in an asynchronous ADD state for a
-- short period after creation on CockroachDB, so writes belong in a later
-- migration transaction.

CREATE TABLE IF NOT EXISTS memory_decisions (
    id STRING PRIMARY KEY,
    actor STRING NOT NULL,
    decision_kind STRING NOT NULL,
    purpose STRING NOT NULL,
    run_id UUID REFERENCES agent_runs (id) ON DELETE SET NULL,
    namespace STRING,
    status STRING NOT NULL DEFAULT 'open',
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sealed_at TIMESTAMPTZ,
    CONSTRAINT memory_decisions_status CHECK (status IN ('open', 'sealed', 'failed')),
    CONSTRAINT memory_decisions_sealed CHECK (
        (status = 'open' AND sealed_at IS NULL)
        OR (status IN ('sealed', 'failed') AND sealed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS memory_namespaces (
    namespace STRING PRIMARY KEY,
    revision INT8 NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semantic_beliefs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_external_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    semantic_memory_id UUID REFERENCES semantic_memories (id) ON DELETE CASCADE,
    episodic_memory_id UUID REFERENCES episodic_memories (id) ON DELETE CASCADE,
    evidence_kind STRING NOT NULL,
    evidence_ref STRING NOT NULL,
    evidence_digest STRING NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    actor STRING NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT memory_external_evidence_target CHECK (
        (semantic_memory_id IS NOT NULL AND episodic_memory_id IS NULL)
        OR (semantic_memory_id IS NULL AND episodic_memory_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS incident_semantic_beliefs (
    incident_id UUID NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    belief_id UUID NOT NULL REFERENCES semantic_beliefs (id),
    relationship STRING NOT NULL,
    PRIMARY KEY (incident_id, belief_id),
    CONSTRAINT incident_belief_relationship CHECK (
        relationship IN ('summary', 'root_cause', 'resolution', 'lesson', 'reflection')
    )
);
