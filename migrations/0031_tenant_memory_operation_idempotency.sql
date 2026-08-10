DROP INDEX IF EXISTS memory_operations@memory_operations_idempotency_idx;

CREATE UNIQUE INDEX IF NOT EXISTS memory_operations_tenant_idempotency_idx
    ON memory_operations (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
