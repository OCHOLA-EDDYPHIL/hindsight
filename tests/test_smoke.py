"""Smoke tests: the scaffold can reach the database and migrations ran.

These skip when DATABASE_URL is unset so the suite passes in environments
without a database (e.g. lint-only CI runs).
"""

import os

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@requires_db
def test_database_reachable():
    from hindsight.db import connect

    with connect() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


@requires_db
def test_bootstrap_migration_applied():
    from hindsight.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_bootstrap'"
        ).fetchone()
        assert row == ("ok",)
