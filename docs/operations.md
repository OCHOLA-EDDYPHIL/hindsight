# Operations

## Deployment boundary

The application Terraform stack packages and deploys the UI, API and worker Lambdas, SQS queues, WebSocket and changefeed relays, DynamoDB delivery state, CloudFront, logs, and alarms. CockroachDB Cloud and SSM SecureString values are external dependencies. Bootstrap and edge concerns have separate state and lifecycle.

Deployment must preserve this order:

1. build deterministic frontend and Lambda artifacts;
2. validate Terraform and the target account/environment;
3. apply database migrations with the deployment identity;
4. apply restricted database roles and initialize durable agent storage;
5. deploy the application with the exact source revision;
6. configure or verify the CockroachDB changefeed against the deployed webhook; and
7. verify readiness, revision identity, queue/worker behavior, realtime delivery, and browser behavior.

The repository workflows are the executable deployment contract. Avoid hand-applying only part of this sequence to a shared environment.

## Health and revision identity

`GET /v1/health/live` reports process liveness and the deployed revision. `GET /v1/health/ready` also verifies a database query. Acceptance reuses a deployed candidate only when its reported 40-character revision exactly matches the requested main SHA; an absent or different revision must deploy once or fail closed.

Normal PR CI proves code and schema checks. Hosted acceptance is owner-authorized, exact-main, and requires a unique successful CI run for the same SHA. Browser-only mode is diagnostic and runs authorization/preflight plus the browser product flow. Full mode is the authoritative final gate and additionally verifies live providers, semantics, consolidation, database roles, worker retries/DLQ recovery, and aggregate success.

## Runtime credentials and least privilege

Store database URLs, Gemini key-pool material, the operator token, and changefeed token as SSM SecureString parameters. The API and worker receive parameter names and resolve values at runtime. Use separate SQL users:

- deployment identity for migrations, role grants, and changefeed lifecycle;
- API identity for product reads/writes and dispatch creation;
- worker identity for leased execution, correction, consolidation, and profile activation.

Never copy a deployment/admin connection string into an application runtime parameter. Rotate one runtime identity at a time, verify readiness and a bounded operation, then retire the prior credential.

## Queues, retries, and recovery

The run queue uses server-side encryption, bounded receive attempts, visibility longer than the worker/attempt lease, partial batch failure reporting, and a DLQ. A scheduled dispatcher recovers unsent durable outbox rows, while a scheduled reaper recovers expired operations. CloudWatch alarms cover Lambda errors and a non-empty run DLQ; alarm destinations are optional Terraform inputs and must be configured for an operational deployment.

On a DLQ alarm:

1. stop guessing and inspect the exact message, run, attempt, and correlated logs;
2. verify the deployed revision and runtime parameter names;
3. classify provider, database, permission, timeout, and invariant failures;
4. correct the underlying cause locally where reproducible; and
5. use the repository’s bounded recovery path and acceptance checks rather than manually rewriting durable run state.

Error persistence redacts common URL passwords, tokens, secrets, and API keys, but operators must still avoid placing credentials in user-controlled text.

### Local SQS contract

The opt-in `aws` Compose profile provides a loopback-only SQS endpoint for
checking the production enqueue contract without accessing an AWS account. It
uses synthetic local credentials, creates a uniquely named temporary queue,
verifies one tenant-scoped command, and removes the queue even when validation
fails.

```bash
make aws-up
make aws-queue-smoke
make aws-down
```

The default Compose stack does not start this service. Its container image can
be a substantial download, so pull it intentionally. The smoke command refuses
non-loopback endpoints and is not a substitute for hosted acceptance.

## Changefeed and realtime lifecycle

The CockroachDB changefeed projects committed outbox rows to an authenticated webhook. The relay deduplicates deliveries, resolves tenant subscriptions, and posts WebSocket notifications. Before destroying or replacing the application endpoint, pause the exact changefeed. After deployment, apply or resume it with `scripts/configure_changefeed.py`, confirm the referenced database and webhook, and exercise reconnect/state-reload behavior.

If realtime delivery is delayed, the HTTP API remains authoritative. Check changefeed job state, webhook authentication, Lambda errors, DynamoDB TTL/idempotency state, API Gateway connection errors, and the browser’s ticket/subscription flow.

## Database lifecycle and rollback

Migrations are forward-only, filename-ordered, recorded in `schema_migrations`, and tested both from a fresh database and a populated historical fixture. Deployment rollback therefore means deploying a compatible prior application revision; it does not mean blindly reversing migrations or restoring old rows.

Memory rewind is a product operation, not a database rollback. It closes and reasserts immutable belief versions through a previewed audited transaction. Backups, restore testing, regional recovery, retention, and recovery objectives remain responsibilities of the chosen CockroachDB Cloud plan and operating policy; this repository does not claim those objectives have been established.

## Capacity and limitations

The Terraform defaults deliberately bound concurrency, duration, batch size, retry count, and log retention. CockroachDB capacity and Gemini quotas are external service controls. Set provider budgets and database capacity limits appropriate to the environment, then load-test representative incident and memory workloads before changing concurrency.

No repository evidence establishes maximum requests per second, concurrent tenants, memory corpus size, queue backlog recovery time, availability, or multi-region disaster recovery. Monitor database request units/storage, provider quota and latency, Lambda throttles/duration/errors, queue age/depth/DLQ, changefeed lag, WebSocket errors, and application-level run latency before making production capacity claims.
