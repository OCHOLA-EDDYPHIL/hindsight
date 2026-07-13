"""Deployment prerequisite tooling."""

import importlib.util
import pathlib
import sys


def _configure_module():
    path = pathlib.Path("scripts/configure_demo_secrets.py")
    spec = importlib.util.spec_from_file_location("configure_demo_secrets", path)
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
