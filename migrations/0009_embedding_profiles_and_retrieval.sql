INSERT INTO embedding_index_state (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE VECTOR INDEX IF NOT EXISTS semantic_memory_vectors_embedding_idx
    ON semantic_memory_vectors (embedding);

CREATE INDEX IF NOT EXISTS semantic_memory_vectors_namespace_profile_idx
    ON semantic_memory_vectors (namespace, profile_id, embedded_at DESC);

INSERT INTO embedding_profiles (
    id, provider, model, dimensions, capability, encoder_revision,
    configuration, status, activated_at
)
SELECT DISTINCT
    'legacy:' || provider || ':' || model || ':' || dimensions::STRING,
    provider,
    model,
    dimensions,
    CASE WHEN provider = 'deterministic' THEN 'lexical_hash' ELSE 'semantic' END,
    'legacy-v1',
    '{}'::JSONB,
    'retired',
    min(embedded_at)
FROM semantic_memory_embeddings
GROUP BY provider, model, dimensions
ON CONFLICT (id) DO NOTHING;

INSERT INTO semantic_memory_vectors (
    memory_id, profile_id, namespace, content_digest, embedding, embedded_at
)
SELECT
    legacy.memory_id,
    'legacy:' || legacy.provider || ':' || legacy.model || ':' || legacy.dimensions::STRING,
    legacy.namespace,
    memory.payload_digest,
    legacy.embedding,
    legacy.embedded_at
FROM semantic_memory_embeddings AS legacy
JOIN semantic_memories AS memory ON memory.id = legacy.memory_id
ON CONFLICT (memory_id, profile_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS embedding_backfill_tasks (
    memory_id UUID NOT NULL REFERENCES semantic_memories (id),
    profile_id STRING NOT NULL REFERENCES embedding_profiles (id),
    status STRING NOT NULL DEFAULT 'pending',
    attempt_count INT8 NOT NULL DEFAULT 0,
    lease_owner STRING,
    lease_expires_at TIMESTAMPTZ,
    error_code STRING,
    error_detail STRING,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (memory_id, profile_id),
    CONSTRAINT embedding_backfill_status CHECK (
        status IN ('pending', 'leased', 'retrying', 'completed', 'failed')
    )
);

CREATE TABLE IF NOT EXISTS memory_retrievals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id STRING NOT NULL REFERENCES memory_decisions (id),
    namespace STRING NOT NULL,
    reader STRING NOT NULL,
    purpose STRING NOT NULL,
    policy STRING NOT NULL,
    policy_version INT8 NOT NULL,
    query_sha256 STRING NOT NULL,
    requested_limit INT8 NOT NULL,
    status STRING NOT NULL,
    selected_strategy STRING,
    embedding_profile_id STRING REFERENCES embedding_profiles (id),
    attempts JSONB NOT NULL DEFAULT '[]'::JSONB,
    returned_memory_ids JSONB NOT NULL DEFAULT '[]'::JSONB,
    error_code STRING,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT memory_retrievals_status CHECK (
        status IN ('succeeded', 'empty', 'degraded', 'failed')
    )
);

ALTER TABLE memory_reads
    ADD COLUMN IF NOT EXISTS retrieval_id UUID,
    ADD COLUMN IF NOT EXISTS rank INT8,
    ADD COLUMN IF NOT EXISTS distance FLOAT8;

ALTER TABLE memory_reads
    ADD CONSTRAINT memory_reads_retrieval_fk
        FOREIGN KEY (retrieval_id) REFERENCES memory_retrievals (id);

CREATE INDEX IF NOT EXISTS memory_retrievals_decision_idx
    ON memory_retrievals (decision_id, started_at DESC);

CREATE INDEX IF NOT EXISTS memory_retrievals_namespace_idx
    ON memory_retrievals (namespace, started_at DESC);
