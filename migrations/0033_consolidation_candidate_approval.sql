-- Quarantine generated procedural lessons until an evidence-bound operator review.

ALTER TABLE consolidation_jobs DROP CONSTRAINT IF EXISTS consolidation_jobs_review_status;
ALTER TABLE consolidation_jobs
    ADD COLUMN IF NOT EXISTS candidate_payload JSONB,
    ADD COLUMN IF NOT EXISTS candidate_content STRING,
    ADD COLUMN IF NOT EXISTS candidate_fingerprint STRING,
    ADD COLUMN IF NOT EXISTS evidence_manifest JSONB,
    ADD COLUMN IF NOT EXISTS evidence_fingerprint STRING,
    ADD COLUMN IF NOT EXISTS generation_receipt JSONB,
    ADD COLUMN IF NOT EXISTS validation_receipt JSONB,
    ADD COLUMN IF NOT EXISTS review_status STRING NOT NULL DEFAULT 'unavailable',
    ADD COLUMN IF NOT EXISTS reviewed_by STRING,
    ADD COLUMN IF NOT EXISTS review_reason STRING,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_operation_id UUID,
    ADD COLUMN IF NOT EXISTS approved_memory_id UUID;

ALTER TABLE consolidation_jobs
    DROP CONSTRAINT IF EXISTS consolidation_jobs_review_operation_fk;
ALTER TABLE consolidation_jobs
    DROP CONSTRAINT IF EXISTS consolidation_jobs_approved_memory_fk;
ALTER TABLE consolidation_jobs
    ADD CONSTRAINT consolidation_jobs_review_status CHECK (
        review_status IN ('unavailable', 'pending', 'approved', 'rejected')
    );

UPDATE memory_namespaces
SET revision = revision + 1, updated_at = now()
WHERE namespace IN (
    SELECT DISTINCT namespace
    FROM semantic_memories
    WHERE content_schema = 'procedural_lesson.v1'
        AND writer = 'consolidation.worker'
        AND trust_status = 'active'
);

UPDATE semantic_memories
SET trust_status = 'review_required'
WHERE content_schema = 'procedural_lesson.v1'
    AND writer = 'consolidation.worker'
    AND trust_status = 'active'
    AND t_invalid IS NULL;

UPDATE consolidation_jobs AS job
SET candidate_payload = memory.structured_payload,
    candidate_content = memory.content,
    candidate_fingerprint = memory.payload_digest,
    evidence_manifest = '{}'::JSONB,
    evidence_fingerprint = 'legacy-unavailable',
    generation_receipt = jsonb_build_object(
        'schema_version', 1,
        'status', 'legacy_unavailable'
    ),
    validation_receipt = jsonb_build_object(
        'schema_version', 1,
        'status', 'legacy_unavailable'
    ),
    review_status = 'pending'
FROM semantic_memories AS memory
WHERE job.lesson_memory_id = memory.id
    AND job.review_status = 'unavailable'
    AND memory.content_schema = 'procedural_lesson.v1'
    AND memory.writer = 'consolidation.worker'
    AND memory.trust_status = 'review_required'
    AND memory.t_invalid IS NULL;

ALTER TABLE memory_operations DROP CONSTRAINT IF EXISTS memory_operations_type;
ALTER TABLE memory_operations
    ADD CONSTRAINT memory_operations_type CHECK (
        operation_type IN (
            'rewind', 'retraction', 'supersession', 'review_resolution',
            'consolidation_approval', 'demo_session_start', 'demo_poison'
        )
    );

ALTER TABLE memory_operation_previews
    DROP CONSTRAINT IF EXISTS memory_operation_preview_type;
ALTER TABLE memory_operation_previews
    ADD CONSTRAINT memory_operation_preview_type CHECK (
        operation_type IN (
            'rewind', 'retraction', 'supersession', 'review_resolution',
            'consolidation_approval'
        )
    );

ALTER TABLE consolidation_jobs
    ADD CONSTRAINT consolidation_jobs_review_operation_fk
        FOREIGN KEY (review_operation_id) REFERENCES memory_operations (id),
    ADD CONSTRAINT consolidation_jobs_approved_memory_fk
        FOREIGN KEY (approved_memory_id) REFERENCES semantic_memories (id);

CREATE INDEX IF NOT EXISTS consolidation_jobs_review_idx
    ON consolidation_jobs (tenant_id, review_status, updated_at DESC, id);

DROP TRIGGER IF EXISTS consolidation_candidate_identity ON consolidation_jobs;
CREATE OR REPLACE FUNCTION guard_consolidation_candidate_identity()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    review_operation_type STRING;
    review_operation_status STRING;
    review_operation_actor STRING;
    review_operation_candidate_id STRING;
    review_operation_candidate_memory_id STRING;
    review_operation_candidate_fingerprint STRING;
    review_operation_evidence_fingerprint STRING;
    review_operation_action STRING;
BEGIN
    IF (OLD).review_status IN ('pending', 'approved', 'rejected')
        AND (
            (NEW).lesson_belief_id IS DISTINCT FROM (OLD).lesson_belief_id
            OR (NEW).lesson_memory_id IS DISTINCT FROM (OLD).lesson_memory_id
            OR (NEW).candidate_payload IS DISTINCT FROM (OLD).candidate_payload
            OR (NEW).candidate_content IS DISTINCT FROM (OLD).candidate_content
            OR (NEW).candidate_fingerprint IS DISTINCT FROM (OLD).candidate_fingerprint
            OR (NEW).evidence_manifest IS DISTINCT FROM (OLD).evidence_manifest
            OR (NEW).evidence_fingerprint IS DISTINCT FROM (OLD).evidence_fingerprint
            OR (NEW).generation_receipt IS DISTINCT FROM (OLD).generation_receipt
            OR (NEW).validation_receipt IS DISTINCT FROM (OLD).validation_receipt
        )
    THEN
        RAISE EXCEPTION 'consolidation candidate identity and receipts are immutable';
    END IF;

    IF (OLD).review_status IN ('approved', 'rejected')
        AND (
            (NEW).review_status IS DISTINCT FROM (OLD).review_status
            OR (NEW).reviewed_by IS DISTINCT FROM (OLD).reviewed_by
            OR (NEW).review_reason IS DISTINCT FROM (OLD).review_reason
            OR (NEW).reviewed_at IS DISTINCT FROM (OLD).reviewed_at
            OR (NEW).review_operation_id IS DISTINCT FROM (OLD).review_operation_id
            OR (NEW).approved_memory_id IS DISTINCT FROM (OLD).approved_memory_id
        )
    THEN
        RAISE EXCEPTION 'consolidation candidate review is terminal';
    END IF;

    IF ((OLD).review_status = 'unavailable'
            AND (NEW).review_status NOT IN ('unavailable', 'pending'))
        OR ((OLD).review_status = 'pending'
            AND (NEW).review_status NOT IN ('pending', 'approved', 'rejected'))
    THEN
        RAISE EXCEPTION 'invalid consolidation candidate review transition';
    END IF;

    IF (OLD).review_status = 'unavailable'
        AND (NEW).review_status = 'pending'
        AND (
            (NEW).lesson_belief_id IS NULL
            OR (NEW).lesson_memory_id IS NULL
            OR (NEW).candidate_payload IS NULL
            OR (NEW).candidate_content IS NULL
            OR (NEW).candidate_fingerprint IS NULL
            OR (NEW).evidence_manifest IS NULL
            OR (NEW).evidence_fingerprint IS NULL
            OR (NEW).generation_receipt IS NULL
            OR (NEW).validation_receipt IS NULL
        )
    THEN
        RAISE EXCEPTION 'pending consolidation candidate is incomplete';
    END IF;

    IF (NEW).review_status IN ('unavailable', 'pending')
        AND (
            (NEW).reviewed_by IS NOT NULL
            OR (NEW).review_reason IS NOT NULL
            OR (NEW).reviewed_at IS NOT NULL
            OR (NEW).review_operation_id IS NOT NULL
            OR (NEW).approved_memory_id IS NOT NULL
        )
    THEN
        RAISE EXCEPTION 'nonterminal consolidation review has terminal fields';
    END IF;

    IF (OLD).review_status = 'pending'
        AND (NEW).review_status IN ('approved', 'rejected')
        AND (
            (NEW).reviewed_by IS NULL
            OR (NEW).review_reason IS NULL
            OR (NEW).reviewed_at IS NULL
            OR (NEW).review_operation_id IS NULL
            OR ((NEW).review_status = 'approved' AND (NEW).approved_memory_id IS NULL)
            OR ((NEW).review_status = 'rejected' AND (NEW).approved_memory_id IS NOT NULL)
        )
    THEN
        RAISE EXCEPTION 'terminal consolidation review is incomplete';
    END IF;

    IF (OLD).review_status = 'pending'
        AND (NEW).review_status IN ('approved', 'rejected')
    THEN
        SELECT approval.operation_type, approval.status, approval.actor,
               approval.request_payload->>'candidate_id',
               approval.request_payload->>'candidate_memory_id',
               approval.request_payload->>'candidate_fingerprint',
               approval.request_payload->>'evidence_fingerprint',
               approval.request_payload->>'action'
        INTO review_operation_type, review_operation_status, review_operation_actor,
             review_operation_candidate_id, review_operation_candidate_memory_id,
             review_operation_candidate_fingerprint,
             review_operation_evidence_fingerprint, review_operation_action
        FROM memory_operations AS approval
        WHERE approval.id = (NEW).review_operation_id
            AND approval.tenant_id = (NEW).tenant_id;

        IF review_operation_type IS NULL
            OR review_operation_type != 'consolidation_approval'
            OR review_operation_status NOT IN ('leased', 'completed')
            OR review_operation_actor IS DISTINCT FROM (NEW).reviewed_by
            OR review_operation_candidate_id IS DISTINCT FROM (OLD).id::STRING
            OR review_operation_candidate_memory_id
                IS DISTINCT FROM (OLD).lesson_memory_id::STRING
            OR review_operation_candidate_fingerprint
                IS DISTINCT FROM (OLD).candidate_fingerprint
            OR review_operation_evidence_fingerprint
                IS DISTINCT FROM (OLD).evidence_fingerprint
            OR ((NEW).review_status = 'approved'
                AND review_operation_action != 'approve')
            OR ((NEW).review_status = 'rejected'
                AND review_operation_action != 'reject')
        THEN
            RAISE EXCEPTION 'terminal consolidation review approval is invalid';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER consolidation_candidate_identity
BEFORE UPDATE ON consolidation_jobs
FOR EACH ROW EXECUTE FUNCTION guard_consolidation_candidate_identity();

DROP TRIGGER IF EXISTS generated_procedural_lesson_approval ON semantic_memories;
CREATE OR REPLACE FUNCTION guard_generated_procedural_lesson_approval()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    approval_operation_type STRING;
    approval_status STRING;
    approval_candidate_memory_id STRING;
BEGIN
    IF (NEW).content_schema = 'procedural_lesson.v1'
        AND (NEW).trust_status = 'active'
        AND (NEW).metadata->>'role' = 'consolidated-lesson'
    THEN
        IF (NEW).created_by_operation_id IS NULL
            OR (NEW).previous_version_id IS NULL
        THEN
            RAISE EXCEPTION 'active generated procedural lesson requires approval provenance';
        END IF;

        SELECT approval.operation_type, approval.status,
               approval.request_payload->>'candidate_memory_id'
        INTO approval_operation_type, approval_status, approval_candidate_memory_id
        FROM memory_operations AS approval
        WHERE approval.id = (NEW).created_by_operation_id
            AND approval.tenant_id = (NEW).tenant_id;

        IF approval_operation_type IS NULL THEN
            RAISE EXCEPTION 'active generated procedural lesson approval is invalid';
        END IF;

        IF approval_operation_type != 'consolidation_approval'
            OR approval_status NOT IN ('leased', 'completed')
            OR approval_candidate_memory_id IS DISTINCT FROM (NEW).previous_version_id::STRING
        THEN
            RAISE EXCEPTION 'active generated procedural lesson approval is invalid';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER generated_procedural_lesson_approval
BEFORE INSERT OR UPDATE ON semantic_memories
FOR EACH ROW EXECUTE FUNCTION guard_generated_procedural_lesson_approval();
