# ADR 0003: Make Rewind an Audited State Transition

## Status

Accepted

## Context

Historical reads can show what the database knew at a previous point in time. Hindsight also needs to change the agent's current belief state after a poisoned or stale memory is found. A historical query alone is not enough; rewind must leave the system in a corrected, auditable state.

## Decision

Define rewind as a transactional memory operation, not only an as-of query.

`rewind(t, reason)` reconstructs the belief set at timestamp `t`, identifies memories written after `t` and derived memories that depend on them, invalidates those memories, writes a first-class rewind event, and returns the restored belief set for replanning.

The rewind event must include who initiated it, the target timestamp, the reason, and the invalidated memory ids.

## Consequences

Rewind becomes explainable and replayable. The correction is itself part of memory history, so later audit can inspect both the bad belief and the operation that fixed it.

The tradeoff is that read tracking and provenance links become required infrastructure. Without them, dependent-memory invalidation cannot be computed reliably.

## Avoided

This avoids presenting a historical query as rollback, deleting poisoned rows, or correcting current state without a traceable event.
