UPDATE demo_sessions AS session
SET
    incident_tenant_id = session.tenant_id,
    incident_id = (
        SELECT run.incident_id
        FROM agent_runs AS run
        WHERE run.tenant_id = session.tenant_id
          AND run.namespace = session.namespace
          AND run.incident_id IS NOT NULL
        ORDER BY run.created_at, run.id
        LIMIT 1
    )
WHERE session.incident_id IS NULL
  AND EXISTS (
      SELECT 1
      FROM agent_runs AS run
      WHERE run.tenant_id = session.tenant_id
        AND run.namespace = session.namespace
        AND run.incident_id IS NOT NULL
  );

ALTER TABLE demo_sessions
    ADD CONSTRAINT IF NOT EXISTS demo_sessions_incident_identity_check
    CHECK (
        (incident_tenant_id IS NULL AND incident_id IS NULL)
        OR (
            incident_tenant_id = tenant_id
            AND incident_id IS NOT NULL
        )
    ),
    ADD CONSTRAINT IF NOT EXISTS demo_sessions_tenant_incident_fk
    FOREIGN KEY (incident_tenant_id, incident_id)
    REFERENCES incidents (tenant_id, id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS demo_sessions_tenant_incident_idx
    ON demo_sessions (incident_tenant_id, incident_id)
    WHERE incident_id IS NOT NULL;

GRANT UPDATE ON TABLE demo_sessions TO hindsight_lifecycle;
