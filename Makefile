.PHONY: dev-up dev-down migrate test lint lambda-zip mcp-server

dev-up:
	docker compose up -d --wait
	@echo "CockroachDB ready: sql at localhost:26257, admin ui at http://localhost:8080"

dev-down:
	docker compose down

migrate:
	uv run python scripts/migrate.py

test:
	uv run pytest -q

lint:
	uv run ruff check .

lambda-zip:
	uv run python scripts/build_lambda_zip.py

mcp-server:
	uv run python scripts/run_mcp_server.py
