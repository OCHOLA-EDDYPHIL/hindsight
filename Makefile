LOCAL_DATABASE_URL ?= postgresql://root@localhost:26257/hindsight?sslmode=disable
LOCAL_AWS_ENDPOINT ?= http://127.0.0.1:4566
.PHONY: dev-up dev-down otel-up otel-down aws-up aws-down aws-queue-smoke migrate migrate-local test lint lambda-artifacts product-api-local changefeed-apply changefeed-pause changefeed-status

dev-up:
	docker compose up -d --wait
	docker compose exec -T crdb cockroach sql --insecure -e "SET CLUSTER SETTING kv.rangefeed.enabled = true"
	docker compose exec -T crdb cockroach sql --insecure -e "SET CLUSTER SETTING feature.vector_index.enabled = true"
	@echo "CockroachDB ready: sql at localhost:26257, admin ui at http://localhost:8080"

dev-down:
	docker compose --profile otel down

otel-up:
	docker compose --profile otel up -d jaeger
	@echo "Jaeger ready: http://localhost:16686"

otel-down:
	docker compose --profile otel stop jaeger

aws-up:
	docker compose --profile aws up -d --wait localstack

aws-down:
	docker compose --profile aws stop localstack

aws-queue-smoke:
	HINDSIGHT_AWS_ENDPOINT_URL="$(LOCAL_AWS_ENDPOINT)" uv run python scripts/run_local_sqs_smoke.py

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

product-api-local:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" HINDSIGHT_DATABASE_URL_PARAM="" HINDSIGHT_GEMINI_API_KEY_PARAM="" HINDSIGHT_GEMINI_API_KEYS_PARAM="" HINDSIGHT_INLINE_WORKER=1 HINDSIGHT_SECURE_COOKIES=0 uv run uvicorn hindsight.api:app --reload --host 127.0.0.1 --port 8766

changefeed-apply:
	uv run python scripts/configure_changefeed.py apply

changefeed-pause:
	uv run python scripts/configure_changefeed.py pause

changefeed-status:
	uv run python scripts/configure_changefeed.py status
