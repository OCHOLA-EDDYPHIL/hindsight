ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS worker_attempt_id UUID,
    ADD COLUMN IF NOT EXISTS worker_attempt_count INT8 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS worker_attempt_command STRING,
    ADD COLUMN IF NOT EXISTS worker_attempt_lease_expires_at TIMESTAMPTZ;

-- Runs left active by the old worker have no durable owner and may no longer
-- have a live queue message. Return them to their dispatchable phase first.
INSERT INTO agent_run_events (run_id, sequence, phase, status, summary, metadata)
SELECT
    run.id,
    COALESCE((
        SELECT max(event.sequence)
        FROM agent_run_events AS event
        WHERE event.run_id = run.id
    ), 0) + 1,
    'recovery',
    CASE WHEN run.status = 'reflecting' THEN 'resuming' ELSE 'queued' END,
    'Recovered an unfenced worker attempt during migration',
    jsonb_build_object('previous_status', run.status)
FROM agent_runs AS run
WHERE run.status IN ('triaging', 'recalling', 'planning', 'reflecting');

UPDATE agent_runs
SET status = CASE WHEN status = 'reflecting' THEN 'resuming' ELSE 'queued' END,
    updated_at = now()
WHERE status IN ('triaging', 'recalling', 'planning', 'reflecting');

DROP TRIGGER IF EXISTS agent_run_dispatch_identity_immutable ON agent_run_dispatches;

INSERT INTO agent_run_dispatches (run_id, command, payload)
SELECT id, 'start', jsonb_build_object('command', 'start', 'run_id', id::STRING)
FROM agent_runs
WHERE status = 'queued'
  AND EXISTS (
      SELECT 1 FROM agent_run_events
      WHERE agent_run_events.run_id = agent_runs.id
        AND agent_run_events.phase = 'recovery'
        AND agent_run_events.summary =
            'Recovered an unfenced worker attempt during migration'
  )
ON CONFLICT (run_id, command) DO UPDATE SET
    status = 'pending',
    attempt_count = 0,
    available_at = now(),
    lease_owner = NULL,
    lease_expires_at = NULL,
    transport_message_id = NULL,
    last_error = NULL,
    updated_at = now(),
    dispatched_at = NULL;

INSERT INTO agent_run_dispatches (run_id, command, payload)
SELECT id, 'resume', jsonb_build_object(
    'command', 'resume',
    'run_id', id::STRING,
    'approved', COALESCE(action_approved, false)
)
FROM agent_runs
WHERE status = 'resuming'
  AND EXISTS (
      SELECT 1 FROM agent_run_events
      WHERE agent_run_events.run_id = agent_runs.id
        AND agent_run_events.phase = 'recovery'
        AND agent_run_events.summary =
            'Recovered an unfenced worker attempt during migration'
  )
ON CONFLICT (run_id, command) DO UPDATE SET
    status = 'pending',
    attempt_count = 0,
    available_at = now(),
    lease_owner = NULL,
    lease_expires_at = NULL,
    transport_message_id = NULL,
    last_error = NULL,
    updated_at = now(),
    dispatched_at = NULL;

CREATE TRIGGER agent_run_dispatch_identity_immutable
BEFORE UPDATE ON agent_run_dispatches
FOR EACH ROW
EXECUTE FUNCTION guard_agent_run_dispatch_identity();

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_worker_attempt_count CHECK (
        (worker_attempt_count = 0 AND worker_attempt_command IS NULL)
        OR (worker_attempt_count >= 1 AND worker_attempt_command IN ('start', 'resume'))
    ),
    ADD CONSTRAINT agent_runs_worker_attempt_lease CHECK (
        (worker_attempt_id IS NULL) = (worker_attempt_lease_expires_at IS NULL)
    ),
    ADD CONSTRAINT agent_runs_worker_attempt_phase CHECK (
        (
            status IN ('triaging', 'recalling', 'planning')
            AND worker_attempt_id IS NOT NULL
            AND worker_attempt_command = 'start'
        )
        OR (
            status = 'reflecting'
            AND worker_attempt_id IS NOT NULL
            AND worker_attempt_command = 'resume'
        )
        OR (
            status IN (
                'queued', 'awaiting_approval', 'resuming',
                'completed', 'rejected', 'failed'
            )
            AND worker_attempt_id IS NULL
        )
    );

CREATE INDEX IF NOT EXISTS agent_runs_worker_attempt_expiry_idx
    ON agent_runs (status, worker_attempt_lease_expires_at);
