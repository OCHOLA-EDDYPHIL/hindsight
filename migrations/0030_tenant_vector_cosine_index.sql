SET sql_safe_updates = false;

CREATE VECTOR INDEX IF NOT EXISTS semantic_memory_vectors_tenant_namespace_profile_embedding_idx
    ON semantic_memory_vectors (
        tenant_id,
        namespace,
        profile_id,
        embedding vector_cosine_ops
    );
