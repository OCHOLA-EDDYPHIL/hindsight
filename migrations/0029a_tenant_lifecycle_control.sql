-- Establish lifecycle control state before tenant foreign keys and row guards
-- are changed in later schema-change transactions.

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
    NOT NULL DEFAULT now();

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_status;
ALTER TABLE tenants ADD CONSTRAINT tenants_status CHECK (
    status IN ('active', 'archived', 'purge_pending', 'purging')
);

CREATE TABLE IF NOT EXISTS tenant_lifecycle_operations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_tenant_id UUID NOT NULL,
    tenant_identity_sha256 STRING(64) NOT NULL,
    status STRING NOT NULL DEFAULT 'pending_export',
    snapshot_hlc STRING,
    schema_identity_sha256 STRING(64),
    export_content_sha256 STRING(64),
    export_fingerprint STRING(64),
    export_bucket STRING,
    export_data_key STRING,
    export_data_version_id STRING,
    export_manifest_key STRING,
    export_manifest_version_id STRING,
    export_retention_until TIMESTAMPTZ,
    export_verified_at TIMESTAMPTZ,
    confirmed_export_fingerprint STRING(64),
    purge_confirmed_at TIMESTAMPTZ,
    principal_hashes JSONB NOT NULL DEFAULT '[]'::JSONB,
    attempt_count INT8 NOT NULL DEFAULT 0,
    lease_owner UUID,
    lease_expires_at TIMESTAMPTZ,
    database_purged_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    failure_code STRING,
    failure_detail STRING,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_lifecycle_operations_identity_hash CHECK (
        tenant_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT tenant_lifecycle_operations_status CHECK (
        status IN (
            'pending_export', 'exporting', 'exported', 'verified',
            'purging', 'database_purged', 'completed', 'failed', 'aborted'
        )
    ),
    CONSTRAINT tenant_lifecycle_operations_hashes CHECK (
        (schema_identity_sha256 IS NULL
            OR schema_identity_sha256 ~ '^[0-9a-f]{64}$')
        AND (export_content_sha256 IS NULL
            OR export_content_sha256 ~ '^[0-9a-f]{64}$')
        AND (export_fingerprint IS NULL
            OR export_fingerprint ~ '^[0-9a-f]{64}$')
        AND (confirmed_export_fingerprint IS NULL
            OR confirmed_export_fingerprint ~ '^[0-9a-f]{64}$')
    ),
    CONSTRAINT tenant_lifecycle_operations_principals CHECK (
        jsonb_typeof(principal_hashes) = 'array'
    ),
    CONSTRAINT tenant_lifecycle_operations_attempts CHECK (attempt_count >= 0),
    CONSTRAINT tenant_lifecycle_operations_lease CHECK (
        (lease_owner IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT tenant_lifecycle_operations_verified_state CHECK (
        status NOT IN ('verified', 'purging', 'database_purged', 'completed')
        OR (
            snapshot_hlc IS NOT NULL
            AND schema_identity_sha256 IS NOT NULL
            AND export_content_sha256 IS NOT NULL
            AND export_fingerprint IS NOT NULL
            AND export_bucket IS NOT NULL
            AND export_data_key IS NOT NULL
            AND export_data_version_id IS NOT NULL
            AND export_manifest_key IS NOT NULL
            AND export_manifest_version_id IS NOT NULL
            AND export_retention_until IS NOT NULL
            AND export_verified_at IS NOT NULL
        )
    ),
    CONSTRAINT tenant_lifecycle_operations_confirmed_state CHECK (
        status NOT IN ('purging', 'database_purged', 'completed')
        OR (
            confirmed_export_fingerprint = export_fingerprint
            AND purge_confirmed_at IS NOT NULL
        )
    ),
    CONSTRAINT tenant_lifecycle_operations_purged_state CHECK (
        status NOT IN ('database_purged', 'completed')
        OR database_purged_at IS NOT NULL
    ),
    CONSTRAINT tenant_lifecycle_operations_completed_state CHECK (
        status != 'completed' OR completed_at IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_lifecycle_one_active_target_idx
    ON tenant_lifecycle_operations (target_tenant_id)
    WHERE status NOT IN ('completed', 'failed', 'aborted');

CREATE INDEX IF NOT EXISTS tenant_lifecycle_status_lease_idx
    ON tenant_lifecycle_operations (status, lease_expires_at, created_at, id);

CREATE TABLE IF NOT EXISTS tenant_purge_tombstones (
    purge_id UUID PRIMARY KEY,
    tenant_identity_sha256 STRING(64) NOT NULL UNIQUE,
    export_fingerprint STRING(64) NOT NULL,
    schema_identity_sha256 STRING(64) NOT NULL,
    database_purged_at TIMESTAMPTZ NOT NULL,
    purged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_purge_tombstones_identity_hash CHECK (
        tenant_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT tenant_purge_tombstones_export_hash CHECK (
        export_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT tenant_purge_tombstones_schema_hash CHECK (
        schema_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT tenant_purge_tombstones_time_order CHECK (
        purged_at >= database_purged_at
    )
);

CREATE TABLE IF NOT EXISTS tenant_lifecycle_tables (
    table_name STRING PRIMARY KEY,
    table_class STRING NOT NULL,
    tenant_column STRING,
    export_order INT8,
    purge_via_tenant_cascade BOOL NOT NULL,
    CONSTRAINT tenant_lifecycle_tables_class CHECK (
        table_class IN ('tenant_owned', 'tenant_root', 'global', 'control')
    ),
    CONSTRAINT tenant_lifecycle_tables_shape CHECK (
        (
            table_class = 'tenant_owned'
            AND tenant_column = 'tenant_id'
            AND export_order IS NOT NULL
            AND purge_via_tenant_cascade
        )
        OR (
            table_class = 'tenant_root'
            AND tenant_column = 'id'
            AND export_order IS NOT NULL
            AND purge_via_tenant_cascade
        )
        OR (
            table_class IN ('global', 'control')
            AND tenant_column IS NULL
            AND export_order IS NULL
            AND NOT purge_via_tenant_cascade
        )
    ),
    UNIQUE (export_order)
);
