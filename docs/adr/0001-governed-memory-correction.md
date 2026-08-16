# ADR 0001: Govern memory correction through bi-temporal version identity and explicit lineage

Status: Accepted

## Context

Memory correction cannot depend on a writer copying a decision identifier into an unrelated provenance field. That convention cannot prove which reads influenced an output, and a missed convention silently weakens descendant invalidation.

Historical inspection and product rewind also solve different problems. A past CockroachDB snapshot can show what rows existed at a cutoff, but reading that snapshot does not make those beliefs current again. Conversely, restoring a prior logical belief state must not rewrite the rows or run history that explain the intervening decisions.

Semantic retrieval cannot hide vector, historical, keyword, and recency behavior behind one call. Corrections must operate on the same explicit belief versions that governed decisions actually read.

## Decision

- A semantic belief has a stable `belief_id`; every assertion, supersession, or rewind reassertion is a new immutable version with independent valid-time and system-time identity.
- Every governed output has an authoritative producer decision. Its reads are typed, and each read is classified as causal derivation or non-causal context before lineage is marked complete.
- Historical inspection uses a separate read transaction with CockroachDB `AS OF SYSTEM TIME`. The product snapshot applies the requested cutoff to persisted system state and logical validity. It does not mutate current memory, enqueue an operation, rerun an agent, or execute a recommendation.
- Exact product rewind executes only from an authenticated, immutable preview. It closes current versions outside the approved target logical state and creates audited reassertion versions where historical content must become current again. It does not restore the database or edit the historical source versions.
- Run transition code inserts ordered `agent_run_events` alongside the mutable run summary. Product rewind does not remove, reorder, or reinterpret those earlier run events; a later run is a separate record.
- Retraction follows confirmed causal edges across namespaces and refuses incomplete lineage or incomplete namespace authorization.
- Correction supersession retracts causal descendants. Evolution supersession preserves descendants but removes them from trusted retrieval until an explicit review operation confirms or retracts them.
- Rewind, retraction, supersession, and review resolution execute as idempotent queued operations. Namespace revisions, lineage closure, preview expiry, authorized scope, and embedding generation are verified in the applying serializable transaction.
- Embeddings remain rebuildable indexes. Retrieval and writes use one database-active, content-addressed profile; profile rotation builds side by side and activates only at complete current-memory coverage.

## Consequences

Callers must open a decision before retrieval, declare causal parents when producing memory, and select an explicit retrieval policy. Mutation APIs return operation resources rather than completed effects.

An operator can inspect a past state without changing it, or approve a rewind that produces a new current state without erasing the path to it. A historical view is therefore evidence about the past, not a preview of an already-applied rewind. A rewind is a governed memory mutation, not database recovery, run replay, recommendation execution, or proof of service recovery.
