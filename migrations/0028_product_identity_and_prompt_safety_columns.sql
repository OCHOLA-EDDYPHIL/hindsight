-- Additive identity-boundary and prompt-safety schema changes. CockroachDB
-- must finish these schema changes before the guarded backfill in 0028a.

CREATE TABLE IF NOT EXISTS product_principal_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_hash STRING(64) NOT NULL UNIQUE,
    provisioning_key STRING(64) NOT NULL UNIQUE,
    tenant_id UUID NOT NULL,
    role STRING NOT NULL,
    status STRING NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT product_principal_roles_principal_hash CHECK (
        principal_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT product_principal_roles_provisioning_key CHECK (
        provisioning_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT product_principal_roles_role CHECK (
        role IN ('viewer', 'operator')
    ),
    CONSTRAINT product_principal_roles_status CHECK (
        status IN ('active', 'revoked')
    ),
    CONSTRAINT product_principal_roles_tenant_fk
        FOREIGN KEY (tenant_id) REFERENCES tenants (id)
);

CREATE INDEX IF NOT EXISTS product_principal_roles_tenant_status_idx
    ON product_principal_roles (tenant_id, status, role, principal_hash);

ALTER TABLE semantic_memories
    ADD COLUMN IF NOT EXISTS prompt_safety_status STRING,
    ADD COLUMN IF NOT EXISTS prompt_safety_scanner_version STRING,
    ADD COLUMN IF NOT EXISTS prompt_safety_reason_codes JSONB;
