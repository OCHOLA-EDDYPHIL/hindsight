"""Provision one invocation-owned tenant fixture for lifecycle operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID

import boto3
import psycopg
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.db import database_url_with_tls_roots  # noqa: E402
from hindsight.lifecycle import (  # noqa: E402
    PublicIdentitySentinel,
    canonical_json_bytes,
    public_demo_identity_sentinel,
)


MANAGED_GROUPS = frozenset({"viewer", "operator"})
TICKET_TABLE_ENV = "HINDSIGHT_REALTIME_TICKET_TABLE"
SUBSCRIPTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_SUBSCRIPTION_TABLE"
CONNECTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_CONNECTION_TABLE"
FIXTURE_STATE_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class FixtureIdentity:
    role: str
    username: str
    password: str
    provisioning_key: str


@dataclass(frozen=True)
class PrincipalMapping:
    role: str
    username: str
    provisioning_key: str
    principal_hash: str


@dataclass(frozen=True)
class FixtureDynamoTables:
    ticket: Any
    subscription: Any
    connection: Any


@dataclass(frozen=True)
class FixtureInventory:
    tenant_rows: int
    principal_mapping_rows: int
    credential_locator_rows: int
    cognito_users: int
    managed_realtime_ticket_rows: int
    managed_websocket_subscription_rows: int
    managed_websocket_connection_rows: int
    sha256: str


@dataclass(frozen=True)
class FixtureReceipt:
    fixture_id: str
    tenant_id: str
    usernames: tuple[str, ...]
    inventory: FixtureInventory
    public_identity: PublicIdentitySentinel
    status: str = "ready_for_lifecycle"


@dataclass(frozen=True)
class _FixtureDynamoItem:
    name: str
    table: Any
    key: Mapping[str, Any]
    item: Mapping[str, Any]
    temporal_fields: tuple[str, ...]


@dataclass(frozen=True)
class _DynamoIndexContract:
    key_schema: tuple[tuple[str, str], ...]
    projection_type: str


@dataclass(frozen=True)
class _DynamoTableContract:
    name: str
    table: Any
    key_schema: tuple[tuple[str, str], ...]
    attribute_types: Mapping[str, str]
    indexes: Mapping[str, _DynamoIndexContract]


def _fixture_uuid(value: str | UUID) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("fixture id must be a UUID") from exc
    if parsed.version != 4:
        raise ValueError("fixture id must be an invocation-generated UUIDv4")
    return str(parsed)


def _required_value(value: str | None, label: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def _principal_hash(*, issuer: str, subject: str) -> str:
    return hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()


def _provisioning_key(*, issuer: str, fixture_id: str, role: str) -> str:
    return hashlib.sha256(f"{issuer}\0lifecycle-fixture\0{fixture_id}\0{role}".encode()).hexdigest()


def _identity_plan(*, issuer: str, fixture_id: str) -> tuple[FixtureIdentity, ...]:
    compact = UUID(fixture_id).hex
    identities = []
    for role in sorted(MANAGED_GROUPS):
        identities.append(
            FixtureIdentity(
                role=role,
                username=f"hindsight-lifecycle-{compact}-{role}",
                password=f"{secrets.token_urlsafe(36)}aA1!",
                provisioning_key=_provisioning_key(
                    issuer=issuer,
                    fixture_id=fixture_id,
                    role=role,
                ),
            )
        )
    return tuple(identities)


def _bind_tenant(connection: psycopg.Connection, tenant_id: str) -> None:
    connection.execute(
        "SELECT set_config('hindsight.tenant_id', %s, true)",
        (tenant_id,),
    )


def _reserve_fixture(
    connection: psycopg.Connection,
    *,
    fixture_id: str,
    user_pool_id: str,
    identities: tuple[FixtureIdentity, ...],
) -> None:
    slug = f"lifecycle-{UUID(fixture_id).hex}"
    _bind_tenant(connection, fixture_id)
    connection.execute(
        """
            INSERT INTO tenants (id, slug, tenant_kind)
            VALUES (%s, %s, 'diagnostic')
            ON CONFLICT (id) DO NOTHING
        """,
        (fixture_id, slug),
    )
    tenant = connection.execute(
        """
            SELECT slug, tenant_kind, status
            FROM tenants
            WHERE id = %s
            FOR UPDATE
        """,
        (fixture_id,),
    ).fetchone()
    if tenant is None or tuple(str(value) for value in tenant) != (
        slug,
        "diagnostic",
        "active",
    ):
        raise RuntimeError("fixture tenant identity is already occupied")

    for identity in identities:
        connection.execute(
            """
                INSERT INTO product_credential_locators (
                    provisioning_key, tenant_id, user_pool_id,
                    cognito_username, role, status
                ) VALUES (%s, %s, %s, %s, %s, 'reserved')
                ON CONFLICT (user_pool_id, cognito_username) DO NOTHING
            """,
            (
                identity.provisioning_key,
                fixture_id,
                user_pool_id,
                identity.username,
                identity.role,
            ),
        )
        locator = connection.execute(
            """
                SELECT tenant_id, provisioning_key, role, status, principal_hash
                FROM product_credential_locators
                WHERE user_pool_id = %s AND cognito_username = %s
                FOR UPDATE
            """,
            (user_pool_id, identity.username),
        ).fetchone()
        if locator is None or tuple(str(value) for value in locator[:3]) != (
            fixture_id,
            identity.provisioning_key,
            identity.role,
        ):
            raise RuntimeError("fixture credential locator is already occupied")
        if str(locator[3]) not in {"reserved", "active"}:
            raise RuntimeError("fixture credential locator has an invalid state")


def _get_or_create_user(
    client: Any,
    *,
    user_pool_id: str,
    identity: FixtureIdentity,
) -> dict[str, Any]:
    try:
        return client.admin_get_user(
            UserPoolId=user_pool_id,
            Username=identity.username,
        )
    except client.exceptions.UserNotFoundException:
        pass
    response = client.admin_create_user(
        UserPoolId=user_pool_id,
        Username=identity.username,
        TemporaryPassword=identity.password,
        MessageAction="SUPPRESS",
    )
    user = response.get("User")
    if not isinstance(user, dict):
        raise RuntimeError("Cognito did not return the fixture user")
    return user


def _required_subject(user: dict[str, Any]) -> str:
    attributes = user.get("UserAttributes")
    if not isinstance(attributes, list):
        attributes = user.get("Attributes", [])
    subjects = [
        attribute.get("Value")
        for attribute in attributes
        if attribute.get("Name") == "sub" and attribute.get("Value")
    ]
    if len(subjects) != 1:
        raise RuntimeError("Cognito fixture user lacks one subject identifier")
    return str(subjects[0])


def _set_managed_group(
    client: Any,
    *,
    user_pool_id: str,
    username: str,
    role: str,
) -> None:
    groups = set()
    paginator = client.get_paginator("admin_list_groups_for_user")
    for response in paginator.paginate(
        UserPoolId=user_pool_id,
        Username=username,
        PaginationConfig={"PageSize": 60},
    ):
        groups.update(
            str(group["GroupName"])
            for group in response.get("Groups", [])
            if group.get("GroupName")
        )
    for group in sorted((groups & MANAGED_GROUPS) - {role}):
        client.admin_remove_user_from_group(
            UserPoolId=user_pool_id,
            Username=username,
            GroupName=group,
        )
    if role not in groups:
        client.admin_add_user_to_group(
            UserPoolId=user_pool_id,
            Username=username,
            GroupName=role,
        )


def _provision_identities(
    client: Any,
    *,
    user_pool_id: str,
    issuer: str,
    identities: tuple[FixtureIdentity, ...],
) -> tuple[PrincipalMapping, ...]:
    mappings = []
    for identity in identities:
        user = _get_or_create_user(
            client,
            user_pool_id=user_pool_id,
            identity=identity,
        )
        client.admin_set_user_password(
            UserPoolId=user_pool_id,
            Username=identity.username,
            Password=identity.password,
            Permanent=True,
        )
        _set_managed_group(
            client,
            user_pool_id=user_pool_id,
            username=identity.username,
            role=identity.role,
        )
        mappings.append(
            PrincipalMapping(
                role=identity.role,
                username=identity.username,
                provisioning_key=identity.provisioning_key,
                principal_hash=_principal_hash(
                    issuer=issuer,
                    subject=_required_subject(user),
                ),
            )
        )
    return tuple(mappings)


def _persist_mappings(
    connection: psycopg.Connection,
    *,
    fixture_id: str,
    user_pool_id: str,
    mappings: tuple[PrincipalMapping, ...],
) -> None:
    _bind_tenant(connection, fixture_id)
    for mapping in mappings:
        connection.execute(
            """
                INSERT INTO product_principal_roles (
                    principal_hash, provisioning_key, tenant_id, role, status
                ) VALUES (%s, %s, %s, %s, 'active')
                ON CONFLICT (provisioning_key) DO NOTHING
            """,
            (
                mapping.principal_hash,
                mapping.provisioning_key,
                fixture_id,
                mapping.role,
            ),
        )
        principal = connection.execute(
            """
                SELECT principal_hash, tenant_id, role, status
                FROM product_principal_roles
                WHERE provisioning_key = %s
                FOR UPDATE
            """,
            (mapping.provisioning_key,),
        ).fetchone()
        if principal is None or tuple(str(value) for value in principal) != (
            mapping.principal_hash,
            fixture_id,
            mapping.role,
            "active",
        ):
            raise RuntimeError("fixture principal mapping is already occupied")

        locator = connection.execute(
            """
                SELECT principal_hash, status
                FROM product_credential_locators
                WHERE tenant_id = %s
                  AND provisioning_key = %s
                  AND user_pool_id = %s
                  AND cognito_username = %s
                  AND role = %s
                FOR UPDATE
            """,
            (
                fixture_id,
                mapping.provisioning_key,
                user_pool_id,
                mapping.username,
                mapping.role,
            ),
        ).fetchone()
        if locator is None:
            raise RuntimeError("fixture credential locator is missing")
        if str(locator[1]) == "active" and str(locator[0]) != mapping.principal_hash:
            raise RuntimeError("fixture credential locator belongs to another principal")
        activated = connection.execute(
            """
                UPDATE product_credential_locators
                SET principal_hash = %s, status = 'active', updated_at = now()
                WHERE tenant_id = %s
                  AND provisioning_key = %s
                  AND user_pool_id = %s
                  AND cognito_username = %s
                  AND role = %s
                RETURNING id
            """,
            (
                mapping.principal_hash,
                fixture_id,
                mapping.provisioning_key,
                user_pool_id,
                mapping.username,
                mapping.role,
            ),
        ).fetchone()
        if activated is None:
            raise RuntimeError("fixture credential locator could not be activated")


def _fixture_dynamo_items(
    tables: FixtureDynamoTables,
    *,
    fixture_id: str,
    expires_at: int,
) -> tuple[_FixtureDynamoItem, ...]:
    compact = UUID(fixture_id).hex
    ticket_digest = hashlib.sha256(
        f"hindsight-lifecycle-fixture-state\0{fixture_id}\0ticket".encode()
    ).hexdigest()
    connection_id = "L" + hashlib.sha256(
        f"hindsight-lifecycle-fixture-state\0{fixture_id}\0connection".encode()
    ).hexdigest()[:15]
    topic_key = f"tenant:{fixture_id}:namespace:lifecycle.fixture"
    principal_id = f"lifecycle-fixture:{compact}"
    return (
        _FixtureDynamoItem(
            name="realtime_ticket",
            table=tables.ticket,
            key={"ticket_digest": ticket_digest},
            item={
                "ticket_digest": ticket_digest,
                "tenant_id": fixture_id,
                "access_class": "viewer",
                "principal_id": principal_id,
                # This cleanup-only row has no issued bearer and remains
                # unredeemable while its independent cleanup TTL is active.
                "redeem_before": 0,
                "session_expires_at": 0,
                "expires_at": expires_at,
                "fixture_kind": "lifecycle_cleanup_only",
            },
            temporal_fields=("expires_at",),
        ),
        _FixtureDynamoItem(
            name="websocket_connection",
            table=tables.connection,
            key={"connection_id": connection_id},
            item={
                "connection_id": connection_id,
                "namespace": "lifecycle.fixture",
                "run_id": "",
                "tenant_id": fixture_id,
                "access_class": "viewer",
                "principal_id": principal_id,
                "expires_at": expires_at,
                "fixture_kind": "lifecycle_cleanup_only",
            },
            temporal_fields=("expires_at",),
        ),
        _FixtureDynamoItem(
            name="websocket_subscription",
            table=tables.subscription,
            key={"topic_key": topic_key, "connection_id": connection_id},
            item={
                "topic_key": topic_key,
                "connection_id": connection_id,
                "tenant_id": fixture_id,
                "expires_at": expires_at,
                "fixture_kind": "lifecycle_cleanup_only",
            },
            temporal_fields=("expires_at",),
        ),
    )


def _dynamo_table_contracts(
    tables: FixtureDynamoTables,
) -> tuple[_DynamoTableContract, ...]:
    tenant_index = _DynamoIndexContract(
        key_schema=(("HASH", "tenant_id"),),
        projection_type="KEYS_ONLY",
    )
    return (
        _DynamoTableContract(
            name="realtime_ticket",
            table=tables.ticket,
            key_schema=(("HASH", "ticket_digest"),),
            attribute_types={"ticket_digest": "S", "tenant_id": "S"},
            indexes={"tenant-id-index": tenant_index},
        ),
        _DynamoTableContract(
            name="websocket_subscription",
            table=tables.subscription,
            key_schema=(("HASH", "topic_key"), ("RANGE", "connection_id")),
            attribute_types={
                "connection_id": "S",
                "tenant_id": "S",
                "topic_key": "S",
            },
            indexes={
                "connection-id-index": _DynamoIndexContract(
                    key_schema=(("HASH", "connection_id"),),
                    projection_type="ALL",
                ),
                "tenant-id-index": tenant_index,
            },
        ),
        _DynamoTableContract(
            name="websocket_connection",
            table=tables.connection,
            key_schema=(("HASH", "connection_id"),),
            attribute_types={"connection_id": "S", "tenant_id": "S"},
            indexes={"tenant-id-index": tenant_index},
        ),
    )


def _described_key_schema(value: Any) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, list):
        return None
    keys = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"AttributeName", "KeyType"}:
            return None
        key_type = item.get("KeyType")
        attribute_name = item.get("AttributeName")
        if key_type not in {"HASH", "RANGE"} or not isinstance(attribute_name, str):
            return None
        keys.append((str(key_type), attribute_name))
    return tuple(sorted(keys, key=lambda key: (key[0] != "HASH", key[1])))


def _validate_dynamo_table(contract: _DynamoTableContract) -> None:
    table_name = getattr(contract.table, "name", None)
    if not isinstance(table_name, str) or not table_name:
        raise RuntimeError(f"{contract.name} DynamoDB table name is unavailable")
    client = contract.table.meta.client
    response = client.describe_table(TableName=table_name)
    if not isinstance(response, Mapping):
        raise RuntimeError(f"{contract.name} DynamoDB table description is invalid")
    table = response.get("Table")
    if not isinstance(table, Mapping):
        raise RuntimeError(f"{contract.name} DynamoDB table description is invalid")
    if table.get("TableName") != table_name or table.get("TableStatus") != "ACTIVE":
        raise RuntimeError(f"{contract.name} DynamoDB table is not active")
    if _described_key_schema(table.get("KeySchema")) != contract.key_schema:
        raise RuntimeError(f"{contract.name} DynamoDB primary key is invalid")

    definitions = table.get("AttributeDefinitions")
    if not isinstance(definitions, list):
        raise RuntimeError(f"{contract.name} DynamoDB attributes are invalid")
    attribute_types: dict[str, str] = {}
    for definition in definitions:
        if not isinstance(definition, Mapping):
            raise RuntimeError(f"{contract.name} DynamoDB attributes are invalid")
        attribute_name = definition.get("AttributeName")
        attribute_type = definition.get("AttributeType")
        if (
            not isinstance(attribute_name, str)
            or attribute_type not in {"S", "N", "B"}
            or attribute_name in attribute_types
        ):
            raise RuntimeError(f"{contract.name} DynamoDB attributes are invalid")
        attribute_types[attribute_name] = str(attribute_type)
    if attribute_types != dict(contract.attribute_types):
        raise RuntimeError(f"{contract.name} DynamoDB attributes are invalid")

    local_indexes = table.get("LocalSecondaryIndexes", [])
    if not isinstance(local_indexes, list) or local_indexes:
        raise RuntimeError(f"{contract.name} DynamoDB local indexes are invalid")

    described_indexes = table.get("GlobalSecondaryIndexes")
    if not isinstance(described_indexes, list):
        raise RuntimeError(f"{contract.name} DynamoDB indexes are invalid")
    indexes: dict[str, Mapping[str, Any]] = {}
    for index in described_indexes:
        if not isinstance(index, Mapping) or not isinstance(index.get("IndexName"), str):
            raise RuntimeError(f"{contract.name} DynamoDB indexes are invalid")
        index_name = str(index["IndexName"])
        if index_name in indexes:
            raise RuntimeError(f"{contract.name} DynamoDB indexes are invalid")
        indexes[index_name] = index
    for index_name, expected in contract.indexes.items():
        index = indexes.get(index_name)
        if index is None:
            raise RuntimeError(f"{contract.name} DynamoDB index {index_name} is missing")
        backfilling = index.get("Backfilling")
        if index.get("IndexStatus") != "ACTIVE" or (
            backfilling is not None and backfilling is not False
        ):
            raise RuntimeError(f"{contract.name} DynamoDB index {index_name} is not active")
        if _described_key_schema(index.get("KeySchema")) != expected.key_schema:
            raise RuntimeError(f"{contract.name} DynamoDB index {index_name} key is invalid")
        projection = index.get("Projection")
        if (
            not isinstance(projection, Mapping)
            or set(projection) != {"ProjectionType"}
            or projection.get("ProjectionType") != expected.projection_type
        ):
            raise RuntimeError(
                f"{contract.name} DynamoDB index {index_name} projection is invalid"
            )
    if set(indexes) != set(contract.indexes):
        raise RuntimeError(f"{contract.name} DynamoDB indexes are invalid")

    ttl_response = client.describe_time_to_live(TableName=table_name)
    if not isinstance(ttl_response, Mapping):
        raise RuntimeError(f"{contract.name} DynamoDB TTL is invalid")
    ttl = ttl_response.get("TimeToLiveDescription")
    if (
        not isinstance(ttl, Mapping)
        or ttl.get("TimeToLiveStatus") != "ENABLED"
        or ttl.get("AttributeName") != "expires_at"
    ):
        raise RuntimeError(f"{contract.name} DynamoDB TTL is invalid")


def _validate_fixture_dynamo_tables(tables: FixtureDynamoTables) -> None:
    for contract in _dynamo_table_contracts(tables):
        _validate_dynamo_table(contract)


def _read_fixture_item(specification: _FixtureDynamoItem) -> Mapping[str, Any] | None:
    response = specification.table.get_item(
        Key=dict(specification.key),
        ConsistentRead=True,
    )
    item = response.get("Item")
    return item if isinstance(item, Mapping) else None


def _decoded_fixture_expiration(specification: _FixtureDynamoItem, value: Any) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{specification.name} fixture expiry is invalid")
    if isinstance(value, int):
        return value
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
    ):
        return int(value)
    raise RuntimeError(f"{specification.name} fixture expiry is invalid")


def _validate_fixture_item(
    specification: _FixtureDynamoItem,
    item: Mapping[str, Any] | None,
    *,
    now: int,
    allow_expired: bool = False,
) -> Mapping[str, Any]:
    if item is None or set(item) != set(specification.item):
        raise RuntimeError(f"{specification.name} fixture row is missing or has drifted")
    temporal = set(specification.temporal_fields)
    if any(
        item.get(field) != value
        for field, value in specification.item.items()
        if field not in temporal
    ):
        raise RuntimeError(f"{specification.name} fixture row belongs to another owner")
    expirations = []
    for field in specification.temporal_fields:
        expiration = _decoded_fixture_expiration(specification, item.get(field))
        requested_expiration = _decoded_fixture_expiration(
            specification,
            specification.item[field],
        )
        if expiration > requested_expiration:
            raise RuntimeError(
                f"{specification.name} fixture expiry exceeds requested lifetime"
            )
        expirations.append(expiration)
    if len(set(expirations)) != 1:
        raise RuntimeError(f"{specification.name} fixture expiry is inconsistent")
    if not allow_expired and expirations[0] <= now:
        raise RuntimeError(f"{specification.name} fixture row is expired")
    return item


def _fixture_expiration(
    specification: _FixtureDynamoItem,
    item: Mapping[str, Any],
) -> int:
    value = item[specification.temporal_fields[0]]
    return int(value)


def _and_conditions(conditions: list[Any]) -> Any:
    condition = conditions[0]
    for additional_condition in conditions[1:]:
        condition &= additional_condition
    return condition


def _put_fixture_item(
    specification: _FixtureDynamoItem,
    *,
    now: int,
) -> Mapping[str, Any]:
    existing = _read_fixture_item(specification)
    if existing is not None:
        validated = _validate_fixture_item(
            specification,
            existing,
            now=now,
            allow_expired=True,
        )
        if _fixture_expiration(specification, validated) > now:
            return validated
        replacement_condition = _and_conditions(
            [Attr(field).eq(value) for field, value in sorted(validated.items())]
        )
        try:
            specification.table.put_item(
                Item=dict(specification.item),
                ConditionExpression=replacement_condition,
            )
        except specification.table.meta.client.exceptions.ConditionalCheckFailedException:
            pass
        return _validate_fixture_item(
            specification,
            _read_fixture_item(specification),
            now=now,
        )

    condition = _and_conditions(
        [Attr(field).not_exists() for field in specification.key]
    )
    try:
        specification.table.put_item(
            Item=dict(specification.item),
            ConditionExpression=condition,
        )
    except specification.table.meta.client.exceptions.ConditionalCheckFailedException:
        pass
    return _validate_fixture_item(
        specification,
        _read_fixture_item(specification),
        now=now,
    )


def _provision_fixture_dynamo_state(
    tables: FixtureDynamoTables,
    *,
    fixture_id: str,
    now: int,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    expires_at = now + FIXTURE_STATE_TTL_SECONDS
    specifications = _fixture_dynamo_items(
        tables,
        fixture_id=fixture_id,
        expires_at=expires_at,
    )
    for specification in specifications:
        existing = _read_fixture_item(specification)
        if existing is not None:
            _validate_fixture_item(
                specification,
                existing,
                now=now,
                allow_expired=True,
            )
    return tuple(
        (specification.name, _put_fixture_item(specification, now=now))
        for specification in specifications
    )


def _fixture_database_rows(
    connection: psycopg.Connection,
    *,
    fixture_id: str,
) -> tuple[tuple[Any, ...], tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...]]:
    with connection.transaction():
        _bind_tenant(connection, fixture_id)
        tenant = connection.execute(
            """
                SELECT id, slug, tenant_kind, status
                FROM tenants WHERE id = %s ORDER BY id
            """,
            (fixture_id,),
        ).fetchall()
        principals = connection.execute(
            """
                SELECT id, principal_hash, provisioning_key, tenant_id, role, status
                FROM product_principal_roles
                WHERE tenant_id = %s
                ORDER BY provisioning_key, principal_hash, id
            """,
            (fixture_id,),
        ).fetchall()
        locators = connection.execute(
            """
                SELECT id, provisioning_key, tenant_id, user_pool_id,
                       cognito_username, role, principal_hash, status
                FROM product_credential_locators
                WHERE tenant_id = %s
                ORDER BY provisioning_key, user_pool_id, cognito_username, id
            """,
            (fixture_id,),
        ).fetchall()
    if len(tenant) != 1 or len(principals) != 2 or len(locators) != 2:
        raise RuntimeError("fixture database inventory is not exact")
    return tuple(tenant[0]), tuple(tuple(row) for row in principals), tuple(
        tuple(row) for row in locators
    )


def _fixture_inventory(
    connection: psycopg.Connection,
    *,
    fixture_id: str,
    mappings: tuple[PrincipalMapping, ...],
    dynamo_items: tuple[tuple[str, Mapping[str, Any]], ...],
) -> FixtureInventory:
    tenant, principals, locators = _fixture_database_rows(
        connection,
        fixture_id=fixture_id,
    )
    by_name = {name: item for name, item in dynamo_items}
    if set(by_name) != {
        "realtime_ticket",
        "websocket_subscription",
        "websocket_connection",
    }:
        raise RuntimeError("fixture realtime inventory is not exact")
    if len(mappings) != 2:
        raise RuntimeError("fixture Cognito inventory is not exact")
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "cognito_users": [asdict(mapping) for mapping in mappings],
                "credential_locators": locators,
                "dynamodb": by_name,
                "principal_mappings": principals,
                "tenant": tenant,
            }
        )
    ).hexdigest()
    return FixtureInventory(
        tenant_rows=1,
        principal_mapping_rows=len(principals),
        credential_locator_rows=len(locators),
        cognito_users=len(mappings),
        managed_realtime_ticket_rows=1,
        managed_websocket_subscription_rows=1,
        managed_websocket_connection_rows=1,
        sha256=digest,
    )


def provision_fixture(
    client: Any,
    *,
    dynamodb_tables: FixtureDynamoTables,
    database_url: str,
    fixture_id: str | UUID,
    user_pool_id: str,
    issuer: str,
) -> FixtureReceipt:
    """Create or verify one fixture whose identities are scoped to its invocation."""

    normalized_fixture = _fixture_uuid(fixture_id)
    normalized_pool = _required_value(user_pool_id, "user pool id")
    normalized_issuer = _required_value(issuer, "issuer")
    _validate_fixture_dynamo_tables(dynamodb_tables)
    identities = _identity_plan(
        issuer=normalized_issuer,
        fixture_id=normalized_fixture,
    )
    with psycopg.connect(
        database_url_with_tls_roots(database_url),
        autocommit=True,
        connect_timeout=5,
        application_name="hindsight-lifecycle-fixture-provisioner",
    ) as connection:
        public_identity_before = public_demo_identity_sentinel(connection)
        with connection.transaction():
            _reserve_fixture(
                connection,
                fixture_id=normalized_fixture,
                user_pool_id=normalized_pool,
                identities=identities,
            )

        with connection.transaction():
            _bind_tenant(connection, normalized_fixture)
            tenant = connection.execute(
                "SELECT status FROM tenants WHERE id = %s FOR UPDATE",
                (normalized_fixture,),
            ).fetchone()
            if tenant is None or str(tenant[0]) != "active":
                raise RuntimeError("fixture tenant is not active")
            mappings = _provision_identities(
                client,
                user_pool_id=normalized_pool,
                issuer=normalized_issuer,
                identities=identities,
            )
            _persist_mappings(
                connection,
                fixture_id=normalized_fixture,
                user_pool_id=normalized_pool,
                mappings=mappings,
            )
        fixture_now = int(time.time())
        dynamo_items = _provision_fixture_dynamo_state(
            dynamodb_tables,
            fixture_id=normalized_fixture,
            now=fixture_now,
        )
        inventory = _fixture_inventory(
            connection,
            fixture_id=normalized_fixture,
            mappings=mappings,
            dynamo_items=dynamo_items,
        )
        public_identity_after = public_demo_identity_sentinel(connection)
        if public_identity_after != public_identity_before:
            raise RuntimeError("public-demo identity changed during fixture provisioning")
    return FixtureReceipt(
        fixture_id=normalized_fixture,
        tenant_id=normalized_fixture,
        usernames=tuple(identity.username for identity in identities),
        inventory=inventory,
        public_identity=public_identity_after,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--issuer", required=True)
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("lifecycle fixture input is invalid: DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        config = aws_client_config(read_timeout=10)
        client = session.client("cognito-idp", config=config)
        dynamodb = session.resource("dynamodb", config=config)
        dynamodb_tables = FixtureDynamoTables(
            ticket=dynamodb.Table(
                _required_value(os.environ.get(TICKET_TABLE_ENV), TICKET_TABLE_ENV)
            ),
            subscription=dynamodb.Table(
                _required_value(os.environ.get(SUBSCRIPTION_TABLE_ENV), SUBSCRIPTION_TABLE_ENV)
            ),
            connection=dynamodb.Table(
                _required_value(os.environ.get(CONNECTION_TABLE_ENV), CONNECTION_TABLE_ENV)
            ),
        )
        receipt = provision_fixture(
            client,
            dynamodb_tables=dynamodb_tables,
            database_url=database_url,
            fixture_id=args.fixture_id,
            user_pool_id=args.user_pool_id,
            issuer=args.issuer,
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "AwsClientError")
        print(f"lifecycle fixture AWS operation failed: {code}", file=sys.stderr)
        return 3
    except psycopg.Error:
        print("lifecycle fixture database operation failed", file=sys.stderr)
        return 4
    except (RuntimeError, ValueError) as exc:
        print(f"lifecycle fixture provisioning refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(receipt), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
