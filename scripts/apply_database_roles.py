"""Apply database grants and verify restricted runtime identities."""

from __future__ import annotations

import argparse
import os
import pathlib
from uuid import uuid4

import boto3
import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _parameter_value(ssm, name: str) -> str:
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _assert_restricted(url: str, *, label: str, deploy_url: str) -> str:
    table_name = f"permission_probe_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as connection:
        identity = connection.execute("SELECT current_user").fetchone()[0]
        for statement in (
            f"CREATE TABLE {table_name} (id INT PRIMARY KEY)",
            "DELETE FROM semantic_memories WHERE false",
        ):
            try:
                connection.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                continue
            if statement.startswith("CREATE TABLE"):
                with psycopg.connect(deploy_url, autocommit=True) as deploy:
                    deploy.execute(f"DROP TABLE IF EXISTS {table_name}")
            raise RuntimeError(f"{label} database identity has excessive privileges")
    return identity


def apply_and_verify(
    *, deploy_url: str, region: str, api_parameter: str, worker_parameter: str
) -> None:
    ssm = boto3.client("ssm", region_name=region)
    api_url = _parameter_value(ssm, api_parameter)
    worker_url = _parameter_value(ssm, worker_parameter)
    with psycopg.connect(deploy_url, autocommit=True) as connection:
        deploy_identity = connection.execute("SELECT current_user").fetchone()[0]
        connection.execute((ROOT / "infra/db/roles.sql").read_text())
    api_identity = _assert_restricted(api_url, label="API", deploy_url=deploy_url)
    worker_identity = _assert_restricted(
        worker_url, label="worker", deploy_url=deploy_url
    )
    if len({deploy_identity, api_identity, worker_identity}) != 3:
        raise RuntimeError("deploy, API, and worker identities must be distinct")
    print("database roles applied; API and worker restrictions verified")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--api-parameter", required=True)
    parser.add_argument("--worker-parameter", required=True)
    args = parser.parse_args()
    deploy_url = os.environ.get("DATABASE_URL", "").strip()
    if not deploy_url:
        raise RuntimeError("DATABASE_URL is required for deploy-only role application")
    try:
        apply_and_verify(
            deploy_url=deploy_url,
            region=args.region,
            api_parameter=args.api_parameter,
            worker_parameter=args.worker_parameter,
        )
    except Exception:
        raise RuntimeError("database role application or verification failed") from None


if __name__ == "__main__":
    main()
