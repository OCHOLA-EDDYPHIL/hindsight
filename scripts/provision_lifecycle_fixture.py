"""Provision one invocation-owned tenant fixture for lifecycle operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import secrets
import sys
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

import boto3
import psycopg
from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.db import database_url_with_tls_roots  # noqa: E402


MANAGED_GROUPS = frozenset({"viewer", "operator"})


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
class FixtureReceipt:
    fixture_id: str
    tenant_id: str
    usernames: tuple[str, ...]
    status: str = "ready_for_lifecycle"


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


def provision_fixture(
    client: Any,
    *,
    database_url: str,
    fixture_id: str | UUID,
    user_pool_id: str,
    issuer: str,
) -> FixtureReceipt:
    """Create or verify one fixture whose identities are scoped to its invocation."""

    normalized_fixture = _fixture_uuid(fixture_id)
    normalized_pool = _required_value(user_pool_id, "user pool id")
    normalized_issuer = _required_value(issuer, "issuer")
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
    return FixtureReceipt(
        fixture_id=normalized_fixture,
        tenant_id=normalized_fixture,
        usernames=tuple(identity.username for identity in identities),
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
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("cognito-idp", config=aws_client_config(read_timeout=10))
    try:
        receipt = provision_fixture(
            client,
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
