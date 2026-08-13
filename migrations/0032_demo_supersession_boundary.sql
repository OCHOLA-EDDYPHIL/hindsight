-- Let the restricted API role close only the known-good seed of an active
-- demo session. Ordinary API writes still cannot UPDATE immutable memory rows.

CREATE ROLE IF NOT EXISTS hindsight_agent_writer NOLOGIN;
ALTER ROLE hindsight_agent_writer NOLOGIN;
ALTER ROLE hindsight_agent_writer NOBYPASSRLS;

CREATE OR REPLACE FUNCTION close_active_demo_seed_for_supersession(
    expected_memory_id UUID,
    expected_namespace STRING
)
RETURNS UUID
LANGUAGE PLpgSQL
SECURITY DEFINER
AS $$
DECLARE
    active_tenant_id UUID;
    closed_memory_id UUID;
BEGIN
    active_tenant_id := public.current_hindsight_tenant_id();
    IF active_tenant_id IS NULL THEN
        RAISE EXCEPTION 'demo supersession requires tenant context';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.demo_sessions AS session
        WHERE session.tenant_id = active_tenant_id
          AND session.namespace = expected_namespace
          AND session.demo_kind = 'compromised_guidance_rewind'
          AND session.status = 'active'
    ) THEN
        RAISE EXCEPTION 'demo supersession requires an active demo session';
    END IF;

    UPDATE public.semantic_memories
    SET
        t_invalid = now(),
        invalidated_by = 'demo.fixture-import',
        invalidation_reason =
            'Supersede the accepted belief with the imported runbook version',
        invalidated_at = now()
    WHERE tenant_id = active_tenant_id
      AND id = expected_memory_id
      AND namespace = expected_namespace
      AND t_invalid IS NULL
      AND writer = 'demo.seed'
      AND source_ref = 'demo:known-good-payment-incident'
      AND metadata->>'demo' = 'compromised-guidance-rewind'
      AND metadata->>'role' = 'known-good'
    RETURNING id INTO closed_memory_id;

    IF closed_memory_id IS NULL THEN
        RAISE EXCEPTION 'known-good demo belief changed before supersession';
    END IF;

    UPDATE public.memory_namespaces
    SET revision = revision + 1, updated_at = now()
    WHERE tenant_id = active_tenant_id
      AND namespace = expected_namespace;

    RETURN closed_memory_id;
END
$$;

REVOKE ALL ON FUNCTION close_active_demo_seed_for_supersession(UUID, STRING)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION close_active_demo_seed_for_supersession(UUID, STRING)
TO hindsight_agent_writer;
