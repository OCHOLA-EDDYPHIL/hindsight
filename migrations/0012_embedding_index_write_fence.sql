-- Serialize semantic-memory writes with embedding-profile snapshots and activation.
-- The row's data is intentionally immutable: callers only take a transactional
-- FOR UPDATE lock and never use it to carry index state.

CREATE TABLE IF NOT EXISTS embedding_index_write_fence (
    singleton BOOL PRIMARY KEY DEFAULT true,
    CONSTRAINT embedding_index_write_fence_singleton CHECK (singleton)
);

INSERT INTO embedding_index_write_fence (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

-- Hosted product roles can predate this migration, while clean development
-- databases may not have applied infra/db/roles.sql. Ensure both principals
-- exist so the migration can grant access without requiring the role template
-- to be replayed after every schema upgrade. Credentials remain externally
-- managed and these statements do not alter an existing role.
CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN;
CREATE ROLE IF NOT EXISTS hindsight_memory_worker LOGIN;

-- Agent writes only lock the immutable fence and enqueue new memories for a
-- profile that is already building. They cannot lease or mutate those tasks.
GRANT SELECT, UPDATE ON TABLE embedding_index_write_fence
TO hindsight_agent_writer;

GRANT SELECT, INSERT ON TABLE embedding_backfill_tasks
TO hindsight_agent_writer;

-- The embedding worker takes the same fence while building and activating
-- profiles. Consolidation also needs to repoint an incident's active belief
-- after publishing a replacement. Its other administration grants remain
-- unchanged.
GRANT SELECT, UPDATE ON TABLE embedding_index_write_fence
TO hindsight_memory_worker;

GRANT UPDATE ON TABLE incident_semantic_beliefs
TO hindsight_memory_worker;
