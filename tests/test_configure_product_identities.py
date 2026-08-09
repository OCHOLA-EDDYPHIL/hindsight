"""Fenced Cognito provisioning and durable deletion-locator contracts."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from scripts.configure_product_identities import (
    DesiredIdentity,
    PrincipalMapping,
    configure_product_identities,
)


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeTransaction:
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        self.database.events.append("transaction-begin")
        return self

    def __exit__(self, exc_type, *_args):
        self.database.events.append(
            "transaction-rollback" if exc_type is not None else "transaction-commit"
        )
        return False


class FakeDatabase:
    def __init__(self, *, tenant_status="active"):
        self.tenant_status = tenant_status
        self.events = []
        self.locators = {}
        self.mappings = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return FakeTransaction(self)

    def execute(self, statement, params):
        query = " ".join(str(statement).split())
        if "set_config('hindsight.tenant_id'" in query:
            return Result((params[0],))
        if query.startswith("SELECT status FROM tenants"):
            self.events.append("tenant-lock")
            return Result((self.tenant_status,))
        if query.startswith("INSERT INTO product_credential_locators"):
            provisioning_key, tenant_id, pool_id, username, role = params
            self.locators.setdefault(
                (pool_id, username),
                {
                    "id": f"locator-{len(self.locators) + 1}",
                    "principal_hash": None,
                    "provisioning_key": provisioning_key,
                    "role": role,
                    "status": "reserved",
                    "tenant_id": tenant_id,
                },
            )
            return Result()
        if query.startswith("SELECT tenant_id, provisioning_key, role"):
            locator = self.locators.get(tuple(params))
            if locator is None:
                return Result()
            return Result(
                (
                    locator["tenant_id"],
                    locator["provisioning_key"],
                    locator["role"],
                )
            )
        if query.startswith("INSERT INTO product_principal_roles"):
            principal_hash, provisioning_key, tenant_id, role = params
            self.mappings[provisioning_key] = (
                principal_hash,
                tenant_id,
                role,
            )
            return Result()
        if query.startswith("UPDATE product_credential_locators"):
            principal_hash, tenant_id, provisioning_key, pool_id, username = params
            locator = self.locators.get((pool_id, username))
            if locator is None or (
                locator["tenant_id"],
                locator["provisioning_key"],
            ) != (tenant_id, provisioning_key):
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
                for group in sorted(
                    self.cognito.groups.get(kwargs["Username"], set())
                )
            ]
        }


class FakeCognito:
    class UserNotFoundException(Exception):
        pass

    exceptions = SimpleNamespace(UserNotFoundException=UserNotFoundException)

    def __init__(self, events, *, fail_password=False):
        self.events = events
        self.fail_password = fail_password
        self.users = {
            "existing-viewer": {
                "Username": "existing-viewer",
                "UserAttributes": [{"Name": "sub", "Value": "viewer-sub"}],
            }
        }
        self.groups = {"existing-viewer": {"operator"}}
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
            "Attributes": [{"Name": "sub", "Value": f"{username}-sub"}],
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


def _configure(monkeypatch, database, cognito, *, desired):
    connects = []
    monkeypatch.setattr(
        "scripts.configure_product_identities.psycopg.connect",
        lambda *args, **kwargs: connects.append((args, kwargs)) or database,
    )
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool"
    mappings = configure_product_identities(
        cognito,
        database_url="postgresql://deploy.example.test/defaultdb?sslmode=verify-full",
        tenant_id="00000000-0000-0000-0000-000000000003",
        user_pool_id="us-east-1_pool",
        issuer=issuer,
        desired=desired,
    )
    return issuer, mappings, connects


def test_provisioning_commits_locators_before_cognito_and_activates_mappings(
    monkeypatch,
):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)
    desired = (
        DesiredIdentity("existing-viewer", "Viewer-password-123!", "viewer"),
        DesiredIdentity("new-operator", "Operator-password-123!", "operator"),
    )

    issuer, mappings, connects = _configure(
        monkeypatch, database, cognito, desired=desired
    )

    assert mappings == (
        PrincipalMapping(
            hashlib.sha256(f"{issuer}\0viewer-sub".encode()).hexdigest(),
            hashlib.sha256(f"{issuer}\0managed-role\0viewer".encode()).hexdigest(),
            "viewer",
            "us-east-1_pool",
            "existing-viewer",
        ),
        PrincipalMapping(
            hashlib.sha256(f"{issuer}\0new-operator-sub".encode()).hexdigest(),
            hashlib.sha256(f"{issuer}\0managed-role\0operator".encode()).hexdigest(),
            "operator",
            "us-east-1_pool",
            "new-operator",
        ),
    )
    first_cognito = database.events.index("cognito-get")
    assert database.events[:first_cognito].count("transaction-commit") == 1
    assert database.events[first_cognito - 2 : first_cognito] == [
        "transaction-begin",
        "tenant-lock",
    ]
    assert database.events[-1] == "transaction-commit"
    assert all(locator["status"] == "active" for locator in database.locators.values())
    assert connects[0][1]["autocommit"] is True
    assert connects[0][1]["application_name"] == (
        "hindsight-product-identity-provisioner"
    )
    assert cognito.groups == {
        "existing-viewer": {"viewer"},
        "new-operator": {"operator"},
    }
    create = next(values for name, values in cognito.calls if name == "create")
    assert create["MessageAction"] == "SUPPRESS"


def test_cognito_failure_leaves_a_committed_deletion_locator(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events, fail_password=True)

    with pytest.raises(RuntimeError, match="injected Cognito failure"):
        _configure(
            monkeypatch,
            database,
            cognito,
            desired=(
                DesiredIdentity(
                    "new-operator", "Operator-password-123!", "operator"
                ),
            ),
        )

    assert database.events.index("transaction-commit") < database.events.index(
        "cognito-create"
    )
    assert database.events[-1] == "transaction-rollback"
    assert list(database.locators.values())[0]["status"] == "reserved"
    assert database.mappings == {}


def test_archived_tenant_blocks_every_cognito_call(monkeypatch):
    database = FakeDatabase(tenant_status="archived")
    cognito = FakeCognito(database.events)

    with pytest.raises(RuntimeError, match="tenant is not active"):
        _configure(
            monkeypatch,
            database,
            cognito,
            desired=(
                DesiredIdentity(
                    "new-operator", "Operator-password-123!", "operator"
                ),
            ),
        )

    assert cognito.calls == []
    assert database.locators == {}


def test_username_whitespace_is_rejected_before_database_or_cognito(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)

    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        _configure(
            monkeypatch,
            database,
            cognito,
            desired=(
                DesiredIdentity(
                    " operator ", "Operator-password-123!", "operator"
                ),
            ),
        )

    assert database.events == []
    assert cognito.calls == []


def test_username_rotation_retains_every_direct_deletion_locator(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)

    _configure(
        monkeypatch,
        database,
        cognito,
        desired=(
            DesiredIdentity("operator-v1", "Operator-password-123!", "operator"),
        ),
    )
    _configure(
        monkeypatch,
        database,
        cognito,
        desired=(
            DesiredIdentity("operator-v2", "Operator-password-456!", "operator"),
        ),
    )

    assert set(database.locators) == {
        ("us-east-1_pool", "operator-v1"),
        ("us-east-1_pool", "operator-v2"),
    }
