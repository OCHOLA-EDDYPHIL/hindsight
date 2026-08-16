# ADR 0002: Admit generated machine memory only through candidate-bound review

Status: Accepted

## Context

A resolved incident can supply useful evidence for a procedural lesson, but a model-generated lesson is not trusted guidance merely because it is well-formed or cites stored evidence. Allowing generated content directly into semantic retrieval would let synthesis, validation, or stale-source errors influence later incident recommendations without an explicit trust decision.

The generated content must remain inspectable, and approval or rejection must refer to the exact candidate the operator reviewed. Rejection must not erase the candidate or turn it into negative guidance that retrieval can accidentally select.

## Decision

- Consolidation writes a generated procedural lesson as a semantic memory version with `trust_status=review_required`, `operator_disposition=unreviewed`, and `usage_instruction=audit_only`.
- The candidate record retains the structured payload, rendered content, source-evidence manifest, candidate and evidence fingerprints, and generation and semantic-validation receipts. Once review is pending, those identity fields and receipts are immutable.
- Positive-guidance retrieval requires an active, prompt-safe memory with approved, safe, supported, positive-guidance governance. The generated candidate therefore remains visible through audit and protected candidate APIs but cannot enter positive-guidance semantic retrieval.
- Candidate inspection and review require the protected operator write scope. The server supplies the authenticated actor. A review preview requires an `approve` or `reject` action and a reason, then binds the candidate ID, candidate memory ID, candidate fingerprint, evidence fingerprint, namespace, actor, and action.
- Execution is an idempotent `consolidation_approval` memory operation. The applying transaction rechecks the pending review state, preview fingerprint, candidate identity, namespace revision, and current memory version.
- Approval additionally requires a passed semantic-validation receipt and revalidates the complete evidence manifest. Cited memory evidence must remain current, active, lineage-complete, linked through the recorded incident relationship, and content-digest identical. The resolution event and resolved incident must retain their recorded identities and digest.
- Successful approval closes the audit-only candidate and creates an active positive-guidance successor. The successor keeps the same belief identity, points to the candidate as its previous version and causal parent, and records the operator and approval operation in provenance. Retrieval can select the successor, never the candidate version.
- Rejection records the authenticated reviewer, reason, operation, and time, creates no successor, and leaves the candidate `review_required` and audit-only. A rejected candidate is not positive or negative guidance.
- Approved and rejected review states are terminal. A changed, incomplete, legacy-unverifiable, or already reviewed candidate fails closed rather than being admitted.

## Consequences

Generation and semantic validation can prepare a reviewable candidate but cannot cross the retrieval trust boundary. Operators review stable content and evidence identities rather than mutable prose.

Approval costs an additional preview and queued operation and creates a successor version instead of changing trust fields in place. That extra state preserves the distinction between machine-generated material, the human admission decision, and the memory version later agents are allowed to use.

Rejection preserves an auditable explanation without teaching the agent the rejected content. Future policy can inspect terminal review history without reinterpreting it as guidance.
