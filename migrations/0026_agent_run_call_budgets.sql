-- Persist external-call reservations across worker retries and lease recovery.

-- A resume may discover that the approved memory selection changed and return
-- to awaiting_approval. Preserve every immutable dispatch row while binding
-- deliveries to the exact approval generation that created them.
ALTER TABLE agent_run_dispatches
    ADD COLUMN IF NOT EXISTS command_generation INT8 NOT NULL DEFAULT 0;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS model_call_count INT8 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cloudwatch_call_count INT8 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS command_generation INT8 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS worker_attempt_generation INT8;
