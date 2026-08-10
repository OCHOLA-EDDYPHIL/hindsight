"""Invocation ownership and purge-readiness contracts for lifecycle fixtures."""

from __future__ import annotations

import hashlib
import json
import pathlib
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml

from scripts import provision_lifecycle_fixture as fixture


FIXTURE_ID = "12345678-1234-4abc-9234-567812345678"
OTHER_FIXTURE_ID = "22345678-1234-4abc-9234-567812345678"
POOL_ID = "us-east-1_fixture"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}"


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Transaction:
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        self.database.events.append("transaction-begin")
        return self

    def __exit__(self, exc_type, *_args):
        self.database.events.append("transaction-rollback" if exc_type else "transaction-commit")
        return False


class FakeDatabase:
    def __init__(self):
        self.events = []
        self.bound_tenant = None
        self.tenants = {}
        self.locators = {}
        self.principals = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return Transaction(self)

    def execute(self, statement, params):
        query = " ".join(str(statement).split())
        if "set_config('hindsight.tenant_id'" in query:
            self.bound_tenant = params[0]
            return Result((params[0],))
        if query.startswith("INSERT INTO tenants"):
            tenant_id, slug = params
            self.tenants.setdefault(
                tenant_id,
                {"slug": slug, "kind": "diagnostic", "status": "active"},
            )
            return Result()
        if query.startswith("SELECT slug, tenant_kind, status FROM tenants"):
            tenant = self.tenants.get(params[0])
            if tenant is None:
                return Result()
            return Result((tenant["slug"], tenant["kind"], tenant["status"]))
        if query.startswith("SELECT status FROM tenants"):
            self.events.append("tenant-lock")
            tenant = self.tenants.get(params[0])
            return Result(None if tenant is None else (tenant["status"],))
        if query.startswith("INSERT INTO product_credential_locators"):
            key, tenant_id, pool_id, username, role = params
            self.locators.setdefault(
                (pool_id, username),
                {
                    "id": f"locator-{len(self.locators) + 1}",
                    "tenant_id": tenant_id,
                    "key": key,
                    "role": role,
                    "status": "reserved",
                    "principal_hash": None,
                },
            )
            return Result()
        if query.startswith("SELECT tenant_id, provisioning_key, role, status, principal_hash"):
            locator = self.locators.get(tuple(params))
            if locator is None:
                return Result()
            return Result(
                (
                    locator["tenant_id"],
                    locator["key"],
                    locator["role"],
                    locator["status"],
                    locator["principal_hash"],
                )
            )
        if query.startswith("INSERT INTO product_principal_roles"):
            principal_hash, key, tenant_id, role = params
            self.principals.setdefault(
                key,
                {
                    "principal_hash": principal_hash,
                    "tenant_id": tenant_id,
                    "role": role,
                    "status": "active",
                },
            )
            return Result()
        if query.startswith(
            "SELECT principal_hash, tenant_id, role, status FROM product_principal_roles"
        ):
            principal = self.principals.get(params[0])
            if principal is None:
                return Result()
            return Result(
                (
                    principal["principal_hash"],
                    principal["tenant_id"],
                    principal["role"],
                    principal["status"],
                )
            )
        if query.startswith("SELECT principal_hash, status FROM product_credential_locators"):
            tenant_id, key, pool_id, username, role = params
            locator = self.locators.get((pool_id, username))
            if locator is None or (
                locator["tenant_id"],
                locator["key"],
                locator["role"],
            ) != (tenant_id, key, role):
                return Result()
            return Result((locator["principal_hash"], locator["status"]))
        if query.startswith("UPDATE product_credential_locators"):
            principal_hash, tenant_id, key, pool_id, username, role = params
            locator = self.locators.get((pool_id, username))
            if locator is None or (
                locator["tenant_id"],
                locator["key"],
                locator["role"],
            ) != (tenant_id, key, role):
                return Result()
            locator.update(principal_hash=principal_hash, status="active")
            return Result((locator["id"],))
        raise AssertionError(query)


class GroupPaginator:
    def __init__(self, cognito):
        self.cognito = cognito

    def paginate(self, **kwargs):
        self.cognito.calls.append(("list-groups", kwargs))
        yield {
            "Groups": [
                {"GroupName": group}
                for group in sorted(self.cognito.groups.get(kwargs["Username"], set()))
            ]
        }


class FakeCognito:
    class UserNotFoundException(Exception):
        pass

    exceptions = SimpleNamespace(UserNotFoundException=UserNotFoundException)

    def __init__(self, events, *, fail_password=False):
        self.events = events
        self.fail_password = fail_password
        self.users = {}
        self.groups = {}
        self.calls = []

    def admin_get_user(self, **kwargs):
        self.events.append("cognito-get")
        self.calls.append(("get", kwargs))
        user = self.users.get(kwargs["Username"])
        if user is None:
            raise self.exceptions.UserNotFoundException()
        return user

    def admin_create_user(self, **kwargs):
        self.events.append("cognito-create")
        self.calls.append(("create", kwargs))
        username = kwargs["Username"]
        user = {
            "Username": username,
            "Attributes": [{"Name": "sub", "Value": f"{username}-subject"}],
        }
        self.users[username] = user
        return {"User": user}

    def admin_set_user_password(self, **kwargs):
        self.events.append("cognito-password")
        self.calls.append(("password", kwargs))
        if self.fail_password:
            raise RuntimeError("injected Cognito failure")

    def get_paginator(self, name):
        assert name == "admin_list_groups_for_user"
        return GroupPaginator(self)

    def admin_remove_user_from_group(self, **kwargs):
        self.calls.append(("remove-group", kwargs))
        self.groups.setdefault(kwargs["Username"], set()).discard(kwargs["GroupName"])

    def admin_add_user_to_group(self, **kwargs):
        self.calls.append(("add-group", kwargs))
        self.groups.setdefault(kwargs["Username"], set()).add(kwargs["GroupName"])


def _provision(monkeypatch, database, cognito, fixture_id=FIXTURE_ID):
    monkeypatch.setattr(
        fixture.psycopg,
        "connect",
        lambda *args, **kwargs: database,
    )
    return fixture.provision_fixture(
        cognito,
        database_url="postgresql://deploy.example.test/hindsight?sslmode=verify-full",
        fixture_id=fixture_id,
        user_pool_id=POOL_ID,
        issuer=ISSUER,
    )


def test_fixture_commits_deletion_locators_before_creating_invocation_users(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)

    receipt = _provision(monkeypatch, database, cognito)

    assert receipt.fixture_id == FIXTURE_ID
    assert receipt.tenant_id == FIXTURE_ID
    assert set(receipt.usernames) == {
        f"hindsight-lifecycle-{UUID(FIXTURE_ID).hex}-operator",
        f"hindsight-lifecycle-{UUID(FIXTURE_ID).hex}-viewer",
    }
    first_cognito = database.events.index("cognito-get")
    assert database.events[:first_cognito].count("transaction-commit") == 1
    assert database.events[first_cognito - 2 : first_cognito] == [
        "transaction-begin",
        "tenant-lock",
    ]
    assert len(database.locators) == 2
    assert all(locator["status"] == "active" for locator in database.locators.values())
    assert {frozenset(groups) for groups in cognito.groups.values()} == {
        frozenset({"viewer"}),
        frozenset({"operator"}),
    }
    assert all(
        call[1].get("MessageAction") == "SUPPRESS" for call in cognito.calls if call[0] == "create"
    )


def test_fixture_keys_and_usernames_are_scoped_to_each_invocation(monkeypatch):
    monkeypatch.setattr(fixture.secrets, "token_urlsafe", lambda _size: "secret")
    first = fixture._identity_plan(issuer=ISSUER, fixture_id=FIXTURE_ID)
    second = fixture._identity_plan(issuer=ISSUER, fixture_id=OTHER_FIXTURE_ID)

    assert {item.username for item in first}.isdisjoint({item.username for item in second})
    assert {item.provisioning_key for item in first}.isdisjoint(
        {item.provisioning_key for item in second}
    )
    public_keys = {
        hashlib.sha256(f"{ISSUER}\0managed-role\0{role}".encode()).hexdigest()
        for role in fixture.MANAGED_GROUPS
    }
    assert {item.provisioning_key for item in first}.isdisjoint(public_keys)


def test_failed_cognito_write_leaves_exact_locators_for_lifecycle_purge(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events, fail_password=True)

    with pytest.raises(RuntimeError, match="injected Cognito failure"):
        _provision(monkeypatch, database, cognito)

    assert database.events.index("transaction-commit") < database.events.index("cognito-create")
    assert set(database.tenants) == {FIXTURE_ID}
    assert len(database.locators) == 2
    assert all(locator["status"] == "reserved" for locator in database.locators.values())
    assert database.principals == {}


def test_retry_reconciles_only_the_same_fixture(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)

    first = _provision(monkeypatch, database, cognito)
    second = _provision(monkeypatch, database, cognito)

    assert first == second
    assert set(database.tenants) == {FIXTURE_ID}
    assert len(database.locators) == 2
    assert len(database.principals) == 2
    assert len([call for call in cognito.calls if call[0] == "create"]) == 2


def test_non_v4_fixture_identity_is_rejected_before_database_or_aws(monkeypatch):
    monkeypatch.setattr(
        fixture.psycopg,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("database connection attempted"),
    )
    cognito = FakeCognito([])

    with pytest.raises(ValueError, match="UUIDv4"):
        fixture.provision_fixture(
            cognito,
            database_url="postgresql://unused",
            fixture_id="00000000-0000-0000-0000-000000000002",
            user_pool_id=POOL_ID,
            issuer=ISSUER,
        )

    assert cognito.calls == []


def test_receipt_contains_no_password_or_principal_material(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)
    monkeypatch.setattr(fixture.secrets, "token_urlsafe", lambda _size: "fixture-secret")
    receipt = _provision(monkeypatch, database, cognito)

    rendered = json.dumps(fixture.asdict(receipt), sort_keys=True)
    assert "fixture-secret" not in rendered
    assert "principal_hash" not in rendered
    assert "provisioning_key" not in rendered


def test_fixture_workflow_is_owner_gated_and_uses_only_the_deploy_role():
    path = pathlib.Path(".github/workflows/provision-lifecycle-fixture.yml")
    workflow_text = path.read_text()
    workflow = yaml.safe_load(workflow_text)
    job = workflow["jobs"]["provision"]

    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.actor == github.repository_owner" in job["if"]
    assert "github.triggering_actor == github.repository_owner" in job["if"]
    assert job["environment"] == "${{ inputs.deployment_environment }}"
    assert job["runs-on"] == "${{ vars.HINDSIGHT_RUNNER_LABEL }}"
    assert "role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}" in workflow_text
    assert "AWS_LIFECYCLE_ROLE_ARN" not in workflow_text
    assert 'test "$REQUESTED_CONFIRMATION" = "create-lifecycle-fixture"' in workflow_text
    assert "scripts/provision_lifecycle_fixture.py" in workflow_text
    assert "admin-delete-user" not in workflow_text.casefold()
    assert "DELETE FROM tenants" not in workflow_text
    assert "lifecycle-fixture-receipt.json" in workflow_text
