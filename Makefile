LOCAL_DATABASE_URL ?= postgresql://root@localhost:26257/hindsight?sslmode=disable
LOCAL_OTEL_ENDPOINT ?= http://localhost:4317
BENCHMARK_MAX_DISTANCE ?= 0.35

.PHONY: dev-up dev-down otel-up otel-down migrate migrate-local test lint lambda-artifacts mcp-server telemetry-demo poison-rewind-demo poison-rewind-demo-local poison-rewind-trace-local cross-episode-demo cross-episode-demo-local cross-episode-trace-local benchmark-smoke benchmark-pilot memory-dashboard memory-dashboard-local product-api-local changefeed-apply changefeed-pause changefeed-status

dev-up:
	docker compose up -d --wait
	docker compose exec -T crdb cockroach sql --insecure -e "SET CLUSTER SETTING kv.rangefeed.enabled = true"
	@echo "CockroachDB ready: sql at localhost:26257, admin ui at http://localhost:8080"

dev-down:
	docker compose --profile otel down

otel-up:
	docker compose --profile otel up -d jaeger
	@echo "Jaeger ready: http://localhost:16686"

otel-down:
	docker compose --profile otel stop jaeger

migrate:
	uv run python scripts/migrate.py

migrate-local:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" uv run python scripts/migrate.py

test:
	uv run pytest -q

lint:
	uv run ruff check .

lambda-artifacts:
	uv run python scripts/build_lambda_artifacts.py

mcp-server:
	uv run python scripts/run_mcp_server.py

telemetry-demo:
	uv run python scripts/run_telemetry_demo.py

poison-rewind-demo:
	uv run python scripts/run_poison_rewind_demo.py all

poison-rewind-demo-local:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" uv run python scripts/run_poison_rewind_demo.py all

poison-rewind-trace-local:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" HINDSIGHT_OTEL_ENABLED=1 OTEL_EXPORTER_OTLP_ENDPOINT="$(LOCAL_OTEL_ENDPOINT)" OTEL_EXPORTER_OTLP_INSECURE=true uv run python scripts/run_poison_rewind_demo.py all

cross-episode-demo:
	uv run python scripts/run_cross_episode_demo.py

cross-episode-demo-local:
	uv run python scripts/run_cross_episode_demo.py --db-url "$(LOCAL_DATABASE_URL)"

cross-episode-trace-local:
	HINDSIGHT_OTEL_ENABLED=1 OTEL_EXPORTER_OTLP_ENDPOINT="$(LOCAL_OTEL_ENDPOINT)" OTEL_EXPORTER_OTLP_INSECURE=true uv run python scripts/run_cross_episode_demo.py --db-url "$(LOCAL_DATABASE_URL)"

benchmark-smoke:
	uv run python scripts/run_learning_benchmark.py ci-smoke

benchmark-pilot:
	HINDSIGHT_BENCHMARK_CODE_SHA="$$(git rev-parse HEAD)" uv run python scripts/run_learning_benchmark.py pilot --max-distance "$(BENCHMARK_MAX_DISTANCE)"

memory-dashboard:
	uv run python scripts/run_memory_dashboard.py

memory-dashboard-local:
	uv run python scripts/run_memory_dashboard.py --db-url "$(LOCAL_DATABASE_URL)"

product-api-local:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" HINDSIGHT_DATABASE_URL_PARAM="" HINDSIGHT_GEMINI_API_KEY_PARAM="" HINDSIGHT_INLINE_WORKER=1 HINDSIGHT_SECURE_COOKIES=0 uv run uvicorn hindsight.api:app --reload --host 127.0.0.1 --port 8766

changefeed-apply:
	uv run python scripts/configure_changefeed.py apply

changefeed-pause:
	uv run python scripts/configure_changefeed.py pause

changefeed-status:
	uv run python scripts/configure_changefeed.py status
