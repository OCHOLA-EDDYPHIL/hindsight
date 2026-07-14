INSERT INTO memory_decisions (
    id, actor, decision_kind, purpose, run_id, namespace, status,
    opened_at, sealed_at, metadata
)
SELECT
    decision_id,
    'agent.run',
    'agent_plan',
    'Backfill durable agent run decision',
    id,
    namespace,
    CASE
        WHEN status = 'failed' THEN 'failed'
        WHEN status IN ('completed', 'rejected') THEN 'sealed'
        ELSE 'open'
    END,
    created_at,
    CASE WHEN status IN ('completed', 'rejected', 'failed')
        THEN COALESCE(completed_at, updated_at) END,
    jsonb_build_object('migrated_from', 'agent_runs')
FROM agent_runs
ON CONFLICT (id) DO UPDATE SET
    actor = excluded.actor,
    decision_kind = excluded.decision_kind,
    purpose = excluded.purpose,
    run_id = excluded.run_id,
    namespace = excluded.namespace,
    status = excluded.status,
    opened_at = excluded.opened_at,
    sealed_at = excluded.sealed_at,
    metadata = memory_decisions.metadata || excluded.metadata
WHERE memory_decisions.actor = 'legacy.import'
    AND memory_decisions.decision_kind = 'legacy_read'
    AND memory_decisions.purpose = 'Backfill pre-governance memory read identity'
    AND memory_decisions.status = 'sealed';

INSERT INTO memory_decisions (
    id, actor, decision_kind, purpose, status, sealed_at
)
SELECT DISTINCT
    decision_id,
    'legacy.import',
    'legacy_read',
    'Backfill pre-governance memory read identity',
    'sealed',
    now()
FROM memory_reads
ON CONFLICT (id) DO NOTHING;

INSERT INTO memory_decisions (
    id, actor, decision_kind, purpose, namespace, status, sealed_at
)
SELECT
    'legacy:write:' || id::STRING,
    writer,
    'legacy_write',
    justification,
    namespace,
    'sealed',
    written_at
FROM semantic_memories
ON CONFLICT (id) DO NOTHING;

INSERT INTO memory_decisions (
    id, actor, decision_kind, purpose, status, sealed_at
)
SELECT
    'legacy:write:' || id::STRING,
    writer,
    'legacy_write',
    justification,
    'sealed',
    written_at
FROM episodic_memories
ON CONFLICT (id) DO NOTHING;

INSERT INTO semantic_beliefs (id, namespace, created_at)
SELECT id, namespace, written_at
FROM semantic_memories
ON CONFLICT (id) DO NOTHING;

INSERT INTO memory_namespaces (namespace)
SELECT DISTINCT namespace
FROM semantic_memories
ON CONFLICT (namespace) DO NOTHING;

UPDATE semantic_memories
SET
    belief_id = id,
    version_number = 1,
    producer_decision_id = 'legacy:write:' || id::STRING,
    transition_kind = 'import',
    content_schema = 'legacy.v1',
    structured_payload = metadata,
    payload_digest = 'legacy:' || id::STRING,
    lineage_status = 'legacy_unverified',
    trust_status = 'active'
WHERE belief_id IS NULL;

ALTER TABLE semantic_memories ALTER COLUMN belief_id SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN version_number SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN producer_decision_id SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN transition_kind SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN content_schema SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN structured_payload SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN payload_digest SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN lineage_status SET NOT NULL;
ALTER TABLE semantic_memories ALTER COLUMN trust_status SET NOT NULL;

ALTER TABLE semantic_memories
    ADD CONSTRAINT semantic_memories_belief_fk
        FOREIGN KEY (belief_id) REFERENCES semantic_beliefs (id),
    ADD CONSTRAINT semantic_memories_previous_version_fk
        FOREIGN KEY (previous_version_id) REFERENCES semantic_memories (id),
    ADD CONSTRAINT semantic_memories_producer_fk
        FOREIGN KEY (producer_decision_id) REFERENCES memory_decisions (id),
    ADD CONSTRAINT semantic_memories_transition_kind CHECK (
        transition_kind IN ('assertion', 'supersession', 'rewind_reassertion', 'import')
    ),
    ADD CONSTRAINT semantic_memories_lineage_status CHECK (
        lineage_status IN ('building', 'complete', 'legacy_unverified')
    ),
    ADD CONSTRAINT semantic_memories_trust_status CHECK (
        trust_status IN ('active', 'review_required')
    ),
    ADD CONSTRAINT semantic_memories_previous_not_self CHECK (
        previous_version_id IS NULL OR previous_version_id != id
    );

CREATE UNIQUE INDEX IF NOT EXISTS semantic_memories_belief_version_idx
    ON semantic_memories (belief_id, version_number);

CREATE UNIQUE INDEX IF NOT EXISTS semantic_memories_one_current_version_idx
    ON semantic_memories (belief_id)
    WHERE t_invalid IS NULL;

CREATE INDEX IF NOT EXISTS semantic_memories_namespace_trust_idx
    ON semantic_memories (namespace, trust_status, t_valid DESC)
    WHERE t_invalid IS NULL;

UPDATE episodic_memories
SET
    producer_decision_id = 'legacy:write:' || id::STRING,
    content_schema = 'legacy.v1',
    structured_payload = metadata,
    payload_digest = 'legacy:' || id::STRING,
    lineage_status = 'legacy_unverified',
    trust_status = 'active'
WHERE producer_decision_id IS NULL;

ALTER TABLE episodic_memories ALTER COLUMN producer_decision_id SET NOT NULL;
ALTER TABLE episodic_memories ALTER COLUMN content_schema SET NOT NULL;
ALTER TABLE episodic_memories ALTER COLUMN structured_payload SET NOT NULL;
ALTER TABLE episodic_memories ALTER COLUMN payload_digest SET NOT NULL;
ALTER TABLE episodic_memories ALTER COLUMN lineage_status SET NOT NULL;
ALTER TABLE episodic_memories ALTER COLUMN trust_status SET NOT NULL;

ALTER TABLE episodic_memories
    ADD CONSTRAINT episodic_memories_producer_fk
        FOREIGN KEY (producer_decision_id) REFERENCES memory_decisions (id),
    ADD CONSTRAINT episodic_memories_lineage_status CHECK (
        lineage_status IN ('building', 'complete', 'legacy_unverified')
    ),
    ADD CONSTRAINT episodic_memories_trust_status CHECK (
        trust_status IN ('active', 'review_required')
    );

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
    THEN
        RAISE EXCEPTION 'semantic memory payload, identity, and provenance are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER semantic_memory_immutable_fields
BEFORE UPDATE ON semantic_memories
FOR EACH ROW
EXECUTE FUNCTION guard_semantic_memory_immutable_fields();

CREATE OR REPLACE FUNCTION guard_episodic_memory_immutable_fields()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
BEGIN
    IF (NEW).id IS DISTINCT FROM (OLD).id
        OR (NEW).episode_id IS DISTINCT FROM (OLD).episode_id
        OR (NEW).role IS DISTINCT FROM (OLD).role
        OR (NEW).content IS DISTINCT FROM (OLD).content
        OR (NEW).metadata IS DISTINCT FROM (OLD).metadata
        OR (NEW).t_valid IS DISTINCT FROM (OLD).t_valid
        OR (NEW).writer IS DISTINCT FROM (OLD).writer
        OR (NEW).source_ref IS DISTINCT FROM (OLD).source_ref
        OR (NEW).justification IS DISTINCT FROM (OLD).justification
        OR (NEW).written_at IS DISTINCT FROM (OLD).written_at
        OR (NEW).producer_decision_id IS DISTINCT FROM (OLD).producer_decision_id
        OR (NEW).content_schema IS DISTINCT FROM (OLD).content_schema
        OR (NEW).structured_payload IS DISTINCT FROM (OLD).structured_payload
        OR (NEW).payload_digest IS DISTINCT FROM (OLD).payload_digest
    THEN
        RAISE EXCEPTION 'episodic memory payload, identity, and provenance are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER episodic_memory_immutable_fields
BEFORE UPDATE ON episodic_memories
FOR EACH ROW
EXECUTE FUNCTION guard_episodic_memory_immutable_fields();

UPDATE memory_reads
SET semantic_memory_id = memory_id
WHERE memory_kind = 'semantic' AND semantic_memory_id IS NULL;

UPDATE memory_reads
SET episodic_memory_id = memory_id
WHERE memory_kind = 'episodic' AND episodic_memory_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS memory_reads_id_decision_idx
    ON memory_reads (id, decision_id);

ALTER TABLE memory_reads
    ADD CONSTRAINT memory_reads_decision_fk
        FOREIGN KEY (decision_id) REFERENCES memory_decisions (id),
    ADD CONSTRAINT memory_reads_semantic_fk
        FOREIGN KEY (semantic_memory_id) REFERENCES semantic_memories (id),
    ADD CONSTRAINT memory_reads_episodic_fk
        FOREIGN KEY (episodic_memory_id) REFERENCES episodic_memories (id),
    ADD CONSTRAINT memory_reads_typed_target CHECK (
        (memory_kind = 'semantic' AND semantic_memory_id IS NOT NULL AND episodic_memory_id IS NULL)
        OR (memory_kind = 'episodic' AND episodic_memory_id IS NOT NULL AND semantic_memory_id IS NULL)
    );

INSERT INTO memory_external_evidence (
    semantic_memory_id, evidence_kind, evidence_ref, evidence_digest,
    observed_at, actor, metadata
)
SELECT
    id, 'legacy', source_ref, 'legacy-unverified', written_at, writer,
    jsonb_build_object('justification', justification)
FROM semantic_memories
ON CONFLICT DO NOTHING;

INSERT INTO memory_external_evidence (
    episodic_memory_id, evidence_kind, evidence_ref, evidence_digest,
    observed_at, actor, metadata
)
SELECT
    id, 'legacy', source_ref, 'legacy-unverified', written_at, writer,
    jsonb_build_object('justification', justification)
FROM episodic_memories
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS memory_lineage_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    child_semantic_memory_id UUID REFERENCES semantic_memories (id),
    child_episodic_memory_id UUID REFERENCES episodic_memories (id),
    parent_read_id UUID NOT NULL,
    producer_decision_id STRING NOT NULL,
    edge_type STRING NOT NULL,
    justification STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT memory_lineage_child CHECK (
        (child_semantic_memory_id IS NOT NULL AND child_episodic_memory_id IS NULL)
        OR (child_semantic_memory_id IS NULL AND child_episodic_memory_id IS NOT NULL)
    ),
    CONSTRAINT memory_lineage_type CHECK (
        edge_type IN ('derived', 'context', 'reasserted_from')
    ),
    CONSTRAINT memory_lineage_read_decision_fk
        FOREIGN KEY (parent_read_id, producer_decision_id)
        REFERENCES memory_reads (id, decision_id),
    UNIQUE (child_semantic_memory_id, parent_read_id, edge_type),
    UNIQUE (child_episodic_memory_id, parent_read_id, edge_type)
);

CREATE INDEX IF NOT EXISTS memory_lineage_semantic_child_idx
    ON memory_lineage_edges (child_semantic_memory_id, edge_type);

CREATE INDEX IF NOT EXISTS memory_lineage_parent_read_idx
    ON memory_lineage_edges (parent_read_id, edge_type);

CREATE TABLE IF NOT EXISTS agent_reflections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id STRING NOT NULL UNIQUE REFERENCES memory_decisions (id),
    run_id UUID REFERENCES agent_runs (id) ON DELETE SET NULL,
    thread_id STRING NOT NULL,
    incident_id STRING NOT NULL,
    namespace STRING NOT NULL,
    service_slug STRING,
    plan STRING NOT NULL,
    proposed_action STRING NOT NULL,
    action_approved BOOL NOT NULL,
    semantic_memory_id UUID NOT NULL UNIQUE REFERENCES semantic_memories (id),
    belief_id UUID NOT NULL REFERENCES semantic_beliefs (id),
    schema_version INT8 NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE memory_operations DROP CONSTRAINT IF EXISTS memory_operations_type;
UPDATE memory_operations
SET
    status = 'completed',
    request_payload = metadata,
    expected_revisions = '{}'::JSONB,
    applied_revisions = '{}'::JSONB,
    attempt_count = 1,
    completed_at = created_at
WHERE status IS NULL;

ALTER TABLE memory_operations ALTER COLUMN status SET NOT NULL;
ALTER TABLE memory_operations ALTER COLUMN request_payload SET NOT NULL;
ALTER TABLE memory_operations ALTER COLUMN expected_revisions SET NOT NULL;
ALTER TABLE memory_operations ALTER COLUMN attempt_count SET NOT NULL;

ALTER TABLE memory_operations
    ADD CONSTRAINT memory_operations_type CHECK (
        operation_type IN (
            'rewind', 'retraction', 'supersession', 'review_resolution',
            'demo_session_start', 'demo_poison'
        )
    ),
    ADD CONSTRAINT memory_operations_status CHECK (
        status IN ('queued', 'leased', 'retrying', 'completed', 'conflict', 'failed')
    );

CREATE UNIQUE INDEX IF NOT EXISTS memory_operations_idempotency_idx
    ON memory_operations (idempotency_key);

CREATE TABLE IF NOT EXISTS memory_operation_previews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_type STRING NOT NULL,
    actor STRING NOT NULL,
    request_payload JSONB NOT NULL,
    effect_payload JSONB NOT NULL,
    expected_revisions JSONB NOT NULL,
    embedding_generation INT8,
    fingerprint STRING NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT memory_operation_preview_type CHECK (
        operation_type IN ('rewind', 'retraction', 'supersession', 'review_resolution')
    )
);

CREATE TABLE IF NOT EXISTS memory_operation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES memory_operations (id) ON DELETE CASCADE,
    sequence INT8 NOT NULL,
    status STRING NOT NULL,
    summary STRING NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (operation_id, sequence)
);

CREATE TABLE IF NOT EXISTS memory_operation_effects (
    operation_id UUID NOT NULL REFERENCES memory_operations (id) ON DELETE CASCADE,
    sequence INT8 NOT NULL,
    effect_type STRING NOT NULL,
    source_memory_id UUID,
    result_memory_id UUID,
    belief_id UUID REFERENCES semantic_beliefs (id),
    namespace STRING,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, sequence),
    CONSTRAINT memory_operation_effect_type CHECK (
        effect_type IN ('closed', 'created', 'reasserted', 'unchanged', 'review_required')
    )
);

CREATE TABLE IF NOT EXISTS memory_review_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES memory_operations (id),
    semantic_memory_id UUID NOT NULL REFERENCES semantic_memories (id),
    status STRING NOT NULL DEFAULT 'open',
    reason STRING NOT NULL,
    resolution_operation_id UUID REFERENCES memory_operations (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (operation_id, semantic_memory_id),
    CONSTRAINT memory_review_status CHECK (status IN ('open', 'confirmed', 'retracted', 'superseded'))
);

INSERT INTO incident_semantic_beliefs (incident_id, belief_id, relationship)
SELECT link.incident_id, memory.belief_id, link.relationship
FROM incident_semantic_memories AS link
JOIN semantic_memories AS memory ON memory.id = link.memory_id
ON CONFLICT (incident_id, belief_id) DO UPDATE SET relationship = excluded.relationship;

ALTER TABLE semantic_memories
    ADD CONSTRAINT semantic_memories_operation_fk
        FOREIGN KEY (created_by_operation_id) REFERENCES memory_operations (id);

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_decision_fk
        FOREIGN KEY (decision_id) REFERENCES memory_decisions (id);

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_retrieval_policy CHECK (
        retrieval_policy IN ('semantic_strict', 'semantic_then_keyword')
    );

CREATE INDEX IF NOT EXISTS memory_operation_events_operation_idx
    ON memory_operation_events (operation_id, sequence);

CREATE INDEX IF NOT EXISTS memory_review_items_status_idx
    ON memory_review_items (status, created_at);

-- CockroachDB expands SELECT * when a view is created, so views created by
-- 0002 must be rebuilt to expose the governed columns added in 0007a.
CREATE OR REPLACE VIEW current_episodic_memories AS
SELECT *
FROM episodic_memories
WHERE t_invalid IS NULL;

CREATE OR REPLACE VIEW current_semantic_memories AS
SELECT *
FROM semantic_memories
WHERE t_invalid IS NULL;
