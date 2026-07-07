# ADR 0001: Use CockroachDB as the Unified Memory Store

## Status

Accepted

## Context

Hindsight needs durable agent state, semantic recall, incident records, provenance, audit history, and historical reads. Splitting those concerns across a relational database, a vector database, and a separate checkpoint store would make the memory system harder to reason about and harder to audit.

## Decision

Use CockroachDB as the primary persistent store for Hindsight memory:

- episodic state, including conversation and reasoning checkpoints
- semantic memory, including vector-indexed incident knowledge
- transactional records, including incidents, services, runbooks, and provenance

The memory API should treat CockroachDB as the source of truth. External services may compute, host, or display information, but durable memory state belongs in CockroachDB.

## Consequences

This keeps recall, provenance, validity, and incident data queryable in one transactional system. It also lets the project show vector retrieval and relational filtering together without duplicating state across systems.

The tradeoff is that the schema and query layer must be designed carefully. Hindsight should avoid hiding important memory behavior behind opaque application-only state.

## Avoided

This avoids a separate vector database as the memory source of truth, separate checkpoint persistence, and audit trails that cannot be joined back to the memories and incidents they describe.
