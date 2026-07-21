"""Drop only a name-fenced disposable rank-diagnostic database."""

from __future__ import annotations

import argparse
import os
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from hindsight.db import database_url_with_tls_roots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    database_name, admin_url = diagnostic_database_target(args.database_url)
    with psycopg.connect(database_url_with_tls_roots(admin_url), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database_name))
        )
    print("diagnostic database: dropped")


def diagnostic_database_target(database_url: str) -> tuple[str, str]:
    parts = urlsplit(database_url)
    database_name = unquote(parts.path.lstrip("/")).split("/", 1)[0]
    if not re.fullmatch(r"hindsight_diagnostic_[0-9]+_[0-9]+", database_name):
        raise RuntimeError("refusing to drop a non-workflow diagnostic database")
    return database_name, urlunsplit(parts._replace(path="/defaultdb"))


if __name__ == "__main__":
    main()
