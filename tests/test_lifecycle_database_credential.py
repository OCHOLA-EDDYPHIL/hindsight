"""Isolation and retry recovery contracts for the lifecycle-only database login."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest

from scripts import provision_lifecycle_database_credential as provision


class ParameterNotFound(Exception):
    pass


class ParameterAlreadyExists(Exception):
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
    exceptions = SimpleNamespace(
        ParameterAlreadyExists=ParameterAlreadyExists,
        ParameterNotFound=ParameterNotFound,
    )

    def __init__(self, deploy_url: str, *, fail_after_put: bool = False):
        self.parameters = {provision.DEPLOY_DATABASE_PARAMETER: (deploy_url, "SecureString")}
        self.fail_after_put = fail_after_put
        self.gets = []
        self.puts = []

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
        if kwargs["Name"] in self.parameters and not kwargs["Overwrite"]:
            raise ParameterAlreadyExists(kwargs["Name"])
        self.parameters[kwargs["Name"]] = (kwargs["Value"], kwargs["Type"])
        if self.fail_after_put:
            self.fail_after_put = False
            raise RuntimeError("injected acknowledgement loss")


class FakeDatabase:
    def __init__(self, *, fail_after: str | None = None):
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
        self.fail_after = fail_after

    def fail_if_requested(self, point: str) -> None:
        if self.fail_after == point:
            self.fail_after = None
            raise RuntimeError(f"injected failure after {point}")

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
            role = self.database.roles.get(self.identity)
            if role is None or self.password != role["password"]:
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
            self.database.fail_if_requested("create")
            return Result()
        if "ALTER ROLE" in query:
            self.database.roles[provision.LIFECYCLE_LOGIN]["password"] = params[0]
            self.database.fail_if_requested("alter")
            return Result()
        if "GRANT" in query:
            self.database.roles[provision.LIFECYCLE_LOGIN]["memberships"].add(
                provision.LIFECYCLE_ROLE
            )
            self.database.fail_if_requested("grant")
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


def _add_login(
    database: FakeDatabase,
    *,
    password: str,
    memberships: set[str] | None = None,
) -> None:
    database.roles[provision.LIFECYCLE_LOGIN] = {
        "can_login": True,
        "superuser": False,
        "bypass_rls": False,
        "memberships": memberships or set(),
        "password": password,
    }


def _add_parameter(ssm: FakeSsm, deploy_url: str, *, password: str) -> str:
    value = provision._runtime_url(deploy_url, password=password)
    ssm.parameters[provision.LIFECYCLE_DATABASE_PARAMETER] = (value, "SecureString")
    return value


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
    assert ssm.puts[0]["Overwrite"] is False
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
    ssm = FakeSsm(deploy_url)
    _add_parameter(ssm, deploy_url, password="existing-secret")
    database = FakeDatabase()
    _add_login(
        database,
        password="existing-secret",
        memberships={provision.LIFECYCLE_ROLE},
    )
    _install(monkeypatch, ssm, database)

    assert provision.reconcile(profile="dala", region="us-east-1") == "unchanged"
    assert ssm.puts == []
    assert database.roles[provision.LIFECYCLE_LOGIN]["password"] == "existing-secret"


def test_parameter_write_acknowledgement_loss_recovers_on_retry(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url, fail_after_put=True)
    database = FakeDatabase()
    _install(monkeypatch, ssm, database)

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        provision.reconcile(profile="dala", region="us-east-1")

    assert provision.LIFECYCLE_DATABASE_PARAMETER in ssm.parameters
    assert provision.LIFECYCLE_LOGIN not in database.roles
    assert provision.reconcile(profile="dala", region="us-east-1") == "recovered"
    assert len(ssm.puts) == 1
    assert database.roles[provision.LIFECYCLE_LOGIN]["memberships"] == {provision.LIFECYCLE_ROLE}


@pytest.mark.parametrize(
    ("failure_point", "retry_result"),
    [("create", "recovered"), ("grant", "unchanged")],
)
def test_database_acknowledgement_loss_recovers_on_retry(monkeypatch, failure_point, retry_result):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    database = FakeDatabase(fail_after=failure_point)
    _install(monkeypatch, ssm, database)

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        provision.reconcile(profile="dala", region="us-east-1")

    assert provision.LIFECYCLE_DATABASE_PARAMETER in ssm.parameters
    assert provision.LIFECYCLE_LOGIN in database.roles
    assert provision.reconcile(profile="dala", region="us-east-1") == retry_result
    assert database.roles[provision.LIFECYCLE_LOGIN]["memberships"] == {provision.LIFECYCLE_ROLE}


def test_role_without_parameter_is_repaired_from_a_new_durable_secret(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    database = FakeDatabase()
    _add_login(
        database,
        password="unrecoverable-secret",
        memberships={provision.LIFECYCLE_ROLE},
    )
    _install(monkeypatch, ssm, database)

    assert provision.reconcile(profile="dala", region="us-east-1") == "recovered"
    assert database.roles[provision.LIFECYCLE_LOGIN]["password"] == "new-secret"
    assert provision.LIFECYCLE_DATABASE_PARAMETER in ssm.parameters


def test_parameter_without_role_is_used_to_recreate_the_fixed_login(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    _add_parameter(ssm, deploy_url, password="durable-secret")
    database = FakeDatabase()
    _install(monkeypatch, ssm, database)

    assert provision.reconcile(profile="dala", region="us-east-1") == "recovered"
    assert ssm.puts == []
    assert database.roles[provision.LIFECYCLE_LOGIN]["password"] == "durable-secret"


def test_parameter_repairs_password_drift_without_replacing_the_secret(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    _add_parameter(ssm, deploy_url, password="durable-secret")
    database = FakeDatabase()
    _add_login(
        database,
        password="stale-secret",
        memberships={provision.LIFECYCLE_ROLE},
    )
    _install(monkeypatch, ssm, database)

    assert provision.reconcile(profile="dala", region="us-east-1") == "recovered"
    assert ssm.puts == []
    assert database.roles[provision.LIFECYCLE_LOGIN]["password"] == "durable-secret"


def test_unexpected_existing_membership_fails_before_any_parameter_write(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    _add_parameter(ssm, deploy_url, password="existing-secret")
    database = FakeDatabase()
    _add_login(
        database,
        password="existing-secret",
        memberships={provision.LIFECYCLE_ROLE, "admin"},
    )
    _install(monkeypatch, ssm, database)

    with pytest.raises(RuntimeError, match="unexpected memberships"):
        provision.reconcile(profile="dala", region="us-east-1")

    assert ssm.puts == []


def test_permission_role_inheritance_fails_before_creating_a_secret(monkeypatch):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    database = FakeDatabase()
    database.roles[provision.LIFECYCLE_ROLE]["memberships"] = {"admin"}
    _install(monkeypatch, ssm, database)

    with pytest.raises(RuntimeError, match="permission role is missing or unsafe"):
        provision.reconcile(profile="dala", region="us-east-1")

    assert ssm.puts == []
    assert provision.LIFECYCLE_LOGIN not in database.roles


@pytest.mark.parametrize(
    ("component", "replacement"),
    [
        ("scheme", "postgres"),
        ("authority", "other.example:26257"),
        ("path", "/other-database"),
        ("query", "sslmode=disable"),
        ("fragment", "different-cluster-route"),
    ],
)
def test_parameter_noncredential_url_components_must_match_exactly(
    monkeypatch, component, replacement
):
    deploy_url = "postgresql://deploy@db.example:26257/hindsight?sslmode=verify-full"
    ssm = FakeSsm(deploy_url)
    parameter = provision._runtime_url(deploy_url, password="existing-secret")
    parts = urlsplit(parameter)
    if component == "authority":
        userinfo = parts.netloc.rsplit("@", 1)[0]
        drifted_parts = parts._replace(netloc=f"{userinfo}@{replacement}")
    else:
        drifted_parts = parts._replace(**{component: replacement})
    drifted = urlunsplit(drifted_parts)
    ssm.parameters[provision.LIFECYCLE_DATABASE_PARAMETER] = (
        drifted,
        "SecureString",
    )
    database = FakeDatabase()
    _install(monkeypatch, ssm, database)

    with pytest.raises(RuntimeError, match="another database configuration"):
        provision.reconcile(profile="dala", region="us-east-1")

    assert ssm.puts == []
    assert provision.LIFECYCLE_LOGIN not in database.roles


def test_source_has_no_api_or_worker_parameter_surface():
    source = pathlib.Path("scripts/provision_lifecycle_database_credential.py").read_text()

    assert "api-database-url" not in source
    assert "worker-database-url" not in source
    assert "api_parameter" not in source
    assert "worker_parameter" not in source
