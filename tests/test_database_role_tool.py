"""Deployment database-role connection behavior."""

from __future__ import annotations

import pathlib
import sys
from urllib.parse import parse_qs, urlsplit

import certifi

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))


def test_role_tool_normalizes_verified_connection_urls(monkeypatch):
    import apply_database_roles as roles

    connected_urls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            if statement == "SELECT current_user":
                return type("Result", (), {"fetchone": lambda self: ("identity",)})()
            raise roles.psycopg.errors.InsufficientPrivilege()

    def connect(url, **_kwargs):
        connected_urls.append(url)
        return Connection()

    monkeypatch.setattr(roles.psycopg, "connect", connect)

    roles._assert_restricted(
        "postgresql://runtime@db.example/hindsight?sslmode=verify-full",
        label="runtime",
        deploy_url="postgresql://deploy@db.example/hindsight?sslmode=verify-full",
    )

    assert len(connected_urls) == 1
    assert parse_qs(urlsplit(connected_urls[0]).query)["sslrootcert"] == [
        certifi.where()
    ]


def test_role_tool_preserves_explicit_tls_root(monkeypatch):
    import apply_database_roles as roles

    connected_urls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            if statement == "SELECT current_user":
                return type("Result", (), {"fetchone": lambda self: ("identity",)})()
            raise roles.psycopg.errors.InsufficientPrivilege()

    monkeypatch.setattr(
        roles.psycopg,
        "connect",
        lambda url, **_kwargs: connected_urls.append(url) or Connection(),
    )

    roles._assert_restricted(
        "postgresql://runtime@db.example/hindsight"
        "?sslmode=verify-full&sslrootcert=system",
        label="runtime",
        deploy_url="postgresql://deploy@db.example/hindsight",
    )

    assert parse_qs(urlsplit(connected_urls[0]).query)["sslrootcert"] == ["system"]
