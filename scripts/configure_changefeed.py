"""Create, resume, inspect, or pause the deployed product changefeed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Callable
from typing import Any, TypeVar

from psycopg import sql
from psycopg.errors import SerializationFailure

from hindsight.db import connect

JOB_KEY = "realtime_changefeed_job_id"
FINGERPRINT_KEY = "realtime_changefeed_fingerprint"
WATCHED_TABLES = ("tenant_event_outbox",)
CHANGEFEED_SCHEMA_VERSION = 3
CHANGEFEED_STATE_TIMEOUT_SECONDS = 60.0
CHANGEFEED_TRANSACTION_ATTEMPTS = 3
TERMINAL_JOB_STATUSES = frozenset({"canceled", "failed", "succeeded"})
CHANGEFEED_OPTIONS = {
    "diff": True,
    "updated": True,
    "initial_scan": "no",
    "resolved": "10s",
    "min_checkpoint_frequency": "10s",
}
T = TypeVar("T")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "pause", "status"))
    args = parser.parse_args()
    if args.command == "apply":
        result = apply_changefeed()
    elif args.command == "pause":
        result = pause_changefeed()
    else:
        result = changefeed_status()
    if args.command in {"apply", "status"} and result["status"] != "running":
        raise RuntimeError(f"managed changefeed is not running: {result.get('status') or 'absent'}")
    print(_summary(result))


def apply_changefeed(
    *,
    webhook_url: str | None = None,
    auth_token: str | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Ensure one running changefeed targets the current deployed webhook."""

    webhook_url = webhook_url or _required_env("HINDSIGHT_CHANGEFEED_WEBHOOK_URL")
    auth_token = auth_token or _required_env("HINDSIGHT_CHANGEFEED_AUTH_TOKEN")
    if not webhook_url.startswith("https://"):
        raise ValueError("changefeed webhook URL must use https")
    sink = f"webhook-{webhook_url}"
    fingerprint_payload = {
        "schema_version": CHANGEFEED_SCHEMA_VERSION,
        "sink": sink,
        "auth_token_sha256": hashlib.sha256(auth_token.encode()).hexdigest(),
        "tables": WATCHED_TABLES,
        "options": CHANGEFEED_OPTIONS,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    job_id, changed = _retry_serialization_failure(
        lambda: _apply_changefeed_once(
            sink=sink,
            auth_token=auth_token,
            fingerprint=fingerprint,
            db_url=db_url,
        )
    )
    return _wait_for_job_status(
        job_id=job_id,
        expected="running",
        changed=changed,
        db_url=db_url,
    )


def _apply_changefeed_once(
    *,
    sink: str,
    auth_token: str,
    fingerprint: str,
    db_url: str | None,
) -> tuple[str, bool]:
    changed = False
    with connect(db_url, application_name="hindsight-changefeed-deploy") as conn:
        with conn.transaction():
            existing_job = _app_meta(conn, JOB_KEY)
            existing_fingerprint = _app_meta(conn, FINGERPRINT_KEY)
            existing_status = _job_status(conn, existing_job) if existing_job else None
            same_job = existing_job and existing_fingerprint == fingerprint
            if same_job and existing_status == "running":
                return str(existing_job), False
            if same_job and existing_status == "paused":
                conn.execute(sql.SQL("RESUME JOB {}").format(sql.Literal(int(existing_job))))
                job_id = str(existing_job)
                changed = True
            else:
                if existing_job and existing_status not in {None, *TERMINAL_JOB_STATUSES}:
                    conn.execute(sql.SQL("CANCEL JOB {}").format(sql.Literal(int(existing_job))))

                tables = sql.SQL(", ").join(sql.Identifier(table) for table in WATCHED_TABLES)
                statement = sql.SQL(
                    """
                        CREATE CHANGEFEED FOR TABLE {}
                        INTO {}
                        WITH diff,
                             updated,
                             initial_scan = 'no',
                             resolved = '10s',
                             min_checkpoint_frequency = '10s',
                             webhook_auth_header = {}
                    """
                ).format(
                    tables,
                    sql.Literal(sink),
                    sql.Literal(f"Bearer {auth_token}"),
                )
                row = conn.execute(statement).fetchone()
                if row is None:
                    raise RuntimeError("CockroachDB did not return a changefeed job id")
                job_id = str(row[0])
                _set_app_meta(conn, JOB_KEY, job_id)
                _set_app_meta(conn, FINGERPRINT_KEY, fingerprint)
                changed = True
    return job_id, changed


def pause_changefeed(*, db_url: str | None = None) -> dict[str, Any]:
    """Pause the managed changefeed before its webhook endpoint is destroyed."""

    job_id, changed = _retry_serialization_failure(lambda: _pause_changefeed_once(db_url=db_url))
    if job_id is None:
        return {"job_id": None, "status": "absent", "changed": False}
    return _wait_for_job_status(
        job_id=job_id,
        expected="paused",
        changed=changed,
        db_url=db_url,
    )


def _pause_changefeed_once(*, db_url: str | None) -> tuple[str | None, bool]:
    changed = False
    with connect(db_url, application_name="hindsight-changefeed-deploy") as conn:
        with conn.transaction():
            job_id = _app_meta(conn, JOB_KEY)
            if not job_id:
                return None, False
            current = _job_status(conn, job_id)
            if current == "running":
                conn.execute(sql.SQL("PAUSE JOB {}").format(sql.Literal(int(job_id))))
                changed = True
            elif current != "paused":
                raise RuntimeError(
                    f"managed changefeed cannot be paused from status {current or 'absent'}"
                )
    return job_id, changed


def changefeed_status(*, db_url: str | None = None) -> dict[str, Any]:
    return _retry_serialization_failure(lambda: _changefeed_status_once(db_url=db_url))


def _changefeed_status_once(*, db_url: str | None) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-changefeed-deploy") as conn:
        job_id = _app_meta(conn, JOB_KEY)
        return {
            "job_id": job_id,
            "status": _job_status(conn, job_id) if job_id else "absent",
            "changed": False,
        }


def _retry_serialization_failure(operation: Callable[[], T]) -> T:
    for attempt in range(CHANGEFEED_TRANSACTION_ATTEMPTS):
        try:
            return operation()
        except SerializationFailure:
            if attempt == CHANGEFEED_TRANSACTION_ATTEMPTS - 1:
                raise
    raise AssertionError("serialization retry loop did not return or raise")


def _wait_for_job_status(
    *,
    job_id: str,
    expected: str,
    changed: bool,
    db_url: str | None,
    timeout_seconds: float = CHANGEFEED_STATE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait until CockroachDB observes a requested managed-job transition."""

    deadline = time.monotonic() + timeout_seconds
    last_status: str | None = None
    while True:
        status = changefeed_status(db_url=db_url)
        if status["job_id"] != job_id:
            raise RuntimeError("managed changefeed identity changed during state transition")
        last_status = str(status["status"] or "") or None
        if last_status == expected:
            return {"job_id": job_id, "status": expected, "changed": changed}
        if last_status in TERMINAL_JOB_STATUSES:
            raise RuntimeError(
                f"managed changefeed entered terminal status {last_status} while awaiting {expected}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "managed changefeed did not reach "
                f"{expected} within {timeout_seconds:g}s (last status: {last_status or 'absent'})"
            )
        time.sleep(1)


def _app_meta(conn: Any, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = %s", (key,)).fetchone()
    return str(row[0]) if row else None


def _set_app_meta(conn: Any, key: str, value: str) -> None:
    conn.execute(
        "UPSERT INTO app_meta (key, value) VALUES (%s, %s)",
        (key, value),
    )


def _job_status(conn: Any, job_id: str | None) -> str | None:
    if not job_id:
        return None
    row = conn.execute(
        "SELECT status FROM [SHOW JOBS] WHERE job_id = %s",
        (int(job_id),),
    ).fetchone()
    return str(row[0]).lower() if row else None


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _summary(result: dict[str, Any]) -> str:
    return (
        f"changefeed: job={result.get('job_id') or 'none'} "
        f"status={result.get('status')} changed={str(bool(result.get('changed'))).lower()}"
    )


if __name__ == "__main__":
    main()
