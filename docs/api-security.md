# API and security

The FastAPI schema is available at `/v1/openapi.json` and interactive documentation at `/v1/docs` on a running API. This page describes the security boundary; the generated schema is the field-level contract.

## Public and protected surfaces

The public `/v1` surface is read-only except for exchanging a short-lived realtime ticket. It exposes:

- liveness, readiness, and deployed revision;
- incident and run status;
- current and historical memory snapshots;
- provenance, decision influence, and redacted signature-scenario evidence;
- canonical causal-evidence downloads; and
- `POST /v1/realtime/ticket` for a public, tenant-bound WebSocket ticket.

Public routes cannot create incidents or runs, approve recommendations, preview or execute memory corrections, review generated lesson candidates, reset the controlled scenario, or create an operator session.

Protected `/v2` routes expose the same reads plus `GET /v2/me` and authorized mutations. Candidate list, detail, and review-preview routes require `write`; so do run creation, recommendation decisions, and correction previews. API Gateway verifies the Cognito access token before the request reaches FastAPI. The application then verifies issuer, client ID, token use, expiry, and exactly one supported Cognito group before consulting its opaque principal-to-tenant mapping.

## Browser identity and authorization

The browser uses Cognito Hosted UI authorization code flow with PKCE. The user-pool client has no client secret, self-registration is disabled, and users are provisioned administratively. The PKCE verifier transaction is held briefly in session storage; the returned access token is kept in memory, and refresh material is not retained by the application.

Cognito groups provide `viewer` or `operator`. The database mapping independently provides a role and tenant. Effective permissions are the intersection:

- viewer: `read`, `realtime`;
- operator: `read`, `realtime`, `write`.

A missing mapping, inactive tenant, invalid or conflicting claim, unsupported group set, expired token, or role downgrade fails closed. The database stores a SHA-256 principal locator derived from issuer and subject rather than the Cognito subject itself.

## Tenant isolation

Clients cannot select or override tenant identity on product routes:

- `/v1` binds the fixed public tenant in server middleware;
- `/v2` binds the tenant from the verified principal mapping;
- trusted queue commands carry a validated server-created tenant ID; and
- database connections reject an explicit tenant context that differs from the bound request or command.

Tenant columns participate in keys and relationships, outbox events carry tenant context, and CockroachDB row-level security adds a database fence. Application authorization, restricted database roles, lifecycle guards, and relationship constraints remain necessary; row-level policy is not the complete authorization system.

## Realtime tickets

HTTP authorization is exchanged for a 256-bit WebSocket bearer ticket. Tickets default to a 60-second redemption window, are bound to one tenant and access class, and cannot outlive the authenticated session. DynamoDB stores only the SHA-256 digest and claims, never the bearer value. Connection consumes the ticket with one conditional delete, so replay, expiry, and concurrent redemption fail closed.

The WebSocket connection records tenant, access class, opaque principal ID, and expiry. Subscriptions are tenant-fenced, lifecycle retirement closes access, and events are notifications rather than durable truth; clients recover authoritative state from HTTP.

## Mutation safety

Write routes require the effective `write` permission. The server injects the authenticated actor and tenant rather than accepting either from request data. Run creation and correction execution use idempotency keys bound to canonical request fingerprints.

Rewind, retraction, supersession, review resolution, and generated-candidate review require immutable previews. Execution rechecks preview identity, fingerprint, expiry, namespace revision, authorized scope, selected evidence, and current state in the applying transaction. Model-selected governed-memory remediation remains a proposal until an operator approves the exact action and binding fingerprints.

Generated candidate review has an additional trust boundary. The preview binds candidate, candidate-memory, candidate-payload, and evidence identities to the authenticated actor and requested action. Approval revalidates the semantic-validation receipt and every evidence-manifest row before creating a positive-guidance successor. Rejection records a terminal review without changing the candidate into guidance. A changed, incomplete, legacy-unverifiable, or already reviewed candidate cannot be approved.

## Causal-evidence download

Both public and protected scenario routes return the same canonical, redacted evidence document. The document is scoped to recorded recommendations and includes the correction receipt, declared intervention, before/after recommendation envelopes, controlled-pair checks, and conservative proof states.

The API serializes strict canonical JSON, supplies it as an attachment, and returns its SHA-256 digest in `X-Hindsight-Evidence-SHA256`. The scenario resource publishes the expected digest and download URL. The browser downloads with credentials omitted on `/v1` or with the in-memory access token on `/v2`, hashes the exact response bytes, and saves the file only when the body digest, response header, and scenario receipt agree.

The public projection replaces tenant and namespace values, incident and memory content, prompt and provenance text, and private identifiers before rebuilding the envelopes and their digests. Digest agreement detects a mismatched document; it is not proof of authorship. An incomplete or altered causal envelope makes the corresponding proof state unavailable rather than relaxing the contract.

## Representative requests

Readiness and public evidence do not require credentials:

```bash
curl --fail https://hindsight.strathmoreedu.qzz.io/v1/health/ready
curl --fail https://hindsight.strathmoreedu.qzz.io/v1/signature-scenarios
curl --fail https://hindsight.strathmoreedu.qzz.io/v1/incidents
```

Protected requests should use the browser's Hosted UI flow. For administrative testing, `scripts/cognito_access_token.py` obtains a short-lived access token without writing it to a repository file. Prefer the generated OpenAPI schema for payload definitions.

## Security limitations

The identity boundary is an admin-provisioned Cognito user pool, not a customer identity lifecycle or self-service multitenancy platform. The repository does not claim external security certification, penetration-test coverage, production compliance, multi-region identity recovery, or a general authorization policy engine. The optional WAF profile is not enabled by default. Production adoption requires credential rotation, audit retention, network controls, recovery testing, and threat and risk review appropriate to the environment.
