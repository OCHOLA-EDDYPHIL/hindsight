"""Provision distinct restricted database credentials without exposing their values."""

from __future__ import annotations

import argparse
import json
import secrets
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import boto3
import psycopg
from psycopg import sql

from hindsight.db import database_url_with_tls_roots


@dataclass(frozen=True)
class ParameterSnapshot:
    exists: bool
    value: str | None
    key_id: str | None
    parameter_type: str | None


def _snapshot(ssm, name: str) -> ParameterSnapshot:
    try:
        parameter = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    except ssm.exceptions.ParameterNotFound:
        return ParameterSnapshot(False, None, None, None)
    return ParameterSnapshot(
        True, parameter["Value"], parameter.get("KeyId"), parameter.get("Type")
    )


def _runtime_url(deploy_url: str, *, username: str, password: str) -> str:
    parts = urlsplit(deploy_url)
    if not parts.hostname or not parts.path:
        raise RuntimeError("deploy database URL is invalid")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = f":{parts.port}" if parts.port is not None else ""
    authority = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit(parts._replace(netloc=authority))


def _put_secure_string(ssm, *, name: str, value: str, key_id: str | None) -> None:
    arguments = {"Name": name, "Value": value, "Type": "SecureString", "Overwrite": True}
    if key_id:
        arguments["KeyId"] = key_id
    ssm.put_parameter(**arguments)


def _restore(ssm, *, name: str, snapshot: ParameterSnapshot) -> None:
    if snapshot.exists:
        if snapshot.parameter_type == "SecureString":
            _put_secure_string(
                ssm, name=name, value=str(snapshot.value), key_id=snapshot.key_id
            )
        else:
            ssm.put_parameter(
                Name=name,
                Value=str(snapshot.value),
                Type=snapshot.parameter_type or "String",
                Overwrite=True,
            )
    else:
        try:
            ssm.delete_parameter(Name=name)
        except ssm.exceptions.ParameterNotFound:
            pass


def prepare(
    *,
    profile: str,
    region: str,
    deploy_parameter: str,
    api_parameter: str,
    worker_parameter: str,
    metadata_parameter: str,
) -> None:
    parameter_names = {deploy_parameter, api_parameter, worker_parameter, metadata_parameter}
    if len(parameter_names) != 4:
        raise RuntimeError("database and rotation parameter paths must be distinct")
    session = boto3.Session(profile_name=profile, region_name=region)
    ssm = session.client("ssm")
    deploy_url = ssm.get_parameter(Name=deploy_parameter, WithDecryption=True)["Parameter"][
        "Value"
    ]
    api_snapshot = _snapshot(ssm, api_parameter)
    worker_snapshot = _snapshot(ssm, worker_parameter)
    metadata_snapshot = _snapshot(ssm, metadata_parameter)
    generation = secrets.token_hex(6)
    roles = {
        "api": f"hindsight_api_{generation}",
        "worker": f"hindsight_worker_{generation}",
    }
    passwords = {label: secrets.token_urlsafe(48) for label in roles}
    runtime_urls = {
        label: _runtime_url(deploy_url, username=role, password=passwords[label])
        for label, role in roles.items()
    }
    created_roles: list[str] = []
    try:
        with psycopg.connect(
            database_url_with_tls_roots(deploy_url), autocommit=True
        ) as connection:
            deploy_identity = connection.execute("SELECT current_user").fetchone()[0]
            if deploy_identity in roles.values():
                raise RuntimeError("deploy identity cannot be a runtime identity")
            for label, role in roles.items():
                connection.execute(
                    sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s NOBYPASSRLS").format(
                        sql.Identifier(role)
                    ),
                    (passwords[label],),
                )
                created_roles.append(role)
                permission_role = (
                    "hindsight_agent_writer"
                    if label == "api"
                    else "hindsight_memory_worker"
                )
                connection.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(permission_role), sql.Identifier(role)
                    )
                )
        for label, url in runtime_urls.items():
            with psycopg.connect(
                database_url_with_tls_roots(url), connect_timeout=5
            ) as connection:
                identity = connection.execute("SELECT current_user").fetchone()[0]
                role_flags = connection.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
            if identity != roles[label]:
                raise RuntimeError("runtime database identity verification failed")
            if role_flags != (False, False):
                raise RuntimeError("runtime database identity can bypass tenant isolation")
        _put_secure_string(
            ssm, name=api_parameter, value=runtime_urls["api"], key_id=api_snapshot.key_id
        )
        _put_secure_string(
            ssm,
            name=worker_parameter,
            value=runtime_urls["worker"],
            key_id=worker_snapshot.key_id,
        )
        ssm.put_parameter(
            Name=metadata_parameter,
            Value=json.dumps(
                {"generation": generation, "status": "prepared", "roles": roles},
                sort_keys=True,
            ),
            Type="String",
            Overwrite=True,
        )
    except Exception:
        _restore(ssm, name=api_parameter, snapshot=api_snapshot)
        _restore(ssm, name=worker_parameter, snapshot=worker_snapshot)
        _restore(ssm, name=metadata_parameter, snapshot=metadata_snapshot)
        if created_roles:
            try:
                with psycopg.connect(
                    database_url_with_tls_roots(deploy_url), autocommit=True
                ) as connection:
                    for role in reversed(created_roles):
                        connection.execute(
                            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                        )
            except Exception:
                pass
        raise RuntimeError("runtime database credential preparation failed") from None
    print(f"runtime database credentials prepared in {api_parameter} and {worker_parameter}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--profile", default="dala")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--deploy-parameter", default="/hindsight/demo/database-url")
    parser.add_argument("--api-parameter", default="/hindsight/demo/api-database-url")
    parser.add_argument("--worker-parameter", default="/hindsight/demo/worker-database-url")
    parser.add_argument(
        "--metadata-parameter", default="/hindsight/demo/database-runtime-rotation"
    )
    args = parser.parse_args()
    prepare(
        profile=args.profile,
        region=args.region,
        deploy_parameter=args.deploy_parameter,
        api_parameter=args.api_parameter,
        worker_parameter=args.worker_parameter,
        metadata_parameter=args.metadata_parameter,
    )


if __name__ == "__main__":
    main()
