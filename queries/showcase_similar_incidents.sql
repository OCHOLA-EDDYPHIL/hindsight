SELECT
    m.id AS memory_id,
    m.content AS memory_content,
    e.embedding <=> %s::VECTOR(1024) AS distance,
    i.slug AS incident_slug,
    i.title AS incident_title,
    i.severity,
    s.slug AS service_slug,
    s.name AS service_name,
    r.slug AS runbook_slug,
    r.title AS runbook_title
FROM current_semantic_memories AS m
JOIN semantic_memory_embeddings AS e
    ON e.memory_id = m.id
JOIN incident_semantic_memories AS im
    ON im.memory_id = m.id
JOIN incidents AS i
    ON i.id = im.incident_id
JOIN incident_services AS isvc
    ON isvc.incident_id = i.id
JOIN services AS s
    ON s.id = isvc.service_id
LEFT JOIN incident_runbooks AS ir
    ON ir.incident_id = i.id
LEFT JOIN runbooks AS r
    ON r.id = ir.runbook_id
WHERE m.namespace = %s
    AND s.slug = %s
ORDER BY e.embedding <=> %s::VECTOR(1024)
LIMIT %s;
