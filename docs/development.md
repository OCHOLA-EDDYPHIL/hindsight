# Development

## Prerequisites

- Python 3.12 and `uv`
- Node.js 22 and npm
- Docker with Compose
- Git
- a Gemini API key for product execution
- AWS credentials only when exercising the optional CloudWatch-backed local acceptance path

Install from a clean checkout:

```bash
cp .env.example .env
uv sync --frozen
npm ci
make dev-up
make migrate-local
```

`make dev-up` starts CockroachDB 25.4.5 on ports 26257 and 8080 and enables rangefeeds and vector indexes. The server is insecure and suitable only for loopback development.

## Production-aligned local loop

Gemini is the reasoning and embedding provider for product entrypoints. Tests inject fakes directly; there is no fake provider mode in the product runtime.

Set the local database and the fixed public-demo tenant, clear deployed SSM parameter locators, and load the Gemini key through your preferred local secret mechanism:

```bash
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

The FastAPI-served build is available at <http://127.0.0.1:8766>. It exercises the public read-only tenant boundary. Protected `/v2` routes intentionally reject requests that do not carry API Gateway-verified Cognito claims.

For component work, run Vite separately:

```bash
npm exec vite -- --config frontend/vite.config.ts
```

Rebuild the FastAPI-served assets after frontend changes with `npm run build:web`.

## Full local product acceptance

`scripts/run_live_acceptance.py local-product-full` creates separate fresh databases for provider/semantic, resilience, and browser stages. The runner binds semantic and resilience work to the fixed acceptance tenant and browser work to the fixed public-demo tenant, overriding inherited `PGOPTIONS`.

The shared browser contract requires the same controlled CloudWatch observations used by the deployed flow. With an authenticated AWS profile that may write and read the controlled metric namespace:

```bash
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export HINDSIGHT_AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export HINDSIGHT_STAGE=local

uv run python scripts/publish_controlled_incident_telemetry.py \
  --stage "$HINDSIGHT_STAGE" \
  --region "$AWS_REGION" \
  --checkout-latency-ms 842.5 \
  --retry-fanout 8 \
  --processor-queue-depth 217 \
  --confirm-controlled-fixture

uv run python scripts/run_live_acceptance.py local-product-full \
  --database-url "postgresql://root@localhost:26257/hindsight?sslmode=disable"
```

The telemetry publisher writes only the explicitly labeled `Hindsight/ControlledIncidentTelemetry` fixture. The agent's diagnostic tool remains read-only, limited to configured queries, at most three calls, and a 15-minute window.

## Verification before pushing

GitHub Actions is a verifier, not the debugging environment. Reproduce and correct behavioral failures locally, then run the applicable checks before pushing:

```bash
uv lock --check
uv run ruff check .
uv run python scripts/ci_test_groups.py validate
uv run python scripts/ci_test_groups.py run unit
npm run check:web
npm run test:web
npm run build:web
git diff --exit-code -- src/hindsight/web
```

Database changes also require a fresh CockroachDB, forward migrations, agent storage initialization, affected product tests, populated-upgrade verification, and fresh/populated schema comparison. `scripts/run_affected_ci.py` and `.github/workflows/ci.yml` are the executable references for affected selection and exact commands.

Validate workflow and acceptance CLI contracts locally when changing automation:

```bash
uv run pytest -q tests/test_ci_contracts.py tests/test_live_acceptance_cli.py
```

After local success, push one coherent revision and let normal PR CI confirm it. Use hosted acceptance only for the integrated deployed boundary—AWS permissions, SSM resolution, Lambda, Cognito/API Gateway, SQS, CloudWatch, changefeeds, WebSockets, exact revision identity, and the deployed origin.

## Provider configuration

The supported product values are `LLM_PROVIDER=gemini` and `EMBEDDING_PROVIDER=gemini`. Model names and the raw-control embedding representation are documented in `.env.example`. A local process may receive `GEMINI_API_KEY` or versioned key-pool material directly; deployed functions receive only SSM parameter names and resolve key material at runtime.

Never commit `.env`, API keys, database URLs, access tokens, or generated provider payloads.

## Fresh data and shutdown

`make dev-down` preserves the named `crdb-data` volume. For migration or isolation testing, use a unique Compose project or fresh derived database as CI and the local acceptance runner do. Inspect the exact target before deleting a database, container, or volume; avoid broad Docker cleanup commands.
