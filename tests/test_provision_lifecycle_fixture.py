"""Invocation ownership and purge-readiness contracts for lifecycle fixtures."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import provision_lifecycle_fixture as fixture
from hindsight.lifecycle import LifecycleConflictError, PublicIdentitySentinel
from hindsight.server_tenants import PUBLIC_DEMO_TENANT_ID


FIXTURE_ID = "12345678-1234-4abc-9234-567812345678"
OTHER_FIXTURE_ID = "22345678-1234-4abc-9234-567812345678"
POOL_ID = "us-east-1_fixture"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}"


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = [] if rows is None else rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


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
        self.public_principals = (
            (
                "public-principal-operator",
                "a" * 64,
                "b" * 64,
                PUBLIC_DEMO_TENANT_ID,
                "operator",
                "active",
            ),
            (
                "public-principal-viewer",
                "c" * 64,
                "d" * 64,
                PUBLIC_DEMO_TENANT_ID,
                "viewer",
                "active",
            ),
        )
        self.public_locators = (
            (
                "public-locator-operator",
                "b" * 64,
                PUBLIC_DEMO_TENANT_ID,
                POOL_ID,
                "public-operator",
                "operator",
                "a" * 64,
                "active",
            ),
            (
                "public-locator-viewer",
                "d" * 64,
                PUBLIC_DEMO_TENANT_ID,
                POOL_ID,
                "public-viewer",
                "viewer",
                "c" * 64,
                "active",
            ),
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return Transaction(self)

    def execute(self, statement, params):
        query = " ".join(str(statement).split())
        if query == "SELECT set_config(%s, %s, true)":
            assert params[0] == "hindsight.tenant_id"
            self.bound_tenant = params[1]
            return Result((params[1],))
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
        if query.startswith("SELECT id, slug, tenant_kind, status FROM tenants"):
            tenant_id = params[0]
            if tenant_id == PUBLIC_DEMO_TENANT_ID:
                return Result(
                    rows=[
                        (
                            PUBLIC_DEMO_TENANT_ID,
                            "public-demo",
                            "public_demo",
                            "active",
                        )
                    ]
                )
            tenant = self.tenants.get(tenant_id)
            if tenant is None:
                return Result(rows=[])
            return Result(
                rows=[
                    (
                        tenant_id,
                        tenant["slug"],
                        tenant["kind"],
                        tenant["status"],
                    )
                ]
            )
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
                    "pool_id": pool_id,
                    "username": username,
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
                    "id": f"principal-{len(self.principals) + 1}",
                    "principal_hash": principal_hash,
                    "tenant_id": tenant_id,
                    "role": role,
                    "status": "active",
                },
            )
            return Result()
        if query.startswith(
            "SELECT id, principal_hash, provisioning_key, tenant_id, role, status"
        ):
            tenant_id = params[0]
            if tenant_id == PUBLIC_DEMO_TENANT_ID:
                return Result(rows=list(self.public_principals))
            rows = [
                (
                    principal["id"],
                    principal["principal_hash"],
                    key,
                    principal["tenant_id"],
                    principal["role"],
                    principal["status"],
                )
                for key, principal in self.principals.items()
                if principal["tenant_id"] == tenant_id
            ]
            return Result(rows=sorted(rows, key=lambda row: (row[2], row[1], row[0])))
        if query.startswith(
            "SELECT id, provisioning_key, tenant_id, user_pool_id,"
        ):
            tenant_id = params[0]
            if tenant_id == PUBLIC_DEMO_TENANT_ID:
                return Result(rows=list(self.public_locators))
            rows = [
                (
                    locator["id"],
                    locator["key"],
                    locator["tenant_id"],
                    locator["pool_id"],
                    locator["username"],
                    locator["role"],
                    locator["principal_hash"],
                    locator["status"],
                )
                for locator in self.locators.values()
                if locator["tenant_id"] == tenant_id
            ]
            return Result(rows=sorted(rows, key=lambda row: (row[1], row[3], row[4], row[0])))
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


class FakeDynamoTable:
    class ConditionalCheckFailedException(Exception):
        pass

    def __init__(self, name, key_fields, indexes, *, fail_puts=0):
        self.name = name
        self.key_fields = tuple(key_fields)
        self.items = {}
        self.fail_puts = fail_puts
        self.miss_reads = 0
        self.put_attempts = 0
        attribute_names = set(self.key_fields)
        global_indexes = []
        for index_name, (index_fields, projection_type) in indexes.items():
            attribute_names.update(index_fields)
            global_indexes.append(
                {
                    "Backfilling": False,
                    "IndexName": index_name,
                    "IndexStatus": "ACTIVE",
                    "KeySchema": [
                        {
                            "AttributeName": field,
                            "KeyType": "HASH" if position == 0 else "RANGE",
                        }
                        for position, field in enumerate(index_fields)
                    ],
                    "Projection": {"ProjectionType": projection_type},
                }
            )
        self.description = {
            "AttributeDefinitions": [
                {"AttributeName": field, "AttributeType": "S"}
                for field in sorted(attribute_names)
            ],
            "GlobalSecondaryIndexes": global_indexes,
            "KeySchema": [
                {
                    "AttributeName": field,
                    "KeyType": "HASH" if position == 0 else "RANGE",
                }
                for position, field in enumerate(self.key_fields)
            ],
            "TableName": name,
            "TableStatus": "ACTIVE",
        }
        self.ttl_description = {
            "AttributeName": "expires_at",
            "TimeToLiveStatus": "ENABLED",
        }
        self.describe_calls = []
        self.meta = SimpleNamespace(
            client=SimpleNamespace(
                describe_table=self.describe_table,
                describe_time_to_live=self.describe_time_to_live,
                exceptions=SimpleNamespace(
                    ConditionalCheckFailedException=self.ConditionalCheckFailedException
                )
            )
        )

    def describe_table(self, **kwargs):
        assert kwargs == {"TableName": self.name}
        self.describe_calls.append("table")
        return {"Table": self.description}

    def describe_time_to_live(self, **kwargs):
        assert kwargs == {"TableName": self.name}
        self.describe_calls.append("ttl")
        return {"TimeToLiveDescription": self.ttl_description}

    def _key(self, value):
        return tuple(value[field] for field in self.key_fields)

    def _condition_matches(self, condition, item):
        expression = condition.get_expression()
        operator = expression["operator"]
        values = expression["values"]
        if operator == "AND":
            return all(self._condition_matches(value, item) for value in values)
        field = values[0].name
        if operator == "attribute_not_exists":
            return item is None or field not in item
        if operator == "=":
            return item is not None and item.get(field) == values[1]
        raise AssertionError(operator)

    def get_item(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        if self.miss_reads:
            self.miss_reads -= 1
            return {}
        item = self.items.get(self._key(kwargs["Key"]))
        if item is None:
            return {}
        return {
            "Item": {
                field: Decimal(value) if isinstance(value, int) else value
                for field, value in item.items()
            }
        }

    def put_item(self, **kwargs):
        self.put_attempts += 1
        assert hasattr(kwargs["ConditionExpression"], "get_expression")
        assert "ExpressionAttributeNames" not in kwargs
        if self.fail_puts:
            self.fail_puts -= 1
            raise RuntimeError("injected DynamoDB failure")
        item = dict(kwargs["Item"])
        key = self._key(item)
        existing = self.items.get(key)
        if not self._condition_matches(kwargs["ConditionExpression"], existing):
            raise self.ConditionalCheckFailedException()
        self.items[key] = item


def _dynamo_tables():
    return fixture.FixtureDynamoTables(
        ticket=FakeDynamoTable(
            "tickets",
            ("ticket_digest",),
            {"tenant-id-index": (("tenant_id",), "KEYS_ONLY")},
        ),
        subscription=FakeDynamoTable(
            "subscriptions",
            ("topic_key", "connection_id"),
            {
                "connection-id-index": (("connection_id",), "ALL"),
                "tenant-id-index": (("tenant_id",), "KEYS_ONLY"),
            },
        ),
        connection=FakeDynamoTable(
            "connections",
            ("connection_id",),
            {"tenant-id-index": (("tenant_id",), "KEYS_ONLY")},
        ),
    )


def _provision(
    monkeypatch,
    database,
    cognito,
    fixture_id=FIXTURE_ID,
    dynamodb_tables=None,
):
    monkeypatch.setattr(
        fixture.psycopg,
        "connect",
        lambda *args, **kwargs: database,
    )
    return fixture.provision_fixture(
        cognito,
        dynamodb_tables=dynamodb_tables or _dynamo_tables(),
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
    assert database.events[:first_cognito].count("transaction-commit") == 2
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
    tables = _dynamo_tables()

    first = _provision(monkeypatch, database, cognito, dynamodb_tables=tables)
    second = _provision(monkeypatch, database, cognito, dynamodb_tables=tables)

    assert first == second
    assert set(database.tenants) == {FIXTURE_ID}
    assert len(database.locators) == 2
    assert len(database.principals) == 2
    assert len([call for call in cognito.calls if call[0] == "create"]) == 2
    assert len(tables.ticket.items) == 1
    assert len(tables.subscription.items) == 1
    assert len(tables.connection.items) == 1
    assert tables.ticket.describe_calls == ["table", "ttl", "table", "ttl"]
    assert tables.subscription.describe_calls == ["table", "ttl", "table", "ttl"]
    assert tables.connection.describe_calls == ["table", "ttl", "table", "ttl"]


def test_retry_completes_a_partial_dynamodb_fixture_without_duplicates(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)
    tables = _dynamo_tables()
    tables.connection.fail_puts = 1

    with pytest.raises(RuntimeError, match="injected DynamoDB failure"):
        _provision(monkeypatch, database, cognito, dynamodb_tables=tables)

    assert len(tables.ticket.items) == 1
    assert tables.connection.items == {}
    assert tables.subscription.items == {}

    receipt = _provision(monkeypatch, database, cognito, dynamodb_tables=tables)

    assert receipt.inventory.managed_realtime_ticket_rows == 1
    assert receipt.inventory.managed_websocket_connection_rows == 1
    assert receipt.inventory.managed_websocket_subscription_rows == 1
    assert len(tables.ticket.items) == 1
    assert len(tables.connection.items) == 1
    assert len(tables.subscription.items) == 1
    assert len([call for call in cognito.calls if call[0] == "create"]) == 2


def test_dynamodb_conditional_race_accepts_only_the_identical_fixture_row():
    tables = _dynamo_tables()
    specification = fixture._fixture_dynamo_items(  # noqa: SLF001
        tables,
        fixture_id=FIXTURE_ID,
        expires_at=1_900_000_000,
    )[2]
    tables.subscription.items[tables.subscription._key(specification.item)] = dict(  # noqa: SLF001
        specification.item
    )
    tables.subscription.miss_reads = 1

    item = fixture._put_fixture_item(  # noqa: SLF001
        specification,
        now=1_800_000_000,
    )

    assert item["tenant_id"] == FIXTURE_ID
    assert tables.subscription.put_attempts == 1
    assert len(tables.subscription.items) == 1


def test_expired_exact_dynamodb_row_is_conditionally_refreshed():
    tables = _dynamo_tables()
    expired = fixture._fixture_dynamo_items(  # noqa: SLF001
        tables,
        fixture_id=FIXTURE_ID,
        expires_at=1_700_000_000,
    )[1]
    replacement = fixture._fixture_dynamo_items(  # noqa: SLF001
        tables,
        fixture_id=FIXTURE_ID,
        expires_at=1_900_000_000,
    )[1]
    tables.connection.items[tables.connection._key(expired.item)] = dict(  # noqa: SLF001
        expired.item
    )

    item = fixture._put_fixture_item(  # noqa: SLF001
        replacement,
        now=1_800_000_000,
    )

    assert item["expires_at"] == Decimal(1_900_000_000)
    assert tables.connection.put_attempts == 1
    assert next(iter(tables.connection.items.values())) == replacement.item


def test_expired_tampered_dynamodb_row_is_not_refreshed():
    tables = _dynamo_tables()
    replacement = fixture._fixture_dynamo_items(  # noqa: SLF001
        tables,
        fixture_id=FIXTURE_ID,
        expires_at=1_900_000_000,
    )[1]
    tampered = {
        **replacement.item,
        "tenant_id": OTHER_FIXTURE_ID,
        "expires_at": 1_700_000_000,
    }
    tables.connection.items[tables.connection._key(tampered)] = tampered  # noqa: SLF001

    with pytest.raises(RuntimeError, match="another owner"):
        fixture._put_fixture_item(  # noqa: SLF001
            replacement,
            now=1_800_000_000,
        )

    assert tables.connection.put_attempts == 0
    assert next(iter(tables.connection.items.values())) == tampered


@pytest.mark.parametrize(
    ("table_name", "specification_index"),
    (
        ("ticket", 0),
        ("connection", 1),
        ("subscription", 2),
    ),
)
def test_far_future_decimal_dynamodb_expiry_is_refused_before_any_put(
    table_name,
    specification_index,
):
    tables = _dynamo_tables()
    now = 1_800_000_000
    requested_expiry = now + fixture.FIXTURE_STATE_TTL_SECONDS
    specification = fixture._fixture_dynamo_items(  # noqa: SLF001
        tables,
        fixture_id=FIXTURE_ID,
        expires_at=requested_expiry,
    )[specification_index]
    table = getattr(tables, table_name)
    tampered = {
        **specification.item,
        "expires_at": Decimal(now + 10 * fixture.FIXTURE_STATE_TTL_SECONDS),
    }
    table.items[table._key(tampered)] = tampered  # noqa: SLF001

    with pytest.raises(RuntimeError, match="expiry exceeds requested lifetime"):
        fixture._provision_fixture_dynamo_state(  # noqa: SLF001
            tables,
            fixture_id=FIXTURE_ID,
            now=now,
        )

    assert sum(
        candidate.put_attempts
        for candidate in (tables.ticket, tables.connection, tables.subscription)
    ) == 0
    assert next(iter(table.items.values())) == tampered


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
            dynamodb_tables=_dynamo_tables(),
            database_url="postgresql://unused",
            fixture_id="00000000-0000-0000-0000-000000000002",
            user_pool_id=POOL_ID,
            issuer=ISSUER,
        )

    assert cognito.calls == []


@pytest.mark.parametrize(
    ("failure", "message"),
    (
        ("malformed_key", "primary key is invalid"),
        ("inactive_table", "table is not active"),
        ("extra_attribute_definition", "attributes are invalid"),
        ("unexpected_lsi", "local indexes are invalid"),
        ("inactive_index", "index tenant-id-index is not active"),
        ("backfilling_index", "index tenant-id-index is not active"),
        ("malformed_backfilling", "index tenant-id-index is not active"),
        ("missing_index", "index connection-id-index is missing"),
        ("extra_index", "indexes are invalid"),
        ("invalid_projection", "index tenant-id-index projection is invalid"),
        ("disabled_ttl", "TTL is invalid"),
        ("wrong_ttl_attribute", "TTL is invalid"),
    ),
)
def test_dynamodb_topology_preflight_fails_before_fixture_side_effects(
    monkeypatch,
    failure,
    message,
):
    tables = _dynamo_tables()

    def index(table, name):
        return next(
            item
            for item in table.description["GlobalSecondaryIndexes"]
            if item["IndexName"] == name
        )

    if failure == "malformed_key":
        tables.ticket.description["KeySchema"] = [
            {"AttributeName": "tenant_id", "KeyType": "HASH"}
        ]
    elif failure == "inactive_table":
        tables.ticket.description["TableStatus"] = "UPDATING"
    elif failure == "extra_attribute_definition":
        tables.ticket.description["AttributeDefinitions"].append(
            {"AttributeName": "unexpected", "AttributeType": "S"}
        )
    elif failure == "unexpected_lsi":
        tables.subscription.description["LocalSecondaryIndexes"] = [
            {
                "IndexName": "unexpected-local-index",
                "KeySchema": [
                    {"AttributeName": "topic_key", "KeyType": "HASH"},
                    {"AttributeName": "tenant_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ]
    elif failure == "inactive_index":
        index(tables.ticket, "tenant-id-index")["IndexStatus"] = "CREATING"
    elif failure == "backfilling_index":
        index(tables.ticket, "tenant-id-index")["Backfilling"] = True
    elif failure == "malformed_backfilling":
        index(tables.ticket, "tenant-id-index")["Backfilling"] = 0
    elif failure == "missing_index":
        tables.subscription.description["GlobalSecondaryIndexes"] = [
            item
            for item in tables.subscription.description["GlobalSecondaryIndexes"]
            if item["IndexName"] != "connection-id-index"
        ]
    elif failure == "extra_index":
        tables.ticket.description["GlobalSecondaryIndexes"].append(
            {
                "IndexName": "extra-active-index",
                "IndexStatus": "ACTIVE",
                "KeySchema": [
                    {"AttributeName": "tenant_id", "KeyType": "HASH"}
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        )
    elif failure == "invalid_projection":
        index(tables.connection, "tenant-id-index")["Projection"] = {
            "ProjectionType": "ALL"
        }
    elif failure == "disabled_ttl":
        tables.connection.ttl_description["TimeToLiveStatus"] = "DISABLED"
    elif failure == "wrong_ttl_attribute":
        tables.connection.ttl_description["AttributeName"] = "delete_after"
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(failure)

    monkeypatch.setattr(
        fixture.psycopg,
        "connect",
        lambda *args, **kwargs: pytest.fail("database connection attempted"),
    )
    monkeypatch.setattr(
        fixture.secrets,
        "token_urlsafe",
        lambda _size: pytest.fail("fixture identity generation attempted"),
    )
    cognito = FakeCognito([])

    with pytest.raises(RuntimeError, match=message):
        fixture.provision_fixture(
            cognito,
            dynamodb_tables=tables,
            database_url="postgresql://unused",
            fixture_id=FIXTURE_ID,
            user_pool_id=POOL_ID,
            issuer=ISSUER,
        )

    assert cognito.calls == []
    for table in (tables.ticket, tables.subscription, tables.connection):
        assert table.put_attempts == 0
        assert table.items == {}


def test_receipt_contains_no_password_or_principal_material(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)
    monkeypatch.setattr(fixture.secrets, "token_urlsafe", lambda _size: "fixture-secret")
    receipt = _provision(monkeypatch, database, cognito)

    rendered = json.dumps(fixture.asdict(receipt), sort_keys=True)
    assert "fixture-secret" not in rendered
    assert "principal_hash" not in rendered
    assert "provisioning_key" not in rendered


def test_fixture_inventory_has_exact_ttl_backed_realtime_rows(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)
    tables = _dynamo_tables()
    now = 1_800_000_000
    monkeypatch.setattr(fixture.time, "time", lambda: now)

    receipt = _provision(
        monkeypatch,
        database,
        cognito,
        dynamodb_tables=tables,
    )

    assert fixture.asdict(receipt.inventory) == {
        "tenant_rows": 1,
        "principal_mapping_rows": 2,
        "credential_locator_rows": 2,
        "cognito_users": 2,
        "managed_realtime_ticket_rows": 1,
        "managed_websocket_subscription_rows": 1,
        "managed_websocket_connection_rows": 1,
        "sha256": receipt.inventory.sha256,
    }
    assert len(receipt.inventory.sha256) == 64
    ticket = next(iter(tables.ticket.items.values()))
    connection = next(iter(tables.connection.items.values()))
    subscription = next(iter(tables.subscription.items.values()))
    assert fixture.FIXTURE_STATE_TTL_SECONDS == 24 * 60 * 60
    expiry = now + fixture.FIXTURE_STATE_TTL_SECONDS
    assert ticket["tenant_id"] == FIXTURE_ID
    assert ticket["redeem_before"] == ticket["session_expires_at"] == 0
    assert ticket["expires_at"] == expiry
    assert connection["tenant_id"] == FIXTURE_ID
    assert subscription["tenant_id"] == FIXTURE_ID
    assert connection["expires_at"] == subscription["expires_at"] == expiry
    assert {
        ticket["fixture_kind"],
        connection["fixture_kind"],
        subscription["fixture_kind"],
    } == {"lifecycle_cleanup_only"}
    assert subscription["connection_id"] == connection["connection_id"]
    assert subscription["topic_key"].startswith(f"tenant:{FIXTURE_ID}:")

    rendered = json.dumps(fixture.asdict(receipt), sort_keys=True)
    assert ticket["ticket_digest"] not in rendered
    assert connection["connection_id"] not in rendered
    assert subscription["topic_key"] not in rendered


def test_fixture_rejects_foreign_dynamo_key_without_overwriting_it(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)
    tables = _dynamo_tables()
    specification = fixture._fixture_dynamo_items(  # noqa: SLF001
        tables,
        fixture_id=FIXTURE_ID,
        expires_at=1_900_000_000,
    )[0]
    foreign = {**specification.item, "tenant_id": OTHER_FIXTURE_ID}
    tables.ticket.items[tables.ticket._key(foreign)] = foreign  # noqa: SLF001

    with pytest.raises(RuntimeError, match="another owner"):
        _provision(
            monkeypatch,
            database,
            cognito,
            dynamodb_tables=tables,
        )

    assert next(iter(tables.ticket.items.values())) == foreign


def test_fixture_refuses_public_identity_drift(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)
    before = PublicIdentitySentinel(1, 2, 2, "a" * 64)
    after = PublicIdentitySentinel(1, 2, 2, "b" * 64)
    sentinels = iter((before, after))
    monkeypatch.setattr(
        fixture,
        "public_demo_identity_sentinel",
        lambda _connection: next(sentinels),
    )

    with pytest.raises(RuntimeError, match="public-demo identity changed"):
        _provision(monkeypatch, database, cognito)


def test_fixture_refuses_incomplete_public_identity_before_side_effects(monkeypatch):
    database = FakeDatabase()
    database.public_locators = database.public_locators[:1]
    cognito = FakeCognito(database.events)

    with pytest.raises(LifecycleConflictError, match="lacks an active credential locator"):
        _provision(monkeypatch, database, cognito)

    assert database.tenants == {}
    assert database.locators == {}
    assert cognito.calls == []


def test_public_identity_receipt_is_bounded_and_opaque(monkeypatch):
    database = FakeDatabase()
    cognito = FakeCognito(database.events)

    receipt = _provision(monkeypatch, database, cognito)

    assert receipt.public_identity.tenant_rows == 1
    assert receipt.public_identity.principal_mapping_rows == 2
    assert receipt.public_identity.credential_locator_rows == 2
    rendered = json.dumps(fixture.asdict(receipt), sort_keys=True)
    assert "public-operator" not in rendered
    assert "public-viewer" not in rendered
    assert "a" * 64 not in rendered
