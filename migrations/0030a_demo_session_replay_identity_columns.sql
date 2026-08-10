ALTER TABLE demo_sessions
    ADD COLUMN IF NOT EXISTS incident_tenant_id UUID,
    ADD COLUMN IF NOT EXISTS incident_id UUID,
    ADD COLUMN IF NOT EXISTS rewind_anchor TIMESTAMPTZ;
