# ADR 0002: Model Memory with Bi-Temporal Validity

## Status

Accepted

## Context

Agent memory changes over time. A fact may be learned at one time, describe the world at another time, and later become contradicted or unsafe to use. Historical database reads can answer when the database knew something, but they do not by themselves model whether the agent should still believe it.

## Decision

Model memory with both transaction time and validity time:

- transaction time comes from CockroachDB history and historical reads
- validity time is represented in memory rows with explicit validity columns
- invalidation is an update that records when and why a memory stopped being trusted
- current-belief queries filter out invalidated memories
- audit queries keep invalidated memories visible

Memory rows should not be deleted to correct belief state. Corrections preserve the row and add invalidation metadata.

## Consequences

This makes current agent behavior and historical belief evolution queryable. It supports provenance, rewind, and audit without erasing evidence.

The tradeoff is that all retrieval paths must respect validity rules. A query that forgets to filter invalidated memories can reintroduce known-bad context.

## Avoided

This avoids destructive correction, silent decay as the source of truth, and memory behavior that cannot distinguish "was once believed" from "is currently trusted."
