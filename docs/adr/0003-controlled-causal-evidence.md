# ADR 0003: Present controlled recommendation evidence without claiming execution or recovery

Status: Accepted

## Context

A before/after recommendation is easy to overstate. Different prompts, telemetry, model configuration, retrieval settings, or memory selections can produce different text without establishing which controlled input changed. Even a valid recommendation delta does not show that an action ran, that a service recovered, or that the result repeats.

The product needs a canonical evidence envelope that can support the narrow claims its persisted records establish, expose missing or mismatched material conservatively, and produce a downloadable public document without leaking private incident or memory content.

## Decision

- Each controlled recommendation records a strict canonical envelope containing identity, invariant inputs, permitted intervention, actual decision inputs, rendered-prompt digests, and structured decision output. Invariant inputs, permitted intervention, and actual decision inputs each carry a canonical SHA-256 binding, and the complete envelope carries its own binding.
- Envelope validation requires the complete versioned field set, exact canonical value types, matching component digests, rendered-prompt digests derived from the recorded model requests, and a matching whole-envelope digest. Legacy, partial, unknown-field, non-finite, or altered envelopes are unavailable.
- The controlled pair requires matching scenario, namespace, replay anchor, routing key, and release identity. It also requires equality of the normalized incident, prompt templates, triage result, ordered tool calls and observations, model-request configuration, tool contract, embedding profile, action catalog, tenant and scenario context, and retrieval policy and version.
- The only permitted intervention is the declared governed-memory delta bound to the correction operation. Removed and restored ordered memory versions, selection fingerprints, changed prompt fragments, operation effects, and correction identity must agree with the completed operation and recorded reads.
- Recommendation comparison uses the server-validated operational-action contract and semantic action fingerprint, not recommendation IDs or explanatory prose. A different valid fingerprint can prove a recorded action delta; an equal fingerprint records no delta; incomplete or invalid structured actions make the delta unavailable.
- Proof is reported independently for memory correction, action delta, and controlled-pair eligibility. A state is `proven` only when its complete checks succeed, `not_proven` when available checks do not establish the proposition, and `unavailable` when required material cannot be validated. Controlled-pair checks retain their individual `matched`, `mismatched`, or `unavailable` reasons.
- Repeatable causal effect and service recovery remain `unavailable` because this comparison does not measure repeated trials or service outcomes. The evidence scope is `recommendation_only`: recommendations are recorded decisions, not executed actions.

## Public projection and download

The internal envelope is not returned directly. The public projection replaces tenant and namespace values, incident and memory content, prompt and provenance text, account-specific values, and private identifiers. It preserves the controlled contract fields needed for comparison, then rebuilds every affected digest around the redacted values.

The evidence document contains the same proof states and controlled-pair checks shown in the scenario, the redacted correction receipt and memory versions, the declared intervention, and the before/after recommendation envelopes. It is serialized as strict canonical JSON.

The scenario response publishes the expected document digest and download URLs. The download response supplies the canonical bytes, an attachment filename, and the same digest in `X-Hindsight-Evidence-SHA256`. The browser hashes the received bytes and saves them only when the computed digest, response header, and scenario receipt match. A mismatch fails closed.

The digest binds content across those product surfaces but is not a digital signature or an external timestamp. Redaction protects the fields it replaces; operators must still avoid putting credentials into incident or model input.

## Consequences

The cockpit can say that a governed memory correction and a recorded structured recommendation change satisfy one controlled comparison. It cannot say that Hindsight executed the recommendation, recovered the service, proved a general causal relationship, or established repeatability.

New evidence fields require a new envelope contract rather than permissive parsing. Older or incomplete runs remain inspectable, but their unsupported proof states stay unavailable.
