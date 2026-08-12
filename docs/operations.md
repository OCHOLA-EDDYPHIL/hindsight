# Operations

## Deployment boundary

The application Terraform stack packages and deploys the UI, API and worker Lambdas, Cognito, SQS queues, HTTP and WebSocket APIs, changefeed relay, DynamoDB projection state, CloudFront, logs, and alarms. CockroachDB Cloud and SSM SecureString values are external dependencies. Bootstrap, lifecycle, and edge concerns have separate state and permissions.

Deployment preserves this order:

1. build the frontend and Lambda artifacts from one source revision;
2. validate Terraform and the target AWS account and environment;
3. apply forward database migrations with the deployment identity;
4. apply restricted database roles and initialize durable agent storage;
5. deploy the application with the exact 40-character source revision;
6. configure or verify the CockroachDB changefeed against the deployed webhook; and
7. verify health, revision identity, identity boundaries, queue/worker behavior, realtime delivery, model behavior, and browser behavior.

The repository workflows are the executable deployment contract. Do not hand-apply only part of this sequence to a shared environment.

## Health and revision monitoring

`GET /v1/health/live` reports process liveness and deployed revision. `GET /v1/health/ready` also verifies a database query. A deployment candidate is reusable only when both health responses report the exact requested main SHA; an absent, shortened, or different revision fails closed.

Normal PR CI verifies code, artifacts, infrastructure, and schema behavior. Full live acceptance is the integrated exact-main gate for providers, semantic retrieval, consolidation, database roles, worker delivery/recovery, Cognito and public boundaries, realtime behavior, governed remediation, historical replay, and the deployed origin.

`.github/workflows/verify-deployed.yml` runs at minute 17 every six hours. It reads `HINDSIGHT_MONITORED_SHA`, checks the deployed health endpoints, and fails if the configured SHA is absent or differs. Update that repository variable only after the corresponding revision has passed the final deployment verification. A deliberate mismatch dispatch is the safe way to test the monitor's red path; restore the accepted SHA immediately afterward.

The public read-only deployment is <https://hindsight.strathmoreedu.qzz.io>.

## Runtime credentials and least privilege

Store API, worker, and deployment database URLs; Gemini key-pool material; and the changefeed token as separate SSM SecureString parameters. Lambdas receive parameter names and resolve values at runtime. Do not place secret values in Terraform state or UI configuration.

Use separate SQL identities:

- deployment identity for migrations, role grants, storage initialization, and changefeed lifecycle;
- API identity for product reads/writes and dispatch creation; and
- worker identity for leased execution, correction, consolidation, and embedding-profile activation.

Cognito access tokens are short-lived and API Gateway verified. Product authorization also requires an active opaque principal mapping and active tenant. Rotate one runtime credential or identity mapping at a time, verify readiness and one bounded operation, then retire the previous credential.

## Diagnostics and observability

The incident agent cannot issue arbitrary AWS queries. Its CloudWatch tool has a server-owned account, region, namespace, dimensions, statistic, period, and query-key allow-list. A run can make at most three `GetMetricStatistics` calls, each within a maximum 15-minute window. Controlled demonstrations publish explicitly labeled metrics separately; the agent tool itself is read-only.

CloudWatch alarms cover Lambda errors and a non-empty run DLQ. Alarm destinations are optional Terraform inputs and must be configured for an operational environment. OpenTelemetry and structured logs correlate API, dispatch, worker, operation, and realtime boundaries without persisting model payloads or credentials.

`.github/workflows/observability-evidence.yml` is a bounded audit workflow. It binds collection to a successful full acceptance run and exact SHA, exercises one SNS publish, samples only configured log and trace boundaries, scans the result for secrets, and records that provider acknowledgement is not proof of endpoint delivery.

## Queues, retries, and recovery

The run queue uses server-side encryption, 14-day source and DLQ retention, one message per worker batch, partial batch failure reporting, and visibility longer than the Lambda timeout and attempt lease. A scheduled dispatcher recovers unsent commands and expired active attempts; a scheduled reaper recovers expired memory operations. Attempt ownership and monotonic command sequence prevent an expired worker from committing over a newer owner.

On a DLQ alarm:

1. inspect the exact message, dispatch, attempt, run, and correlated logs;
2. verify the deployed revision and runtime parameter locators;
3. classify provider, database, permission, timeout, and invariant failures;
4. reproduce and correct the cause locally where possible; and
5. use the bounded recovery path rather than rewriting durable run state manually.

Persisted failure details redact common URL passwords, tokens, secrets, and API keys. Operators must still keep credentials out of incident text and model input.

## Changefeed and realtime lifecycle

The CockroachDB changefeed projects committed outbox rows to an authenticated webhook. The relay uses owner-fenced leases, permits takeover only after expiry, completes only after the durable delivery boundary, and deduplicates stable event identities. The browser also deduplicates replayed projections while retaining newer state.

Before destroying or replacing the application endpoint, pause the exact changefeed. After deployment, apply or resume it with `scripts/configure_changefeed.py`, confirm the database and webhook identity, and exercise reconnect plus state reload. If realtime delivery is delayed, HTTP remains authoritative; inspect changefeed job state, webhook authentication, Lambda errors, DynamoDB TTL/idempotency records, API Gateway connection errors, and ticket/subscription flow.

## Database lifecycle and rollback

Migrations are forward-only, filename-ordered, recorded in `schema_migrations`, and tested from both a fresh database and a populated historical fixture. Application rollback means deploying a compatible prior revision; it does not mean reversing migrations or restoring old rows blindly.

Memory rewind is a product operation, not a database rollback. Tenant retirement is a separate privileged export-and-purge workflow with leases, versioned S3 data and manifest objects, schema and content fingerprints, explicit confirmation, lifecycle fences, catalog-driven deletion, and a tombstone. The repository does not claim a completed hosted lifecycle drill. Validate backup, retention, restore, purge, and recovery policy before enabling that path for non-fixture tenants.

## Capacity and limitations

Terraform deliberately bounds concurrency, duration, batch size, retries, and log retention. A direct resource diagnostic completed with 75,000 synthetic vectors across 15 tenants. That diagnostic explicitly is not capacity qualification. The exact-main 100,000-vector, 20-tenant qualification attempt reached its 20-minute bound before completion, so that target remains unproven.

No result establishes maximum requests per second, arbitrary tenant count, queue backlog recovery time, availability, or multi-region disaster recovery. Monitor CockroachDB request units, storage and vector-index health; Gemini quota and latency; Lambda throttles, duration and errors; queue age, depth and DLQ; changefeed lag; WebSocket failures; and end-to-end run latency before changing limits or making production capacity claims.
