-- Add an append-only, tenant-bound authority and evidence ledger for learning runs.

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_kind;
ALTER TABLE tenants ADD CONSTRAINT tenants_kind CHECK (
    tenant_kind IN (
        'legacy', 'public_demo', 'acceptance', 'diagnostic', 'learning'
    )
);

INSERT INTO tenants (id, slug, tenant_kind)
VALUES ('00000000-0000-0000-0000-000000000004', 'learning', 'learning')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS learning_protocol_authorizations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL DEFAULT
        nullif(current_setting('hindsight.tenant_id', true), '')::UUID,
    authorization_slot STRING NOT NULL UNIQUE,
    authorization_payload JSONB NOT NULL,
    authorization_sha256 STRING NOT NULL UNIQUE,
    protocol_schema_version INT8 NOT NULL,
    protocol_identity_sha256 STRING NOT NULL,
    corpus_sha256 STRING NOT NULL,
    code_sha STRING NOT NULL,
    reasoning_provider STRING NOT NULL,
    reasoning_model STRING NOT NULL,
    embedding_profile_id STRING NOT NULL,
    embedding_provider STRING NOT NULL,
    embedding_model STRING NOT NULL,
    embedding_max_distance FLOAT8 NOT NULL,
    qualification_run_id INT8 NOT NULL,
    qualification_evidence_sha256 STRING NOT NULL,
    product_run_id INT8 NOT NULL,
    product_provenance_sha256 STRING NOT NULL,
    authorized_by STRING NOT NULL,
    authorization_workflow_run_id INT8 NOT NULL,
    authorization_workflow_run_attempt INT8 NOT NULL,
    archive_bucket STRING NOT NULL,
    archive_key STRING NOT NULL,
    archive_version_id STRING NOT NULL,
    archive_sha256 STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT learning_protocol_authorization_slot CHECK (
        authorization_slot = 'protocol-v3-reset-1'
    ),
    CONSTRAINT learning_protocol_authorization_contract CHECK (
        protocol_schema_version = 3
        AND embedding_max_distance = 0.35
    ),
    CONSTRAINT learning_protocol_authorization_run_ids CHECK (
        qualification_run_id > 0
        AND product_run_id > 0
        AND authorization_workflow_run_id > 0
        AND authorization_workflow_run_attempt > 0
    ),
    UNIQUE (tenant_id, id),
    UNIQUE (archive_bucket, archive_key, archive_version_id)
);

CREATE TABLE IF NOT EXISTS learning_execution_authorizations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL DEFAULT
        nullif(current_setting('hindsight.tenant_id', true), '')::UUID,
    protocol_authorization_id UUID NOT NULL,
    sequence INT8 NOT NULL,
    status STRING NOT NULL DEFAULT 'ready',
    authorization_payload JSONB NOT NULL,
    authorization_sha256 STRING NOT NULL UNIQUE,
    authorization_workflow_run_id INT8 NOT NULL,
    authorization_workflow_run_attempt INT8 NOT NULL,
    authorization_archive_key STRING NOT NULL,
    authorization_archive_version_id STRING NOT NULL,
    authorization_archive_sha256 STRING NOT NULL,
    consumer_workflow_run_id INT8,
    consumer_workflow_run_attempt INT8,
    consumer_code_sha STRING,
    consumption_payload JSONB,
    consumption_sha256 STRING,
    consumption_archive_key STRING,
    consumption_archive_version_id STRING,
    consumed_at TIMESTAMPTZ,
    terminal_class STRING,
    terminal_reason STRING,
    terminal_evidence_sha256 STRING,
    finalized_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT learning_execution_sequence CHECK (sequence IN (1, 2)),
    CONSTRAINT learning_execution_status CHECK (
        status IN ('ready', 'consumed', 'finalized')
    ),
    CONSTRAINT learning_execution_terminal_class CHECK (
        terminal_class IS NULL OR terminal_class IN (
            'claim_authorized',
            'not_demonstrated',
            'scientific_terminal',
            'protocol_terminal',
            'infrastructure_outcome_bearing',
            'infrastructure_outcome_free'
        )
    ),
    CONSTRAINT learning_execution_state_fields CHECK (
        (
            status = 'ready'
            AND consumer_workflow_run_id IS NULL
            AND consumer_workflow_run_attempt IS NULL
            AND consumer_code_sha IS NULL
            AND consumption_payload IS NULL
            AND consumption_sha256 IS NULL
            AND consumption_archive_key IS NULL
            AND consumption_archive_version_id IS NULL
            AND consumed_at IS NULL
            AND terminal_class IS NULL
            AND terminal_reason IS NULL
            AND terminal_evidence_sha256 IS NULL
            AND finalized_at IS NULL
        ) OR (
            status = 'consumed'
            AND consumer_workflow_run_id IS NOT NULL
            AND consumer_workflow_run_attempt IS NOT NULL
            AND consumer_code_sha IS NOT NULL
            AND consumption_payload IS NOT NULL
            AND consumption_sha256 IS NOT NULL
            AND consumption_archive_key IS NOT NULL
            AND consumption_archive_version_id IS NOT NULL
            AND consumed_at IS NOT NULL
            AND terminal_class IS NULL
            AND terminal_reason IS NULL
            AND terminal_evidence_sha256 IS NULL
            AND finalized_at IS NULL
        ) OR (
            status = 'finalized'
            AND consumer_workflow_run_id IS NOT NULL
            AND consumer_workflow_run_attempt IS NOT NULL
            AND consumer_code_sha IS NOT NULL
            AND consumption_payload IS NOT NULL
            AND consumption_sha256 IS NOT NULL
            AND consumption_archive_key IS NOT NULL
            AND consumption_archive_version_id IS NOT NULL
            AND consumed_at IS NOT NULL
            AND terminal_class IS NOT NULL
            AND terminal_reason IS NOT NULL
            AND terminal_evidence_sha256 IS NOT NULL
            AND finalized_at IS NOT NULL
        )
    ),
    UNIQUE (tenant_id, id),
    UNIQUE (protocol_authorization_id, sequence),
    UNIQUE (authorization_archive_key, authorization_archive_version_id),
    UNIQUE (consumption_archive_key, consumption_archive_version_id),
    CONSTRAINT learning_execution_protocol_fk FOREIGN KEY (
        tenant_id, protocol_authorization_id
    ) REFERENCES learning_protocol_authorizations (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS learning_evidence_records (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL DEFAULT
        nullif(current_setting('hindsight.tenant_id', true), '')::UUID,
    evidence_kind STRING NOT NULL,
    result STRING NOT NULL,
    protocol_valid BOOL NOT NULL,
    reason_code STRING NOT NULL,
    code_sha STRING NOT NULL,
    protocol_identity_sha256 STRING NOT NULL,
    protocol_authorization_id UUID,
    execution_authorization_id UUID,
    workflow_name STRING NOT NULL,
    workflow_run_id INT8 NOT NULL,
    workflow_run_attempt INT8 NOT NULL,
    canonical_report BYTES NOT NULL,
    canonical_report_sha256 STRING NOT NULL,
    archive_bucket STRING NOT NULL,
    manifest_key STRING NOT NULL,
    manifest_version_id STRING NOT NULL,
    manifest_sha256 STRING NOT NULL,
    retain_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT learning_evidence_kind CHECK (
        evidence_kind IN ('prior_failure', 'qualification', 'study')
    ),
    CONSTRAINT learning_evidence_result CHECK (
        result IN (
            'preserved', 'qualified', 'qualification_failed',
            'accepted', 'not_demonstrated', 'inconclusive'
        )
    ),
    CONSTRAINT learning_evidence_workflow_identity CHECK (
        workflow_run_id > 0 AND workflow_run_attempt > 0
    ),
    UNIQUE (tenant_id, id),
    UNIQUE (workflow_name, workflow_run_id, workflow_run_attempt, evidence_kind),
    UNIQUE (archive_bucket, manifest_key, manifest_version_id),
    CONSTRAINT learning_evidence_protocol_fk FOREIGN KEY (
        tenant_id, protocol_authorization_id
    ) REFERENCES learning_protocol_authorizations (tenant_id, id),
    CONSTRAINT learning_evidence_execution_fk FOREIGN KEY (
        tenant_id, execution_authorization_id
    ) REFERENCES learning_execution_authorizations (tenant_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS learning_evidence_consumed_reset_idx
    ON learning_evidence_records (protocol_authorization_id, evidence_kind)
    WHERE protocol_authorization_id IS NOT NULL AND evidence_kind = 'study';

ALTER TABLE benchmark_experiments
    ADD COLUMN IF NOT EXISTS protocol_authorization_id UUID,
    ADD COLUMN IF NOT EXISTS execution_authorization_id UUID;

ALTER TABLE benchmark_experiments
    ADD CONSTRAINT IF NOT EXISTS benchmark_experiment_protocol_authorization_fk
    FOREIGN KEY (tenant_id, protocol_authorization_id)
    REFERENCES learning_protocol_authorizations (tenant_id, id),
    ADD CONSTRAINT IF NOT EXISTS benchmark_experiment_execution_authorization_fk
    FOREIGN KEY (tenant_id, execution_authorization_id)
    REFERENCES learning_execution_authorizations (tenant_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS benchmark_execution_kind_idx
    ON benchmark_experiments (execution_authorization_id, experiment_kind)
    WHERE execution_authorization_id IS NOT NULL;

CREATE POLICY IF NOT EXISTS learning_protocol_authorizations_tenant_permissive
    ON learning_protocol_authorizations
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY IF NOT EXISTS learning_protocol_authorizations_tenant_fence
    ON learning_protocol_authorizations
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE learning_protocol_authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_protocol_authorizations FORCE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS learning_execution_authorizations_tenant_permissive
    ON learning_execution_authorizations
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY IF NOT EXISTS learning_execution_authorizations_tenant_fence
    ON learning_execution_authorizations
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE learning_execution_authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_execution_authorizations FORCE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS learning_evidence_records_tenant_permissive
    ON learning_evidence_records
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY IF NOT EXISTS learning_evidence_records_tenant_fence
    ON learning_evidence_records
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE learning_evidence_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_evidence_records FORCE ROW LEVEL SECURITY;

DROP TRIGGER IF EXISTS learning_protocol_authorization_immutable
    ON learning_protocol_authorizations;
DROP TRIGGER IF EXISTS learning_evidence_record_immutable
    ON learning_evidence_records;

CREATE OR REPLACE FUNCTION guard_append_only_learning_record()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    RAISE EXCEPTION 'learning authorization and evidence records are append-only';
END
$$;

DROP TRIGGER IF EXISTS learning_protocol_authorization_immutable
    ON learning_protocol_authorizations;
CREATE TRIGGER learning_protocol_authorization_immutable
BEFORE UPDATE OR DELETE ON learning_protocol_authorizations
FOR EACH ROW
EXECUTE FUNCTION guard_append_only_learning_record();

DROP TRIGGER IF EXISTS learning_evidence_record_immutable
    ON learning_evidence_records;
CREATE TRIGGER learning_evidence_record_immutable
BEFORE UPDATE OR DELETE ON learning_evidence_records
FOR EACH ROW
EXECUTE FUNCTION guard_append_only_learning_record();

DROP TRIGGER IF EXISTS learning_execution_authorization_guarded
    ON learning_execution_authorizations;

CREATE OR REPLACE FUNCTION guard_learning_execution_authorization()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'learning execution authorizations cannot be deleted';
    END IF;
    IF (NEW) IS NOT DISTINCT FROM (OLD) THEN
        RETURN NEW;
    END IF;
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).tenant_id IS DISTINCT FROM (OLD).tenant_id
        OR (NEW).protocol_authorization_id IS DISTINCT FROM (OLD).protocol_authorization_id
        OR (NEW).sequence IS DISTINCT FROM (OLD).sequence
        OR (NEW).authorization_payload IS DISTINCT FROM (OLD).authorization_payload
        OR (NEW).authorization_sha256 IS DISTINCT FROM (OLD).authorization_sha256
        OR (NEW).authorization_workflow_run_id IS DISTINCT FROM (OLD).authorization_workflow_run_id
        OR (NEW).authorization_workflow_run_attempt IS DISTINCT FROM (OLD).authorization_workflow_run_attempt
        OR (NEW).authorization_archive_key IS DISTINCT FROM (OLD).authorization_archive_key
        OR (NEW).authorization_archive_version_id IS DISTINCT FROM (OLD).authorization_archive_version_id
        OR (NEW).authorization_archive_sha256 IS DISTINCT FROM (OLD).authorization_archive_sha256
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
    THEN
        RAISE EXCEPTION 'learning execution authorization identity is immutable';
    END IF;
    IF (OLD).status = 'ready' AND (NEW).status = 'consumed' THEN
        IF (NEW).consumer_workflow_run_id IS NULL
            OR (NEW).consumer_workflow_run_attempt IS NULL
            OR (NEW).consumer_code_sha IS NULL
            OR (NEW).consumption_payload IS NULL
            OR (NEW).consumption_sha256 IS NULL
            OR (NEW).consumption_archive_key IS NULL
            OR (NEW).consumption_archive_version_id IS NULL
            OR (NEW).consumed_at IS NULL
            OR (NEW).terminal_class IS NOT NULL
            OR (NEW).terminal_reason IS NOT NULL
            OR (NEW).terminal_evidence_sha256 IS NOT NULL
            OR (NEW).finalized_at IS NOT NULL
        THEN
            RAISE EXCEPTION 'learning execution consumption is incomplete';
        END IF;
        RETURN NEW;
    END IF;
    IF (OLD).status = 'consumed' AND (NEW).status = 'finalized' THEN
        IF (NEW).consumer_workflow_run_id IS DISTINCT FROM (OLD).consumer_workflow_run_id
            OR (NEW).consumer_workflow_run_attempt IS DISTINCT FROM (OLD).consumer_workflow_run_attempt
            OR (NEW).consumer_code_sha IS DISTINCT FROM (OLD).consumer_code_sha
            OR (NEW).consumption_payload IS DISTINCT FROM (OLD).consumption_payload
            OR (NEW).consumption_sha256 IS DISTINCT FROM (OLD).consumption_sha256
            OR (NEW).consumption_archive_key IS DISTINCT FROM (OLD).consumption_archive_key
            OR (NEW).consumption_archive_version_id IS DISTINCT FROM (OLD).consumption_archive_version_id
            OR (NEW).consumed_at IS DISTINCT FROM (OLD).consumed_at
            OR (NEW).terminal_class IS NULL
            OR (NEW).terminal_reason IS NULL
            OR (NEW).terminal_evidence_sha256 IS NULL
            OR (NEW).finalized_at IS NULL
        THEN
            RAISE EXCEPTION 'learning execution finalization is incomplete';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid learning execution authorization transition';
END
$$;

DROP TRIGGER IF EXISTS learning_execution_authorization_guarded
    ON learning_execution_authorizations;
CREATE TRIGGER learning_execution_authorization_guarded
BEFORE UPDATE OR DELETE ON learning_execution_authorizations
FOR EACH ROW
EXECUTE FUNCTION guard_learning_execution_authorization();

DROP TRIGGER IF EXISTS benchmark_experiment_contract_immutable
    ON benchmark_experiments;

CREATE OR REPLACE FUNCTION guard_benchmark_experiment_contract()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).tenant_id IS DISTINCT FROM (OLD).tenant_id
        OR (NEW).experiment_kind IS DISTINCT FROM (OLD).experiment_kind
        OR (NEW).manifest IS DISTINCT FROM (OLD).manifest
        OR (NEW).manifest_sha256 IS DISTINCT FROM (OLD).manifest_sha256
        OR (NEW).preregistration IS DISTINCT FROM (OLD).preregistration
        OR (NEW).preregistration_sha256 IS DISTINCT FROM (OLD).preregistration_sha256
        OR (NEW).provider IS DISTINCT FROM (OLD).provider
        OR (NEW).model IS DISTINCT FROM (OLD).model
        OR (NEW).embedding_profile_id IS DISTINCT FROM (OLD).embedding_profile_id
        OR (NEW).study_key_sha256 IS DISTINCT FROM (OLD).study_key_sha256
        OR (NEW).claim_family_sha256 IS DISTINCT FROM (OLD).claim_family_sha256
        OR (NEW).code_sha IS DISTINCT FROM (OLD).code_sha
        OR (NEW).protocol_authorization_id IS DISTINCT FROM (OLD).protocol_authorization_id
        OR (NEW).execution_authorization_id IS DISTINCT FROM (OLD).execution_authorization_id
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
        OR (OLD).status IN ('completed', 'incomplete', 'failed')
    THEN
        RAISE EXCEPTION 'benchmark contract and terminal experiment are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_experiment_contract_immutable
BEFORE UPDATE ON benchmark_experiments
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_experiment_contract();
