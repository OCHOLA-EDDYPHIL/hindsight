SELECT
    thread_id,
    checkpoint_ns,
    checkpoint_id,
    parent_checkpoint_id,
    metadata,
    created_at
FROM checkpoints
WHERE thread_id = %s
ORDER BY checkpoint_id DESC;
