"""Secret-safe runtime database credential provisioning."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import certifi
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))


class ParameterNotFound(Exception):
    pass


class FakeSsm:
    exceptions = SimpleNamespace(ParameterNotFound=ParameterNotFound)

    def __init__(self, deploy_url: str) -> None:
        self.parameters = {"/deploy": (deploy_url, "SecureString")}
        self.puts = []

    def get_parameter(self, *, Name, WithDecryption):
        del WithDecryption
        if Name not in self.parameters:
            raise ParameterNotFound(Name)
        value, parameter_type = self.parameters[Name]
        return {"Parameter": {"Name": Name, "Value": value, "Type": parameter_type}}

    def put_parameter(self, **kwargs):
        self.parameters[kwargs["Name"]] = (kwargs["Value"], kwargs["Type"])
        self.puts.append(kwargs)

    def delete_parameter(self, *, Name):
        if Name not in self.parameters:
            raise ParameterNotFound(Name)
        del self.parameters[Name]


class FakeConnection:
    def __init__(self, url: str) -> None:
        self.identity = unquote(urlsplit(url).username or "root")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params=None):
        del params
        if "rolsuper, rolbypassrls" in str(statement):
            return SimpleNamespace(fetchone=lambda: (False, False))
        return SimpleNamespace(fetchone=lambda: (self.identity,))


def test_runtime_url_preserves_database_and_encodes_credentials():
    from provision_runtime_database_credentials import _runtime_url

    result = _runtime_url(
        "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full",
        username="api user",
        password="p@ss/word",
    )

    assert result == (
        "postgresql://api%20user:p%40ss%2Fword@db.example:26257/"
        "hindsight?sslmode=verify-full"
    )


def test_prepare_uses_dala_and_writes_distinct_secure_strings(monkeypatch, capsys):
    import provision_runtime_database_credentials as provision

    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    sessions = []
    connected_urls = []

    class FakeSession:
        def __init__(self, *, profile_name, region_name):
            sessions.append((profile_name, region_name))

        def client(self, name):
            assert name == "ssm"
            return ssm

    monkeypatch.setattr(provision.boto3, "Session", FakeSession)

    def connect(url, **_kwargs):
        connected_urls.append(url)
        return FakeConnection(url)

    monkeypatch.setattr(provision.psycopg, "connect", connect)
    monkeypatch.setattr(provision.secrets, "token_hex", lambda _size: "abc123def456")
    generated = iter(("api-secret", "worker-secret"))
    monkeypatch.setattr(provision.secrets, "token_urlsafe", lambda _size: next(generated))

    provision.prepare(
        profile="dala",
        region="us-east-1",
        deploy_parameter="/deploy",
        api_parameter="/api",
        worker_parameter="/worker",
        metadata_parameter="/rotation",
    )

    assert sessions == [("dala", "us-east-1")]
    api_value, api_type = ssm.parameters["/api"]
    worker_value, worker_type = ssm.parameters["/worker"]
    assert api_type == worker_type == "SecureString"
    assert api_value != worker_value != deploy_url
    assert all(
        parse_qs(urlsplit(url).query)["sslrootcert"] == [certifi.where()]
        for url in connected_urls
    )
    assert "sslrootcert" not in parse_qs(urlsplit(api_value).query)
    assert "sslrootcert" not in parse_qs(urlsplit(worker_value).query)
    output = capsys.readouterr().out
    assert "api-secret" not in output
    assert "worker-secret" not in output
    assert deploy_url not in output


def test_prepare_preserves_explicit_tls_root(monkeypatch):
    import provision_runtime_database_credentials as provision

    deploy_url = (
        "postgresql://deploy@db.example:26257/hindsight"
        "?sslmode=verify-full&sslrootcert=system"
    )
    ssm = FakeSsm(deploy_url)
    connected_urls = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def client(self, _name):
            return ssm

    monkeypatch.setattr(provision.boto3, "Session", FakeSession)

    def connect(url, **_kwargs):
        connected_urls.append(url)
        return FakeConnection(url)

    monkeypatch.setattr(provision.psycopg, "connect", connect)
    provision.prepare(
        profile="dala",
        region="us-east-1",
        deploy_parameter="/deploy",
        api_parameter="/api",
        worker_parameter="/worker",
        metadata_parameter="/rotation",
    )

    assert all(
        parse_qs(urlsplit(url).query)["sslrootcert"] == ["system"]
        for url in connected_urls
    )


def test_prepare_rejects_reused_parameter_paths_before_aws(monkeypatch):
    import provision_runtime_database_credentials as provision

    monkeypatch.setattr(
        provision.boto3,
        "Session",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("AWS session created")),
    )
    with pytest.raises(RuntimeError, match="must be distinct"):
        provision.prepare(
            profile="dala",
            region="us-east-1",
            deploy_parameter="/same",
            api_parameter="/same",
            worker_parameter="/worker",
            metadata_parameter="/rotation",
        )
