"""Create, resume, inspect, or pause the deployed product changefeed."""

from __future__ import annotations

import argparse
import hashlib
import os
from typing import Any

from psycopg import sql

from hindsight.db import connect

JOB_KEY = "realtime_changefeed_job_id"
FINGERPRINT_KEY = "realtime_changefeed_fingerprint"
WATCHED_TABLES = (
    "semantic_memories",
    "memory_operations",
    "agent_runs",
    "agent_run_events",
)


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
    fingerprint = hashlib.sha256(f"{sink}\0{auth_token}".encode()).hexdigest()
    with connect(db_url, application_name="hindsight-changefeed-deploy") as conn:
        with conn.transaction():
            existing_job = _app_meta(conn, JOB_KEY)
            existing_fingerprint = _app_meta(conn, FINGERPRINT_KEY)
            existing_status = _job_status(conn, existing_job) if existing_job else None
            if existing_job and existing_fingerprint == fingerprint and existing_status:
                if existing_status == "paused":
                    conn.execute(sql.SQL("RESUME JOB {}").format(sql.Literal(int(existing_job))))
                    existing_status = "running"
                return {"job_id": existing_job, "status": existing_status, "changed": False}
            if existing_job and existing_status not in {None, "canceled", "failed", "succeeded"}:
                conn.execute(sql.SQL("CANCEL JOB {}").format(sql.Literal(int(existing_job))))

            tables = sql.SQL(", ").join(sql.Identifier(table) for table in WATCHED_TABLES)
            statement = sql.SQL(
                """
                    CREATE CHANGEFEED FOR TABLE {}
                    INTO {}
                    WITH updated,
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
            return {"job_id": job_id, "status": "running", "changed": True}


def pause_changefeed(*, db_url: str | None = None) -> dict[str, Any]:
    """Pause the managed changefeed before its webhook endpoint is destroyed."""

    with connect(db_url, application_name="hindsight-changefeed-deploy") as conn:
        with conn.transaction():
            job_id = _app_meta(conn, JOB_KEY)
            if not job_id:
                return {"job_id": None, "status": "absent", "changed": False}
            current = _job_status(conn, job_id)
            if current == "running":
                conn.execute(sql.SQL("PAUSE JOB {}").format(sql.Literal(int(job_id))))
                return {"job_id": job_id, "status": "paused", "changed": True}
            return {"job_id": job_id, "status": current or "absent", "changed": False}


def changefeed_status(*, db_url: str | None = None) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-changefeed-deploy") as conn:
        job_id = _app_meta(conn, JOB_KEY)
        return {
            "job_id": job_id,
            "status": _job_status(conn, job_id) if job_id else "absent",
            "changed": False,
        }


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
