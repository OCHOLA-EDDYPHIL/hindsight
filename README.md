# Hindsight

An incident-response copilot that can rewind its own mind.

Hindsight is an agentic application that treats an AI agent's memory the way SREs treat production systems: as infrastructure that must be auditable, transactional, observable, and recoverable. Every fact the agent learns is a database row with provenance. Every decision can be traced back to the memories that shaped it. And when a bad memory leads the agent astray, its belief state can be rewound to a point in time before the damage was done.

The memory layer is built on CockroachDB, which stores the agent's episodic memory (conversation and reasoning state), semantic memory (vector-indexed knowledge from past incidents), and the transactional system-of-record data (incidents, services, runbooks) in a single distributed SQL database. The agent runs on AWS.

This project is under active development for the CockroachDB and AWS "Build with Agentic Memory" hackathon. The roadmap, design decisions, and open questions live in this repository's Issues and Milestones rather than in documents in the codebase — start with the pinned issue titled "North Star" for the full picture.

## MCP memory inspection

Hindsight exposes a read-only MCP server for inspecting the agent's memory from an MCP client:

```bash
uv run python scripts/run_mcp_server.py
```

The server uses `DATABASE_URL` for the CockroachDB cluster and exposes four tools:

- `current_beliefs`: current semantic memories by namespace, or episodic memories by episode id.
- `beliefs_as_of`: semantic belief state visible at an ISO-8601 timestamp.
- `provenance_chain`: a memory row, its origin/invalidation provenance, and decisions that read it.
- `mcp_audit_log`: recent MCP audit events.

Each inspection tool records an `mcp_audit_events` row with the tool name, actor, purpose, arguments, result count, and timestamp. Memory-row inspection also records `memory_reads` rows so MCP reads appear in the same provenance trail as agent reads.

## Telemetry ingestion demo

Hindsight includes a scriptable telemetry demo that turns an induced checkout failure into incident context:

```bash
uv run python scripts/run_telemetry_demo.py
```

The demo service emits Prometheus-style checkout metrics and structured JSON log events. Its retry-fanout failure produces a webhook-equivalent telemetry signal, opens an `incidents` row, writes an `incident_events` telemetry alert, stores the metric/log excerpt as semantic memory with `telemetry.ingest` provenance, and then runs the incident agent against that namespace.

## License

Apache License 2.0. See [LICENSE](LICENSE).
