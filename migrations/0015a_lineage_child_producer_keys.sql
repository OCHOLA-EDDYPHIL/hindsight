CREATE UNIQUE INDEX IF NOT EXISTS semantic_memories_id_producer_idx
    ON semantic_memories (id, producer_decision_id);

CREATE UNIQUE INDEX IF NOT EXISTS episodic_memories_id_producer_idx
    ON episodic_memories (id, producer_decision_id);
