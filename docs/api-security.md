# API and security

The FastAPI schema is available at `/v1/openapi.json` and interactive documentation at `/v1/docs` on a running API. This page describes the security boundary; the generated schema is the field-level contract.

## Surfaces

The public product surface is `/v1`:

- health and deployed revision;
- incident listing and detail;
- run status, memory snapshots, provenance, influence, and signature-scenario inspection;
- short-lived realtime tickets; and
- operator-authorized incident, run, correction, and demo mutations.

Protected `/v2` routes expose bearer-authenticated readiness, realtime tickets, and paginated incident reads/writes for a separate server-owned tenant. This credential model is currently a bounded protected integration surface, not a general customer identity or self-service multitenancy system.

## Authentication and browser sessions

Mutating `/v1` routes require the operator secret as `Authorization: Bearer <token>` or a signed operator session cookie. Login exchanges the token for an HTTP-only, same-site-strict cookie with a four-hour lifetime. Hosted cookies are secure. Cookie-authenticated mutations reject unapproved cross-origin requests; CORS origins must be explicit normalized HTTP(S) origins.

The secret is supplied directly only for local development. Hosted Lambdas resolve parameter names to SSM SecureString values and do not receive secret values through Terraform state. Use distinct restricted CockroachDB connection strings for API, worker, and deployment identities as described in [Database roles](../infra/db/README.md).

Do not expose the operator token in URLs, browser configuration, logs, screenshots, or committed files.

## Tenant isolation

Clients cannot select or override tenant identity on product routes:

- `/v1` binds the fixed public-demo tenant in middleware;
- authenticated `/v2` binds the fixed protected tenant represented by that credential;
- worker messages carry a validated tenant identity created by trusted server code; and
- the current context rejects an explicit database tenant that differs from the bound tenant.

Tenant columns are part of natural keys and relationships, outbox events carry tenant context, and CockroachDB row-level security adds a database boundary. Application checks and database roles remain necessary: row-level policy alone is not the complete authorization system.

## Mutation safety

Run creation and correction execution support idempotency. Rewind, retraction, supersession, and review resolution require an immutable preview; execution verifies the preview fingerprint and current authorization/state before applying it. Cross-namespace causal correction requires an explicit authorized namespace set and fails closed when lineage or authority is incomplete.

Realtime tickets expire after 60 seconds and are signed for one tenant. WebSocket events are notifications, not authorization grants or durable truth.

## Representative requests

Readiness and public incident reads do not require an operator token:

```bash
curl --fail http://127.0.0.1:8766/v1/health/ready
curl --fail http://127.0.0.1:8766/v1/incidents
```

For a local operator mutation, keep the token in an environment variable and send it in a header:

```bash
curl --fail --request POST http://127.0.0.1:8766/v1/operator/session \
  --header 'Content-Type: application/json' \
  --data "{\"token\":\"${HINDSIGHT_FUNCTION_AUTH_TOKEN}\"}"
```

Prefer the generated OpenAPI schema for payload definitions. Correction execution also requires an `Idempotency-Key` header and the `preview_id` and `fingerprint` returned by its preview route.

## Security limitations

The demo operator secret is a shared administrative credential, not per-user authentication. `/v2` currently maps one protected credential to one fixed tenant and fixed scopes. The repository does not claim a customer identity lifecycle, fine-grained human roles, external security certification, penetration-test coverage, or production compliance. Production adoption requires an identity provider, credential rotation policy, audit retention policy, network controls, and threat/risk review appropriate to the deployment.
