# Development

## Prerequisites

- Python 3.12 and `uv`
- Node.js 22 and npm
- Docker with Compose
- Git

Start from a clean checkout:

```bash
cp .env.example .env
uv sync --frozen
npm ci
make dev-up
make migrate-local
DATABASE_URL="postgresql://root@localhost:26257/hindsight?sslmode=disable" \
  EMBEDDING_PROVIDER=deterministic LLM_PROVIDER=deterministic \
  uv run python scripts/initialize_agent_storage.py
```

`make dev-up` starts CockroachDB 25.2 on ports 26257 and 8080 and enables rangefeeds and vector indexes. The Compose server is insecure and suitable only for local development.

## Deterministic product loop

Keep local debugging independent from cloud credentials:

```bash
export DATABASE_URL="postgresql://root@localhost:26257/hindsight?sslmode=disable"
export EMBEDDING_PROVIDER=deterministic
export LLM_PROVIDER=deterministic
export HINDSIGHT_DATABASE_URL_PARAM=""
export HINDSIGHT_GEMINI_API_KEY_PARAM=""
export HINDSIGHT_GEMINI_API_KEYS_PARAM=""
export HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM=""
export HINDSIGHT_FUNCTION_AUTH_TOKEN=local-operator-token
export HINDSIGHT_INLINE_WORKER=1
export HINDSIGHT_SECURE_COOKIES=0
npm run build:web
uv run uvicorn hindsight.api:app --reload --host 127.0.0.1 --port 8766
```

The built UI is served at <http://127.0.0.1:8766>. Re-run `npm run build:web` after frontend changes, or use Vite’s development server separately:

```bash
npm exec vite -- --config frontend/vite.config.ts
```

The standalone Vite server is useful for component work, but full product behavior should be checked against the FastAPI-served build.

Run the complete deterministic correction scenario:

```bash
make poison-rewind-demo-local
```

It creates a clean session, records a good belief, injects a poisoned belief, demonstrates its influence, previews and applies a rewind, and verifies the corrected result.

## Verification before pushing

Use hosted Actions to validate cloud-only behavior, not as the primary debugging loop. Before a behavioral change is pushed, run the applicable local checks:

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

Database changes also require a fresh CockroachDB, migrations, agent storage initialization, affected product tests, populated-upgrade verification, and fresh/populated schema comparison. `scripts/run_affected_ci.py` and `.github/workflows/ci.yml` are the executable references for the affected test selection and exact commands.

Validate workflow and CLI contracts locally when changing automation:

```bash
uv run pytest -q tests/test_ci_contracts.py tests/test_live_acceptance_cli.py
```

After local success, push one coherent revision and allow normal PR CI to run once. A targeted hosted browser run is appropriate only for AWS permissions, SSM, Lambda, WebSocket, Gemini, changefeed, revision-identity, or deployed-origin behavior that local execution cannot prove. Run the complete exact-main hosted acceptance once after merge when the change affects product behavior.

## Live providers

Local development defaults should remain deterministic. To opt into Gemini, set `LLM_PROVIDER=gemini`, `EMBEDDING_PROVIDER=gemini`, the model names shown in `.env.example`, and a local `GEMINI_API_KEY` (or versioned key-pool material). Never commit `.env` or credentials.

The code also contains Bedrock reasoning and embedding providers for explicitly configured local/runtime use. The current hosted application Terraform intentionally accepts Gemini as its embedding provider; do not infer hosted Bedrock deployment support from the provider classes alone.

## Fresh data and shutdown

`make dev-down` preserves the named `crdb-data` volume. For migration or isolation testing, use a unique Compose project/database as CI does, and delete only that exact project’s volumes after inspecting the target. Avoid broad Docker cleanup commands.
