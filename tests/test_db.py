"""Tests for database connection helpers."""

from urllib.parse import parse_qs, urlsplit


def test_connect_uses_timeout_and_application_name(monkeypatch):
    import hindsight.db as db

    calls = []

    def fake_connect(conninfo, **kwargs):
        calls.append((conninfo, kwargs))
        return object()

    monkeypatch.setenv("DATABASE_URL", "postgresql://db")
    monkeypatch.setattr(db.psycopg, "connect", fake_connect)

    connection = db.connect()

    assert connection is not None
    assert calls == [
        (
            "postgresql://db",
            {
                "connect_timeout": db.DEFAULT_CONNECT_TIMEOUT_SECONDS,
                "application_name": db.DEFAULT_APPLICATION_NAME,
            },
        )
    ]


def test_connect_allows_call_site_overrides(monkeypatch):
    import hindsight.db as db

    calls = []

    def fake_connect(conninfo, **kwargs):
        calls.append((conninfo, kwargs))
        return object()

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)

    db.connect("postgresql://other", connect_timeout=2, application_name="hindsight-dashboard")

    assert calls == [
        (
            "postgresql://other",
            {
                "connect_timeout": 2,
                "application_name": "hindsight-dashboard",
            },
        )
    ]


def test_database_url_adds_bundled_ca_for_verified_connections(monkeypatch):
    import certifi

    from hindsight.db import database_url

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://db.example/hindsight?sslmode=verify-full",
    )

    query = parse_qs(urlsplit(database_url()).query)

    assert query == {
        "sslmode": ["verify-full"],
        "sslrootcert": [certifi.where()],
    }


def test_database_url_preserves_explicit_ca(monkeypatch):
    from hindsight.db import database_url

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://db.example/hindsight?sslmode=verify-full&sslrootcert=system",
    )

    query = parse_qs(urlsplit(database_url()).query)

    assert query["sslrootcert"] == ["system"]
