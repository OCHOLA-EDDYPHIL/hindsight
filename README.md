# Hindsight

**An incident agent's memory can be confidently wrong.** A stale runbook lesson gets recalled first during the next outage and steers the agent toward the same bad recommendation. Hindsight lets an operator answer three questions before acting: *which memory shaped this recommendation, how do I correct it without destroying history, and did the correction actually change what the agent recommended?*

Hindsight is a governed-memory incident-response cockpit built on CockroachDB and AWS. Every recalled belief is versioned, its influence on a recommendation is recorded, and every correction preserves the history that explains why the recommendation changed.

- **Live cockpit (no login):** <https://hindsight.strathmoreedu.qzz.io>
- **Walkthrough video:** <https://vimeo.com/1218738316>
- **Service readiness:** <https://hindsight.strathmoreedu.qzz.io/v1/health/ready>
- **Persisted scenario (raw):** <https://hindsight.strathmoreedu.qzz.io/v1/signature-scenarios>

## The loop, in one scenario

The public cockpit replays one persisted payments incident. It holds the incident input and the normalized CloudWatch observations **constant**, and allows exactly one intervention — a governed change to memory:

1. **A trusted belief exists**, with a stale successor version recorded above it. History is retained; nothing is silently replaced.
2. **The incident runs.** CockroachDB Distributed Vector Indexing ranks the *stale* version first among tenant-scoped memory.
3. **The agent records a recommendation.** Reasoning cites that exact memory version and produces `scale_workers`.
4. **The operator rejects it.** No infrastructure action runs — Hindsight waits for human control.
5. **A governed correction is previewed and applied.** CockroachDB invalidates the stale version while keeping it in history, and reasserts the trusted belief.
6. **The same incident replays.** The trusted version now ranks first, and the recorded recommendation changes to `throttle_retries`.

The before/after comparison **fails closed**: Hindsight shows "recorded recommendation changed after correction" *only* when both structured recommendation fingerprints are valid, the incident input and observations match, the governed-memory intervention is canonical, and durable correction lineage connects the two runs. It never infers the result from recommendation prose.

## Why CockroachDB is the point

CockroachDB is the system of record, not a cache behind the model. Bi-temporal memory versions, vector embeddings, provenance, run events, correction operations, and dispatch identities share **one transactional store**. A single serializable transaction binds the selected memory read to the incident run, the decision, the operator verdict, the governed correction, the temporal lineage, and the transactional outbox.

That co-location is the design bet: provenance is trivial when the versioned belief, the run that used it, the model recommendation, the human decision, and the correction that followed all live in the same database — instead of being stitched across a separate vector store.

> CockroachDB's own historical reads expose what was persisted at a past cutoff. A Hindsight *rewind* is different: a separate governed write that creates new versions while retaining the old ones.

## Required-tool integration

**CockroachDB (two tools):**
- **Distributed Vector Indexing — on the runtime path.** A tenant-scoped cosine vector index ranks semantic memory for every incident run, and the selected version is recorded with the decision. Vector-plan evidence comes from DVI qualification (`EXPLAIN` over the tenant-leading access path).
- **Cloud Managed MCP Server — development-side inspection.** An earlier audit client used `get-table-schema` and `select-query` under read-only OAuth to reconstruct persisted decision, retrieval, memory, rewind, lineage, and embedding-profile identities. This is a development inspection surface, **not** the application's runtime connection.
- *Supplementary:* a deterministic privilege audit informed by pinned CockroachDB Skill SQL, executed through a restricted auditor role — separate from Managed MCP.

**AWS (application and durable execution plane):** Lambda runs the API, worker, and realtime/changefeed components behind API Gateway and CloudFront. SQS carries durable agent commands and EventBridge reclaims expired work. DynamoDB holds fenced realtime connection state. CloudWatch supplies allow-listed, time-bounded diagnostic observations. Cognito guards operator controls while the replay stays publicly inspectable. SSM stores runtime configuration; S3 retains bounded evidence. OpenTelemetry is exported through AWS ADOT to X-Ray with bounded sampling, correlating API, dispatch, worker, and memory spans.

**Reasoning:** schema-constrained Gemini reasoning. The model can request one server-owned diagnostic or return one bounded terminal recommendation — it cannot execute infrastructure changes. Gemini embeddings produce 1,024-dimensional vectors, partitioned by tenant, namespace, provider, model, and representation profile.

## Scope and boundaries

Hindsight records recommendations and governed-memory operations. It deliberately does **not**:

- execute infrastructure remediation, or claim either recommendation was run;
- assert that the service recovered;
- claim a repeatable causal effect across trials, or that it detects every poisoned memory;
- claim production-scale capacity, multi-region resilience, or customer adoption.

This is one controlled scenario that proves a bounded causal chain — memory correction, a recorded recommendation delta, and controlled-pair eligibility under equal inputs — not a general learning result. A direct diagnostic exercised 75,000 vectors across 15 tenants; a larger 100,000-vector, 20-tenant target was not established within its bounded attempt.

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
