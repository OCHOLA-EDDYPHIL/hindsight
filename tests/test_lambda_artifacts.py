"""Deployment artifact boundaries."""

import importlib.util
import pathlib


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


def test_realtime_artifact_has_no_third_party_dependency_bundle():
    builder = _builder()

    assert builder.ARTIFACTS["realtime"]["dependencies"] == []
    assert builder.ARTIFACTS["realtime"]["modules"] == [
        "__init__.py",
        "aws.py",
        "queueing.py",
        "realtime.py",
        "security.py",
    ]


def test_api_artifact_does_not_inherit_agent_or_mcp_dependencies():
    builder = _builder()
    api = builder.ARTIFACTS["api"]

    assert "agent.py" not in api["modules"]
    assert "mcp_server.py" not in api["modules"]
    assert not any("langgraph" in dependency for dependency in api["dependencies"])
    assert not any(dependency.startswith("mcp") for dependency in api["dependencies"])
    assert "operations.py" in api["modules"]


def test_worker_artifact_does_not_include_frontend_or_api_framework():
    builder = _builder()
    worker = builder.ARTIFACTS["worker"]

    assert "web" not in worker["modules"]
    assert "api.py" not in worker["modules"]
    assert not any("fastapi" in dependency for dependency in worker["dependencies"])
    assert {"operations.py", "consolidation.py"} <= set(worker["modules"])
