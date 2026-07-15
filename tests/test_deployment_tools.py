"""Deployment prerequisite tooling."""

import importlib.util
import pathlib
import sys

import pytest


def _configure_module():
    path = pathlib.Path("scripts/configure_demo_secrets.py")
    spec = importlib.util.spec_from_file_location("configure_demo_secrets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _changefeed_module():
    path = pathlib.Path("scripts/configure_changefeed.py")
    spec = importlib.util.spec_from_file_location("configure_changefeed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configure_demo_secrets_dry_run_never_prints_values(monkeypatch, capsys):
    configure = _configure_module()

    class FakeSsm:
        def get_parameter(self, **kwargs):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "missing"}},
                "GetParameter",
            )

    class FakeSession:
        def client(self, *args, **kwargs):
            return FakeSsm()

    monkeypatch.setattr(configure.boto3, "Session", lambda **kwargs: FakeSession())
    monkeypatch.setattr(configure, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-database")
    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "secret-operator")
    monkeypatch.setenv("HINDSIGHT_CHANGEFEED_AUTH_TOKEN", "secret-changefeed")
    for index in range(5):
        name = "GEMINI_API_KEY" if index == 0 else f"GEMINI_API_KEY_{index}"
        monkeypatch.setenv(name, f"secret-gemini-{index}")
    monkeypatch.setattr(sys, "argv", ["configure_demo_secrets.py", "--dry-run"])

    configure.main()

    output = capsys.readouterr().out
    assert "/hindsight/demo/gemini-api-keys" in output
    assert "secret-" not in output


def test_configure_demo_secrets_preserves_generated_tokens_on_overwrite(
    monkeypatch, capsys
):
    configure = _configure_module()

    writes = []

    class FakeSsm:
        def get_parameter(self, **kwargs):
            return {"Parameter": {"Name": kwargs["Name"]}}

        def put_parameter(self, **kwargs):
            writes.append(kwargs)

    class FakeSession:
        def client(self, *args, **kwargs):
            return FakeSsm()

    monkeypatch.setattr(configure.boto3, "Session", lambda **kwargs: FakeSession())
    monkeypatch.setattr(configure, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://database")
    monkeypatch.delenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("HINDSIGHT_CHANGEFEED_AUTH_TOKEN", raising=False)
    for index in range(5):
        name = "GEMINI_API_KEY" if index == 0 else f"GEMINI_API_KEY_{index}"
        monkeypatch.setenv(name, f"key-{index}")
    monkeypatch.setattr(sys, "argv", ["configure_demo_secrets.py", "--overwrite"])

    configure.main()

    assert {write["Name"] for write in writes} == {
        "/hindsight/demo/database-url",
        "/hindsight/demo/gemini-api-keys",
    }
    assert "operator-token" in capsys.readouterr().out


def test_changefeed_wait_observes_requested_database_state(monkeypatch):
    configure = _changefeed_module()
    statuses = iter(
        [
            {"job_id": "42", "status": "pause-requested"},
            {"job_id": "42", "status": "paused"},
        ]
    )
    monkeypatch.setattr(configure, "changefeed_status", lambda **_kwargs: next(statuses))
    monkeypatch.setattr(configure.time, "sleep", lambda _seconds: None)

    result = configure._wait_for_job_status(  # noqa: SLF001 - CLI state-machine test
        job_id="42",
        expected="paused",
        changed=True,
        db_url="postgresql://db",
    )

    assert result == {"job_id": "42", "status": "paused", "changed": True}


def test_changefeed_wait_rejects_terminal_failure(monkeypatch):
    configure = _changefeed_module()
    monkeypatch.setattr(
        configure,
        "changefeed_status",
        lambda **_kwargs: {"job_id": "42", "status": "failed"},
    )

    with pytest.raises(RuntimeError, match="terminal status failed"):
        configure._wait_for_job_status(  # noqa: SLF001 - CLI state-machine test
            job_id="42",
            expected="running",
            changed=False,
            db_url="postgresql://db",
        )


def test_changefeed_status_command_fails_when_not_running(monkeypatch):
    configure = _changefeed_module()
    monkeypatch.setattr(
        configure,
        "changefeed_status",
        lambda: {"job_id": "42", "status": "failed", "changed": False},
    )
    monkeypatch.setattr(sys, "argv", ["configure_changefeed.py", "status"])

    with pytest.raises(RuntimeError, match="not running: failed"):
        configure.main()


def test_live_acceptance_restores_changefeed_after_benchmark_failure():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    assert "github.triggering_actor" in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert "test_live_gemini_embedding_provider" in workflow
    assert "BEDROCK" not in workflow
    assert "Bedrock" not in workflow
    restore_job = workflow.split("  restore_changefeed:\n", 1)[1].split(
        "  browser_acceptance:\n", 1
    )[0]

    assert "if: always() && needs.deploy.result == 'success'" in restore_job
    assert "if: needs.benchmark.result != 'success'" in restore_job
    assert "run_learning_benchmark.py finalize-interrupted" in restore_job
    restore_step = restore_job.split(
        "- name: Restore and verify the managed changefeed", 1
    )[1]
    assert "if: always()" in restore_step
    assert "configure_changefeed.py apply" in restore_step
    assert "configure_changefeed.py status" in restore_step
