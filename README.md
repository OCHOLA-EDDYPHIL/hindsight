# Hindsight

Hindsight is an incident-response copilot with inspectable, rewindable agent memory. It records what an agent read, what it decided, and which memory versions influenced that decision so an operator can diagnose a bad recommendation, preview its impact, and apply an audited correction without rewriting history.

The product includes:

- a React incident cockpit and operator console;
- a FastAPI HTTP API and WebSocket updates;
- CockroachDB-backed incidents, runs, checkpoints, bitemporal memory, provenance, correction operations, and transactional dispatch;
- bounded asynchronous execution through AWS Lambda and SQS; and
- deterministic local providers for repeatable development without cloud model calls.

See [Architecture](docs/architecture.md), [API and security](docs/api-security.md), and [Operations](docs/operations.md) for the verified boundaries behind these capabilities.

## Run locally

Prerequisites: Git, Docker with Compose, Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 22 with npm.

```bash
git clone https://github.com/OCHOLA-EDDYPHIL/hindsight.git
cd hindsight
cp .env.example .env
uv sync --frozen
npm ci
make dev-up
make migrate-local
DATABASE_URL="postgresql://root@localhost:26257/hindsight?sslmode=disable" \
  EMBEDDING_PROVIDER=deterministic LLM_PROVIDER=deterministic \
  uv run python scripts/initialize_agent_storage.py
npm run build:web
LLM_PROVIDER=deterministic EMBEDDING_PROVIDER=deterministic \
  HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM="" \
  HINDSIGHT_FUNCTION_AUTH_TOKEN=local-operator-token \
  make product-api-local
```

Open <http://127.0.0.1:8766>. The local API serves the compiled UI and exposes interactive API documentation at <http://127.0.0.1:8766/v1/docs>. Use `local-operator-token` only in this local process when the UI asks to unlock operator actions.

In another terminal, verify the deterministic poison/rewind lifecycle:

```bash
make poison-rewind-demo-local
```

The local database is intentionally insecure and bound to localhost; it is for development only. `make dev-down` stops the services but preserves the database volume. Use an explicitly scoped Compose project or remove that volume yourself when a genuinely fresh database is required.

For provider configuration, frontend development, tests, and fresh-database workflows, see [Development](docs/development.md).

## What is proven—and what is not

Repository tests and owner-authorized hosted acceptance cover schema upgrades, tenant isolation, transactional dispatch, bounded retries and DLQ behavior, semantic retrieval, the poison/rewind browser flow, deployed revision identity, AWS permissions, runtime credentials, changefeeds, WebSockets, and the deployed origin.

This is a bounded reference deployment, not a production-capacity claim. Lambda concurrency, queue visibility, retry counts, database service limits, provider quotas, and alarms are configured and tested at demo scale. The project does not publish load-test results, an availability objective, a disaster-recovery objective, or evidence that these defaults support arbitrary tenant count or traffic. Capacity must be measured and configured for each production workload.

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
