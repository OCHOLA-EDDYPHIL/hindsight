-- Repair only agent-run decisions created as sealed legacy-read placeholders
-- by an earlier draft of 0008. Legitimate terminal decisions stay terminal.

UPDATE memory_decisions AS decision
SET
    actor = 'agent.run',
    decision_kind = 'agent_plan',
    purpose = 'Backfill durable agent run decision',
    run_id = run.id,
    namespace = run.namespace,
    status = CASE
        WHEN run.status = 'failed' THEN 'failed'
        WHEN run.status IN ('completed', 'rejected') THEN 'sealed'
        ELSE 'open'
    END,
    opened_at = run.created_at,
    sealed_at = CASE WHEN run.status IN ('completed', 'rejected', 'failed')
        THEN COALESCE(run.completed_at, run.updated_at) END,
    metadata = decision.metadata || jsonb_build_object('migrated_from', 'agent_runs')
FROM agent_runs AS run
WHERE decision.id = run.decision_id
    AND decision.actor = 'legacy.import'
    AND decision.decision_kind = 'legacy_read'
    AND decision.purpose = 'Backfill pre-governance memory read identity'
    AND decision.status = 'sealed';

-- An earlier reflection write path could commit the immutable semantic memory
-- while rolling back its typed projection. Recover only unambiguous rows whose
-- payload and owning run agree on every projection identity field.
INSERT INTO agent_reflections (
    decision_id, run_id, thread_id, incident_id, namespace, service_slug,
    plan, proposed_action, action_approved, semantic_memory_id, belief_id,
    schema_version, created_at
)
SELECT
    memory.producer_decision_id,
    run.id,
    memory.structured_payload->>'thread_id',
    memory.structured_payload->>'incident_id',
    memory.namespace,
    CASE
        WHEN jsonb_typeof(memory.structured_payload->'service_slug') = 'string'
            THEN memory.structured_payload->>'service_slug'
        ELSE NULL
    END,
    memory.structured_payload->>'plan',
    memory.structured_payload->>'proposed_action',
    (memory.structured_payload->>'action_approved')::BOOL,
    memory.id,
    memory.belief_id,
    1,
    memory.written_at
FROM semantic_memories AS memory
JOIN agent_runs AS run
    ON run.id::STRING = memory.structured_payload->>'run_id'
    AND run.decision_id = memory.producer_decision_id
WHERE memory.content_schema = 'agent_reflection.v1'
    AND memory.writer = 'agent.reflect'
    AND memory.source_ref = memory.producer_decision_id
    AND memory.lineage_status = 'complete'
    AND jsonb_typeof(memory.structured_payload) = 'object'
    AND jsonb_typeof(memory.structured_payload->'schema_version') = 'number'
    AND memory.structured_payload->>'schema_version' = '1'
    AND jsonb_typeof(memory.structured_payload->'thread_id') = 'string'
    AND memory.structured_payload->>'thread_id' = run.thread_id
    AND jsonb_typeof(memory.structured_payload->'run_id') = 'string'
    AND jsonb_typeof(memory.structured_payload->'incident_id') = 'string'
    AND memory.structured_payload->>'incident_id' = run.incident_slug
    AND jsonb_typeof(memory.structured_payload->'namespace') = 'string'
    AND memory.structured_payload->>'namespace' = memory.namespace
    AND memory.namespace = run.namespace
    AND (
        (
            jsonb_typeof(memory.structured_payload->'service_slug') = 'null'
            AND run.service_slug IS NULL
        )
        OR (
            jsonb_typeof(memory.structured_payload->'service_slug') = 'string'
            AND memory.structured_payload->>'service_slug' = run.service_slug
        )
    )
    AND jsonb_typeof(memory.structured_payload->'plan') = 'string'
    AND jsonb_typeof(memory.structured_payload->'proposed_action') = 'string'
    AND jsonb_typeof(memory.structured_payload->'action_approved') = 'boolean'
    AND NOT EXISTS (
        SELECT 1
        FROM agent_reflections AS reflection
        WHERE reflection.decision_id = memory.producer_decision_id
            OR reflection.semantic_memory_id = memory.id
    )
    AND (
        SELECT count(*)
        FROM semantic_memories AS candidate
        WHERE candidate.producer_decision_id = memory.producer_decision_id
            AND candidate.content_schema = 'agent_reflection.v1'
    ) = 1
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION guard_terminal_memory_decision()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (OLD).status IN ('sealed', 'failed')
        AND (NEW).status IS DISTINCT FROM (OLD).status
    THEN
        RAISE EXCEPTION 'terminal memory decision status is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER memory_decision_terminal_status
BEFORE UPDATE ON memory_decisions
FOR EACH ROW
EXECUTE FUNCTION guard_terminal_memory_decision();

CREATE OR REPLACE FUNCTION guard_open_memory_producer()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    producer_status STRING;
BEGIN
    SELECT status INTO producer_status
    FROM memory_decisions
    WHERE id = (NEW).producer_decision_id;

    IF producer_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION 'memory producer decision must be open';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER semantic_memory_open_producer
BEFORE INSERT ON semantic_memories
FOR EACH ROW
EXECUTE FUNCTION guard_open_memory_producer();

CREATE TRIGGER episodic_memory_open_producer
BEFORE INSERT ON episodic_memories
FOR EACH ROW
EXECUTE FUNCTION guard_open_memory_producer();
