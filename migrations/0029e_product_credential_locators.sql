-- Persist direct Cognito deletion locators before provisioning can create a
-- credential, then include those locators in the fenced tenant purge path.

CREATE TABLE IF NOT EXISTS product_credential_locators (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provisioning_key STRING(64) NOT NULL,
    tenant_id UUID NOT NULL,
    user_pool_id STRING(128) NOT NULL,
    cognito_username STRING(128) NOT NULL,
    role STRING NOT NULL,
    principal_hash STRING(64),
    status STRING NOT NULL DEFAULT 'reserved',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT product_credential_locators_provisioning_key CHECK (
        provisioning_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT product_credential_locators_directory CHECK (
        length(trim(user_pool_id)) BETWEEN 1 AND 128
    ),
    CONSTRAINT product_credential_locators_username CHECK (
        length(trim(cognito_username)) BETWEEN 1 AND 128
    ),
    CONSTRAINT product_credential_locators_role CHECK (
        role IN ('viewer', 'operator')
    ),
    CONSTRAINT product_credential_locators_principal_hash CHECK (
        principal_hash IS NULL OR principal_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT product_credential_locators_status CHECK (
        status IN ('reserved', 'active')
    ),
    CONSTRAINT product_credential_locators_active_state CHECK (
        status != 'active' OR principal_hash IS NOT NULL
    ),
    CONSTRAINT product_credential_locators_tenant_fk
        FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
    UNIQUE (user_pool_id, cognito_username)
);

CREATE INDEX IF NOT EXISTS product_credential_locators_tenant_idx
    ON product_credential_locators (
        tenant_id, user_pool_id, cognito_username, id
    );

CREATE POLICY IF NOT EXISTS product_credential_locators_tenant_permissive
ON product_credential_locators
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY IF NOT EXISTS product_credential_locators_tenant_fence
ON product_credential_locators
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE product_credential_locators ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_credential_locators FORCE ROW LEVEL SECURITY;

DROP TRIGGER IF EXISTS product_credential_locators_tenant_lifecycle_state
ON product_credential_locators;
CREATE TRIGGER product_credential_locators_tenant_lifecycle_state
BEFORE INSERT OR UPDATE OR DELETE ON product_credential_locators
FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state();

INSERT INTO tenant_lifecycle_tables (
    table_name, table_class, tenant_column, export_order,
    purge_via_tenant_cascade
) VALUES (
    'product_credential_locators', 'tenant_owned', 'tenant_id', 60, true
)
ON CONFLICT (table_name) DO UPDATE SET
    table_class = excluded.table_class,
    tenant_column = excluded.tenant_column,
    export_order = excluded.export_order,
    purge_via_tenant_cascade = excluded.purge_via_tenant_cascade;

ALTER TABLE tenant_lifecycle_operations
    ADD COLUMN IF NOT EXISTS cognito_credential_locators JSONB
        NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN IF NOT EXISTS cleanup_targets_captured_at TIMESTAMPTZ;

ALTER TABLE tenant_lifecycle_operations
    ADD CONSTRAINT IF NOT EXISTS tenant_lifecycle_operations_locators CHECK (
        jsonb_typeof(cognito_credential_locators) = 'array'
    ),
    ADD CONSTRAINT IF NOT EXISTS tenant_lifecycle_operations_cleanup_targets CHECK (
        status NOT IN ('database_purged', 'completed')
        OR cleanup_targets_captured_at IS NOT NULL
    );

CREATE OR REPLACE FUNCTION current_hindsight_lifecycle_purge_allowed(
    row_tenant_id UUID
)
RETURNS BOOL
LANGUAGE SQL
STABLE
SECURITY DEFINER
AS $$
    SELECT current_hindsight_lifecycle_role_member()
        AND EXISTS (
            SELECT 1
            FROM public.tenant_lifecycle_operations AS operation
            WHERE operation.id = nullif(
                    current_setting('hindsight.lifecycle_operation_id', true),
                    ''
                )::UUID
              AND operation.lease_owner = nullif(
                    current_setting('hindsight.lifecycle_lease_owner', true),
                    ''
                )::UUID
              AND operation.target_tenant_id = row_tenant_id
              AND operation.status = 'purging'
              AND operation.lease_expires_at > now()
              AND operation.export_verified_at IS NOT NULL
              AND operation.confirmed_export_fingerprint
                    = operation.export_fingerprint
              AND operation.purge_confirmed_at IS NOT NULL
              AND operation.purge_confirmed_at
                    <= operation.export_retention_until
              AND operation.cleanup_targets_captured_at IS NOT NULL
        )
$$;

REVOKE ALL ON TABLE product_credential_locators FROM PUBLIC;
REVOKE ALL ON TABLE product_credential_locators
FROM hindsight_agent_writer, hindsight_memory_worker,
    hindsight_mcp_readonly, hindsight_dashboard_reader,
    hindsight_archive, hindsight_cdc;
GRANT SELECT, DELETE ON TABLE product_credential_locators
TO hindsight_lifecycle;
