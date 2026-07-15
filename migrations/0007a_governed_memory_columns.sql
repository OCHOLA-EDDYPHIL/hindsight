-- CockroachDB requires newly-added columns to finish their schema-change
-- backfill before a later transaction can read or update them. Keep this
-- additive phase separate from the governed-memory data backfill in 0008.

ALTER TABLE semantic_memories
    ADD COLUMN IF NOT EXISTS belief_id UUID,
    ADD COLUMN IF NOT EXISTS version_number INT8,
    ADD COLUMN IF NOT EXISTS previous_version_id UUID,
    ADD COLUMN IF NOT EXISTS producer_decision_id STRING,
    ADD COLUMN IF NOT EXISTS transition_kind STRING,
    ADD COLUMN IF NOT EXISTS content_schema STRING,
    ADD COLUMN IF NOT EXISTS structured_payload JSONB,
    ADD COLUMN IF NOT EXISTS payload_digest STRING,
    ADD COLUMN IF NOT EXISTS lineage_status STRING,
    ADD COLUMN IF NOT EXISTS trust_status STRING,
    ADD COLUMN IF NOT EXISTS created_by_operation_id UUID;

ALTER TABLE episodic_memories
    ADD COLUMN IF NOT EXISTS producer_decision_id STRING,
    ADD COLUMN IF NOT EXISTS content_schema STRING,
    ADD COLUMN IF NOT EXISTS structured_payload JSONB,
    ADD COLUMN IF NOT EXISTS payload_digest STRING,
    ADD COLUMN IF NOT EXISTS lineage_status STRING,
    ADD COLUMN IF NOT EXISTS trust_status STRING;

ALTER TABLE memory_reads
    ADD COLUMN IF NOT EXISTS semantic_memory_id UUID,
    ADD COLUMN IF NOT EXISTS episodic_memory_id UUID;

ALTER TABLE memory_operations
    ADD COLUMN IF NOT EXISTS idempotency_key STRING,
    ADD COLUMN IF NOT EXISTS status STRING,
    ADD COLUMN IF NOT EXISTS preview_id UUID,
    ADD COLUMN IF NOT EXISTS preview_fingerprint STRING,
    ADD COLUMN IF NOT EXISTS root_memory_kind STRING,
    ADD COLUMN IF NOT EXISTS root_memory_id UUID,
    ADD COLUMN IF NOT EXISTS expected_revisions JSONB,
    ADD COLUMN IF NOT EXISTS applied_revisions JSONB,
    ADD COLUMN IF NOT EXISTS request_payload JSONB,
    ADD COLUMN IF NOT EXISTS attempt_count INT8,
    ADD COLUMN IF NOT EXISTS lease_owner STRING,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS failure_code STRING,
    ADD COLUMN IF NOT EXISTS failure_detail STRING;

ALTER TABLE incidents
    ADD COLUMN IF NOT EXISTS resolution_event_id UUID;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS retrieval_policy STRING NOT NULL DEFAULT 'semantic_strict';
