ALTER TABLE incident_events
    ADD COLUMN IF NOT EXISTS event_schema STRING NOT NULL DEFAULT 'incident_event.v1',
    ADD COLUMN IF NOT EXISTS payload_digest STRING,
    ADD COLUMN IF NOT EXISTS structured_payload JSONB NOT NULL DEFAULT '{}'::JSONB;

ALTER TABLE incidents
    ADD CONSTRAINT incidents_resolution_event_fk
        FOREIGN KEY (resolution_event_id) REFERENCES incident_events (id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS consolidation_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents (id),
    source_event_id UUID REFERENCES incident_events (id),
    decision_id STRING REFERENCES memory_decisions (id),
    status STRING NOT NULL DEFAULT 'queued',
    attempt_count INT8 NOT NULL DEFAULT 0,
    lease_owner STRING,
    lease_expires_at TIMESTAMPTZ,
    lesson_belief_id UUID REFERENCES semantic_beliefs (id),
    lesson_memory_id UUID REFERENCES semantic_memories (id),
    reason STRING,
    error_code STRING,
    error_detail STRING,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (incident_id, source_event_id),
    CONSTRAINT consolidation_jobs_status CHECK (
        status IN ('queued', 'leased', 'retrying', 'completed', 'not_eligible', 'failed')
    )
);

CREATE TABLE IF NOT EXISTS demo_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    demo_kind STRING NOT NULL,
    namespace STRING NOT NULL UNIQUE,
    status STRING NOT NULL DEFAULT 'active',
    created_by STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ,
    CONSTRAINT demo_sessions_status CHECK (status IN ('active', 'archived'))
);

CREATE TABLE IF NOT EXISTS benchmark_experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_kind STRING NOT NULL,
    status STRING NOT NULL DEFAULT 'created',
    manifest JSONB NOT NULL,
    manifest_sha256 STRING NOT NULL,
    preregistration JSONB,
    preregistration_sha256 STRING,
    provider STRING NOT NULL,
    model STRING NOT NULL,
    embedding_profile_id STRING REFERENCES embedding_profiles (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT benchmark_experiment_kind CHECK (
        experiment_kind IN ('pilot', 'confirmation', 'ci_smoke')
    ),
    CONSTRAINT benchmark_experiment_status CHECK (
        status IN ('created', 'running', 'completed', 'incomplete', 'failed')
    )
);

CREATE TABLE IF NOT EXISTS benchmark_trials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id UUID NOT NULL REFERENCES benchmark_experiments (id) ON DELETE CASCADE,
    variant_id STRING NOT NULL,
    repetition INT8 NOT NULL,
    arm STRING NOT NULL,
    namespace STRING NOT NULL,
    status STRING NOT NULL DEFAULT 'queued',
    lesson_memory_id UUID REFERENCES semantic_memories (id),
    recovered BOOL,
    action_count INT8,
    penalized_action_count INT8,
    unsafe_action_count INT8,
    elapsed_ms INT8,
    input_tokens INT8,
    output_tokens INT8,
    failure_code STRING,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (experiment_id, variant_id, repetition, arm),
    CONSTRAINT benchmark_trial_arm CHECK (
        arm IN ('no_lesson', 'gold_lesson', 'consolidated_lesson')
    ),
    CONSTRAINT benchmark_trial_status CHECK (
        status IN ('queued', 'running', 'completed', 'infrastructure_failed', 'invalid')
    )
);

CREATE TABLE IF NOT EXISTS benchmark_actions (
    trial_id UUID NOT NULL REFERENCES benchmark_trials (id) ON DELETE CASCADE,
    step INT8 NOT NULL,
    decision_id STRING NOT NULL REFERENCES memory_decisions (id),
    retrieval_id UUID REFERENCES memory_retrievals (id),
    action STRING NOT NULL,
    observation JSONB NOT NULL,
    cited_memory_ids JSONB NOT NULL DEFAULT '[]'::JSONB,
    unsafe BOOL NOT NULL DEFAULT false,
    recovered BOOL NOT NULL DEFAULT false,
    usage JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trial_id, step)
);

CREATE INDEX IF NOT EXISTS consolidation_jobs_status_idx
    ON consolidation_jobs (status, created_at);

CREATE INDEX IF NOT EXISTS benchmark_trials_experiment_idx
    ON benchmark_trials (experiment_id, variant_id, repetition, arm);

CREATE OR REPLACE FUNCTION guard_benchmark_experiment_contract()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).experiment_kind IS DISTINCT FROM (OLD).experiment_kind
        OR (NEW).manifest IS DISTINCT FROM (OLD).manifest
        OR (NEW).manifest_sha256 IS DISTINCT FROM (OLD).manifest_sha256
        OR (NEW).preregistration IS DISTINCT FROM (OLD).preregistration
        OR (NEW).preregistration_sha256 IS DISTINCT FROM (OLD).preregistration_sha256
        OR (NEW).provider IS DISTINCT FROM (OLD).provider
        OR (NEW).model IS DISTINCT FROM (OLD).model
        OR (NEW).embedding_profile_id IS DISTINCT FROM (OLD).embedding_profile_id
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

CREATE OR REPLACE FUNCTION guard_benchmark_trial_trace()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).experiment_id IS DISTINCT FROM (OLD).experiment_id
        OR (NEW).variant_id IS DISTINCT FROM (OLD).variant_id
        OR (NEW).repetition IS DISTINCT FROM (OLD).repetition
        OR (NEW).arm IS DISTINCT FROM (OLD).arm
        OR (NEW).namespace IS DISTINCT FROM (OLD).namespace
        OR (NEW).lesson_memory_id IS DISTINCT FROM (OLD).lesson_memory_id
        OR (NEW).started_at IS DISTINCT FROM (OLD).started_at
        OR (OLD).status IN ('completed', 'infrastructure_failed', 'invalid')
    THEN
        RAISE EXCEPTION 'benchmark trial identity and terminal trace are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_trial_trace_immutable
BEFORE UPDATE ON benchmark_trials
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_trial_trace();

CREATE OR REPLACE FUNCTION guard_benchmark_action_immutable()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    RAISE EXCEPTION 'benchmark action traces are immutable';
END
$$;

CREATE TRIGGER benchmark_action_trace_immutable
BEFORE UPDATE ON benchmark_actions
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_action_immutable();
