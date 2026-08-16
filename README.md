# Hindsight

Stale runbooks teach incident agents the wrong lesson. Hindsight gives those agents governed memory: every recalled belief is versioned, recorded influence is traceable, and every correction preserves the history that explains why a recommendation changed.

## Rewind what your incident agent learned

When guidance goes bad, Hindsight does not erase the audit trail. An operator can inspect the exact memory versions an agent used, preview a correction, approve a new current belief state, and compare a later recorded recommendation against the earlier one under a controlled evidence contract.

CockroachDB is the product's system of record, not a cache behind the model. Bi-temporal memory versions, vector embeddings, provenance, run events, correction operations, and dispatch identities share one transactional store. CockroachDB historical reads expose what was persisted at a past cutoff; a Hindsight rewind is a separate governed write that creates new versions while retaining the old ones.

The product includes:

- a React incident cockpit with a public, redacted walkthrough and protected operator controls;
- FastAPI HTTP APIs, Cognito authorization-code sign-in with PKCE, and tenant-bound WebSocket notifications;
- CockroachDB-backed incidents, runs, checkpoints, vector memory, provenance, immutable memory history, and transactional dispatch;
- schema-constrained Gemini reasoning and bounded, read-only CloudWatch diagnostics;
- generated lesson candidates that remain audit-only until an authenticated, fingerprint-bound review approves a successor; and
- AWS Lambda, SQS, EventBridge, DynamoDB, S3, SSM, API Gateway, and CloudFront deployment components.

The public cockpit is available at <https://hindsight.strathmoreedu.qzz.io>.

## Public walkthrough

The credential-free cockpit presents a persisted payments scenario. It shows the stale guidance selected before one rejected recommendation, the governed rewind that retains that guidance as history, and a later recommendation recorded from the corrected memory selection.

The comparison is deliberately narrow. Hindsight reports a controlled recommendation change only when the structured action fingerprints differ, the invariant inputs match, and the declared memory intervention is bound to the completed correction. It does not claim that either recommendation was executed, that the service recovered, or that one comparison establishes a repeatable causal effect.

- [Open the cockpit](https://hindsight.strathmoreedu.qzz.io)
- [Inspect service readiness](https://hindsight.strathmoreedu.qzz.io/v1/health/ready)
- [Inspect the persisted scenario](https://hindsight.strathmoreedu.qzz.io/v1/signature-scenarios)

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

For the local development workflow, controlled CloudWatch fixture, frontend checks, and fresh-database guidance, see [Development](docs/development.md).

## Product boundaries

Hindsight records recommendations and governed-memory operations; it does not execute infrastructure remediation or assert service recovery. Historical inspection is read-only, while product rewind changes only the governed memory state through new audited versions. Generated lessons cannot enter positive-guidance retrieval until approved. Terminal run redrive creates an idempotent fresh run from the persisted source inputs rather than resuming or rewriting the failed run.

Capacity, availability, provider quotas, CockroachDB sizing, concurrency, backlog time, regional resilience, and recovery objectives remain deployment-specific operating concerns.

## Documentation

- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [API and security](docs/api-security.md)
- [Operations](docs/operations.md)
- [Governed memory correction decision](docs/adr/0001-governed-memory-correction.md)
- [Generated machine-memory admission decision](docs/adr/0002-generated-machine-memory-admission.md)
- [Controlled causal-evidence decision](docs/adr/0003-controlled-causal-evidence.md)
- [Database roles](infra/db/README.md)
- [Application infrastructure](infra/terraform/app/README.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).
