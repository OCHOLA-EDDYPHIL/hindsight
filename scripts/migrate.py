"""Apply SQL migrations in filename order.

Migrations are plain .sql files in migrations/, named NNNN_description.sql.
Applied filenames are recorded in schema_migrations so reruns are no-ops.
The target database is created if it does not exist yet.
"""

import argparse
import pathlib
import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from hindsight.db import database_url  # noqa: E402

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def _blocking_lifecycle_operations(
    conn: psycopg.Connection,
) -> tuple[tuple[str, str], ...]:
    try:
        rows = conn.execute(
            """
                SELECT id::STRING, status
                FROM tenant_lifecycle_operations
                WHERE status IN ('purging', 'database_purged')
                ORDER BY id
            """
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        return ()
    return tuple((str(row[0]), str(row[1])) for row in rows)


def ensure_database(url: str) -> None:
    parts = urlsplit(url)
    dbname = parts.path.lstrip("/") or "defaultdb"
    admin_url = urlunsplit(parts._replace(path="/defaultdb"))
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE IF NOT EXISTS "{dbname}"')


def apply_migrations(url: str, *, through: str | None = None) -> int:
    ensure_database(url)
    available = sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))
    if through is not None and through not in {path.name for path in available}:
        raise ValueError(f"unknown migration filename: {through}")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename STRING PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {
            row[0] for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
        pending = [
            path
            for path in available
            if path.name not in applied and (through is None or path.name <= through)
        ]
        if not pending:
            print("migrations: up to date")
            return 0
        blocking_operations = _blocking_lifecycle_operations(conn)
        if blocking_operations:
            details = ", ".join(
                f"{operation_id} ({status})"
                for operation_id, status in blocking_operations
            )
            raise RuntimeError(
                "database migrations are fenced while tenant purges await recovery: "
                f"{details}; resume or finalize those purge operations before applying "
                "schema changes"
            )
        for path in pending:
            print(f"applying {path.name}")
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
        print(f"migrations: applied {len(pending)}")
        return len(pending)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--through", help="stop after this exact migration filename")
    args = parser.parse_args()
    apply_migrations(database_url(), through=args.through)


if __name__ == "__main__":
    main()
