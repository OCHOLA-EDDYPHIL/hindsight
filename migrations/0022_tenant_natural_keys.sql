DROP INDEX IF EXISTS services@services_slug_key CASCADE;
DROP INDEX IF EXISTS incidents@incidents_slug_key CASCADE;
DROP INDEX IF EXISTS runbooks@runbooks_slug_key CASCADE;
DROP INDEX IF EXISTS demo_sessions@demo_sessions_namespace_key CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS services_tenant_slug_idx
    ON services (tenant_id, slug);
CREATE UNIQUE INDEX IF NOT EXISTS incidents_tenant_slug_idx
    ON incidents (tenant_id, slug);
CREATE UNIQUE INDEX IF NOT EXISTS runbooks_tenant_slug_idx
    ON runbooks (tenant_id, slug);
CREATE UNIQUE INDEX IF NOT EXISTS demo_sessions_tenant_namespace_idx
    ON demo_sessions (tenant_id, namespace);
