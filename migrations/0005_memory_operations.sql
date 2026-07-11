CREATE TABLE IF NOT EXISTS memory_operations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_type STRING NOT NULL,
    actor STRING NOT NULL,
    reason STRING NOT NULL,
    target_timestamp TIMESTAMPTZ,
    namespace STRING,
    invalidated_memory_ids JSONB NOT NULL DEFAULT '[]'::JSONB,
    restored_memory_ids JSONB NOT NULL DEFAULT '[]'::JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT memory_operations_type CHECK (operation_type IN ('rewind'))
);

CREATE INDEX IF NOT EXISTS memory_operations_created_idx
    ON memory_operations (operation_type, created_at DESC);

CREATE INDEX IF NOT EXISTS memory_operations_namespace_idx
    ON memory_operations (namespace, created_at DESC)
    WHERE namespace IS NOT NULL;
