-- Bind new lifecycle operations and tombstones to the public-demo identity
-- digest observed before tenant export. Existing rows remain readable but are
-- deliberately unbound so privileged code can refuse to purge or replay them.

ALTER TABLE tenant_lifecycle_operations
    ADD COLUMN IF NOT EXISTS public_identity_sha256 STRING(64);

ALTER TABLE tenant_lifecycle_operations
    ADD CONSTRAINT IF NOT EXISTS tenant_lifecycle_operations_public_identity_hash
    CHECK (
        public_identity_sha256 IS NULL
        OR public_identity_sha256 ~ '^[0-9a-f]{64}$'
    );

CREATE OR REPLACE FUNCTION guard_lifecycle_public_identity_baseline()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).public_identity_sha256 IS DISTINCT FROM (OLD).public_identity_sha256 THEN
        RAISE EXCEPTION 'tenant lifecycle public identity baseline is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS tenant_lifecycle_public_identity_baseline_immutable
ON tenant_lifecycle_operations;
CREATE TRIGGER tenant_lifecycle_public_identity_baseline_immutable
BEFORE UPDATE ON tenant_lifecycle_operations
FOR EACH ROW EXECUTE FUNCTION guard_lifecycle_public_identity_baseline();

ALTER TABLE tenant_purge_tombstones
    ADD COLUMN IF NOT EXISTS public_identity_sha256 STRING(64);

ALTER TABLE tenant_purge_tombstones
    ADD CONSTRAINT IF NOT EXISTS tenant_purge_tombstones_public_identity_hash
    CHECK (
        public_identity_sha256 IS NULL
        OR public_identity_sha256 ~ '^[0-9a-f]{64}$'
    );
