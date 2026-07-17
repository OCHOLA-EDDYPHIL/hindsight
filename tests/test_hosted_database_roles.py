"""Opt-in database permission checks for deployed environments."""

from __future__ import annotations

import os
from uuid import uuid4

import boto3
import psycopg
import pytest

from hindsight.db import database_url_with_tls_roots

requires_hosted = pytest.mark.skipif(
    os.environ.get("RUN_HOSTED_ACCEPTANCE") != "1",
    reason="hosted acceptance is opt-in",
)


def _database_url(ssm, env_name: str) -> str:
    parameter_name = os.environ[env_name]
    value = ssm.get_parameter(Name=parameter_name, WithDecryption=True)["Parameter"]["Value"]
    return database_url_with_tls_roots(value)


def test_database_url_adds_a_tls_root_for_verified_ssm_urls(monkeypatch):
    class Ssm:
        def get_parameter(self, **kwargs):
            assert kwargs == {"Name": "/database/api", "WithDecryption": True}
            return {
                "Parameter": {
                    "Value": "postgresql://api@example.test:26257/app?sslmode=verify-full"
                }
            }

    monkeypatch.setenv("API_DATABASE_PARAMETER", "/database/api")

    url = _database_url(Ssm(), "API_DATABASE_PARAMETER")

    assert "sslmode=verify-full" in url
    assert "sslrootcert=" in url


@requires_hosted
def test_hosted_runtime_database_identities_are_distinct_and_restricted():
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    urls = {
        "deploy": _database_url(ssm, "HINDSIGHT_DEPLOY_DATABASE_URL_PARAM"),
        "api": _database_url(ssm, "HINDSIGHT_API_DATABASE_URL_PARAM"),
        "worker": _database_url(ssm, "HINDSIGHT_WORKER_DATABASE_URL_PARAM"),
    }
    identities = {}
    for label, url in urls.items():
        with psycopg.connect(url, autocommit=True) as connection:
            identities[label] = connection.execute("SELECT current_user").fetchone()[0]
            if label == "deploy":
                assert connection.execute(
                    "SELECT count(*) > 0 FROM schema_migrations WHERE filename = %s",
                    ("0018_agent_run_attempt_fencing.sql",),
                ).fetchone() == (True,)
                continue
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    f"CREATE TABLE hosted_permission_probe_{uuid4().hex} "
                    "(id INT PRIMARY KEY)"
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("DELETE FROM semantic_memories WHERE false")
    assert len(set(identities.values())) == 3
