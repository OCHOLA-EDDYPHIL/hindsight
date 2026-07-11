CREATE TABLE IF NOT EXISTS semantic_memory_embeddings (
    memory_id UUID PRIMARY KEY REFERENCES semantic_memories (id),
    namespace STRING NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    provider STRING NOT NULL,
    model STRING NOT NULL,
    dimensions INT8 NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT semantic_embedding_dimensions CHECK (dimensions = 1024)
);

CREATE VECTOR INDEX semantic_memory_embeddings_namespace_embedding_idx
    ON semantic_memory_embeddings (namespace, embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS semantic_memory_embeddings_namespace_idx
    ON semantic_memory_embeddings (namespace, embedded_at DESC);
