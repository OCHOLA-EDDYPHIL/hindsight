SELECT
    session_id,
    message->>'type' AS message_type,
    message->'data'->>'content' AS content,
    created_at
FROM agent_chat_messages
WHERE session_id = %s
ORDER BY created_at ASC;
