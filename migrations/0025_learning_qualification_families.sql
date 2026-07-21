-- Fence terminal qualification families independently from code revisions.

CREATE TABLE IF NOT EXISTS learning_qualification_attempts (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL DEFAULT
        nullif(current_setting('hindsight.tenant_id', true), '')::UUID,
    family_sha256 STRING NOT NULL,
    sequence INT8 NOT NULL,
    family_contract JSONB NOT NULL,
    status STRING NOT NULL,
    authorization_payload JSONB NOT NULL,
    authorization_sha256 STRING NOT NULL UNIQUE,
    authorization_archive_key STRING NOT NULL,
    authorization_archive_version_id STRING NOT NULL,
    consumption_payload JSONB NOT NULL,
    consumption_sha256 STRING NOT NULL UNIQUE,
    consumption_archive_key STRING NOT NULL,
    consumption_archive_version_id STRING NOT NULL,
    consumer_workflow_run_id INT8 NOT NULL,
    consumer_workflow_run_attempt INT8 NOT NULL,
    consumer_code_sha STRING NOT NULL,
    consumed_at TIMESTAMPTZ NOT NULL,
    qualification_status STRING,
    terminal_class STRING,
    finalization_payload JSONB,
    finalization_sha256 STRING UNIQUE,
    finalization_archive_key STRING,
    finalization_archive_version_id STRING,
    finalized_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT learning_qualification_attempt_sequence CHECK (sequence IN (1, 2)),
    CONSTRAINT learning_qualification_attempt_status CHECK (
        status IN ('consumed', 'finalized')
    ),
    CONSTRAINT learning_qualification_attempt_terminal CHECK (
        terminal_class IS NULL OR terminal_class IN (
            'qualified', 'scientific_failed', 'protocol_terminal',
            'infrastructure_outcome_bearing', 'infrastructure_outcome_free'
        )
    ),
    CONSTRAINT learning_qualification_attempt_state CHECK (
        (
            status = 'consumed'
            AND qualification_status IS NULL
            AND terminal_class IS NULL
            AND finalization_payload IS NULL
            AND finalization_sha256 IS NULL
            AND finalization_archive_key IS NULL
            AND finalization_archive_version_id IS NULL
            AND finalized_at IS NULL
        ) OR (
            status = 'finalized'
            AND qualification_status IS NOT NULL
            AND terminal_class IS NOT NULL
            AND finalization_payload IS NOT NULL
            AND finalization_sha256 IS NOT NULL
            AND finalization_archive_key IS NOT NULL
            AND finalization_archive_version_id IS NOT NULL
            AND finalized_at IS NOT NULL
        )
    ),
    UNIQUE (tenant_id, id),
    UNIQUE (family_sha256, sequence),
    UNIQUE (authorization_archive_key, authorization_archive_version_id),
    UNIQUE (consumption_archive_key, consumption_archive_version_id),
    UNIQUE (finalization_archive_key, finalization_archive_version_id)
);

CREATE TABLE IF NOT EXISTS learning_qualification_family_terminals (
    family_sha256 STRING PRIMARY KEY,
    tenant_id UUID NOT NULL DEFAULT
        nullif(current_setting('hindsight.tenant_id', true), '')::UUID,
    family_contract JSONB NOT NULL,
    terminal_class STRING NOT NULL,
    qualification_status STRING NOT NULL,
    terminal_payload JSONB NOT NULL,
    terminal_sha256 STRING NOT NULL UNIQUE,
    archive_bucket STRING NOT NULL,
    archive_key STRING NOT NULL,
    archive_version_id STRING NOT NULL,
    manifest_key STRING NOT NULL,
    manifest_version_id STRING NOT NULL,
    manifest_sha256 STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT learning_qualification_family_terminal_class CHECK (
        terminal_class IN (
            'qualified', 'scientific_failed', 'protocol_terminal',
            'infrastructure_outcome_bearing', 'infrastructure_outcome_free'
        )
    ),
    UNIQUE (tenant_id, family_sha256),
    UNIQUE (archive_bucket, archive_key, archive_version_id),
    UNIQUE (archive_bucket, manifest_key, manifest_version_id)
);

CREATE POLICY IF NOT EXISTS learning_qualification_attempts_tenant_permissive
    ON learning_qualification_attempts
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY IF NOT EXISTS learning_qualification_attempts_tenant_fence
    ON learning_qualification_attempts
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE learning_qualification_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_qualification_attempts FORCE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS learning_qualification_terminals_tenant_permissive
    ON learning_qualification_family_terminals
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY IF NOT EXISTS learning_qualification_terminals_tenant_fence
    ON learning_qualification_family_terminals
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE learning_qualification_family_terminals ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_qualification_family_terminals FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION guard_learning_qualification_attempt()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'learning qualification attempts cannot be deleted';
    END IF;
    IF (NEW) IS NOT DISTINCT FROM (OLD) THEN
        RETURN NEW;
    END IF;
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).tenant_id IS DISTINCT FROM (OLD).tenant_id
        OR (NEW).family_sha256 IS DISTINCT FROM (OLD).family_sha256
        OR (NEW).sequence IS DISTINCT FROM (OLD).sequence
        OR (NEW).family_contract IS DISTINCT FROM (OLD).family_contract
        OR (NEW).authorization_payload IS DISTINCT FROM (OLD).authorization_payload
        OR (NEW).authorization_sha256 IS DISTINCT FROM (OLD).authorization_sha256
        OR (NEW).authorization_archive_key IS DISTINCT FROM (OLD).authorization_archive_key
        OR (NEW).authorization_archive_version_id IS DISTINCT FROM (OLD).authorization_archive_version_id
        OR (NEW).consumption_payload IS DISTINCT FROM (OLD).consumption_payload
        OR (NEW).consumption_sha256 IS DISTINCT FROM (OLD).consumption_sha256
        OR (NEW).consumption_archive_key IS DISTINCT FROM (OLD).consumption_archive_key
        OR (NEW).consumption_archive_version_id IS DISTINCT FROM (OLD).consumption_archive_version_id
        OR (NEW).consumer_workflow_run_id IS DISTINCT FROM (OLD).consumer_workflow_run_id
        OR (NEW).consumer_workflow_run_attempt IS DISTINCT FROM (OLD).consumer_workflow_run_attempt
        OR (NEW).consumer_code_sha IS DISTINCT FROM (OLD).consumer_code_sha
        OR (NEW).consumed_at IS DISTINCT FROM (OLD).consumed_at
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
    THEN
        RAISE EXCEPTION 'learning qualification attempt identity is immutable';
    END IF;
    IF (OLD).status = 'consumed' AND (NEW).status = 'finalized'
        AND (NEW).qualification_status IS NOT NULL
        AND (NEW).terminal_class IS NOT NULL
        AND (NEW).finalization_payload IS NOT NULL
        AND (NEW).finalization_sha256 IS NOT NULL
        AND (NEW).finalization_archive_key IS NOT NULL
        AND (NEW).finalization_archive_version_id IS NOT NULL
        AND (NEW).finalized_at IS NOT NULL
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid learning qualification attempt transition';
END
$$;

DROP TRIGGER IF EXISTS learning_qualification_attempt_guarded
    ON learning_qualification_attempts;
CREATE TRIGGER learning_qualification_attempt_guarded
BEFORE UPDATE OR DELETE ON learning_qualification_attempts
FOR EACH ROW
EXECUTE FUNCTION guard_learning_qualification_attempt();

DROP TRIGGER IF EXISTS learning_qualification_terminal_immutable
    ON learning_qualification_family_terminals;
CREATE TRIGGER learning_qualification_terminal_immutable
BEFORE UPDATE OR DELETE ON learning_qualification_family_terminals
FOR EACH ROW
EXECUTE FUNCTION guard_append_only_learning_record();
