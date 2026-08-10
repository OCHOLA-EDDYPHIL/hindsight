"""Isolation and rollback contracts for the lifecycle-only database login."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

from scripts import provision_lifecycle_database_credential as provision


class ParameterNotFound(Exception):
    pass


class Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeSsm:
    exceptions = SimpleNamespace(ParameterNotFound=ParameterNotFound)

    def __init__(self, deploy_url: str, *, fail_put: bool = False):
        self.parameters = {provision.DEPLOY_DATABASE_PARAMETER: (deploy_url, "SecureString")}
        self.fail_put = fail_put
        self.gets = []
        self.puts = []
        self.deletes = []

    def get_parameter(self, *, Name, WithDecryption):
        assert WithDecryption is True
        self.gets.append(Name)
        try:
            value, parameter_type = self.parameters[Name]
        except KeyError as exc:
            raise ParameterNotFound(Name) from exc
        return {"Parameter": {"Value": value, "Type": parameter_type}}

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)
        if self.fail_put:
            self.fail_put = False
            raise RuntimeError("injected parameter failure")
        self.parameters[kwargs["Name"]] = (kwargs["Value"], kwargs["Type"])

    def delete_parameter(self, *, Name):
        self.deletes.append(Name)
        if Name not in self.parameters:
            raise ParameterNotFound(Name)
        del self.parameters[Name]


class FakeDatabase:
    def __init__(self):
        self.roles = {
            provision.LIFECYCLE_ROLE: {
                "can_login": False,
                "superuser": False,
                "bypass_rls": False,
                "memberships": set(),
                "password": None,
            }
        }
        self.statements = []

    def connect(self, url: str, **kwargs):
        return FakeConnection(self, url, kwargs)


class FakeConnection:
    def __init__(self, database: FakeDatabase, url: str, kwargs):
        self.database = database
        self.identity = unquote(urlsplit(url).username or "deploy")
        self.password = unquote(urlsplit(url).password or "")
        self.kwargs = kwargs

    def __enter__(self):
        if self.identity == provision.LIFECYCLE_LOGIN:
            expected = self.database.roles[self.identity]["password"]
            if self.password != expected:
                raise RuntimeError("invalid database password")
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        query = " ".join(str(statement).split())
        self.database.statements.append((query, params))
        if query == "SELECT current_user":
            return Result(row=(self.identity,))
        if "SELECT rolcanlogin, rolsuper, rolbypassrls" in query:
            role = self.database.roles.get(params[0])
            if role is None:
                return Result()
            return Result(row=(role["can_login"], role["superuser"], role["bypass_rls"]))
        if "SELECT granted.rolname" in query:
            role = self.database.roles.get(params[0])
            rows = [] if role is None else [(item,) for item in sorted(role["memberships"])]
            return Result(rows=rows)
        if "CREATE ROLE" in query:
            assert provision.LIFECYCLE_LOGIN not in self.database.roles
            self.database.roles[provision.LIFECYCLE_LOGIN] = {
                "can_login": True,
                "superuser": False,
                "bypass_rls": False,
                "memberships": set(),
                "password": params[0],
            }
            return Result()
        if "GRANT" in query:
            self.database.roles[provision.LIFECYCLE_LOGIN]["memberships"].add(
                provision.LIFECYCLE_ROLE
            )
            return Result()
        if "REVOKE" in query:
            self.database.roles[provision.LIFECYCLE_LOGIN]["memberships"].discard(
                provision.LIFECYCLE_ROLE
            )
            return Result()
        if "DROP ROLE" in query:
            del self.database.roles[provision.LIFECYCLE_LOGIN]
            return Result()
        raise AssertionError(query)


def _install(monkeypatch, ssm: FakeSsm, database: FakeDatabase):
    sessions = []

    class Session:
        def __init__(self, *, profile_name, region_name):
            sessions.append((profile_name, region_name))

        def client(self, name, *, config):
            assert name == "ssm"
            assert config.read_timeout == 10
            return ssm

    monkeypatch.setattr(provision.boto3, "Session", Session)
    monkeypatch.setattr(provision.psycopg, "connect", database.connect)
    monkeypatch.setattr(provision.secrets, "token_urlsafe", lambda _size: "new-secret")
    return sessions


def test_reconcile_creates_only_the_fixed_lifecycle_parameter(monkeypatch, capsys):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    database = FakeDatabase()
    sessions = _install(monkeypatch, ssm, database)

    assert provision.reconcile(profile="dala", region="us-east-1") == "created"

    assert sessions == [("dala", "us-east-1")]
    assert set(ssm.gets) == {
        provision.DEPLOY_DATABASE_PARAMETER,
        provision.LIFECYCLE_DATABASE_PARAMETER,
    }
    assert [put["Name"] for put in ssm.puts] == [provision.LIFECYCLE_DATABASE_PARAMETER]
    assert ssm.puts[0]["Type"] == "SecureString"
    assert database.roles[provision.LIFECYCLE_LOGIN] == {
        "can_login": True,
        "superuser": False,
        "bypass_rls": False,
        "memberships": {provision.LIFECYCLE_ROLE},
        "password": "new-secret",
    }
    assert provision.main(["--profile", "dala"]) == 0
    output = capsys.readouterr().out
    assert "new-secret" not in output
    assert deploy_url not in output


def test_reconcile_is_noop_when_its_existing_credential_verifies(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    existing_url = provision._runtime_url(deploy_url, password="existing-secret")
    ssm = FakeSsm(deploy_url)
    ssm.parameters[provision.LIFECYCLE_DATABASE_PARAMETER] = (
        existing_url,
        "SecureString",
    )
    database = FakeDatabase()
    database.roles[provision.LIFECYCLE_LOGIN] = {
        "can_login": True,
        "superuser": False,
        "bypass_rls": False,
        "memberships": {provision.LIFECYCLE_ROLE},
        "password": "existing-secret",
    }
    _install(monkeypatch, ssm, database)

    assert provision.reconcile(profile="dala", region="us-east-1") == "unchanged"
    assert ssm.puts == []
    assert database.roles[provision.LIFECYCLE_LOGIN]["password"] == "existing-secret"


def test_failed_parameter_write_restores_absence_before_dropping_login(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url, fail_put=True)
    database = FakeDatabase()
    _install(monkeypatch, ssm, database)

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        provision.reconcile(profile="dala", region="us-east-1")

    assert provision.LIFECYCLE_DATABASE_PARAMETER not in ssm.parameters
    assert ssm.deletes == [provision.LIFECYCLE_DATABASE_PARAMETER]
    assert provision.LIFECYCLE_LOGIN not in database.roles


def test_unexpected_existing_membership_fails_before_any_parameter_write(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    existing_url = provision._runtime_url(deploy_url, password="existing-secret")
    ssm = FakeSsm(deploy_url)
    ssm.parameters[provision.LIFECYCLE_DATABASE_PARAMETER] = (
        existing_url,
        "SecureString",
    )
    database = FakeDatabase()
    database.roles[provision.LIFECYCLE_LOGIN] = {
        "can_login": True,
        "superuser": False,
        "bypass_rls": False,
        "memberships": {provision.LIFECYCLE_ROLE, "admin"},
        "password": "existing-secret",
    }
    _install(monkeypatch, ssm, database)

    with pytest.raises(RuntimeError, match="unexpected memberships"):
        provision.reconcile(profile="dala", region="us-east-1")

    assert ssm.puts == []


def test_source_has_no_api_or_worker_parameter_surface():
    source = pathlib.Path("scripts/provision_lifecycle_database_credential.py").read_text()

    assert "api-database-url" not in source
    assert "worker-database-url" not in source
    assert "api_parameter" not in source
    assert "worker_parameter" not in source
