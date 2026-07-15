-- Keep this migration schema-only. CockroachDB must commit these schema
-- changes before the following migration backfills or installs triggers that
-- access the new tables and columns.

ALTER TABLE incidents
    ADD COLUMN IF NOT EXISTS consolidation_policy STRING NOT NULL DEFAULT 'managed',
    ADD CONSTRAINT incidents_consolidation_policy CHECK (
        consolidation_policy IN ('managed', 'manual')
    );

ALTER TABLE benchmark_experiments
    ADD COLUMN IF NOT EXISTS study_key_sha256 STRING,
    ADD COLUMN IF NOT EXISTS claim_family_sha256 STRING,
    ADD COLUMN IF NOT EXISTS code_sha STRING;

ALTER TABLE benchmark_trials
    DROP CONSTRAINT IF EXISTS benchmark_trial_arm;

ALTER TABLE benchmark_trials
    ADD CONSTRAINT benchmark_trial_arm_v2 CHECK (
        arm IN (
            'no_lesson',
            'gold_lesson',
            'reference_lesson',
            'consolidated_lesson'
        )
    );

CREATE TABLE IF NOT EXISTS benchmark_confirmation_preregistrations (
    pilot_experiment_id UUID PRIMARY KEY REFERENCES benchmark_experiments (id),
    preregistration JSONB NOT NULL,
    preregistration_sha256 STRING NOT NULL UNIQUE,
    confirmation_experiment_id UUID UNIQUE REFERENCES benchmark_experiments (id),
    prepared_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    bound_at TIMESTAMPTZ,
    CONSTRAINT benchmark_preregistration_binding CHECK (
        (confirmation_experiment_id IS NULL AND bound_at IS NULL)
        OR (confirmation_experiment_id IS NOT NULL AND bound_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS benchmark_confirmation_bindings (
    confirmation_experiment_id UUID PRIMARY KEY REFERENCES benchmark_experiments (id),
    pilot_experiment_id UUID NOT NULL REFERENCES benchmark_experiments (id),
    preregistration_sha256 STRING NOT NULL,
    binding_sequence INT8 NOT NULL,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pilot_experiment_id, binding_sequence),
    CONSTRAINT benchmark_confirmation_binding_sequence CHECK (binding_sequence > 0)
);

CREATE TABLE IF NOT EXISTS benchmark_variant_preparations (
    experiment_id UUID NOT NULL REFERENCES benchmark_experiments (id),
    variant_id STRING NOT NULL,
    definition_sha256 STRING NOT NULL,
    phase STRING NOT NULL DEFAULT 'pending',
    status STRING NOT NULL DEFAULT 'queued',
    attempt_count INT8 NOT NULL DEFAULT 0,
    lease_owner STRING,
    lease_expires_at TIMESTAMPTZ,
    incident_id UUID REFERENCES incidents (id),
    source_memory_id UUID REFERENCES semantic_memories (id),
    reference_memory_id UUID REFERENCES semantic_memories (id),
    consolidated_memory_id UUID REFERENCES semantic_memories (id),
    failure_class STRING,
    failure_code STRING,
    failure_detail STRING,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (experiment_id, variant_id),
    CONSTRAINT benchmark_variant_preparation_phase CHECK (
        phase IN ('pending', 'static_context', 'consolidation', 'rank_check', 'complete')
    ),
    CONSTRAINT benchmark_variant_preparation_status CHECK (
        status IN (
            'queued', 'leased', 'retrying', 'completed',
            'scientific_failed', 'infrastructure_failed'
        )
    ),
    CONSTRAINT benchmark_variant_preparation_attempts CHECK (
        attempt_count >= 0 AND attempt_count <= 3
    ),
    CONSTRAINT benchmark_variant_preparation_attempt_state CHECK (
        (status = 'queued' AND attempt_count = 0)
        OR (status = 'infrastructure_failed' AND attempt_count >= 0)
        OR (status NOT IN ('queued', 'infrastructure_failed') AND attempt_count >= 1)
    ),
    CONSTRAINT benchmark_variant_preparation_lease CHECK (
        (
            status = 'leased'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
        ) OR (
            status != 'leased'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CONSTRAINT benchmark_variant_preparation_failure_class CHECK (
        failure_class IS NULL
        OR failure_class IN ('infrastructure', 'scientific', 'protocol')
    ),
    CONSTRAINT benchmark_variant_preparation_completion CHECK (
        (
            status IN ('completed', 'scientific_failed', 'infrastructure_failed')
            AND completed_at IS NOT NULL
        ) OR (
            status NOT IN ('completed', 'scientific_failed', 'infrastructure_failed')
            AND completed_at IS NULL
        )
    ),
    CONSTRAINT benchmark_variant_preparation_failure_state CHECK (
        (status IN ('queued', 'leased', 'completed') AND failure_class IS NULL)
        OR (status = 'retrying' AND failure_class = 'infrastructure')
        OR (
            status = 'scientific_failed'
            AND failure_class IN ('scientific', 'protocol')
        )
        OR (status = 'infrastructure_failed' AND failure_class = 'infrastructure')
    ),
    CONSTRAINT benchmark_variant_preparation_completed_phase CHECK (
        status != 'completed' OR phase = 'complete'
    )
);
