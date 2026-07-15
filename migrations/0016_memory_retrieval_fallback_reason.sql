ALTER TABLE memory_retrievals
    ADD COLUMN IF NOT EXISTS fallback_reason STRING;

ALTER TABLE memory_retrievals
    ADD CONSTRAINT memory_retrievals_fallback_reason CHECK (
        fallback_reason IS NULL
        OR fallback_reason IN ('semantic_vector_empty', 'semantic_vector_error')
    );
