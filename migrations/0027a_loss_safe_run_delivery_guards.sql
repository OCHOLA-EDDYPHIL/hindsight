-- Apply backfill, relationship, and state guards only after CockroachDB has
-- committed the schema changes in 0027_loss_safe_run_delivery_columns.sql.

-- A failed schema-change transaction can leave an earlier trigger installed.
-- Rebuild both identity guards around all retryable delivery-state changes.
DROP TRIGGER IF EXISTS agent_run_dispatch_identity_immutable
ON agent_run_dispatches;
DROP TRIGGER IF EXISTS agent_run_dispatch_attempt_identity_immutable
ON agent_run_dispatch_attempts;

-- Existing sent rows have no durable attempt identity, so they cannot prove a
-- worker received the command. Make all pre-attempt deliveries eligible again.
UPDATE agent_run_dispatches AS dispatch
SET status = 'pending',
    attempt_count = 0,
    lease_owner = NULL,
    lease_expires_at = NULL,
    transport_message_id = NULL,
    dispatched_at = NULL,
    acknowledged_attempt_id = NULL,
    acknowledged_at = NULL,
    updated_at = now()
WHERE dispatch.status IN ('pending', 'leased', 'sent')
  AND dispatch.acknowledged_attempt_id IS NULL
  AND NOT EXISTS (
      SELECT 1
      FROM agent_run_dispatch_attempts AS attempt
      WHERE attempt.dispatch_id = dispatch.id
  );

-- CockroachDB implements the inline legacy UNIQUE constraint as an index.
DROP INDEX IF EXISTS agent_runs@agent_runs_idempotency_key_key CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_tenant_idempotency_key_idx
    ON agent_runs (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- A keyed row created before this migration has no canonical digest. Keep that
-- NULL until the API safely compares its stored fields and seals the digest.
ALTER TABLE agent_runs
    ADD CONSTRAINT IF NOT EXISTS agent_runs_request_fingerprint CHECK (
        request_fingerprint IS NULL
        OR (
            idempotency_key IS NOT NULL
            AND request_fingerprint ~ '^[0-9a-f]{64}$'
        )
    );

CREATE UNIQUE INDEX IF NOT EXISTS agent_run_dispatch_attempts_dispatch_sequence_key
    ON agent_run_dispatch_attempts (dispatch_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS agent_run_dispatch_attempts_tenant_id_idx
    ON agent_run_dispatch_attempts (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS agent_run_dispatch_attempts_tenant_dispatch_id_idx
    ON agent_run_dispatch_attempts (tenant_id, dispatch_id, id);

ALTER TABLE agent_run_dispatch_attempts
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatch_attempts_sequence CHECK (
        sequence >= 1
    ),
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatch_attempts_lease_state CHECK (
        (lease_owner IS NULL) = (lease_expires_at IS NULL)
    ),
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatch_attempts_transport_state CHECK (
        (transport_message_id IS NULL) = (sent_at IS NULL)
    ),
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatch_attempts_acknowledgement_state CHECK (
        (worker_message_id IS NULL) = (acknowledged_at IS NULL)
    ),
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatch_attempts_tenant_fk
        FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatch_attempts_tenant_dispatch_fk
        FOREIGN KEY (tenant_id, dispatch_id)
        REFERENCES agent_run_dispatches (tenant_id, id);

ALTER TABLE agent_run_dispatches
    DROP CONSTRAINT IF EXISTS agent_run_dispatches_status,
    DROP CONSTRAINT IF EXISTS agent_run_dispatches_lease_state;

ALTER TABLE agent_run_dispatches
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatches_status CHECK (
        status IN ('pending', 'leased', 'sent', 'acknowledged')
    ),
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatches_lease_state CHECK (
        attempt_count >= 0
        AND (lease_owner IS NULL) = (lease_expires_at IS NULL)
        AND (transport_message_id IS NULL) = (dispatched_at IS NULL)
        AND (acknowledged_attempt_id IS NULL) = (acknowledged_at IS NULL)
        AND (
            (
                status = 'pending'
                AND lease_owner IS NULL
                AND transport_message_id IS NULL
                AND acknowledged_attempt_id IS NULL
            )
            OR (
                status = 'leased'
                AND lease_owner IS NOT NULL
                AND attempt_count >= 1
                AND transport_message_id IS NULL
                AND acknowledged_attempt_id IS NULL
            )
            OR (
                status = 'sent'
                AND lease_owner IS NULL
                AND attempt_count >= 1
                AND transport_message_id IS NOT NULL
                AND acknowledged_attempt_id IS NULL
            )
            OR (
                status = 'acknowledged'
                AND attempt_count >= 1
                AND acknowledged_attempt_id IS NOT NULL
            )
        )
    ),
    ADD CONSTRAINT IF NOT EXISTS agent_run_dispatches_acknowledged_attempt_fk
        FOREIGN KEY (tenant_id, id, acknowledged_attempt_id)
        REFERENCES agent_run_dispatch_attempts (tenant_id, dispatch_id, id);

CREATE POLICY IF NOT EXISTS agent_run_dispatch_attempts_tenant_permissive
ON agent_run_dispatch_attempts
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
CREATE POLICY IF NOT EXISTS agent_run_dispatch_attempts_tenant_fence
ON agent_run_dispatch_attempts
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (tenant_id = current_hindsight_tenant_id())
    WITH CHECK (tenant_id = current_hindsight_tenant_id());
ALTER TABLE agent_run_dispatch_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_run_dispatch_attempts FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION guard_agent_run_dispatch_identity()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).tenant_id IS DISTINCT FROM (OLD).tenant_id
        OR (NEW).run_id IS DISTINCT FROM (OLD).run_id
        OR (NEW).command IS DISTINCT FROM (OLD).command
        OR (NEW).command_generation IS DISTINCT FROM (OLD).command_generation
        OR (NEW).payload IS DISTINCT FROM (OLD).payload
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
    THEN
        RAISE EXCEPTION 'agent run dispatch identity is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER agent_run_dispatch_identity_immutable
BEFORE UPDATE ON agent_run_dispatches
FOR EACH ROW
EXECUTE FUNCTION guard_agent_run_dispatch_identity();

CREATE OR REPLACE FUNCTION guard_agent_run_dispatch_attempt_identity()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).tenant_id IS DISTINCT FROM (OLD).tenant_id
        OR (NEW).dispatch_id IS DISTINCT FROM (OLD).dispatch_id
        OR (NEW).sequence IS DISTINCT FROM (OLD).sequence
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
    THEN
        RAISE EXCEPTION 'agent run dispatch attempt identity is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER agent_run_dispatch_attempt_identity_immutable
BEFORE UPDATE ON agent_run_dispatch_attempts
FOR EACH ROW
EXECUTE FUNCTION guard_agent_run_dispatch_attempt_identity();

CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_memory_worker LOGIN;

GRANT SELECT, INSERT, UPDATE ON TABLE agent_run_dispatch_attempts
TO hindsight_agent_writer;
GRANT SELECT, UPDATE ON TABLE agent_run_dispatch_attempts
TO hindsight_memory_worker;
