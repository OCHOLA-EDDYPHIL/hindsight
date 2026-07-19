"""Deployment artifact boundaries."""

import importlib.util
import pathlib
import sys


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_lambda_artifacts",
        pathlib.Path("scripts/build_lambda_artifacts.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smoke():
    spec = importlib.util.spec_from_file_location(
        "smoke_lambda_artifacts",
        pathlib.Path("scripts/smoke_lambda_artifacts.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_realtime_artifact_has_no_third_party_dependency_bundle():
    builder = _builder()

    assert builder.ARTIFACTS["realtime"]["dependencies"] == []
    assert builder.ARTIFACTS["realtime"]["modules"] == [
        "__init__.py",
        "aws.py",
        "queueing.py",
        "realtime.py",
        "realtime_ticket.py",
        "security.py",
        "server_tenants.py",
        "tenant.py",
    ]


def test_api_artifact_does_not_inherit_agent_or_mcp_dependencies():
    builder = _builder()
    api = builder.ARTIFACTS["api"]

    assert "agent.py" not in api["modules"]
    assert "mcp_server.py" not in api["modules"]
    assert not any("langgraph" in dependency for dependency in api["dependencies"])
    assert not any(dependency.startswith("mcp") for dependency in api["dependencies"])
    assert any(dependency.startswith("google-genai") for dependency in api["dependencies"])
    assert "embedding_index.py" in api["modules"]
    assert "operations.py" in api["modules"]
    assert "run_dispatch.py" in api["modules"]
    assert "trace_contract.py" in api["modules"]
    assert {"realtime_ticket.py", "server_tenants.py", "tenant.py"} <= set(api["modules"])


def test_worker_artifact_does_not_include_frontend_or_api_framework():
    builder = _builder()
    worker = builder.ARTIFACTS["worker"]

    assert "web" not in worker["modules"]
    assert "api.py" not in worker["modules"]
    assert not any("fastapi" in dependency for dependency in worker["dependencies"])
    assert {
        "operations.py",
        "consolidation.py",
        "embedding_index.py",
        "queueing.py",
        "run_dispatch.py",
        "server_tenants.py",
        "tenant.py",
    } <= set(worker["modules"])


def test_artifact_smoke_uses_every_terraform_configured_handler():
    smoke = _smoke()

    assert [
        (handler.function, handler.artifact, handler.handler)
        for handler in smoke.configured_handlers()
    ] == [
        ("api", "api", "hindsight.api.handler"),
        ("worker", "worker", "hindsight.worker.handler"),
        ("websocket", "realtime", "hindsight.realtime.websocket_handler"),
        ("changefeed", "realtime", "hindsight.realtime.changefeed_handler"),
    ]


def test_required_database_job_checks_lock_and_built_lambda_handlers():
    workflow = pathlib.Path(".github/workflows/ci.yml").read_text()
    test_job = workflow.split("  test:\n", 1)[1]

    assert "uv lock --check" in test_job
    assert "npm ci" in test_job
    assert "npm run check:web" in test_job
    assert "npm run test:web" in test_job
    assert "npm run build:web" in test_job
    assert "git diff --exit-code -- src/hindsight/web" in test_job
    assert "docker compose up -d crdb" in test_job
    assert "uv run pytest -q" in test_job
    assert "uv run python scripts/build_lambda_artifacts.py" in test_job
    assert "uv run python scripts/smoke_lambda_artifacts.py" in test_job
