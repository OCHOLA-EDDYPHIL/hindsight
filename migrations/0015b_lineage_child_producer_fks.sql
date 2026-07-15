ALTER TABLE memory_lineage_edges
    ADD CONSTRAINT memory_lineage_semantic_child_producer_fk
        FOREIGN KEY (child_semantic_memory_id, producer_decision_id)
        REFERENCES semantic_memories (id, producer_decision_id),
    ADD CONSTRAINT memory_lineage_episodic_child_producer_fk
        FOREIGN KEY (child_episodic_memory_id, producer_decision_id)
        REFERENCES episodic_memories (id, producer_decision_id);
