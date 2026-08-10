"""Deployment artifact boundaries."""

import importlib.util
import pathlib
import sys
import zipfile

import pytest


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


def test_realtime_artifact_bundles_only_required_tracing_dependencies():
    builder = _builder()

    assert builder.ARTIFACTS["realtime"]["dependencies"] == [
        "opentelemetry-api>=1.43.0",
        "opentelemetry-exporter-otlp-proto-grpc>=1.43.0",
        "opentelemetry-sdk>=1.43.0",
    ]
    assert builder.ARTIFACTS["realtime"]["modules"] == [
        "__init__.py",
        "aws.py",
        "observability.py",
        "queueing.py",
        "realtime.py",
        "realtime_ticket.py",
        "security.py",
        "server_tenants.py",
        "tenant.py",
        "tracing.py",
    ]


def test_api_artifact_does_not_inherit_agent_or_mcp_dependencies():
    builder = _builder()
    api = builder.ARTIFACTS["api"]

    assert "agent.py" not in api["modules"]
    assert "dashboard.py" not in api["modules"]
    assert "mcp_server.py" not in api["modules"]
    assert not any("langgraph" in dependency for dependency in api["dependencies"])
    assert not any(dependency.startswith("mcp") for dependency in api["dependencies"])
    assert any(dependency.startswith("google-genai") for dependency in api["dependencies"])
    assert "embedding_index.py" in api["modules"]
    assert "operations.py" in api["modules"]
    assert "prompt_safety.py" in api["modules"]
    assert "run_dispatch.py" in api["modules"]
    assert "snapshots.py" in api["modules"]
    assert "trace_contract.py" in api["modules"]
    assert {"realtime_ticket.py", "server_tenants.py", "tenant.py"} <= set(api["modules"])
    assert {"lifecycle.py", "lifecycle_aws.py"}.isdisjoint(api["modules"])


def test_worker_artifact_does_not_include_frontend_or_api_framework():
    builder = _builder()
    worker = builder.ARTIFACTS["worker"]

    assert "web" not in worker["modules"]
    assert "api.py" not in worker["modules"]
    assert not any("fastapi" in dependency for dependency in worker["dependencies"])
    assert {
        "operations.py",
        "prompt_safety.py",
        "consolidation.py",
        "embedding_index.py",
        "queueing.py",
        "run_dispatch.py",
        "server_tenants.py",
        "tenant.py",
    } <= set(worker["modules"])
    assert {"lifecycle.py", "lifecycle_aws.py"}.isdisjoint(worker["modules"])


def test_worker_uses_managed_runtime_aws_sdk_instead_of_bundling_a_second_copy():
    builder = _builder()

    assert not any(
        dependency.startswith(("boto3", "botocore"))
        for dependency in builder.ARTIFACTS["worker"]["dependencies"]
    )


def test_lambda_size_budget_includes_confirmed_adot_layer():
    builder = _builder()

    assert builder.AWS_LAMBDA_UNZIPPED_LIMIT_BYTES == 262_144_000
    assert builder.ADOT_PYTHON_LAYER_UNZIPPED_BYTES == 53_504_816
    assert (
        builder.AWS_LAMBDA_UNZIPPED_LIMIT_BYTES
        - builder.ADOT_PYTHON_LAYER_UNZIPPED_BYTES
        == 208_639_184
    )


def test_lambda_size_guard_rejects_combined_artifact_and_layer_over_limit(
    tmp_path, monkeypatch
):
    builder = _builder()
    artifact = tmp_path / "worker.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("payload", b"1234567")
    monkeypatch.setattr(builder, "AWS_LAMBDA_UNZIPPED_LIMIT_BYTES", 10)
    monkeypatch.setattr(builder, "ADOT_PYTHON_LAYER_UNZIPPED_BYTES", 4)

    with pytest.raises(RuntimeError, match="extract to 11 bytes; limit is 10 bytes"):
        builder.validate_unzipped_size("worker", artifact)


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


def test_required_ci_status_aggregates_lock_web_database_and_lambda_checks():
    workflow = pathlib.Path(".github/workflows/ci.yml").read_text()
    static_job = workflow.split("  python_static:\n", 1)[1].split("  database:\n", 1)[0]
    database_job = workflow.split("  database:\n", 1)[1].split(
        "  main_qualification:\n", 1
    )[0]
    frontend_job = workflow.split("  frontend:\n", 1)[1].split(
        "  lambda_artifacts:\n", 1
    )[0]
    lambda_job = workflow.split("  lambda_artifacts:\n", 1)[1].split(
        "  terraform:\n", 1
    )[0]
    aggregate_job = workflow.split("  test:\n", 1)[1]

    assert "uv lock --check" in static_job
    assert "scripts/ci_test_groups.py run unit" in static_job
    assert "docker compose up -d crdb" in database_job
    assert "scripts/ci_test_groups.py run product" in database_job
    for command in (
        "npm ci",
        "npm run check:web",
        "npm run test:web",
        "npm run build:web",
        "git diff --exit-code -- src/hindsight/web",
    ):
        assert command in frontend_job
    assert "uv run python scripts/build_lambda_artifacts.py" in lambda_job
    assert "uv run python scripts/smoke_lambda_artifacts.py" in lambda_job
    for dependency in ("python_static", "database", "frontend", "lambda_artifacts"):
        assert f"      - {dependency}" in aggregate_job
    assert "python scripts/verify_ci_components.py" in aggregate_job
