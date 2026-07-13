"""Database connection helpers.

All of Hindsight's state lives in CockroachDB. Every component gets its
connection through here so the URL, and later the role separation, is
configured in exactly one place.
"""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_APPLICATION_NAME = "hindsight"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and adjust it, "
            "or export DATABASE_URL directly."
        )
    return url


def connect(
    url: str | None = None,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name: str = DEFAULT_APPLICATION_NAME,
) -> psycopg.Connection:
    """Open a CockroachDB connection with bounded startup behavior."""

    return psycopg.connect(
        url or database_url(),
        connect_timeout=connect_timeout,
        application_name=application_name,
    )
