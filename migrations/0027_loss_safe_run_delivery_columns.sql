-- Commit additive delivery columns before CockroachDB validates their guards.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS request_fingerprint STRING;

ALTER TABLE agent_run_dispatches
    ADD COLUMN IF NOT EXISTS acknowledged_attempt_id UUID,
    ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS agent_run_dispatch_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL DEFAULT
        nullif(current_setting('hindsight.tenant_id', true), '')::UUID,
    dispatch_id UUID NOT NULL,
    sequence INT8 NOT NULL,
    lease_owner UUID,
    lease_expires_at TIMESTAMPTZ,
    transport_message_id STRING,
    sent_at TIMESTAMPTZ,
    worker_message_id STRING,
    acknowledged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
