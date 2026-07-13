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

## Poison and rewind demo

The signature memory demo is also scriptable:

```bash
uv run python scripts/run_poison_rewind_demo.py all
```

The sequence seeds a known-good payment-latency memory, runs the agent cleanly, inserts a plausible poisoned memory with provenance, shows the agent make the wrong recommendation, traces the bad decision back to the memory it recalled, rewinds the namespace in one audited transaction, and reruns the agent to produce the corrected recommendation.

For rehearsals, the same script exposes smaller steps:

```bash
uv run python scripts/run_poison_rewind_demo.py poison --namespace demo:payments
uv run python scripts/run_poison_rewind_demo.py run --namespace demo:payments --label poisoned
uv run python scripts/run_poison_rewind_demo.py diagnose --decision-id agent:demo:payments:poisoned:plan
```

## Cross-episode learning demo

The repeat-incident demo shows Hindsight consolidating a resolved episode into a reusable lesson, then resolving a similar incident in fewer steps:

```bash
make cross-episode-demo-local
```

Run it with the live dashboard open on the printed namespace to watch the consolidated lesson appear as semantic memory:

```bash
make memory-dashboard-local
```

The demo output compares episode one and episode two with a deterministic steps-to-resolution metric and includes the consolidated memory recalled by episode two.

## Live memory dashboard

The demo dashboard shows semantic memories and rewind operations for one namespace as they change:

```bash
make dev-up
make migrate-local
make memory-dashboard-local
```

Open the printed local URL, then run the poison/rewind sequence against the same local database and namespace:

```bash
make poison-rewind-demo-local
```

The page receives memory and rewind events through a CockroachDB changefeed-backed Server-Sent Events stream. Invalidated memories remain visible with strikethrough styling, and the timeline scrubber can replay belief state at earlier timestamps.

Set `HINDSIGHT_DASHBOARD_AUTH_TOKEN` or pass `--auth-token` to require dashboard authentication. Visit the dashboard once with `?token=<token>` to set the browser cookie; scripted checks can use `Authorization: Bearer <token>`.

For final demo checks against CockroachDB Cloud, point `DATABASE_URL` or `--db-url` at the Cloud cluster explicitly. Local CockroachDB is the default rehearsal target.

## Incident cockpit and product API

The enhanced dashboard adds the incident workflow around the live memory view: agent run phases, plans and approvals, exact decision-to-memory influence, provenance details, and a guarded rewind preview. Read-only inspection stays public; model calls and memory mutations require the shared operator token.

Run the complete local product surface with an inline background worker:

```bash
make dev-up
make migrate-local
HINDSIGHT_FUNCTION_AUTH_TOKEN=local-demo LLM_PROVIDER=deterministic make product-api-local
```

Open `http://127.0.0.1:8766`. The hosted runtime replaces the inline worker with SQS, while CockroachDB remains the durable source of run state and ordered phase events.

The versioned API and interactive OpenAPI document are available under `/v1` and `/v1/docs`. Its primary resources are:

- incidents and asynchronous agent runs;
- approval/resume of interrupted runs;
- current or historical belief state;
- memory provenance and decision influence;
- rewind preview and guarded execution;
- operator-only signature-demo controls.

Run creation accepts an `Idempotency-Key` header and returns `202 Accepted`. The client follows the resulting run resource until it reaches `awaiting_approval`, `completed`, `rejected`, or `failed`.

In the deployed product, CockroachDB sends changes from `semantic_memories`, `memory_operations`, `agent_runs`, and `agent_run_events` to an authenticated webhook. Lightweight Lambda handlers fan one versioned event envelope out through API Gateway WebSockets. DynamoDB contains only expiring connection subscriptions; durable run and memory state never leaves CockroachDB.

Deployment automation manages the changefeed lifecycle separately from schema migrations:

```bash
make changefeed-apply
make changefeed-status
make changefeed-pause
```

`changefeed-apply` is idempotent for the same endpoint and token. Teardown pauses the feed before removing the AWS webhook so CockroachDB does not retry a dead sink.

## AWS deployment lifecycle

Terraform owns the complete ephemeral AWS application: private S3 UI hosting through CloudFront, API Gateway HTTP and WebSocket APIs, split Lambda artifacts, SQS with a dead-letter queue, the expiring connection registry, IAM, logs, throttles, and alarms. CockroachDB Cloud, SecureString values, and the Terraform bootstrap state are intentionally external to routine teardown.

Bootstrap the remote state bucket and GitHub OIDC role once from `infra/terraform/bootstrap`. The `deploy demo` workflow can then produce a plan or apply it through the protected `demo` environment. The `destroy demo` workflow requires the exact `destroy-demo` confirmation, pauses CockroachDB delivery first, and removes only the application stack.

Required GitHub repository variables are `AWS_DEPLOY_ROLE_ARN`, `TF_STATE_BUCKET`, and optionally `AWS_REGION` plus SSM parameter-name overrides. No long-lived AWS access keys are stored in GitHub.

## OpenTelemetry memory traces

The memory layer emits safe OpenTelemetry spans for reads, writes, invalidations, rewinds, and agent recall/reasoning/reflection boundaries. Spans include namespaces, memory IDs, counts, decision IDs, and writer names, but do not record raw memory content, prompts, recall queries, DB URLs, secrets, or operator-entered reasons.

Run a local Jaeger collector and a traced demo:

```bash
make dev-up
make migrate-local
make otel-up
make poison-rewind-trace-local
```

Open Jaeger at `http://localhost:16686` and search for the `hindsight-demo` service. The poison/rewind trace shows the clean recall, poison write, poisoned recall, rewind invalidation, and corrected recall. The cross-episode trace target similarly shows the consolidation write and episode-two lesson recall:

```bash
make cross-episode-trace-local
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
