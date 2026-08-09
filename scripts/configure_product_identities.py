"""Provision the hosted viewer/operator identities and opaque database mappings."""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys
from dataclasses import dataclass
from typing import Any

import boto3
import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.db import database_url_with_tls_roots  # noqa: E402
from hindsight.tenant import normalize_tenant_id  # noqa: E402

MANAGED_GROUPS = frozenset({"viewer", "operator"})


@dataclass(frozen=True)
class DesiredIdentity:
    username: str
    password: str
    role: str


@dataclass(frozen=True)
class PrincipalMapping:
    principal_hash: str
    provisioning_key: str
    role: str
    user_pool_id: str
    username: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--viewer-username", default="hindsight-viewer")
    parser.add_argument("--operator-username", default="hindsight-operator")
    parser.add_argument("--viewer-password-env", default="HINDSIGHT_VIEWER_PASSWORD")
    parser.add_argument("--operator-password-env", default="HINDSIGHT_OPERATOR_PASSWORD")
    args = parser.parse_args()
    load_dotenv()

    database_url = _required_environment("DATABASE_URL")
    desired = (
        DesiredIdentity(
            username=_required_value(args.viewer_username, "viewer username"),
            password=_required_environment(args.viewer_password_env),
            role="viewer",
        ),
        DesiredIdentity(
            username=_required_value(args.operator_username, "operator username"),
            password=_required_environment(args.operator_password_env),
            role="operator",
        ),
    )
    if desired[0].username == desired[1].username:
        parser.error("viewer and operator usernames must be distinct")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("cognito-idp", config=aws_client_config(read_timeout=10))
    mappings = configure_product_identities(
        client,
        database_url=database_url,
        tenant_id=normalize_tenant_id(args.tenant_id),
        user_pool_id=args.user_pool_id,
        issuer=_required_value(args.issuer, "issuer"),
        desired=desired,
    )
    print(f"product identities ready: {len(mappings)} opaque principal mappings")


def configure_product_identities(
    client: Any,
    *,
    database_url: str,
    tenant_id: str,
    user_pool_id: str,
    issuer: str,
    desired: tuple[DesiredIdentity, ...],
) -> tuple[PrincipalMapping, ...]:
    """Provision users only after durable locators and an active-tenant lock."""

    normalized_tenant = normalize_tenant_id(tenant_id)
    normalized_pool = _required_value(user_pool_id, "user pool id")
    normalized_issuer = _required_value(issuer, "issuer")
    if not desired:
        raise ValueError("at least one product identity is required")
    intents: list[tuple[str, str, str]] = []
    for identity in desired:
        if identity.role not in MANAGED_GROUPS:
            raise ValueError("product identity role is invalid")
        username = _required_value(identity.username, "product identity username")
        if username != identity.username:
            raise ValueError(
                "product identity username cannot have leading or trailing whitespace"
            )
        if len(username) > 128:
            raise ValueError("product identity username is too long")
        intents.append(
            (
                _provisioning_key(issuer=normalized_issuer, role=identity.role),
                username,
                identity.role,
            )
        )
    if len({intent[1].casefold() for intent in intents}) != len(intents):
        raise ValueError("product identity usernames must be distinct")

    with psycopg.connect(
        database_url_with_tls_roots(database_url),
        autocommit=True,
        connect_timeout=5,
        application_name="hindsight-product-identity-provisioner",
    ) as connection:
        # This transaction must commit before the first Cognito call. If the
        # process stops after creating a user, purge still has a direct locator.
        with connection.transaction():
            _lock_active_tenant(connection, tenant_id=normalized_tenant)
            _reserve_credential_locators(
                connection,
                tenant_id=normalized_tenant,
                user_pool_id=normalized_pool,
                intents=intents,
            )

        # Holding the tenant row prevents begin_export from archiving the
        # tenant between the final active-state check and the Cognito writes.
        with connection.transaction():
            _lock_active_tenant(connection, tenant_id=normalized_tenant)
            mappings = _provision_cognito_identities(
                client,
                user_pool_id=normalized_pool,
                issuer=normalized_issuer,
                desired=desired,
            )
            _persist_principal_mappings(
                connection,
                tenant_id=normalized_tenant,
                mappings=mappings,
            )
    return mappings


def _provision_cognito_identities(
    client: Any,
    *,
    user_pool_id: str,
    issuer: str,
    desired: tuple[DesiredIdentity, ...],
) -> tuple[PrincipalMapping, ...]:
    mappings = []
    for identity in desired:
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
        subject = _required_subject(user)
        mappings.append(
            PrincipalMapping(
                principal_hash=_principal_hash(issuer=issuer, subject=subject),
                provisioning_key=_provisioning_key(
                    issuer=issuer, role=identity.role
                ),
                role=identity.role,
                user_pool_id=user_pool_id,
                username=identity.username,
            )
        )
    return tuple(mappings)


def _persist_principal_mappings(
    connection: psycopg.Connection,
    *,
    tenant_id: str,
    mappings: tuple[PrincipalMapping, ...],
) -> None:
    if not mappings:
        raise ValueError("at least one product identity mapping is required")
    for mapping in mappings:
        if mapping.role not in MANAGED_GROUPS:
            raise ValueError("product identity role is invalid")
        connection.execute(
            """
                INSERT INTO product_principal_roles (
                    principal_hash,
                    provisioning_key,
                    tenant_id,
                    role,
                    status
                )
                VALUES (%s, %s, %s, %s, 'active')
                ON CONFLICT (provisioning_key) DO UPDATE
                SET principal_hash = excluded.principal_hash,
                    tenant_id = excluded.tenant_id,
                    role = excluded.role,
                    status = 'active',
                    updated_at = now()
            """,
            (
                mapping.principal_hash,
                mapping.provisioning_key,
                tenant_id,
                mapping.role,
            ),
        )
        activated = connection.execute(
            """
                UPDATE product_credential_locators
                SET principal_hash = %s, status = 'active', updated_at = now()
                WHERE tenant_id = %s
                  AND provisioning_key = %s
                  AND user_pool_id = %s
                  AND cognito_username = %s
                RETURNING id
            """,
            (
                mapping.principal_hash,
                tenant_id,
                mapping.provisioning_key,
                mapping.user_pool_id,
                mapping.username,
            ),
        ).fetchone()
        if activated is None:
            raise RuntimeError("durable Cognito credential locator is missing")


def _lock_active_tenant(
    connection: psycopg.Connection, *, tenant_id: str
) -> None:
    connection.execute(
        "SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,)
    )
    row = connection.execute(
        "SELECT status FROM tenants WHERE id = %s FOR UPDATE", (tenant_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("product identity tenant does not exist")
    if str(row[0]) != "active":
        raise RuntimeError("product identity tenant is not active")


def _reserve_credential_locators(
    connection: psycopg.Connection,
    *,
    tenant_id: str,
    user_pool_id: str,
    intents: list[tuple[str, str, str]],
) -> None:
    for provisioning_key, username, role in intents:
        connection.execute(
            """
                INSERT INTO product_credential_locators (
                    provisioning_key, tenant_id, user_pool_id,
                    cognito_username, role, status
                ) VALUES (%s, %s, %s, %s, %s, 'reserved')
                ON CONFLICT (user_pool_id, cognito_username) DO NOTHING
            """,
            (provisioning_key, tenant_id, user_pool_id, username, role),
        )
        reserved = connection.execute(
            """
                SELECT tenant_id, provisioning_key, role
                FROM product_credential_locators
                WHERE user_pool_id = %s AND cognito_username = %s
                FOR UPDATE
            """,
            (user_pool_id, username),
        ).fetchone()
        if reserved is None or tuple(str(value) for value in reserved) != (
            tenant_id,
            provisioning_key,
            role,
        ):
            raise RuntimeError("Cognito credential locator is already reserved")


def _get_or_create_user(
    client: Any,
    *,
    user_pool_id: str,
    identity: DesiredIdentity,
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
        raise RuntimeError("Cognito did not return the provisioned user")
    return user


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
            str(group.get("GroupName"))
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
        raise RuntimeError("Cognito user does not have exactly one subject identifier")
    return str(subjects[0])


def _principal_hash(*, issuer: str, subject: str) -> str:
    return hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()


def _provisioning_key(*, issuer: str, role: str) -> str:
    """Identify one managed role slot without storing its pool or username."""

    return hashlib.sha256(f"{issuer}\0managed-role\0{role}".encode()).hexdigest()


def _required_environment(name: str) -> str:
    return _required_value(os.environ.get(name), name)


def _required_value(value: str | None, label: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


if __name__ == "__main__":
    main()
