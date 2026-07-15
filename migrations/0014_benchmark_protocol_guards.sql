CREATE UNIQUE INDEX IF NOT EXISTS benchmark_experiments_study_kind_idx
    ON benchmark_experiments (study_key_sha256, experiment_kind)
    WHERE study_key_sha256 IS NOT NULL
        AND status IN ('created', 'running', 'completed', 'failed');

-- Code SHA is deliberately absent from the claim family. Prevent pilot shopping
-- after no-op commits as well as repeated held-out attempts, while allowing a
-- purely infrastructure-incomplete attempt to be replaced without deleting it.
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_experiments_claim_family_kind_active_idx
    ON benchmark_experiments (claim_family_sha256, experiment_kind)
    WHERE claim_family_sha256 IS NOT NULL
        AND status IN ('created', 'running', 'completed', 'failed');

CREATE INDEX IF NOT EXISTS benchmark_confirmation_bindings_pilot_idx
    ON benchmark_confirmation_bindings (pilot_experiment_id, binding_sequence);

CREATE OR REPLACE FUNCTION guard_incident_consolidation_policy()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).consolidation_policy IS DISTINCT FROM (OLD).consolidation_policy THEN
        RAISE EXCEPTION 'incident consolidation policy is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER incident_consolidation_policy_immutable
BEFORE UPDATE ON incidents
FOR EACH ROW
EXECUTE FUNCTION guard_incident_consolidation_policy();

-- Preserve the first historical confirmation for an upgraded pilot. Any
-- already-duplicated confirmations remain visible, but the pilot is closed to
-- further attempts under the governed protocol.
WITH confirmation_candidates AS (
    SELECT
        pilot.id AS pilot_experiment_id,
        confirmation.preregistration,
        confirmation.preregistration_sha256,
        confirmation.id AS confirmation_experiment_id,
        confirmation.created_at
    FROM benchmark_experiments AS confirmation
    JOIN benchmark_experiments AS pilot
        ON pilot.id::STRING = confirmation.preregistration->>'pilot_experiment_id'
        AND pilot.experiment_kind = 'pilot'
        AND pilot.status = 'completed'
    WHERE confirmation.experiment_kind = 'confirmation'
        AND confirmation.preregistration IS NOT NULL
        AND confirmation.preregistration_sha256 IS NOT NULL
), ranked_confirmations AS (
    SELECT
        confirmation_candidates.*,
        row_number() OVER (
            PARTITION BY pilot_experiment_id
            ORDER BY created_at, confirmation_experiment_id
        ) AS confirmation_rank,
        row_number() OVER (
            PARTITION BY preregistration_sha256
            ORDER BY created_at, confirmation_experiment_id
        ) AS preregistration_rank
    FROM confirmation_candidates
)
INSERT INTO benchmark_confirmation_preregistrations (
    pilot_experiment_id, preregistration, preregistration_sha256,
    confirmation_experiment_id, prepared_at, bound_at
)
SELECT
    pilot_experiment_id, preregistration, preregistration_sha256,
    confirmation_experiment_id, created_at, created_at
FROM ranked_confirmations
WHERE confirmation_rank = 1 AND preregistration_rank = 1
ON CONFLICT DO NOTHING;

-- Preserve every legacy confirmation that exactly matches the retained pilot
-- contract, including historical duplicates. The current pointer above remains
-- the first attempt; this append-only table is the binding audit history.
INSERT INTO benchmark_confirmation_bindings (
    confirmation_experiment_id, pilot_experiment_id,
    preregistration_sha256, binding_sequence, bound_at
)
SELECT
    confirmation.id, preregistration.pilot_experiment_id,
    preregistration.preregistration_sha256,
    row_number() OVER (
        PARTITION BY preregistration.pilot_experiment_id
        ORDER BY confirmation.created_at, confirmation.id
    ),
    confirmation.created_at
FROM benchmark_confirmation_preregistrations AS preregistration
JOIN benchmark_experiments AS confirmation
    ON confirmation.experiment_kind = 'confirmation'
    AND confirmation.preregistration->>'pilot_experiment_id'
        = preregistration.pilot_experiment_id::STRING
    AND confirmation.preregistration = preregistration.preregistration
    AND confirmation.preregistration_sha256 = preregistration.preregistration_sha256
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION guard_benchmark_incomplete_replacement()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM benchmark_experiments AS prior
        JOIN benchmark_trials AS trial ON trial.experiment_id = prior.id
        WHERE prior.experiment_kind = (NEW).experiment_kind
            AND prior.status = 'incomplete'
            AND (
                (
                    (NEW).study_key_sha256 IS NOT NULL
                    AND prior.study_key_sha256 = (NEW).study_key_sha256
                ) OR (
                    (NEW).claim_family_sha256 IS NOT NULL
                    AND prior.claim_family_sha256 = (NEW).claim_family_sha256
                )
            )
            AND (
                trial.status IN ('completed', 'invalid')
                OR trial.penalized_action_count IS NOT NULL
                OR EXISTS (
                    SELECT 1 FROM benchmark_actions AS action
                    WHERE action.trial_id = trial.id
                )
            )
    ) THEN
        RAISE EXCEPTION 'outcome-bearing incomplete benchmark attempts cannot be replaced';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_incomplete_replacement_outcome_free
BEFORE INSERT ON benchmark_experiments
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_incomplete_replacement();

CREATE OR REPLACE FUNCTION guard_benchmark_preregistration_insert()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    pilot_kind STRING;
    pilot_status STRING;
BEGIN
    SELECT experiment_kind, status INTO pilot_kind, pilot_status
    FROM benchmark_experiments
    WHERE id = (NEW).pilot_experiment_id;
    IF pilot_kind IS DISTINCT FROM 'pilot' OR pilot_status IS DISTINCT FROM 'completed' THEN
        RAISE EXCEPTION 'confirmation preregistration requires a completed pilot';
    END IF;
    IF (NEW).preregistration->>'pilot_experiment_id'
            IS DISTINCT FROM (NEW).pilot_experiment_id::STRING
        OR (NEW).preregistration->>'sha256'
            IS DISTINCT FROM (NEW).preregistration_sha256
    THEN
        RAISE EXCEPTION 'confirmation preregistration identity does not match its contract';
    END IF;
    IF (NEW).confirmation_experiment_id IS NOT NULL OR (NEW).bound_at IS NOT NULL THEN
        RAISE EXCEPTION 'confirmation preregistrations must be inserted unbound';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_preregistration_insert_valid
BEFORE INSERT ON benchmark_confirmation_preregistrations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_preregistration_insert();

CREATE OR REPLACE FUNCTION guard_benchmark_preregistration_mutation()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    matching_confirmation UUID;
    previous_confirmation_status STRING;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'benchmark preregistrations are immutable';
    END IF;
    IF (NEW).pilot_experiment_id IS DISTINCT FROM (OLD).pilot_experiment_id
        OR (NEW).preregistration IS DISTINCT FROM (OLD).preregistration
        OR (NEW).preregistration_sha256 IS DISTINCT FROM (OLD).preregistration_sha256
        OR (NEW).prepared_at IS DISTINCT FROM (OLD).prepared_at
        OR (NEW).confirmation_experiment_id IS NULL
        OR (NEW).bound_at IS NULL
        OR (NEW).confirmation_experiment_id
            IS NOT DISTINCT FROM (OLD).confirmation_experiment_id
    THEN
        RAISE EXCEPTION 'benchmark preregistrations permit only verified binding transitions';
    END IF;
    IF (OLD).confirmation_experiment_id IS NOT NULL THEN
        SELECT status INTO previous_confirmation_status
        FROM benchmark_experiments
        WHERE id = (OLD).confirmation_experiment_id;
        IF previous_confirmation_status IS DISTINCT FROM 'incomplete' THEN
            RAISE EXCEPTION 'only an infrastructure-incomplete confirmation may be replaced';
        END IF;
    END IF;
    SELECT id INTO matching_confirmation
    FROM benchmark_experiments
    WHERE id = (NEW).confirmation_experiment_id
        AND experiment_kind = 'confirmation'
        AND preregistration = (OLD).preregistration
        AND preregistration_sha256 = (OLD).preregistration_sha256
        AND preregistration->>'pilot_experiment_id'
            = (OLD).pilot_experiment_id::STRING;
    IF matching_confirmation IS NULL THEN
        RAISE EXCEPTION 'direct binding requires an existing matching confirmation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_preregistration_update_immutable
BEFORE UPDATE ON benchmark_confirmation_preregistrations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_preregistration_mutation();

CREATE TRIGGER benchmark_preregistration_delete_immutable
BEFORE DELETE ON benchmark_confirmation_preregistrations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_preregistration_mutation();

CREATE OR REPLACE FUNCTION guard_benchmark_confirmation_binding_history()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    matching_confirmation UUID;
    expected_sequence INT8;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'benchmark confirmation binding history is append-only';
    END IF;
    SELECT confirmation.id INTO matching_confirmation
    FROM benchmark_confirmation_preregistrations AS preregistration
    JOIN benchmark_experiments AS confirmation
        ON confirmation.id = (NEW).confirmation_experiment_id
    WHERE preregistration.pilot_experiment_id = (NEW).pilot_experiment_id
        AND preregistration.confirmation_experiment_id
            = (NEW).confirmation_experiment_id
        AND preregistration.preregistration_sha256
            = (NEW).preregistration_sha256
        AND confirmation.experiment_kind = 'confirmation'
        AND confirmation.preregistration = preregistration.preregistration
        AND confirmation.preregistration_sha256
            = preregistration.preregistration_sha256
        AND confirmation.preregistration->>'pilot_experiment_id'
            = preregistration.pilot_experiment_id::STRING;
    IF matching_confirmation IS NULL THEN
        RAISE EXCEPTION 'confirmation binding history must match the current verified binding';
    END IF;
    SELECT COALESCE(max(binding_sequence), 0) + 1 INTO expected_sequence
    FROM benchmark_confirmation_bindings
    WHERE pilot_experiment_id = (NEW).pilot_experiment_id;
    IF (NEW).binding_sequence IS DISTINCT FROM expected_sequence THEN
        RAISE EXCEPTION 'confirmation binding sequence must be the next pilot sequence';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_confirmation_binding_insert_valid
BEFORE INSERT ON benchmark_confirmation_bindings
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_confirmation_binding_history();

CREATE TRIGGER benchmark_confirmation_binding_update_immutable
BEFORE UPDATE ON benchmark_confirmation_bindings
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_confirmation_binding_history();

CREATE TRIGGER benchmark_confirmation_binding_delete_immutable
BEFORE DELETE ON benchmark_confirmation_bindings
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_confirmation_binding_history();

CREATE OR REPLACE FUNCTION record_benchmark_confirmation_binding()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    next_sequence INT8;
BEGIN
    SELECT COALESCE(max(binding_sequence), 0) + 1 INTO next_sequence
    FROM benchmark_confirmation_bindings
    WHERE pilot_experiment_id = (NEW).pilot_experiment_id;
    INSERT INTO benchmark_confirmation_bindings (
        confirmation_experiment_id, pilot_experiment_id,
        preregistration_sha256, binding_sequence, bound_at
    ) VALUES (
        (NEW).confirmation_experiment_id, (NEW).pilot_experiment_id,
        (NEW).preregistration_sha256, next_sequence, (NEW).bound_at
    );
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_confirmation_binding_recorded
AFTER UPDATE ON benchmark_confirmation_preregistrations
FOR EACH ROW
EXECUTE FUNCTION record_benchmark_confirmation_binding();

CREATE OR REPLACE FUNCTION bind_benchmark_confirmation_preregistration()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    bound_pilot UUID;
    previous_confirmation UUID;
    previous_confirmation_status STRING;
BEGIN
    IF (NEW).experiment_kind != 'confirmation' THEN
        RETURN NEW;
    END IF;
    IF (NEW).preregistration IS NULL OR (NEW).preregistration_sha256 IS NULL THEN
        RAISE EXCEPTION 'confirmation requires a durable preregistration';
    END IF;
    SELECT pilot_experiment_id, confirmation_experiment_id
    INTO bound_pilot, previous_confirmation
    FROM benchmark_confirmation_preregistrations
    WHERE pilot_experiment_id = CAST(
            (NEW).preregistration->>'pilot_experiment_id' AS UUID
        )
        AND preregistration = (NEW).preregistration
        AND preregistration_sha256 = (NEW).preregistration_sha256
    FOR UPDATE;
    IF bound_pilot IS NULL THEN
        RAISE EXCEPTION 'pilot has no matching preregistration';
    END IF;
    IF previous_confirmation IS NOT NULL THEN
        SELECT status INTO previous_confirmation_status
        FROM benchmark_experiments
        WHERE id = previous_confirmation;
        IF previous_confirmation_status IS DISTINCT FROM 'incomplete' THEN
            RAISE EXCEPTION 'pilot already has a claim-bearing confirmation';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM benchmark_trials AS trial
            WHERE trial.experiment_id = previous_confirmation
                AND (
                    trial.status IN ('completed', 'invalid')
                    OR trial.penalized_action_count IS NOT NULL
                    OR EXISTS (
                        SELECT 1 FROM benchmark_actions AS action
                        WHERE action.trial_id = trial.id
                    )
                )
        ) THEN
            RAISE EXCEPTION 'outcome-bearing incomplete confirmations cannot be rebound';
        END IF;
    END IF;
    UPDATE benchmark_confirmation_preregistrations
    SET confirmation_experiment_id = (NEW).id, bound_at = now()
    WHERE pilot_experiment_id = bound_pilot
        AND confirmation_experiment_id IS NOT DISTINCT FROM previous_confirmation
    RETURNING pilot_experiment_id INTO bound_pilot;
    IF bound_pilot IS NULL THEN
        RAISE EXCEPTION 'pilot binding changed concurrently';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_confirmation_requires_preregistration
AFTER INSERT ON benchmark_experiments
FOR EACH ROW
EXECUTE FUNCTION bind_benchmark_confirmation_preregistration();

-- CockroachDB cannot replace a function while an active trigger references it.
-- Rebind the two v1 guards atomically inside this migration transaction.
DROP TRIGGER IF EXISTS benchmark_experiment_contract_immutable
    ON benchmark_experiments;
DROP TRIGGER IF EXISTS benchmark_trial_trace_immutable
    ON benchmark_trials;

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
        OR (NEW).study_key_sha256 IS DISTINCT FROM (OLD).study_key_sha256
        OR (NEW).claim_family_sha256 IS DISTINCT FROM (OLD).claim_family_sha256
        OR (NEW).code_sha IS DISTINCT FROM (OLD).code_sha
        OR (NEW).created_at IS DISTINCT FROM (OLD).created_at
        OR (OLD).status IN ('completed', 'incomplete', 'failed')
    THEN
        RAISE EXCEPTION 'benchmark contract and terminal experiment are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION guard_benchmark_trial_trace()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    experiment_status STRING;
BEGIN
    SELECT status INTO experiment_status
    FROM benchmark_experiments
    WHERE id = (OLD).experiment_id;
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).experiment_id IS DISTINCT FROM (OLD).experiment_id
        OR (NEW).variant_id IS DISTINCT FROM (OLD).variant_id
        OR (NEW).repetition IS DISTINCT FROM (OLD).repetition
        OR (NEW).arm IS DISTINCT FROM (OLD).arm
        OR (NEW).namespace IS DISTINCT FROM (OLD).namespace
        OR (NEW).lesson_memory_id IS DISTINCT FROM (OLD).lesson_memory_id
        OR (NEW).started_at IS DISTINCT FROM (OLD).started_at
        OR (OLD).status IN ('completed', 'infrastructure_failed', 'invalid')
        OR experiment_status IN ('completed', 'incomplete', 'failed')
    THEN
        RAISE EXCEPTION 'benchmark trial identity and terminal trace are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_experiment_contract_immutable
BEFORE UPDATE ON benchmark_experiments
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_experiment_contract();

CREATE TRIGGER benchmark_trial_trace_immutable
BEFORE UPDATE ON benchmark_trials
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_trial_trace();

CREATE OR REPLACE FUNCTION guard_benchmark_experiment_delete()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    RAISE EXCEPTION 'benchmark experiment traces cannot be deleted';
END
$$;

CREATE TRIGGER benchmark_experiment_delete_immutable
BEFORE DELETE ON benchmark_experiments
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_experiment_delete();

CREATE OR REPLACE FUNCTION guard_benchmark_trial_insert_delete()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    experiment_status STRING;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'benchmark trial traces cannot be deleted';
    END IF;
    SELECT status INTO experiment_status
    FROM benchmark_experiments
    WHERE id = (NEW).experiment_id;
    IF experiment_status IS DISTINCT FROM 'running' THEN
        RAISE EXCEPTION 'benchmark trials can be inserted only while the experiment is running';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_trial_insert_running_only
BEFORE INSERT ON benchmark_trials
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_trial_insert_delete();

CREATE TRIGGER benchmark_trial_delete_immutable
BEFORE DELETE ON benchmark_trials
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_trial_insert_delete();

CREATE OR REPLACE FUNCTION guard_benchmark_action_insert_delete()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    trial_status STRING;
    experiment_status STRING;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'benchmark action traces cannot be deleted';
    END IF;
    SELECT trial.status, experiment.status
    INTO trial_status, experiment_status
    FROM benchmark_trials AS trial
    JOIN benchmark_experiments AS experiment ON experiment.id = trial.experiment_id
    WHERE trial.id = (NEW).trial_id;
    IF trial_status IS DISTINCT FROM 'running'
        OR experiment_status IS DISTINCT FROM 'running'
    THEN
        RAISE EXCEPTION 'benchmark actions can be inserted only into a running trial';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_action_insert_running_only
BEFORE INSERT ON benchmark_actions
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_action_insert_delete();

CREATE TRIGGER benchmark_action_delete_immutable
BEFORE DELETE ON benchmark_actions
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_action_insert_delete();

CREATE OR REPLACE FUNCTION guard_benchmark_variant_preparation_mutation()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    experiment_status STRING;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'benchmark variant preparation traces cannot be deleted';
    END IF;
    SELECT status INTO experiment_status
    FROM benchmark_experiments
    WHERE id = (OLD).experiment_id;
    IF (NEW).experiment_id IS DISTINCT FROM (OLD).experiment_id
        OR (NEW).variant_id IS DISTINCT FROM (OLD).variant_id
        OR (NEW).definition_sha256 IS DISTINCT FROM (OLD).definition_sha256
        OR (OLD).status IN ('completed', 'scientific_failed', 'infrastructure_failed')
        OR experiment_status != 'created'
    THEN
        RAISE EXCEPTION 'benchmark variant identity and terminal preparation are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION guard_benchmark_variant_preparation_insert()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    experiment_status STRING;
BEGIN
    SELECT status INTO experiment_status
    FROM benchmark_experiments
    WHERE id = (NEW).experiment_id;
    IF experiment_status IS DISTINCT FROM 'created' THEN
        RAISE EXCEPTION 'benchmark preparations can be inserted only while the experiment is created';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER benchmark_variant_preparation_insert_created_only
BEFORE INSERT ON benchmark_variant_preparations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_variant_preparation_insert();

CREATE TRIGGER benchmark_variant_preparation_update_immutable
BEFORE UPDATE ON benchmark_variant_preparations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_variant_preparation_mutation();

CREATE TRIGGER benchmark_variant_preparation_delete_immutable
BEFORE DELETE ON benchmark_variant_preparations
FOR EACH ROW
EXECUTE FUNCTION guard_benchmark_variant_preparation_mutation();
