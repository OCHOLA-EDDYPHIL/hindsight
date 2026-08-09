"""Run a destructive, isolated CockroachDB database backup and restore drill."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

import psycopg
from psycopg import sql

from hindsight.db import database_url_with_tls_roots


ROOT = Path(__file__).resolve().parents[1]
RUN_ID_PATTERN = re.compile(r"[a-z0-9]{8,20}")
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DATABASE_PREFIX = "hindsight_recovery_"
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 1800
CONNECT_TIMEOUT_SECONDS = 5
CLEANUP_TIMEOUT_SECONDS = 30
PRE_BACKUP_MARKER_KEY = "recovery_drill_pre_backup"
POST_BACKUP_MARKER_KEY = "recovery_drill_post_backup"
SCHEMA_VERSION = "hindsight.recovery_drill.v1"
LIMITATIONS = [
    "The workflow uses one local CockroachDB node and does not exercise node or region loss.",
    "The backup uses userfile storage on the same ephemeral cluster, so it does not prove "
    "off-cluster media durability.",
    "The drill simulates logical source-database loss; it does not simulate disk, network, "
    "credential, encryption-key, or control-plane failure.",
    "The measured intervals are client-observed on a small migrated fixture and are not "
    "production RPO, RTO, capacity, or SLO claims.",
]


@dataclass(frozen=True)
class DrillTargets:
    run_id: str
    source_database: str
    restore_database: str
    userfile_prefix: str
    backup_uri: str


@dataclass(frozen=True)
class Deadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: int) -> Deadline:
        return cls(time.monotonic() + seconds)

    def limit(self, maximum: int) -> int:
        remaining = math.ceil(self.expires_at - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("recovery drill exceeded its bounded execution time")
        return max(1, min(maximum, remaining))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration(start: datetime, end: datetime) -> float:
    return round(max(0.0, (end - start).total_seconds()), 6)


def _targets(run_id: str) -> DrillTargets:
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run id must contain 8-20 lowercase ASCII letters or digits")
    source = f"{DATABASE_PREFIX}source_{run_id}"
    restore = f"{DATABASE_PREFIX}restore_{run_id}"
    userfile_prefix = f"{DATABASE_PREFIX}userfile_{run_id}"
    return DrillTargets(
        run_id=run_id,
        source_database=source,
        restore_database=restore,
        userfile_prefix=userfile_prefix,
        backup_uri=f"userfile://defaultdb.public.{userfile_prefix}/backup",
    )


def _validate_admin_url(admin_url: str) -> str:
    parts = urlsplit(admin_url)
    database = unquote(parts.path.lstrip("/")).split("/", 1)[0]
    if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
        raise ValueError("admin URL must be a PostgreSQL connection URL")
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("the destructive recovery drill accepts only a loopback database host")
    if database != "defaultdb":
        raise ValueError("admin URL must target defaultdb")
    if unquote(parts.username or "") != "root":
        raise ValueError("the local recovery drill requires the root SQL user")
    return database_url_with_tls_roots(admin_url)


def _database_url(admin_url: str, database: str) -> str:
    if not _is_disposable_database(database):
        raise RuntimeError("refusing to construct a URL for a non-drill database")
    return urlunsplit(urlsplit(admin_url)._replace(path=f"/{database}"))


def _is_disposable_database(database: str) -> bool:
    return (
        re.fullmatch(r"hindsight_recovery_(?:source|restore)_[a-z0-9]{8,20}", database) is not None
    )


def _confirm_source_loss(targets: DrillTargets, confirmation: str) -> None:
    expected = f"drop:{targets.source_database}"
    if confirmation != expected:
        raise RuntimeError(f"source-loss confirmation must exactly equal {expected}")


@contextmanager
def _connection(url: str, deadline: Deadline) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=deadline.limit(CONNECT_TIMEOUT_SECONDS),
        application_name="hindsight-recovery-drill",
    ) as conn:
        statement_timeout_ms = deadline.limit(MAX_TIMEOUT_SECONDS) * 1000
        conn.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{statement_timeout_ms}ms",),
        )
        yield conn


def _existing_databases(conn: psycopg.Connection[Any]) -> set[str]:
    return {str(row[0]) for row in conn.execute("SHOW DATABASES").fetchall()}


def _guard_clean_start(admin_url: str, targets: DrillTargets, deadline: Deadline) -> None:
    with _connection(admin_url, deadline) as conn:
        collisions = {targets.source_database, targets.restore_database}.intersection(
            _existing_databases(conn)
        )
        userfile_tables = {
            f"{targets.userfile_prefix}_upload_files",
            f"{targets.userfile_prefix}_upload_payload",
        }
        existing_userfile_tables = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_catalog = 'defaultdb'
                  AND table_schema = 'public'
                  AND table_name = ANY(%s)
                """,
                (sorted(userfile_tables),),
            ).fetchall()
        }
    if collisions or existing_userfile_tables:
        occupied = sorted(collisions.union(existing_userfile_tables))
        raise RuntimeError("refusing to reuse existing drill resources: " + ", ".join(occupied))


def _create_database(admin_url: str, database: str, deadline: Deadline) -> None:
    if not _is_disposable_database(database):
        raise RuntimeError("refusing to create a non-drill database")
    with _connection(admin_url, deadline) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def _drop_database(conn: psycopg.Connection[Any], database: str, *, allowed: set[str]) -> None:
    if database not in allowed or not _is_disposable_database(database):
        raise RuntimeError("refusing to drop a database outside the exact drill target set")
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database)))


def _run_repository_script(
    script: str,
    args: list[str],
    *,
    database_url: str,
    deadline: Deadline,
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=deadline.limit(600),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1][:400]}" if detail else ""
        raise RuntimeError(f"{script} failed with exit code {completed.returncode}{suffix}")


def _apply_all_migrations(database_url: str, deadline: Deadline) -> None:
    _run_repository_script("migrate.py", [], database_url=database_url, deadline=deadline)


def _schema_manifest(database_url: str, deadline: Deadline) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hindsight-recovery-schema-") as directory:
        output = Path(directory) / "schema.json"
        _run_repository_script(
            "schema_manifest.py",
            ["export", "--output", str(output)],
            database_url=database_url,
            deadline=deadline,
        )
        value = json.loads(output.read_text())
    if not isinstance(value, dict):
        raise RuntimeError("schema manifest did not contain a JSON object")
    return value


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return {"type": "float", "value": value.hex()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, datetime_time):
        return {"type": "time", "value": value.isoformat()}
    if isinstance(value, UUID):
        return {"type": "uuid", "value": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "type": "bytes",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return {"type": type(value).__name__, "value": str(value)}


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical_value(value), sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _data_snapshot(database_url: str, deadline: Deadline) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    total_rows = 0
    with _connection(database_url, deadline) as conn:
        table_names = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            ).fetchall()
        ]
        for table_name in table_names:
            columns = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (table_name,),
                ).fetchall()
            ]
            query = sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.Identifier(table_name),
            )
            rows = sorted(_canonical_json(list(row)) for row in conn.execute(query).fetchall())
            row_digest = hashlib.sha256()
            for row in rows:
                row_digest.update(row.encode())
                row_digest.update(b"\n")
            tables[table_name] = {
                "columns": columns,
                "row_count": len(rows),
                "row_sha256": row_digest.hexdigest(),
            }
            total_rows += len(rows)
    summary = {
        "table_count": len(tables),
        "row_count": total_rows,
        "tables": tables,
    }
    return {**summary, "sha256": _sha256(summary)}


def _manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "sha256": _sha256(manifest),
        "section_counts": {
            key: len(value) if isinstance(value, (dict, list)) else 1
            for key, value in sorted(manifest.items())
        },
    }


def _migration_summary(database_url: str, deadline: Deadline) -> dict[str, Any]:
    with _connection(database_url, deadline) as conn:
        filenames = [
            str(row[0])
            for row in conn.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
        ]
    if not filenames:
        raise RuntimeError("full migration application produced no schema_migrations rows")
    return {
        "count": len(filenames),
        "last_filename": filenames[-1],
        "filenames_sha256": _sha256(filenames),
    }


def _insert_marker(database_url: str, marker_id: str, payload: str, deadline: Deadline) -> datetime:
    if marker_id not in {PRE_BACKUP_MARKER_KEY, POST_BACKUP_MARKER_KEY}:
        raise RuntimeError("refusing to write an unknown recovery marker key")
    with _connection(database_url, deadline) as conn:
        conn.execute("UPSERT INTO app_meta (key, value) VALUES (%s, %s)", (marker_id, payload))
        row = conn.execute("SELECT clock_timestamp()").fetchone()
    if row is None or not isinstance(row[0], datetime):
        raise RuntimeError("marker insert did not return a timestamp")
    return row[0]


def _markers(database_url: str, deadline: Deadline) -> dict[str, str]:
    with _connection(database_url, deadline) as conn:
        rows = conn.execute(
            """
            SELECT key, value
            FROM app_meta
            WHERE key = ANY(%s)
            ORDER BY key
            """,
            ([PRE_BACKUP_MARKER_KEY, POST_BACKUP_MARKER_KEY],),
        ).fetchall()
    return {str(marker_id): str(payload) for marker_id, payload in rows}


def _backup_statement(targets: DrillTargets) -> sql.Composed:
    return sql.SQL("BACKUP DATABASE {} INTO {}").format(
        sql.Identifier(targets.source_database), sql.Literal(targets.backup_uri)
    )


def _restore_statement(targets: DrillTargets) -> sql.Composed:
    return sql.SQL("RESTORE DATABASE {} FROM LATEST IN {} WITH new_db_name = {}").format(
        sql.Identifier(targets.source_database),
        sql.Literal(targets.backup_uri),
        sql.Literal(targets.restore_database),
    )


def _show_backup_statement(targets: DrillTargets) -> sql.Composed:
    return sql.SQL("SHOW BACKUP FROM LATEST IN {}").format(sql.Literal(targets.backup_uri))


def _backup_restore_point(admin_url: str, targets: DrillTargets, deadline: Deadline) -> datetime:
    with _connection(admin_url, deadline) as conn:
        result = conn.execute(_show_backup_statement(targets))
        columns = [column.name for column in result.description or ()]
        if "end_time" not in columns:
            raise RuntimeError("backup metadata does not expose a restore point")
        end_time_index = columns.index("end_time")
        restore_points = {
            row[end_time_index] for row in result.fetchall() if row[end_time_index] is not None
        }
    if len(restore_points) != 1:
        raise RuntimeError("backup metadata does not identify one restore point")
    restore_point = restore_points.pop()
    if not isinstance(restore_point, datetime):
        raise RuntimeError("backup restore point is not a timestamp")
    return restore_point.astimezone(timezone.utc)


def _engine_and_topology(
    admin_url: str, deadline: Deadline
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _connection(admin_url, deadline) as conn:
        engine_version = str(conn.execute("SELECT version()").fetchone()[0])
        cluster_version = str(conn.execute("SHOW CLUSTER SETTING version").fetchone()[0])
        cluster_id = str(conn.execute("SELECT crdb_internal.cluster_id()").fetchone()[0])
        node_count = int(
            conn.execute("SELECT count(*) FROM crdb_internal.gossip_nodes").fetchone()[0]
        )
    if node_count != 1:
        raise RuntimeError(f"local recovery drill requires exactly one node; observed {node_count}")
    return (
        {
            "product": "CockroachDB",
            "version_string": engine_version,
            "cluster_setting_version": cluster_version,
            "cluster_id": cluster_id,
        },
        {"mode": "local-single-node", "node_count": node_count},
    )


def _cleanup_resources(admin_url: str, targets: DrillTargets) -> tuple[dict[str, Any], list[str]]:
    cleanup = {
        "source_database_absent": False,
        "restore_database_absent": False,
        "userfile_tables_absent": False,
    }
    errors: list[str] = []
    deadline = Deadline.after(CLEANUP_TIMEOUT_SECONDS)
    allowed = {targets.source_database, targets.restore_database}
    try:
        with _connection(admin_url, deadline) as conn:
            _drop_database(conn, targets.restore_database, allowed=allowed)
            _drop_database(conn, targets.source_database, allowed=allowed)
            payload_table = f"{targets.userfile_prefix}_upload_payload"
            files_table = f"{targets.userfile_prefix}_upload_files"
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS defaultdb.public.{}").format(
                    sql.Identifier(payload_table)
                )
            )
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS defaultdb.public.{} CASCADE").format(
                    sql.Identifier(files_table)
                )
            )
            databases = _existing_databases(conn)
            cleanup["source_database_absent"] = targets.source_database not in databases
            cleanup["restore_database_absent"] = targets.restore_database not in databases
            remaining_tables = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_catalog = 'defaultdb'
                      AND table_schema = 'public'
                      AND table_name = ANY(%s)
                    """,
                    ([payload_table, files_table],),
                ).fetchall()
            }
            cleanup["userfile_tables_absent"] = not remaining_tables
    except Exception as exc:  # cleanup evidence must survive the original failure
        errors.append(f"{type(exc).__name__}: {exc}")
    for key, confirmed in cleanup.items():
        if not confirmed:
            errors.append(f"cleanup confirmation failed: {key}")
    return cleanup, errors


def _write_evidence(output: Path, evidence: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)


def run_drill(
    *,
    admin_url: str,
    run_id: str,
    source_sha: str,
    confirmation: str,
    output: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    admin_url = _validate_admin_url(admin_url)
    targets = _targets(run_id)
    if SOURCE_SHA_PATTERN.fullmatch(source_sha) is None:
        raise ValueError("source SHA must be exactly 40 lowercase hexadecimal characters")
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds"
        )
    _confirm_source_loss(targets, confirmation)

    deadline = Deadline.after(timeout_seconds)
    started_at = _utc_now()
    timeline: dict[str, str] = {"started_at": _timestamp(started_at)}
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "run_id": run_id,
        "source_sha": source_sha,
        "scope": {
            "source_database": targets.source_database,
            "restore_database": targets.restore_database,
            "backup_uri": targets.backup_uri,
            "destructive_scope": "derived disposable resources only",
        },
        "timeline": timeline,
        "limitations": LIMITATIONS,
    }
    error: Exception | None = None
    source_manifest: dict[str, Any] | None = None
    source_data: dict[str, Any] | None = None
    cleanup_owned = False
    try:
        _guard_clean_start(admin_url, targets, deadline)
        engine, topology = _engine_and_topology(admin_url, deadline)
        evidence["engine"] = engine
        evidence["topology"] = topology

        _create_database(admin_url, targets.source_database, deadline)
        cleanup_owned = True
        timeline["source_database_created_at"] = _timestamp(_utc_now())
        source_url = _database_url(admin_url, targets.source_database)
        _apply_all_migrations(source_url, deadline)
        timeline["migrations_completed_at"] = _timestamp(_utc_now())
        evidence["migrations"] = _migration_summary(source_url, deadline)

        pre_payload = f"pre-backup:{run_id}:{source_sha}"
        pre_marker_at = _insert_marker(source_url, PRE_BACKUP_MARKER_KEY, pre_payload, deadline)
        timeline["pre_backup_marker_at"] = _timestamp(pre_marker_at)

        source_manifest = _schema_manifest(source_url, deadline)
        source_data = _data_snapshot(source_url, deadline)
        backup_started_at = _utc_now()
        backup_started_monotonic = time.monotonic()
        timeline["backup_started_at"] = _timestamp(backup_started_at)
        with _connection(admin_url, deadline) as conn:
            conn.execute(_backup_statement(targets)).fetchall()
        backup_completed_at = _utc_now()
        backup_completed_monotonic = time.monotonic()
        timeline["backup_completed_at"] = _timestamp(backup_completed_at)
        backup_restore_point_at = _backup_restore_point(admin_url, targets, deadline)
        if not backup_started_at <= backup_restore_point_at <= backup_completed_at:
            raise RuntimeError("backup restore point is outside the observed backup interval")
        timeline["backup_restore_point_at"] = _timestamp(backup_restore_point_at)

        post_payload = f"post-backup:{run_id}:{source_sha}"
        post_marker_at = _insert_marker(source_url, POST_BACKUP_MARKER_KEY, post_payload, deadline)
        timeline["post_backup_marker_at"] = _timestamp(post_marker_at)

        source_loss_started_at = _utc_now()
        source_loss_started_monotonic = time.monotonic()
        with _connection(admin_url, deadline) as conn:
            _drop_database(
                conn,
                targets.source_database,
                allowed={targets.source_database, targets.restore_database},
            )
        source_loss_completed_at = _utc_now()
        timeline["source_loss_started_at"] = _timestamp(source_loss_started_at)
        timeline["source_loss_completed_at"] = _timestamp(source_loss_completed_at)

        restore_started_at = _utc_now()
        restore_started_monotonic = time.monotonic()
        timeline["restore_started_at"] = _timestamp(restore_started_at)
        with _connection(admin_url, deadline) as conn:
            conn.execute(_restore_statement(targets)).fetchall()
        restore_completed_at = _utc_now()
        timeline["restore_completed_at"] = _timestamp(restore_completed_at)
        restore_url = _database_url(admin_url, targets.restore_database)

        restored_markers = _markers(restore_url, deadline)
        restored_manifest = _schema_manifest(restore_url, deadline)
        restored_data = _data_snapshot(restore_url, deadline)
        validation_completed_at = _utc_now()
        validation_completed_monotonic = time.monotonic()
        timeline["validation_completed_at"] = _timestamp(validation_completed_at)

        pre_present = restored_markers.get(PRE_BACKUP_MARKER_KEY) == pre_payload
        post_absent = POST_BACKUP_MARKER_KEY not in restored_markers
        schema_matches = restored_manifest == source_manifest
        data_matches = restored_data == source_data
        evidence["validation"] = {
            "markers": {
                "pre_backup_present": pre_present,
                "post_backup_absent": post_absent,
            },
            "schema_identity": {
                "matches": schema_matches,
                "source": _manifest_summary(source_manifest),
                "restored": _manifest_summary(restored_manifest),
            },
            "data_identity": {
                "matches": data_matches,
                "source": source_data,
                "restored": restored_data,
            },
        }
        evidence["measurements"] = {
            "backup_seconds": round(backup_completed_monotonic - backup_started_monotonic, 6),
            "recovery_point_gap_seconds": _duration(
                backup_restore_point_at, source_loss_started_at
            ),
            "recovery_point_gap_basis": (
                "CockroachDB backup end_time restore point through the start of "
                "simulated source loss"
            ),
            "first_unrestored_write_age_seconds": _duration(post_marker_at, source_loss_started_at),
            "restore_to_validation_seconds": round(time.monotonic() - restore_started_monotonic, 6),
            "source_loss_to_validation_seconds": round(
                validation_completed_monotonic - source_loss_started_monotonic, 6
            ),
        }
        if not all((pre_present, post_absent, schema_matches, data_matches)):
            raise RuntimeError("restored database failed marker, schema, or data validation")
    except Exception as exc:
        error = exc
    finally:
        if cleanup_owned:
            cleanup, cleanup_errors = _cleanup_resources(admin_url, targets)
        else:
            cleanup = {"skipped_unowned_resources": True}
            cleanup_errors = []
        evidence["cleanup"] = cleanup
        timeline["cleanup_completed_at"] = _timestamp(_utc_now())
        if cleanup_errors:
            evidence["cleanup_errors"] = cleanup_errors
            if error is None:
                error = RuntimeError("; ".join(cleanup_errors))
        completed_at = _utc_now()
        timeline["completed_at"] = _timestamp(completed_at)
        evidence["elapsed_seconds"] = _duration(started_at, completed_at)
        if error is None:
            evidence["status"] = "passed"
        else:
            evidence["status"] = "failed"
            evidence["error"] = {
                "type": type(error).__name__,
                "message": str(error).replace(admin_url, "<redacted-admin-url>"),
            }
        _write_evidence(output, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--confirm-source-loss", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if not args.admin_url:
        parser.error("--admin-url or DATABASE_URL is required")
    evidence = run_drill(
        admin_url=args.admin_url,
        run_id=args.run_id,
        source_sha=args.source_sha,
        confirmation=args.confirm_source_loss,
        output=args.output,
        timeout_seconds=args.timeout_seconds,
    )
    print(f"recovery drill: {evidence['status']}; evidence={args.output}")
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
