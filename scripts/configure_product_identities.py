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
from botocore.exceptions import ClientError
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
    mappings = provision_cognito_identities(
        client,
        user_pool_id=args.user_pool_id,
        issuer=_required_value(args.issuer, "issuer"),
        desired=desired,
    )
    persist_principal_mappings(
        database_url=database_url,
        tenant_id=normalize_tenant_id(args.tenant_id),
        mappings=mappings,
    )
    print(f"product identities ready: {len(mappings)} opaque principal mappings")


def provision_cognito_identities(
    client: Any,
    *,
    user_pool_id: str,
    issuer: str,
    desired: tuple[DesiredIdentity, ...],
) -> tuple[tuple[str, str, str], ...]:
    """Idempotently provision users and return only opaque hashes and roles."""

    mappings = []
    for identity in desired:
        if identity.role not in MANAGED_GROUPS:
            raise ValueError("product identity role is invalid")
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
            (
                _principal_hash(issuer=issuer, subject=subject),
                _provisioning_key(issuer=issuer, role=identity.role),
                identity.role,
            )
        )
    return tuple(mappings)


def persist_principal_mappings(
    *,
    database_url: str,
    tenant_id: str,
    mappings: tuple[tuple[str, str, str], ...],
) -> None:
    """Upsert the privileged global mapping without persisting Cognito PII."""

    if not mappings:
        raise ValueError("at least one product identity mapping is required")
    with psycopg.connect(
        database_url_with_tls_roots(database_url),
        connect_timeout=5,
        application_name="hindsight-product-identity-provisioner",
    ) as connection:
        with connection.transaction():
            for principal_hash, provisioning_key, role in mappings:
                if role not in MANAGED_GROUPS:
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
                    (principal_hash, provisioning_key, tenant_id, role),
                )


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
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UserNotFoundException":
            raise
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
    next_token: str | None = None
    while True:
        request: dict[str, Any] = {
            "UserPoolId": user_pool_id,
            "Username": username,
            "Limit": 60,
        }
        if next_token:
            request["NextToken"] = next_token
        response = client.admin_list_groups_for_user(**request)
        groups.update(
            str(group.get("GroupName"))
            for group in response.get("Groups", [])
            if group.get("GroupName")
        )
        next_token = response.get("NextToken")
        if not next_token:
            break
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
