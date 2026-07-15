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

## Cross-episode mechanism demo

The repeat-incident demo illustrates the mechanism: a resolved episode produces an evidence-cited procedural lesson, and a later incident retrieves that lesson. It is intentionally not a benchmark and reports no improvement metric:

```bash
make cross-episode-demo-local
```

Run it with the live dashboard open on the printed namespace to watch the consolidated lesson appear as semantic memory:

```bash
make memory-dashboard-local
```

The output includes the typed lesson, its citations and lineage, and the lesson recalled by episode two. Performance claims belong to the separate three-arm benchmark: no lesson, a project-curated reference lesson derived from the external simulator specification, and a normally consolidated lesson. The frozen corpus balances six independent failure mechanisms—retry amplification, cache stampedes, connection leaks, hot partitions, poison messages, and lock contention—across pilot and confirmation splits. Source evidence lives outside the tested arm namespaces. Every arm receives identical background and hard lexical distractors, and the claim gate verifies that the model saw the same ordered non-target content in every arm; random memory IDs and mechanism labels are never shown to the model. Recurrence prompts deliberately share little vocabulary with their targets. A live study proceeds only when both reference and consolidated lessons rank first under a precommitted semantic-profile distance cutoff.

Live benchmark commands require explicit reasoning and semantic-embedding providers. For Gemini, keep the API key in the local environment or `.env`, select both providers explicitly, and choose the cosine-distance cutoff on a separate calibration corpus before looking at pilot or confirmation outcomes. Profile activation, the pilot manifest, and the durable preregistration must all contain that same cutoff:

```bash
export LLM_PROVIDER=gemini
export EMBEDDING_PROVIDER=gemini
export GEMINI_MODEL=gemini-2.5-flash
export GEMINI_EMBEDDING_MODEL=gemini-embedding-2
export BENCHMARK_MAX_DISTANCE=<precommitted-cosine-distance>
export HINDSIGHT_BENCHMARK_CODE_SHA="$(git rev-parse HEAD)"

uv run python scripts/reembed_memories.py --max-distance "$BENCHMARK_MAX_DISTANCE"
uv run python scripts/run_learning_benchmark.py pilot \
  --max-distance "$BENCHMARK_MAX_DISTANCE"
uv run python scripts/run_learning_benchmark.py preregister \
  --pilot-experiment-id <pilot-experiment-id>
uv run python scripts/run_learning_benchmark.py confirmation \
  --pilot-experiment-id <pilot-experiment-id>
```

`preregister` derives the minimum independent sample from the completed pilot, then commits all twelve pilot-frozen held-out variants rather than outcome-selecting a subset. Two repetitions are aggregated within each incident variant, and same-mechanism variants are then aggregated before inference. Efficacy and reference noninferiority use one-sided exact sign-flip tests over mechanism-level action differences with Bonferroni control, plus an observed one-action minimum-effect gate; the pilot power calculation is explicitly a nominal normal approximation. Six mechanisms support pilot standard deviations up to about 0.755 actions at the frozen alpha, power, and effect target. If either endpoint requires more than six mechanisms, confirmation is not created and the study authorizes no claim; the corpus must be versioned and expanded without retuning against held-out outcomes. The study contract pins the code commit, corpus, providers, active embedding profile, cutoff, simulator, and endpoints, and permits only one outcome-bearing pilot and confirmation for that identity. `confirmation` loads that durable contract and cannot regenerate it. `ci-smoke` uses deterministic fixtures, skips live semantic rank checks, and is never eligible for performance claims.

The owner-authorized `live acceptance` workflow runs provider, migration, hosted deployment, benchmark, and Firefox checks on GitHub-hosted runners. It assumes the demo role through OIDC and reads the encrypted database and Gemini pool from SSM; local `.env` credentials are not copied into GitHub. Gemini semantic validation is mandatory. Set the repository variable `HINDSIGHT_REQUIRE_BEDROCK_ACCEPTANCE=1` when the account has Titan invocation quota; otherwise the workflow records that Bedrock invocation was deferred and makes no Bedrock-live claim.

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
- immutable correction previews and asynchronous rewind, retraction, supersession, and review operations;
- operator-only signature-demo controls.

Run creation and governed memory mutations accept an `Idempotency-Key` header and return `202 Accepted`. Mutation workers verify namespace revisions, lineage closure, preview expiry, and embedding generation before committing any effect.

In the deployed product, CockroachDB sends incident, governed-memory, operation, and agent-run changes to an authenticated webhook. Full before/after envelopes queue consolidation only for real transitions to `resolved`; the same feed fans versioned state events through API Gateway WebSockets. DynamoDB contains only expiring connection subscriptions; durable run and memory state never leaves CockroachDB.

Deployment automation manages the changefeed lifecycle separately from schema migrations:

```bash
make changefeed-apply
make changefeed-status
make changefeed-pause
```

`changefeed-apply` is idempotent for the same endpoint and token. Teardown pauses the feed before removing the AWS webhook so CockroachDB does not retry a dead sink.

## Retrieval and embedding profiles

Hosted retrieval is strict semantic vector search by default. A miss stays empty; keyword fallback occurs only when a run explicitly selects the degraded `semantic_then_keyword` policy, and every attempt, ordered hit, decision, and profile is audited. The deterministic hashing provider is a lexical-hash test fixture—not a semantic encoder—and hosted activation rejects it.

The hosted worker uses an SSM-backed Gemini key pool for reasoning and 1,024-dimensional `gemini-embedding-2` vectors, or configured Bedrock models. Local runs can set `GEMINI_API_KEY` plus numbered keys such as `GEMINI_API_KEY_1`.

Embedding spaces are content-addressed profiles. Model rotation builds vectors side-by-side and activation fails until every current trusted memory has coverage, so retrieval never mixes spaces or silently loses rows:

```bash
uv run python scripts/reembed_memories.py
```

## AWS deployment lifecycle

Terraform owns the complete ephemeral AWS application: private S3 UI hosting through CloudFront, API Gateway HTTP and WebSocket APIs, split Lambda artifacts, SQS with a dead-letter queue, expiring WebSocket and Gemini-cooldown registries, IAM, logs, throttles, alarms, and the DNS-only Cloudflare alias. CockroachDB Cloud, SecureString values, ACM validation, deployment IAM, and Terraform bootstrap state are intentionally external to routine teardown.

Bootstrap the deployment role and custom-domain certificate once from `infra/terraform/bootstrap`, reusing an existing state bucket and GitHub OIDC provider. The `deploy demo` workflow can then produce a plan or apply it through the protected `demo` environment. Apply checks SSM, ACM, Cloudflare, and CockroachDB before creating resources. The `destroy demo` workflow requires the exact `destroy-demo` confirmation, pauses CockroachDB delivery first, and removes only the application stack.

Required GitHub repository variables are `AWS_DEPLOY_ROLE_ARN`, `TF_STATE_BUCKET`, `HINDSIGHT_ACM_CERTIFICATE_ARN`, `HINDSIGHT_DOMAIN_NAME`, and `CLOUDFLARE_ZONE_ID`, plus optional region and SSM parameter-name overrides. `CLOUDFLARE_API_TOKEN` is a zone-scoped `demo` environment secret. No long-lived AWS access keys are stored in GitHub.

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
