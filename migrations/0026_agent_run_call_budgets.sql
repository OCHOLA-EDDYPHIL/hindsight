-- Persist external-call reservations across worker retries and lease recovery.

-- A resume may discover that the approved memory selection changed and return
-- to awaiting_approval. Preserve every immutable dispatch row while binding
-- deliveries to the exact approval generation that created them.
ALTER TABLE agent_run_dispatches
    ADD COLUMN IF NOT EXISTS command_generation INT8 NOT NULL DEFAULT 0;

ALTER TABLE agent_run_dispatches
    DROP CONSTRAINT IF EXISTS agent_run_dispatches_run_id_command_key;

ALTER TABLE agent_run_dispatches
    ADD CONSTRAINT agent_run_dispatches_run_command_generation_key
    UNIQUE (run_id, command, command_generation),
    ADD CONSTRAINT agent_run_dispatches_command_generation CHECK (
        command_generation >= 0
        AND COALESCE((payload->>'command_generation')::INT8, 0) = command_generation
    );

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS model_call_count INT8 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cloudwatch_call_count INT8 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS command_generation INT8 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS worker_attempt_generation INT8;

UPDATE agent_runs
SET worker_attempt_generation = command_generation
WHERE worker_attempt_id IS NOT NULL AND worker_attempt_generation IS NULL;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_model_call_budget CHECK (
        model_call_count BETWEEN 0 AND 4
    ),
    ADD CONSTRAINT agent_runs_cloudwatch_call_budget CHECK (
        cloudwatch_call_count BETWEEN 0 AND 3
    ),
    ADD CONSTRAINT agent_runs_command_generation CHECK (
        command_generation >= 0
    );

ALTER TABLE agent_runs
    DROP CONSTRAINT IF EXISTS agent_runs_worker_attempt_phase;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_worker_attempt_phase CHECK (
        (
            status IN ('triaging', 'recalling')
            AND worker_attempt_id IS NOT NULL
            AND worker_attempt_command = 'start'
            AND worker_attempt_generation = command_generation
        )
        OR (
            status = 'planning'
            AND worker_attempt_id IS NOT NULL
            AND worker_attempt_command IN ('start', 'resume')
            AND worker_attempt_generation = command_generation
        )
        OR (
            status = 'reflecting'
            AND worker_attempt_id IS NOT NULL
            AND worker_attempt_command = 'resume'
            AND worker_attempt_generation = command_generation
        )
        OR (
            status IN (
                'queued', 'awaiting_approval', 'resuming',
                'completed', 'rejected', 'failed'
            )
            AND worker_attempt_id IS NULL
        )
    );

CREATE OR REPLACE FUNCTION guard_agent_run_call_budgets()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).model_call_count < (OLD).model_call_count
        OR (NEW).model_call_count > (OLD).model_call_count + 1
        OR (NEW).cloudwatch_call_count < (OLD).cloudwatch_call_count
        OR (NEW).cloudwatch_call_count > (OLD).cloudwatch_call_count + 1
        OR (NEW).command_generation < (OLD).command_generation
        OR (NEW).command_generation > (OLD).command_generation + 1
    THEN
        RAISE EXCEPTION 'agent run call budgets must advance monotonically';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS agent_run_call_budgets_monotonic ON agent_runs;
CREATE TRIGGER agent_run_call_budgets_monotonic
BEFORE UPDATE ON agent_runs
FOR EACH ROW
EXECUTE FUNCTION guard_agent_run_call_budgets();

CREATE OR REPLACE FUNCTION guard_agent_run_dispatch_identity()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).run_id IS DISTINCT FROM (OLD).run_id
        OR (NEW).command IS DISTINCT FROM (OLD).command
        OR (NEW).command_generation IS DISTINCT FROM (OLD).command_generation
        OR (NEW).payload IS DISTINCT FROM (OLD).payload
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
        OR ((OLD).status = 'sent' AND (NEW).status IS DISTINCT FROM (OLD).status)
    THEN
        RAISE EXCEPTION 'agent run dispatch identity is immutable';
    END IF;
    RETURN NEW;
END
$$;
