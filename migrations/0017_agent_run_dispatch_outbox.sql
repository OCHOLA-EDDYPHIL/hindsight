CREATE TABLE IF NOT EXISTS agent_run_dispatches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs (id),
    command STRING NOT NULL,
    payload JSONB NOT NULL,
    status STRING NOT NULL DEFAULT 'pending',
    attempt_count INT8 NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner UUID,
    lease_expires_at TIMESTAMPTZ,
    transport_message_id STRING,
    last_error STRING,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at TIMESTAMPTZ,
    UNIQUE (run_id, command),
    CONSTRAINT agent_run_dispatches_command CHECK (command IN ('start', 'resume')),
    CONSTRAINT agent_run_dispatches_payload CHECK (
        jsonb_typeof(payload) = 'object'
        AND payload->>'command' = command
        AND payload->>'run_id' = run_id::STRING
        AND (
            command = 'start'
            OR COALESCE(jsonb_typeof(payload->'approved') = 'boolean', false)
        )
    ),
    CONSTRAINT agent_run_dispatches_status CHECK (status IN ('pending', 'leased', 'sent')),
    CONSTRAINT agent_run_dispatches_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT agent_run_dispatches_lease_state CHECK (
        (
            status = 'pending'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND transport_message_id IS NULL
            AND dispatched_at IS NULL
        )
        OR (
            status = 'leased'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND attempt_count >= 1
            AND transport_message_id IS NULL
            AND dispatched_at IS NULL
        )
        OR (
            status = 'sent'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND attempt_count >= 1
            AND transport_message_id IS NOT NULL
            AND dispatched_at IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS agent_run_dispatches_pending_idx
    ON agent_run_dispatches (status, available_at, lease_expires_at, created_at);

CREATE OR REPLACE FUNCTION guard_agent_run_dispatch_identity()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).run_id IS DISTINCT FROM (OLD).run_id
        OR (NEW).command IS DISTINCT FROM (OLD).command
        OR (NEW).payload IS DISTINCT FROM (OLD).payload
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
        OR ((OLD).status = 'sent' AND (NEW).status IS DISTINCT FROM (OLD).status)
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

-- Preserve queued work created before the outbox existed. A duplicate delivery
-- is harmless because worker claims compare the run's expected phase status.
INSERT INTO agent_run_dispatches (run_id, command, payload)
SELECT id, 'start', jsonb_build_object('command', 'start', 'run_id', id::STRING)
FROM agent_runs
WHERE status = 'queued'
ON CONFLICT (run_id, command) DO NOTHING;

INSERT INTO agent_run_dispatches (run_id, command, payload)
SELECT id, 'resume', jsonb_build_object(
    'command', 'resume',
    'run_id', id::STRING,
    'approved', action_approved
)
FROM agent_runs
WHERE status = 'resuming'
ON CONFLICT (run_id, command) DO NOTHING;

CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_memory_worker LOGIN;

GRANT SELECT, INSERT, UPDATE ON TABLE agent_run_dispatches
TO hindsight_agent_writer;

GRANT SELECT, UPDATE ON TABLE agent_run_dispatches
TO hindsight_memory_worker;
