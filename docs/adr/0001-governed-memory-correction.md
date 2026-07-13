# ADR 0001: Govern memory correction through version identity and explicit lineage

Status: Accepted

## Context

Memory correction cannot depend on a writer copying a decision identifier into an unrelated provenance field. That convention cannot prove which reads caused an output, and a missed convention silently weakens descendant invalidation.

Semantic retrieval also cannot hide vector, historical, keyword, and recency behavior behind one call. Corrections must operate on the same explicit belief versions that decisions actually read.

## Decision

- A semantic belief has a stable `belief_id`; every assertion, supersession, or rewind reassertion is a new immutable version.
- Every output has an authoritative producer decision. Its reads are typed, and each read is classified as causal derivation or non-causal context before lineage is marked complete.
- Exact rewind closes versions outside the approved target logical state and creates audited reassertion versions where historical beliefs must become current again. Historical rows are never rewritten.
- Retraction follows confirmed causal edges across namespaces and refuses incomplete lineage or incomplete namespace authorization.
- Correction supersession retracts causal descendants. Evolution supersession preserves descendants but removes them from trusted retrieval until an explicit review operation confirms or retracts them.
- Rewind, retraction, supersession, and review resolution execute from immutable previews as idempotent queued operations. Namespace revisions, lineage closure, preview expiry, and embedding generation are verified in the applying serializable transaction.
- Embeddings remain rebuildable indexes. Retrieval and writes use one database-active, content-addressed profile; profile rotation builds side-by-side and activates only at complete current-memory coverage.

## Consequences

Callers must open a decision before retrieval, declare causal parents when producing memory, and select an explicit retrieval policy. Mutation APIs return operation resources rather than completed effects. These requirements add state and worker machinery, but correction guarantees no longer depend on prose identifiers or partial best-effort cascades.
