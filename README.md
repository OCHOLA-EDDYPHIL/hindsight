# Hindsight

Hindsight is an incident-response copilot with inspectable, rewindable agent memory. It records what an agent recalled, what Gemini observed and decided, and which memory versions influenced the decision. An operator can then approve a governed correction without deleting history and replay the incident against the corrected belief state.

The product includes:

- a React incident cockpit with public evidence and protected operator controls;
- FastAPI HTTP APIs, Cognito authorization-code sign-in with PKCE, and tenant-bound WebSocket updates;
- CockroachDB-backed incidents, runs, checkpoints, vector memory, provenance, immutable history, correction operations, and transactional dispatch;
- schema-constrained Gemini reasoning and embeddings with bounded read-only CloudWatch diagnostics; and
- durable asynchronous execution through AWS Lambda, SQS, EventBridge, DynamoDB, S3, SSM, API Gateway, and CloudFront.

The deployed read-only demo is available at <https://hindsight.strathmoreedu.qzz.io>.

See [Architecture](docs/architecture.md), [API and security](docs/api-security.md), and [Operations](docs/operations.md) for the boundaries behind these capabilities.

## Public walkthrough

The public cockpit opens to a credential-free, persisted payments replay. Follow the cited stale memory into the rejected `scale_workers` recommendation, the governed rewind that preserves the historical version, and the approved `throttle_retries` recommendation produced from the same incident input and normalized CloudWatch observations.

Hindsight labels the result as a recorded action change only when the structured actions validate and differ, the controlled inputs match, the correction is proven, and the invalidated memory is absent from the later run. Otherwise it shows only the narrower supported result. Operator mutations require the protected Cognito operator role.

- [Open the cockpit](https://hindsight.strathmoreedu.qzz.io)
- [Check readiness and deployed revision](https://hindsight.strathmoreedu.qzz.io/v1/health/ready)
- [Inspect the persisted scenario evidence](https://hindsight.strathmoreedu.qzz.io/v1/signature-scenarios)

## Run locally

Prerequisites: Git, Docker with Compose, Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 22 with npm, and a Gemini API key supplied through the local process environment.

```bash
git clone https://github.com/OCHOLA-EDDYPHIL/hindsight.git
cd hindsight
cp .env.example .env
uv sync --frozen
npm ci
make dev-up
make migrate-local

export DATABASE_URL="postgresql://root@localhost:26257/hindsight?sslmode=disable"
export PGOPTIONS="-c hindsight.tenant_id=00000000-0000-0000-0000-000000000002"
export LLM_PROVIDER=gemini
export EMBEDDING_PROVIDER=gemini
export HINDSIGHT_DATABASE_URL_PARAM=""
export HINDSIGHT_GEMINI_API_KEY_PARAM=""
export HINDSIGHT_GEMINI_API_KEYS_PARAM=""
# Export GEMINI_API_KEY from your local secret manager here.

uv run python scripts/initialize_agent_storage.py
uv run python scripts/reembed_memories.py --max-distance 0.35
npm run build:web
make product-api-local
```

Open <http://127.0.0.1:8766>. The local API serves the compiled UI and exposes interactive public API documentation at <http://127.0.0.1:8766/v1/docs>. This direct server exposes the read-only `/v1` experience; the protected `/v2` browser flow depends on a verified Gateway/Cognito identity.

The Compose database is CockroachDB 25.4.5, bound insecurely to localhost for development only. `make dev-down` stops services but preserves the named database volume.

For the production-aligned local acceptance runner, controlled CloudWatch fixture, frontend workflow, tests, and fresh-database guidance, see [Development](docs/development.md).

## Verified boundaries

The repository tests and exact-revision acceptance cover schema upgrades, server-bound tenant isolation, Cognito roles, one-time realtime tickets, transactional run dispatch, worker leasing and recovery, bounded Gemini/CloudWatch decisions, semantic retrieval, governed correction, historical replay, database roles, changefeeds, WebSockets, and deployed revision identity.

These checks establish a bounded reference deployment, not an availability or production-capacity guarantee. A resource diagnostic has exercised 75,000 synthetic vectors across 15 tenants; it is not capacity qualification. The larger 100,000-vector, 20-tenant qualification target has not been established. Provider quotas, CockroachDB sizing, concurrency, queue recovery, regional resilience, and operating objectives must be measured for each deployment.

## Documentation

- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [API and security](docs/api-security.md)
- [Operations](docs/operations.md)
- [Governed memory correction decision](docs/adr/0001-governed-memory-correction.md)
- [Database roles](infra/db/README.md)
- [Application infrastructure](infra/terraform/app/README.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).
