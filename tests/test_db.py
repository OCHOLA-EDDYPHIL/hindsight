"""Tests for database connection helpers."""


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
