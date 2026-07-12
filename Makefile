LOCAL_DATABASE_URL ?= postgresql://root@localhost:26257/hindsight?sslmode=disable

.PHONY: dev-up dev-down migrate migrate-local test lint lambda-zip mcp-server telemetry-demo poison-rewind-demo poison-rewind-demo-local memory-dashboard memory-dashboard-local

dev-up:
	docker compose up -d --wait
	docker compose exec -T crdb cockroach sql --insecure -e "SET CLUSTER SETTING kv.rangefeed.enabled = true"
	@echo "CockroachDB ready: sql at localhost:26257, admin ui at http://localhost:8080"

dev-down:
	docker compose down

migrate:
	uv run python scripts/migrate.py

migrate-local:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" uv run python scripts/migrate.py

test:
	uv run pytest -q

lint:
	uv run ruff check .

lambda-zip:
	uv run python scripts/build_lambda_zip.py

mcp-server:
	uv run python scripts/run_mcp_server.py

telemetry-demo:
	uv run python scripts/run_telemetry_demo.py

poison-rewind-demo:
	uv run python scripts/run_poison_rewind_demo.py all

poison-rewind-demo-local:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" uv run python scripts/run_poison_rewind_demo.py all

memory-dashboard:
	uv run python scripts/run_memory_dashboard.py

memory-dashboard-local:
	uv run python scripts/run_memory_dashboard.py --db-url "$(LOCAL_DATABASE_URL)"
