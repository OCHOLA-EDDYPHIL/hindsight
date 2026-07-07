"""Database connection helpers.

All of Hindsight's state lives in CockroachDB. Every component gets its
connection through here so the URL, and later the role separation, is
configured in exactly one place.
"""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and adjust it, "
            "or export DATABASE_URL directly."
        )
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())
