-- Persist only routing identifiers and state transitions. The source row and
-- user-authored content never enter the CDC boundary.

CREATE OR REPLACE FUNCTION emit_tenant_event_outbox()
RETURNS TRIGGER
LANGUAGE PLpgSQL
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    row_data JSONB;
    event_tenant UUID;
    aggregate_id STRING;
    run_id STRING;
    event_topics JSONB;
    event_payload JSONB;
BEGIN
    IF TG_OP = 'DELETE' THEN
        row_data := to_jsonb(OLD);
    ELSE
        row_data := to_jsonb(NEW);
    END IF;

    event_tenant := (row_data->>'tenant_id')::UUID;
    aggregate_id := row_data->>'id';
    run_id := CASE
        WHEN TG_TABLE_NAME = 'agent_runs' THEN aggregate_id
        ELSE row_data->>'run_id'
    END;
    event_topics := jsonb_build_array(
        'tenant:' || event_tenant::STRING,
        'table:' || TG_TABLE_NAME,
        'aggregate:' || aggregate_id
    );
    IF run_id IS NOT NULL THEN
        event_topics := event_topics || jsonb_build_array('run:' || run_id);
    END IF;

    event_payload := jsonb_strip_nulls(jsonb_build_object(
        'id', aggregate_id,
        'run_id', run_id,
        'incident_id', row_data->>'incident_id',
        'status', row_data->>'status',
        'operation_type', row_data->>'operation_type',
        'sequence', row_data->>'sequence',
        'updated_at', COALESCE(row_data->>'updated_at', row_data->>'created_at')
    ));

    INSERT INTO tenant_event_outbox (
        tenant_id, event_type, aggregate_type, aggregate_id, topics, payload
    ) VALUES (
        event_tenant,
        lower(TG_TABLE_NAME || '.' || TG_OP),
        TG_TABLE_NAME,
        aggregate_id,
        event_topics,
        event_payload
    );
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER incidents_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON incidents
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();

CREATE TRIGGER semantic_memories_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON semantic_memories
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();

CREATE TRIGGER memory_operations_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON memory_operations
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();

CREATE TRIGGER agent_runs_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON agent_runs
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();

CREATE TRIGGER agent_run_events_tenant_event_outbox
AFTER INSERT OR UPDATE OR DELETE ON agent_run_events
FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox();
