-- Additive tenant columns must commit before backfill and constraints.

CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug STRING NOT NULL UNIQUE,
    tenant_kind STRING NOT NULL,
    status STRING NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenants_kind CHECK (
        tenant_kind IN ('legacy', 'public_demo', 'acceptance', 'diagnostic')
    ),
    CONSTRAINT tenants_status CHECK (status IN ('active', 'archived'))
);

ALTER TABLE episodic_memories ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE semantic_memories ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_reads ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE semantic_memory_embeddings ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE services ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE incident_services ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE incident_events ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE runbooks ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE incident_runbooks ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE incident_semantic_memories ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_operations ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE mcp_audit_events ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE agent_run_events ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_decisions ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_namespaces ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE semantic_beliefs ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_external_evidence ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE incident_semantic_beliefs ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE semantic_memory_vectors ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE embedding_backfill_tasks ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_retrievals ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_lineage_edges ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE agent_reflections ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_operation_previews ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_operation_events ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_operation_effects ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE memory_review_items ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE consolidation_jobs ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE demo_sessions ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE benchmark_experiments ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE benchmark_trials ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE benchmark_actions ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE benchmark_confirmation_preregistrations ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE benchmark_confirmation_bindings ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE benchmark_variant_preparations ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE agent_run_dispatches ADD COLUMN IF NOT EXISTS tenant_id UUID;

-- Create the vendor-owned persistence tables before their setup helpers run so
-- tenant context is part of the durable schema on both fresh and upgraded data.
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id STRING NOT NULL,
    checkpoint_ns STRING NOT NULL DEFAULT '',
    checkpoint_id STRING NOT NULL,
    parent_checkpoint_id STRING,
    type STRING,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id STRING NOT NULL,
    checkpoint_ns STRING NOT NULL DEFAULT '',
    channel STRING NOT NULL,
    version STRING NOT NULL,
    type STRING NOT NULL,
    blob BYTES,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id STRING NOT NULL,
    checkpoint_ns STRING NOT NULL DEFAULT '',
    checkpoint_id STRING NOT NULL,
    task_id STRING NOT NULL,
    task_path STRING NOT NULL DEFAULT '',
    idx INT8 NOT NULL,
    channel STRING NOT NULL,
    type STRING,
    blob BYTES NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE TABLE IF NOT EXISTS agent_chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id STRING NOT NULL,
    message JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE checkpoint_blobs ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE agent_chat_messages ADD COLUMN IF NOT EXISTS tenant_id UUID;

CREATE TABLE IF NOT EXISTS tenant_event_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL DEFAULT
        nullif(current_setting('hindsight.tenant_id', true), '')::UUID
        REFERENCES tenants (id),
    event_type STRING NOT NULL,
    aggregate_type STRING NOT NULL,
    aggregate_id STRING NOT NULL,
    topics JSONB NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_event_outbox_topics_array CHECK (jsonb_typeof(topics) = 'array'),
    CONSTRAINT tenant_event_outbox_payload_object CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX IF NOT EXISTS tenant_event_outbox_tenant_created_idx
    ON tenant_event_outbox (tenant_id, created_at, id);
