"""Database connection helpers.

All of Hindsight's state lives in CockroachDB. Every component gets its
connection through here so the URL, and later the role separation, is
configured in exactly one place.
"""

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
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
    return database_url_with_tls_roots(url)


def database_url_with_tls_roots(url: str) -> str:
    """Use the bundled public CA when a verified URL omits an explicit root."""

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("sslmode") in {"verify-ca", "verify-full"} and not query.get(
        "sslrootcert"
    ):
        query["sslrootcert"] = certifi.where()
    return urlunsplit(parts._replace(query=urlencode(query)))


def connect(
    url: str | None = None,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    application_name: str = DEFAULT_APPLICATION_NAME,
) -> psycopg.Connection:
    """Open a CockroachDB connection with bounded startup behavior."""

    return psycopg.connect(
        database_url_with_tls_roots(url) if url is not None else database_url(),
        connect_timeout=connect_timeout,
        application_name=application_name,
    )
