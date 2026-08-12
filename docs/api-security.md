# API and security

The FastAPI schema is available at `/v1/openapi.json` and interactive documentation at `/v1/docs` on a running API. This page describes the security boundary; the generated schema is the field-level contract.

## Public and protected surfaces

The public `/v1` surface is deliberately read-only except for exchanging a short-lived realtime ticket. It exposes:

- liveness, readiness, and deployed revision;
- incident and run status;
- current and historical memory snapshots;
- provenance, decision influence, and signature-scenario evidence; and
- `POST /v1/realtime/ticket` for a public, tenant-bound WebSocket ticket.

Public routes cannot create incidents or runs, approve actions, preview or execute corrections, reset scenarios, or create an operator session.

Protected `/v2` routes expose the same reads plus `GET /v2/me` and authorized mutations. API Gateway verifies the Cognito access token before the request reaches FastAPI. The application then verifies issuer, client ID, token use, expiry, and exactly one supported Cognito group before consulting its opaque principal-to-tenant mapping.

## Browser identity and authorization

The browser uses Cognito Hosted UI authorization code flow with PKCE. The user-pool client has no client secret, self-registration is disabled, and users are provisioned administratively. The PKCE verifier transaction is held briefly in session storage; the returned access token is kept in memory, and refresh material is not retained by the application.

Cognito groups provide `viewer` or `operator`. The database mapping independently provides a role and tenant. Effective permissions are the intersection:

- viewer: `read`, `realtime`;
- operator: `read`, `realtime`, `write`.

A missing mapping, inactive tenant, invalid or conflicting claim, unsupported group set, expired token, or role downgrade fails closed. The database stores a SHA-256 principal locator derived from issuer and subject rather than the Cognito subject itself.

## Tenant isolation

Clients cannot select or override tenant identity on product routes:

- `/v1` binds the fixed public-demo tenant in server middleware;
- `/v2` binds the tenant from the verified principal mapping;
- trusted queue commands carry a validated server-created tenant ID; and
- database connections reject an explicit tenant context that differs from the bound request or command.

Tenant columns participate in keys and relationships, outbox events carry tenant context, and CockroachDB row-level security adds a database fence. Application authorization, restricted database roles, lifecycle guards, and relationship constraints remain necessary; row-level policy is not the complete authorization system.

## Realtime tickets

HTTP authorization is exchanged for a 256-bit WebSocket bearer ticket. Tickets default to a 60-second redemption window, are bound to one tenant and access class, and cannot outlive the authenticated session. DynamoDB stores only the SHA-256 digest and claims, never the bearer value. Connection consumes the ticket with one conditional delete, so replay, expiry, and concurrent redemption fail closed.

The WebSocket connection records tenant, access class, opaque principal ID, and expiry. Subscriptions are tenant-fenced, lifecycle retirement closes access, and events are notifications rather than durable truth; clients recover authoritative state from HTTP.

## Mutation safety

Write routes require the effective `write` permission. The server injects the authenticated actor and tenant rather than accepting either from request data. Run creation and correction execution use idempotency keys bound to canonical request fingerprints.

Rewind, retraction, supersession, and review resolution require an immutable preview. Execution rechecks preview identity, fingerprint, expiry, namespace revision, authorized causal scope, selected evidence, and current state in the applying transaction. Model-selected governed retraction remains a proposal until an operator approves the exact action and binding fingerprint.

## Representative requests

Readiness and public evidence do not require credentials:

```bash
curl --fail https://hindsight.strathmoreedu.qzz.io/v1/health/ready
curl --fail https://hindsight.strathmoreedu.qzz.io/v1/incidents
```

Protected requests should use the browser's Hosted UI flow. For administrative testing, `scripts/cognito_access_token.py` obtains a short-lived access token without writing it to a repository file. Prefer the generated OpenAPI schema for payload definitions.

## Security limitations

The deployed identity boundary is an admin-provisioned Cognito user pool, not a customer identity lifecycle or self-service multitenancy platform. The repository does not claim external security certification, penetration-test coverage, production compliance, multi-region identity recovery, or a general authorization policy engine. The optional WAF profile is not enabled by default. Production adoption requires credential rotation, audit retention, network controls, recovery testing, and threat/risk review appropriate to the environment.
