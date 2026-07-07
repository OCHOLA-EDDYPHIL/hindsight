"""Apply SQL migrations in filename order.

Migrations are plain .sql files in migrations/, named NNNN_description.sql.
Applied filenames are recorded in schema_migrations so reruns are no-ops.
The target database is created if it does not exist yet.
"""

import pathlib
import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from hindsight.db import database_url  # noqa: E402

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def ensure_database(url: str) -> None:
    parts = urlsplit(url)
    dbname = parts.path.lstrip("/") or "defaultdb"
    admin_url = urlunsplit(parts._replace(path="/defaultdb"))
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE IF NOT EXISTS "{dbname}"')


def main() -> None:
    url = database_url()
    ensure_database(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename STRING PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {
            row[0] for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
        pending = sorted(
            p for p in MIGRATIONS_DIR.glob("[0-9]*.sql") if p.name not in applied
        )
        if not pending:
            print("migrations: up to date")
            return
        for path in pending:
            print(f"applying {path.name}")
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
        print(f"migrations: applied {len(pending)}")


if __name__ == "__main__":
    main()
