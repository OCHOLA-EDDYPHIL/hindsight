# ADR 0004: Keep Model and Embedding Providers Pluggable

## Status

Accepted

## Context

Reasoning and embedding providers can change because of cost, access, latency, deployment requirements, or model quality. Hindsight's durable value is the memory system, not a hard dependency on one provider's SDK or model family.

## Decision

Keep model and embedding calls behind provider interfaces selected by configuration.

The agent loop may use a configured chat model for reasoning and a configured embedding model for vector memory. The memory schema and memory API should not depend on provider-specific response shapes, credentials, or SDK objects.

## Consequences

This keeps development, deployment, and future evaluation flexible. It also lets tests exercise memory behavior without requiring live model calls.

The tradeoff is a small abstraction layer around provider clients. That layer should stay thin and expose only the behavior the agent needs.

## Avoided

This avoids coupling core memory code to one provider, committing provider-specific credentials or endpoint details, and making architectural decisions based on temporary access constraints.
