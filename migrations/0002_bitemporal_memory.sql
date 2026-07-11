CREATE TABLE IF NOT EXISTS episodic_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id STRING NOT NULL,
    role STRING NOT NULL,
    content STRING NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    t_valid TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalid TIMESTAMPTZ,
    writer STRING NOT NULL,
    source_ref STRING NOT NULL,
    justification STRING NOT NULL,
    written_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalidated_by STRING,
    invalidation_reason STRING,
    invalidated_at TIMESTAMPTZ,
    CONSTRAINT episodic_memory_validity_order CHECK (
        t_invalid IS NULL OR t_invalid >= t_valid
    ),
    CONSTRAINT episodic_memory_invalidation_metadata CHECK (
        (
            t_invalid IS NULL
            AND invalidated_by IS NULL
            AND invalidation_reason IS NULL
            AND invalidated_at IS NULL
        )
        OR (
            t_invalid IS NOT NULL
            AND invalidated_by IS NOT NULL
            AND invalidation_reason IS NOT NULL
            AND invalidated_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS semantic_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace STRING NOT NULL,
    content STRING NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    t_valid TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalid TIMESTAMPTZ,
    writer STRING NOT NULL,
    source_ref STRING NOT NULL,
    justification STRING NOT NULL,
    written_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalidated_by STRING,
    invalidation_reason STRING,
    invalidated_at TIMESTAMPTZ,
    CONSTRAINT semantic_memory_validity_order CHECK (
        t_invalid IS NULL OR t_invalid >= t_valid
    ),
    CONSTRAINT semantic_memory_invalidation_metadata CHECK (
        (
            t_invalid IS NULL
            AND invalidated_by IS NULL
            AND invalidation_reason IS NULL
            AND invalidated_at IS NULL
        )
        OR (
            t_invalid IS NOT NULL
            AND invalidated_by IS NOT NULL
            AND invalidation_reason IS NOT NULL
            AND invalidated_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS memory_reads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id STRING NOT NULL,
    memory_kind STRING NOT NULL,
    memory_id UUID NOT NULL,
    reader STRING NOT NULL,
    purpose STRING NOT NULL,
    read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT memory_reads_kind CHECK (memory_kind IN ('episodic', 'semantic'))
);

CREATE INDEX IF NOT EXISTS episodic_memories_current_idx
    ON episodic_memories (episode_id, t_valid DESC)
    WHERE t_invalid IS NULL;

CREATE INDEX IF NOT EXISTS semantic_memories_current_idx
    ON semantic_memories (namespace, t_valid DESC)
    WHERE t_invalid IS NULL;

CREATE INDEX IF NOT EXISTS memory_reads_decision_idx
    ON memory_reads (decision_id, read_at DESC);

CREATE INDEX IF NOT EXISTS memory_reads_memory_idx
    ON memory_reads (memory_kind, memory_id, read_at DESC);

CREATE OR REPLACE VIEW current_episodic_memories AS
SELECT *
FROM episodic_memories
WHERE t_invalid IS NULL;

CREATE OR REPLACE VIEW current_semantic_memories AS
SELECT *
FROM semantic_memories
WHERE t_invalid IS NULL;
