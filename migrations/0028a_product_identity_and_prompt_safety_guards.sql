-- Backfill legacy rows fail-closed, then make prompt-safety results immutable.

UPDATE semantic_memories
SET prompt_safety_status = 'unassessed',
    prompt_safety_scanner_version = 'legacy.unassessed',
    prompt_safety_reason_codes = '["legacy_unassessed"]'::JSONB
WHERE prompt_safety_status IS NULL
   OR prompt_safety_scanner_version IS NULL
   OR prompt_safety_reason_codes IS NULL;

ALTER TABLE semantic_memories
    ALTER COLUMN prompt_safety_status SET DEFAULT 'unassessed',
    ALTER COLUMN prompt_safety_scanner_version SET DEFAULT 'legacy.unassessed',
    ALTER COLUMN prompt_safety_reason_codes
        SET DEFAULT '["legacy_unassessed"]'::JSONB,
    ALTER COLUMN prompt_safety_status SET NOT NULL,
    ALTER COLUMN prompt_safety_scanner_version SET NOT NULL,
    ALTER COLUMN prompt_safety_reason_codes SET NOT NULL;

ALTER TABLE semantic_memories
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_prompt_safety_status CHECK (
        prompt_safety_status IN ('clear', 'suspected', 'unassessed')
    ),
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_prompt_safety_scanner_version CHECK (
        length(trim(prompt_safety_scanner_version)) > 0
    ),
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_prompt_safety_reason_codes CHECK (
        jsonb_typeof(prompt_safety_reason_codes) = 'array'
    ),
    ADD CONSTRAINT IF NOT EXISTS semantic_memories_prompt_safety_state CHECK (
        (
            prompt_safety_status = 'clear'
            AND prompt_safety_reason_codes = '[]'::JSONB
        )
        OR (
            prompt_safety_status IN ('suspected', 'unassessed')
            AND jsonb_array_length(prompt_safety_reason_codes) >= 1
        )
    );

-- CockroachDB expands SELECT * when a view is created, so rebuild the view to
-- expose the new prompt-safety columns.
CREATE OR REPLACE VIEW current_semantic_memories AS
SELECT *
FROM semantic_memories
WHERE t_invalid IS NULL;

DROP TRIGGER IF EXISTS semantic_memory_immutable_fields
ON semantic_memories;

CREATE OR REPLACE FUNCTION guard_semantic_memory_immutable_fields()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).belief_id IS DISTINCT FROM (OLD).belief_id
        OR (NEW).version_number IS DISTINCT FROM (OLD).version_number
        OR (NEW).previous_version_id IS DISTINCT FROM (OLD).previous_version_id
        OR (NEW).namespace IS DISTINCT FROM (OLD).namespace
        OR (NEW).content IS DISTINCT FROM (OLD).content
        OR (NEW).metadata IS DISTINCT FROM (OLD).metadata
        OR (NEW).t_valid IS DISTINCT FROM (OLD).t_valid
        OR (NEW).writer IS DISTINCT FROM (OLD).writer
        OR (NEW).source_ref IS DISTINCT FROM (OLD).source_ref
        OR (NEW).justification IS DISTINCT FROM (OLD).justification
        OR (NEW).written_at IS DISTINCT FROM (OLD).written_at
        OR (NEW).producer_decision_id IS DISTINCT FROM (OLD).producer_decision_id
        OR (NEW).transition_kind IS DISTINCT FROM (OLD).transition_kind
        OR (NEW).content_schema IS DISTINCT FROM (OLD).content_schema
        OR (NEW).structured_payload IS DISTINCT FROM (OLD).structured_payload
        OR (NEW).payload_digest IS DISTINCT FROM (OLD).payload_digest
        OR (NEW).created_by_operation_id IS DISTINCT FROM (OLD).created_by_operation_id
        OR (NEW).prompt_safety_status IS DISTINCT FROM (OLD).prompt_safety_status
        OR (NEW).prompt_safety_scanner_version
            IS DISTINCT FROM (OLD).prompt_safety_scanner_version
        OR (NEW).prompt_safety_reason_codes
            IS DISTINCT FROM (OLD).prompt_safety_reason_codes
    THEN
        RAISE EXCEPTION 'semantic memory payload, identity, provenance, and prompt safety are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER semantic_memory_immutable_fields
BEFORE UPDATE ON semantic_memories
FOR EACH ROW
EXECUTE FUNCTION guard_semantic_memory_immutable_fields();

-- Principal-to-tenant authorization is global lookup data. It intentionally
-- has no tenant RLS policy: the product API can read it before binding a
-- tenant, but no product runtime role may mutate it.
CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN;
REVOKE ALL ON TABLE product_principal_roles FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON TABLE product_principal_roles
FROM hindsight_agent_writer;
GRANT SELECT ON TABLE product_principal_roles TO hindsight_agent_writer;
