# Operations

## Deployment boundary

The application Terraform stack packages and deploys the UI, API and worker Lambdas, Cognito, SQS queues, HTTP and WebSocket APIs, the changefeed relay, DynamoDB projection and quarantine state, CloudFront, logs, and alarms. CockroachDB Cloud and SSM SecureString values are external dependencies. Bootstrap, lifecycle, and edge concerns have separate state and permissions.

Deployment preserves this order:

1. build the frontend and Lambda artifacts from one source revision;
2. validate Terraform and the target AWS account and environment;
3. apply forward database migrations with the deployment identity;
4. apply restricted database roles and initialize durable agent storage;
5. deploy the application with the source revision embedded in its health responses;
6. configure or verify the CockroachDB changefeed against the deployed webhook; and
7. verify the public and protected boundaries appropriate to the environment.

The repository workflows preserve this ordering and their identity checks. Do not hand-apply only part of the sequence to a shared environment.

## Health and monitored revision

`GET /v1/health/live` reports process liveness and the embedded source revision. `GET /v1/health/ready` adds a database query. These endpoints identify the serving build and database reachability; they are not availability, capacity, or end-to-end correctness guarantees.

The scheduled deployed verifier resolves its expected revision from the repository-level Actions variable `HINDSIGHT_MONITORED_SHA` before entering the protected `demo` environment. It rejects an absent or malformed full revision and fails when the direct liveness or readiness response reports a different value. It also checks the UI-proxied readiness response and a ticketed WebSocket connection. An owner-triggered run may supply an explicit expected revision and choose the supported deployment environment.

The repository variable is a monitoring target. Changing it does not deploy code, inspect the current branch automatically, or make a mismatched deployment healthy. Update it only as part of an intentional deployment handoff.

The public read-only deployment is <https://hindsight.strathmoreedu.qzz.io>.

## Runtime credentials and least privilege

Store API, worker, and deployment database URLs; Gemini key-pool material; and the changefeed token as separate SSM SecureString parameters. Lambdas receive parameter names and resolve values at runtime. Do not place secret values in Terraform state or UI configuration.

Use separate SQL identities:

- deployment identity for migrations, role grants, storage initialization, and changefeed lifecycle;
- API identity for product reads and writes plus dispatch creation; and
- worker identity for leased execution, correction, consolidation, and embedding-profile activation.

Cognito access tokens are short-lived and API Gateway verified. Product authorization also requires an active opaque principal mapping and active tenant. Rotate one runtime credential or identity mapping at a time, verify readiness and one bounded operation, then retire the previous credential.

## Diagnostics, alarms, and notification limits

The incident agent cannot issue arbitrary AWS queries. Its CloudWatch tool has a server-owned account, region, namespace, dimensions, statistic, period, and query-key allow-list. A run can make at most three `GetMetricStatistics` calls, each within a maximum 15-minute window. Controlled metrics are published separately; the agent tool itself is read-only.

CloudWatch alarms cover Lambda errors, queue age and depth, scheduler failures, terminal quarantine, and other bounded service metrics. The application stack always subscribes an encrypted, exact-stage SQS receiver to its operational and budget SNS topics. The controlled probe can show that a selected alarm transition reached that receiver.

The controlled receiver is not an on-call destination and does not prove delivery to a person, email provider, webhook, or another external subscriber. Configure and confirm the optional email subscriber or additional operational SNS actions for the deployment's notification policy. Keep the SQS receiver as a bounded delivery-inspection surface rather than treating it as incident response.

Runtime instrumentation emits OpenTelemetry spans with bounded sampling through AWS Distro for OpenTelemetry (ADOT) to AWS X-Ray. Trace attributes correlate API, dispatch, worker, memory-read, reflection, and memory-write boundaries. Realtime is separately traced and correlated by run and tenant. Span attributes and structured logs exclude model payloads, memory content, and credentials; operators must still keep credentials out of incident text and model input.

## Queue attempts, terminal quarantine, and redrive

The run queue uses server-side encryption, 14-day source and DLQ retention, one message per worker batch, partial batch failure reporting, and visibility longer than the Lambda timeout and attempt lease. A scheduled dispatcher recovers unsent commands and expired active attempts; a scheduled reaper handles expired memory operations. Attempt ownership and monotonic command sequence prevent an expired worker from committing over a newer owner.

Retryable worker failures return the record to SQS. Deterministic terminal conditions such as a malformed envelope, unsupported command, missing run, or exhausted run attempts are written to a strict DynamoDB quarantine ledger. A successful quarantine write acknowledges the SQS record. If the ledger write fails, the message remains failed and can eventually reach the raw fallback DLQ. That DLQ has no event-source consumer.

A quarantine record derives a stable ID from the trusted queue and message identities. It stores only allow-listed routing identities, reason, attempt metadata, the exact raw-body SHA-256 digest, and a digest of the canonical record; it does not retain the raw message body. Duplicate writes are idempotent only when the stored identity still matches. Unknown fields, invalid state combinations, and identity or digest conflicts fail closed.

Only records for runs finalized with `RunAttemptsExhausted` are redrivable. The workflow requires the repository owner on protected main, the monitored deployed revision, the exact quarantine ID, the stored body digest, and the matching confirmation phrase. The redrive code revalidates the canonical record digest and uses conditional transitions from `quarantined` to `redrive_pending` to `redriven`.

Redrive does not feed the raw message back to SQS or reopen the failed run. It creates one idempotent fresh run from the source run's incident, namespace, user input, service, and retrieval policy, then records that new run ID in the quarantine ledger. Repeating the same bound request returns the same logical effect; a different binding is rejected.

When terminal-work alarms fire:

1. identify whether the work is in the strict quarantine ledger or the raw fallback DLQ;
2. inspect the bound run, dispatch, attempt, reason code, and correlated logs without copying raw payloads into tickets or chat;
3. correct the provider, database, permission, timeout, or invariant cause; and
4. use the owner-gated redrive only for an eligible exhausted run.

Do not rewrite durable run state or manually requeue an unverified body.

## Changefeed and realtime lifecycle

The CockroachDB changefeed projects committed outbox rows to an authenticated webhook. The relay uses owner-fenced leases, permits takeover only after expiry, completes only after the durable delivery boundary, and deduplicates stable event identities. The browser also deduplicates replayed projections while retaining newer state.

Before destroying or replacing the application endpoint, pause the exact changefeed. After deployment, apply or resume it with `scripts/configure_changefeed.py`, confirm the database and webhook identity, and exercise reconnect plus state reload. If realtime delivery is delayed, HTTP remains authoritative; inspect changefeed job state, webhook authentication, Lambda errors, DynamoDB TTL and idempotency records, API Gateway connection errors, and ticket and subscription flow.

## Database history, rewind, and lifecycle

Migrations are forward-only, filename-ordered, and recorded in `schema_migrations`. Application rollback means deploying a compatible prior revision; it does not mean reversing migrations or restoring old rows blindly.

CockroachDB `AS OF SYSTEM TIME` powers read-only historical inspection. A Hindsight rewind is a product operation that closes current memory versions and writes audited reassertion versions; it is not a database rollback and does not alter past run events.

Tenant retirement is a separate privileged export-and-purge workflow with leases, versioned S3 data and manifest objects, schema and content fingerprints, explicit confirmation, lifecycle fences, catalog-driven deletion, and a tombstone. Establish backup, retention, restore, purge, and recovery policy before enabling that path for a tenant.

## Database audit surfaces

The repository infrastructure audit authenticates pinned CockroachDB guidance, runs an application-owned fixed read-only SQL catalog, and performs separate denial probes against the restricted auditor role. Its receipts cover only the catalog, tenant-bound persisted provenance, and the tested privilege denials.

CockroachDB Managed MCP is separate development tooling for inspecting schema shape and selected persisted identities. It is not a runtime database connection, the restricted-role privilege audit, a vector-index plan or capacity probe, or an assertion that a later deployment still matches the inspected database.

## Capacity and recovery limits

Terraform bounds concurrency, duration, batch size, retries, and log retention, but those settings do not establish a maximum request rate, tenant count, queue-backlog time, availability objective, or multi-region recovery posture. Monitor CockroachDB request units, storage and vector-index health; Gemini quota and latency; Lambda throttles, duration and errors; queue age, depth and terminal work; changefeed lag; WebSocket failures; and end-to-end run latency before changing limits.
