-- Establish profile and vector tables before 0009 seeds or backfills them.

CREATE TABLE IF NOT EXISTS embedding_profiles (
    id STRING PRIMARY KEY,
    provider STRING NOT NULL,
    model STRING NOT NULL,
    dimensions INT8 NOT NULL,
    capability STRING NOT NULL,
    encoder_revision STRING NOT NULL,
    configuration JSONB NOT NULL DEFAULT '{}'::JSONB,
    max_distance FLOAT8,
    status STRING NOT NULL DEFAULT 'building',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    CONSTRAINT embedding_profiles_capability CHECK (capability IN ('semantic', 'lexical_hash')),
    CONSTRAINT embedding_profiles_status CHECK (status IN ('building', 'active', 'retired', 'failed')),
    CONSTRAINT embedding_profiles_dimensions CHECK (dimensions = 1024)
);

CREATE TABLE IF NOT EXISTS embedding_index_state (
    singleton BOOL PRIMARY KEY DEFAULT true,
    active_profile_id STRING REFERENCES embedding_profiles (id),
    building_profile_id STRING REFERENCES embedding_profiles (id),
    generation INT8 NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT embedding_index_singleton CHECK (singleton)
);

CREATE TABLE IF NOT EXISTS semantic_memory_vectors (
    memory_id UUID NOT NULL REFERENCES semantic_memories (id) ON DELETE CASCADE,
    profile_id STRING NOT NULL REFERENCES embedding_profiles (id),
    namespace STRING NOT NULL,
    content_digest STRING NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, profile_id)
);
