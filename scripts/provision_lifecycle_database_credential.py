"""Reconcile the one restricted database credential used for lifecycle work."""

from __future__ import annotations

import argparse
import os
import pathlib
import secrets
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, quote, unquote, urlsplit, urlunsplit

import boto3
import psycopg
from psycopg import sql

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.db import database_url_with_tls_roots  # noqa: E402


DEPLOY_DATABASE_PARAMETER = "/hindsight/demo/database-url"
LIFECYCLE_DATABASE_PARAMETER = "/hindsight/demo/lifecycle-database-url"
LIFECYCLE_LOGIN = "hindsight_lifecycle_demo_login"
LIFECYCLE_ROLE = "hindsight_lifecycle"


@dataclass(frozen=True)
class ParameterSnapshot:
    exists: bool
    value: str | None = None
    parameter_type: str | None = None


@dataclass(frozen=True)
class RoleState:
    exists: bool
    can_login: bool = False
    is_superuser: bool = False
    bypasses_rls: bool = False
    memberships: frozenset[str] = frozenset()


def _snapshot_parameter(ssm: Any, name: str) -> ParameterSnapshot:
    try:
        parameter = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    except ssm.exceptions.ParameterNotFound:
        return ParameterSnapshot(False)
    return ParameterSnapshot(
        True,
        value=str(parameter["Value"]),
        parameter_type=str(parameter["Type"]),
    )


def _url_parts(value: str) -> SplitResult:
    parts = urlsplit(value)
    if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname or not parts.path:
        raise RuntimeError("database URL is invalid")
    return parts


def _runtime_url(deploy_url: str, *, password: str) -> str:
    parts = _url_parts(deploy_url)
    host_port = parts.netloc.rsplit("@", 1)[-1]
    authority = f"{quote(LIFECYCLE_LOGIN, safe='')}:{quote(password, safe='')}@{host_port}"
    return urlunsplit(parts._replace(netloc=authority))


def _noncredential_url(value: str) -> tuple[str, str, str, str, str]:
    parts = _url_parts(value)
    return (
        parts.scheme,
        parts.netloc.rsplit("@", 1)[-1],
        parts.path,
        parts.query,
        parts.fragment,
    )


def _parameter_login(value: str) -> tuple[str, str]:
    parts = urlsplit(value)
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if not username or not password:
        raise RuntimeError("lifecycle database parameter lacks a login credential")
    return username, password


def _role_state(connection: psycopg.Connection, name: str) -> RoleState:
    row = connection.execute(
        """
            SELECT rolcanlogin, rolsuper, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = %s
        """,
        (name,),
    ).fetchone()
    if row is None:
        return RoleState(False)
    memberships = connection.execute(
        """
            SELECT granted.rolname
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS granted
              ON granted.oid = membership.roleid
            JOIN pg_catalog.pg_roles AS member
              ON member.oid = membership.member
            WHERE member.rolname = %s
            ORDER BY granted.rolname
        """,
        (name,),
    ).fetchall()
    return RoleState(
        True,
        can_login=bool(row[0]),
        is_superuser=bool(row[1]),
        bypasses_rls=bool(row[2]),
        memberships=frozenset(str(item[0]) for item in memberships),
    )


def _assert_permission_role(connection: psycopg.Connection) -> None:
    state = _role_state(connection, LIFECYCLE_ROLE)
    if (
        not state.exists
        or state.can_login
        or state.is_superuser
        or state.bypasses_rls
        or state.memberships
    ):
        raise RuntimeError("lifecycle permission role is missing or unsafe")


def _assert_recoverable_login(state: RoleState) -> None:
    if not state.exists:
        raise RuntimeError("lifecycle database login is missing")
    if not state.can_login or state.is_superuser or state.bypasses_rls:
        raise RuntimeError("lifecycle database login is not restricted")
    if state.memberships not in {
        frozenset(),
        frozenset({LIFECYCLE_ROLE}),
    }:
        raise RuntimeError("lifecycle database login has unexpected memberships")


def _assert_managed_login(state: RoleState) -> None:
    _assert_recoverable_login(state)
    if state.memberships != frozenset({LIFECYCLE_ROLE}):
        raise RuntimeError("lifecycle database login is missing its permission role")


def _verify_runtime(value: str) -> None:
    with psycopg.connect(
        database_url_with_tls_roots(value),
        connect_timeout=5,
        application_name="hindsight-lifecycle-credential-verifier",
    ) as connection:
        identity = str(connection.execute("SELECT current_user").fetchone()[0])
        state = _role_state(connection, identity)
    if identity != LIFECYCLE_LOGIN:
        raise RuntimeError("lifecycle database identity verification failed")
    _assert_managed_login(state)


def _put_parameter(ssm: Any, value: str) -> None:
    ssm.put_parameter(
        Name=LIFECYCLE_DATABASE_PARAMETER,
        Value=value,
        Type="SecureString",
        Overwrite=False,
    )


def _parameter_password(snapshot: ParameterSnapshot, *, deploy_url: str) -> str:
    if not snapshot.exists or snapshot.value is None:
        raise RuntimeError("lifecycle database parameter is missing")
    if snapshot.parameter_type != "SecureString":
        raise RuntimeError("lifecycle database parameter must be a SecureString")
    username, password = _parameter_login(snapshot.value)
    if username != LIFECYCLE_LOGIN:
        raise RuntimeError("lifecycle parameter belongs to another database login")
    if _noncredential_url(snapshot.value) != _noncredential_url(deploy_url):
        raise RuntimeError("lifecycle parameter targets another database configuration")
    return password


def _write_new_parameter(ssm: Any, *, deploy_url: str) -> ParameterSnapshot:
    password = secrets.token_urlsafe(48)
    runtime_url = _runtime_url(deploy_url, password=password)
    _put_parameter(ssm, runtime_url)
    written = _snapshot_parameter(ssm, LIFECYCLE_DATABASE_PARAMETER)
    if (
        not written.exists
        or written.parameter_type != "SecureString"
        or written.value != runtime_url
    ):
        raise RuntimeError("lifecycle database parameter verification failed")
    return written


def _assert_parameter_unchanged(ssm: Any, expected: ParameterSnapshot) -> None:
    if _snapshot_parameter(ssm, LIFECYCLE_DATABASE_PARAMETER) != expected:
        raise RuntimeError("lifecycle database parameter changed during reconciliation")


def _reconcile_login(
    connection: psycopg.Connection,
    *,
    existing: RoleState,
    password: str,
) -> None:
    if existing.exists:
        _assert_recoverable_login(existing)
        connection.execute(
            sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(sql.Identifier(LIFECYCLE_LOGIN)),
            (password,),
        )
    else:
        connection.execute(
            sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s NOBYPASSRLS").format(
                sql.Identifier(LIFECYCLE_LOGIN)
            ),
            (password,),
        )
    connection.execute(
        sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(LIFECYCLE_ROLE),
            sql.Identifier(LIFECYCLE_LOGIN),
        )
    )
    _assert_managed_login(_role_state(connection, LIFECYCLE_LOGIN))


def reconcile(*, profile: str | None, region: str) -> str:
    """Create, repair, or verify the fixed lifecycle login and its sole parameter."""

    session = boto3.Session(profile_name=profile, region_name=region)
    ssm = session.client("ssm", config=aws_client_config(read_timeout=10))
    deploy_url = str(
        ssm.get_parameter(Name=DEPLOY_DATABASE_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    )
    lifecycle_snapshot = _snapshot_parameter(ssm, LIFECYCLE_DATABASE_PARAMETER)

    with psycopg.connect(
        database_url_with_tls_roots(deploy_url),
        autocommit=True,
        connect_timeout=5,
        application_name="hindsight-lifecycle-credential-provisioner",
    ) as connection:
        _assert_permission_role(connection)
        existing = _role_state(connection, LIFECYCLE_LOGIN)

    initial_role_exists = existing.exists
    initial_parameter_exists = lifecycle_snapshot.exists

    if existing.exists:
        _assert_recoverable_login(existing)
    if lifecycle_snapshot.exists:
        _parameter_password(lifecycle_snapshot, deploy_url=deploy_url)

    if (
        existing.exists
        and existing.memberships == frozenset({LIFECYCLE_ROLE})
        and lifecycle_snapshot.exists
        and lifecycle_snapshot.value is not None
    ):
        try:
            _verify_runtime(lifecycle_snapshot.value)
        except (psycopg.Error, RuntimeError):
            pass
        else:
            _assert_parameter_unchanged(ssm, lifecycle_snapshot)
            return "unchanged"

    try:
        if not lifecycle_snapshot.exists:
            lifecycle_snapshot = _write_new_parameter(ssm, deploy_url=deploy_url)
        password = _parameter_password(lifecycle_snapshot, deploy_url=deploy_url)
        with psycopg.connect(
            database_url_with_tls_roots(deploy_url),
            autocommit=True,
            connect_timeout=5,
            application_name="hindsight-lifecycle-credential-provisioner",
        ) as connection:
            _assert_permission_role(connection)
            current = _role_state(connection, LIFECYCLE_LOGIN)
            _reconcile_login(
                connection,
                existing=current,
                password=password,
            )
        if lifecycle_snapshot.value is None:
            raise RuntimeError("lifecycle database parameter is missing")
        _verify_runtime(lifecycle_snapshot.value)
        _assert_parameter_unchanged(ssm, lifecycle_snapshot)
    except Exception as exc:
        raise RuntimeError("lifecycle database credential reconciliation failed") from exc
    if not initial_role_exists and not initial_parameter_exists:
        return "created"
    return "recovered"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args(argv)
    try:
        result = reconcile(profile=args.profile, region=args.region)
    except Exception:
        print("lifecycle database credential reconciliation failed", file=sys.stderr)
        return 1
    print(
        f"lifecycle database credential {result}: "
        f"{LIFECYCLE_LOGIN} -> {LIFECYCLE_DATABASE_PARAMETER}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
