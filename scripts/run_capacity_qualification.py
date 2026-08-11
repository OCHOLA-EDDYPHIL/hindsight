"""Produce bounded capacity evidence in one disposable local CockroachDB database."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hindsight.vector_index_qualification import (  # noqa: E402
    TENANT_VECTOR_INDEX,
    explain_semantic_vector_search,
    qualify_semantic_vector_plan,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "hindsight.capacity_qualification.v5"
DIAGNOSTIC_SCHEMA_VERSION = "hindsight.capacity_resource_diagnostic.v2"
ATTEMPT_PROGRESS_SCHEMA_VERSION = "hindsight.capacity_attempt_progress.v2"
ATTEMPT_PROGRESS_FILENAME = "capacity-attempt-progress.json"
MODES = frozenset({"diagnostic", "qualification"})
QUALIFICATION_TARGETS = {
    "vectors": 100_000,
    "tenants": 20,
    "clients": 20,
    "backlog_messages": 1_000,
}
DIAGNOSTIC_TARGETS = {
    "vectors": 75_000,
    "tenants": 15,
    "clients": 20,
    "backlog_messages": 1_000,
}
TARGETS = dict(QUALIFICATION_TARGETS)
VECTOR_DIMENSIONS = 1024
ROWS_PER_TENANT = TARGETS["vectors"] // TARGETS["tenants"]
VECTOR_CODE_BITS = 13
VECTOR_CODE_OFFSET = TARGETS["tenants"]
VECTOR_CODE_MULTIPLIER = 4051
VECTOR_CODE_MAGNITUDE = "0.05"
VECTOR_METHOD = "deterministic_tenant_anchored_13bit_1024d"
MAX_DURATION_SECONDS = 1_200
MAX_STORAGE_BYTES = 1_500_000_000
MAX_EXTERNAL_COST_USD = 0
MAX_CLIENTS = 20
SEED_SHARDS = 1
SEEDING_METHOD = (
    "single_bounded_writer_one_atomic_copy_transaction_per_tenant_"
    "between_exact_legacy_index_drop_and_restore"
)
FIXTURE_VECTOR_INDEX_METHOD = (
    "legacy_only_before_seed_then_none_during_copy_then_legacy_restored_"
    "before_populated_tenant_index_migration"
)
DATABASE_METHOD = "disposable_local_single_node_cockroachdb_in_memory_2_gib_explicit_memory_budgets"
RUNTIME_MEMORY_ENVELOPE = {
    "image": (
        "cockroachdb/cockroach@sha256:"
        "53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
    ),
    "image_id": (
        "sha256:53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
    ),
    "image_platform": "linux/amd64",
    "execution_topology": "owner_runner_sibling_dind_capacity_cgroup_v2",
    "start_args": [
        "--store=type=mem,size=2GiB",
        "--cache=128MiB",
        "--max-sql-memory=128MiB",
        "--max-tsdb-memory=64MiB",
        "--max-go-memory=3GiB",
    ],
    "capacity_boundary": {
        "cgroup_version": 2,
        "memory_max_bytes": 4 * 1024**3,
        "memory_swap_max": "max",
        "swap_devices": 0,
        "cpu_quota_us": 150_000,
        "cpu_period_us": 100_000,
    },
    "telemetry_probe": {
        "image": (
            "cockroachdb/cockroach@sha256:"
            "53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
        ),
        "image_id": (
            "sha256:53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
        ),
        "image_platform": "linux/amd64",
        "cgroup_namespace": "host",
        "network": "none",
        "read_only": True,
        "user": "65534:65534",
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "privileged": False,
        "workspace_mounts": 0,
        "pids_limit": 16,
        "memory_bytes": 32 * 1024**2,
        "nano_cpus": 100_000_000,
        "nominal_sample_sleep_seconds": 0.25,
        "maximum_sample_gap_seconds": 1.0,
    },
    "memory_bytes": {
        "store": 2 * 1024**3,
        "cache": 128 * 1024**2,
        "sql": 128 * 1024**2,
        "tsdb": 64 * 1024**2,
        "go": 3 * 1024**3,
    },
}
MAX_CLEANUP_SECONDS = 120
CLEANUP_JOB_SECONDS = 90
CLEANUP_DROP_SECONDS = 25
CLEANUP_VERIFY_SECONDS = 5
CLEANUP_POLL_SECONDS = 0.5
RUN_ID_PATTERN = re.compile(r"[a-z0-9]{8,20}")
EXECUTION_ID_PATTERN = re.compile(r"capacity_[0-9]+_1_(diagnostic|qualification)")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DATABASE_PREFIX = "hindsight_capacity_"
PROFILE_ID = "capacity-synthetic-v1"
NAMESPACE_PREFIX = "capacity.synthetic"
BASE_SCHEMA_THROUGH = "0029e_product_credential_locators.sql"
LEGACY_VECTOR_INDEX = "semantic_memory_vectors_embedding_idx"
LEGACY_VECTOR_MIGRATION = "0009_embedding_profiles_and_retrieval.sql"
LEGACY_VECTOR_INDEX_CREATE_SQL = (
    "CREATE VECTOR INDEX IF NOT EXISTS semantic_memory_vectors_embedding_idx "
    "ON semantic_memory_vectors (embedding)"
)
TENANT_VECTOR_MIGRATION = "0030_tenant_vector_cosine_index.sql"
QUALIFIED_VECTOR_INDEXES = frozenset({LEGACY_VECTOR_INDEX, TENANT_VECTOR_INDEX})
CLEANUP_VECTOR_INDEX_JOB_TYPES = frozenset({"SCHEMA CHANGE", "NEW SCHEMA CHANGE"})
CLEANUP_VECTOR_INDEX_CANCELLABLE_STATUSES = frozenset(
    {"pending", "running", "retry-running", "paused", "pause-requested"}
)
CLEANUP_VECTOR_INDEX_WAIT_STATUSES = frozenset(
    {
        *CLEANUP_VECTOR_INDEX_CANCELLABLE_STATUSES,
        "cancel-requested",
        "reverting",
        "retry-reverting",
    }
)
CLEANUP_VECTOR_INDEX_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
LIMITATIONS = [
    "This is a single-node local in-memory CockroachDB benchmark, not a production topology.",
    "The deterministic synthetic vectors and in-process backlog do not model live traffic.",
    "Fixture loading temporarily removes and exactly recreates the legacy vector index; "
    "seed timing is not indexed-ingestion throughput.",
    "Synthetic fixture loading restores write triggers before qualification; it does not "
    "measure application ingestion throughput or realtime outbox load.",
    "The measurements are benchmark evidence and are not production SLO claims.",
    "The 2 GiB in-memory store size is configured capacity, not a kernel allocation cap; "
    "the Go limit is soft, database budgets overlap, cache is outside the Go limit, and "
    "the 4 GiB DinD cgroup remains the hard database execution boundary.",
    "The sampled 4 GiB and 1.5 CPU cgroup contains the sibling DinD daemon and its database "
    "descendants. The qualification producer runs in the owner-runner sibling, so this "
    "evidence makes no whole-host or producer memory-envelope claim.",
    "In-memory SQL temporary storage retains CockroachDB 25.4.5's fixed 100 MiB default.",
]
BULK_SEED_GUARDS = {
    "semantic_beliefs": "semantic_beliefs_tenant_lifecycle_state",
    "semantic_memories": "semantic_memories_tenant_lifecycle_state",
    "semantic_memory_vectors": "semantic_memory_vectors_tenant_lifecycle_state",
}
BULK_SEED_MEMORY_TRIGGERS = {
    "semantic_memory_open_producer": (
        "CREATE TRIGGER semantic_memory_open_producer BEFORE INSERT ON semantic_memories "
        "FOR EACH ROW EXECUTE FUNCTION guard_open_memory_producer()"
    ),
    "semantic_memories_tenant_event_outbox": (
        "CREATE TRIGGER semantic_memories_tenant_event_outbox "
        "AFTER INSERT OR UPDATE OR DELETE ON semantic_memories "
        "FOR EACH ROW EXECUTE FUNCTION emit_tenant_event_outbox()"
    ),
}


class Deadline:
    def __init__(self, expires_at: float) -> None:
        self.expires_at = expires_at

    @classmethod
    def after(cls, seconds: int) -> Deadline:
        return cls(time.monotonic() + seconds)

    def remaining(self, maximum: int = MAX_DURATION_SECONDS) -> int:
        value = math.ceil(self.expires_at - time.monotonic())
        if value <= 0:
            raise TimeoutError("capacity qualification exceeded its duration ceiling")
        return min(maximum, value)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _require_empty_output_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise RuntimeError("capacity output directory must be an empty directory")
    else:
        path.mkdir(parents=True)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class CapacityAttemptProgress:
    def __init__(
        self,
        path: Path,
        *,
        source_revision: str,
        run_id: str,
        execution_id: str,
        database: str,
        mode: str,
        targets: dict[str, int],
    ) -> None:
        started_at = _timestamp()
        self.path = path
        self._started_monotonic = time.monotonic()
        self._phase_started_monotonic: float | None = None
        self._document: dict[str, Any] = {
            "schema_version": ATTEMPT_PROGRESS_SCHEMA_VERSION,
            "kind": "capacity_attempt_diagnostic",
            "qualification_evidence": False,
            "source_revision": source_revision,
            "run_id": run_id,
            "execution_id": execution_id,
            "database": database,
            "mode": mode,
            "targets": dict(targets),
            "status": "running",
            "started_at": started_at,
            "updated_at": started_at,
            "elapsed_seconds": 0.0,
            "current_phase": None,
            "completed_phases": [],
            "failure": None,
        }
        self._checkpoint()

    def _elapsed(self, started: float | None = None) -> float:
        origin = started if started is not None else self._started_monotonic
        return round(time.monotonic() - origin, 6)

    def _checkpoint(self, **updates: Any) -> None:
        document = {
            **self._document,
            **updates,
            "updated_at": _timestamp(),
            "elapsed_seconds": self._elapsed(),
        }
        _write_json_atomic(self.path, document)
        self._document = document

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if self._document["status"] != "running" or self._document["current_phase"] is not None:
            raise RuntimeError("capacity attempt diagnostic phase state is invalid")
        phase_started_monotonic = time.monotonic()
        self._checkpoint(current_phase={"name": name, "started_at": _timestamp()})
        self._phase_started_monotonic = phase_started_monotonic
        try:
            yield
        except BaseException:
            raise
        else:
            completed_phases = [
                *self._document["completed_phases"],
                {
                    "name": name,
                    "duration_seconds": self._elapsed(phase_started_monotonic),
                },
            ]
            self._checkpoint(current_phase=None, completed_phases=completed_phases)
            self._phase_started_monotonic = None

    def record_failure(self, error: BaseException) -> None:
        try:
            current_phase = self._document["current_phase"]
            phase_name = current_phase["name"] if isinstance(current_phase, dict) else None
            phase_duration = (
                self._elapsed(self._phase_started_monotonic)
                if self._phase_started_monotonic is not None
                else None
            )
            self._checkpoint(
                status="failed",
                current_phase=None,
                failure={
                    "phase": phase_name,
                    "type": type(error).__name__,
                    "message": str(error)[:800],
                    "duration_seconds": phase_duration,
                },
            )
            self._phase_started_monotonic = None
        except BaseException as progress_error:
            print(
                "capacity attempt diagnostic failure checkpoint could not be written: "
                f"{type(progress_error).__name__}",
                file=sys.stderr,
            )

    def mark_workload_completed(self) -> None:
        if self._document["current_phase"] is not None:
            raise RuntimeError("capacity attempt diagnostic has an unfinished phase")
        self._checkpoint(status="workload_completed")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configure_mode(mode: str) -> None:
    global ROWS_PER_TENANT, TARGETS, VECTOR_CODE_OFFSET
    if mode == "qualification":
        targets = QUALIFICATION_TARGETS
    elif mode == "diagnostic":
        targets = DIAGNOSTIC_TARGETS
    else:
        raise ValueError("capacity mode is unsupported")
    if targets["vectors"] % targets["tenants"] != 0:
        raise RuntimeError("capacity target vectors must divide evenly across tenants")
    TARGETS = dict(targets)
    ROWS_PER_TENANT = TARGETS["vectors"] // TARGETS["tenants"]
    VECTOR_CODE_OFFSET = TARGETS["tenants"]


def _validate_inputs(
    admin_url: str,
    run_id: str,
    execution_id: str,
    source_sha: str,
    timeout_seconds: int,
    mode: str = "qualification",
) -> str:
    parts = urlsplit(admin_url)
    if (
        parts.scheme not in {"postgres", "postgresql"}
        or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
        or unquote(parts.username or "") != "root"
        or unquote(parts.path.lstrip("/")) != "defaultdb"
    ):
        raise ValueError("capacity qualification requires root on loopback defaultdb")
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run id must contain 8-20 lowercase letters or digits")
    if (
        EXECUTION_ID_PATTERN.fullmatch(execution_id) is None
        or not execution_id.endswith(f"_{mode}")
        or run_id != hashlib.sha256(execution_id.encode()).hexdigest()[:16]
    ):
        raise ValueError("run id must be derived from the original workflow execution identity")
    if SHA_PATTERN.fullmatch(source_sha) is None:
        raise ValueError("source SHA must be a full lowercase Git SHA")
    if not 60 <= timeout_seconds <= MAX_DURATION_SECONDS:
        raise ValueError(f"timeout must be between 60 and {MAX_DURATION_SECONDS} seconds")
    if mode not in MODES:
        raise ValueError("capacity mode is unsupported")
    return admin_url


def _verify_checkout(source_sha: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != source_sha:
        raise RuntimeError("capacity qualification checkout differs from the exact source SHA")
    for args in (["git", "diff", "--quiet"], ["git", "diff", "--cached", "--quiet"]):
        if subprocess.run(args, cwd=ROOT, check=False).returncode != 0:
            raise RuntimeError("capacity qualification requires a clean exact-source checkout")


def _database_url(admin_url: str, database: str) -> str:
    return urlunsplit(urlsplit(admin_url)._replace(path=f"/{database}"))


def _is_disposable(database: str) -> bool:
    return re.fullmatch(r"hindsight_capacity_[a-z0-9]{8,20}", database) is not None


def _connection(url: str, deadline: Deadline) -> psycopg.Connection[Any]:
    conn = psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=min(5, deadline.remaining()),
        application_name="hindsight-capacity-qualification",
    )
    conn.execute(
        "SELECT set_config('statement_timeout', %s, false)",
        (f"{deadline.remaining() * 1000}ms",),
    )
    return conn


def _refresh_qualification_timeout(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    conn.execute(
        "SELECT set_config('statement_timeout', %s, false)",
        (f"{deadline.remaining() * 1000}ms",),
    )


def _create_database(admin_url: str, database: str, deadline: Deadline) -> None:
    if not _is_disposable(database):
        raise RuntimeError("refusing to create a non-capacity database")
    with _connection(admin_url, deadline) as conn:
        existing = {str(row[0]) for row in conn.execute("SHOW DATABASES").fetchall()}
        if database in existing:
            raise RuntimeError("refusing to reuse an existing capacity database")
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def _drop_database(admin_url: str, database: str, deadline: Deadline) -> None:
    if not _is_disposable(database):
        raise RuntimeError("refusing to drop a non-capacity database")
    with psycopg.connect(
        admin_url,
        autocommit=True,
        connect_timeout=min(5, deadline.remaining(MAX_CLEANUP_SECONDS)),
        application_name="hindsight-capacity-cleanup",
    ) as conn:
        cancel_deadline = Deadline(
            min(
                deadline.expires_at - CLEANUP_DROP_SECONDS - CLEANUP_VERIFY_SECONDS,
                time.monotonic() + CLEANUP_JOB_SECONDS,
            )
        )
        _cancel_disposable_vector_index_jobs(conn, database, cancel_deadline)
        if deadline.remaining(MAX_CLEANUP_SECONDS) < (
            CLEANUP_DROP_SECONDS + CLEANUP_VERIFY_SECONDS
        ):
            raise TimeoutError("capacity cleanup did not preserve database-drop headroom")
        _set_cleanup_timeouts(conn, CLEANUP_DROP_SECONDS)
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database)))
        _set_cleanup_timeouts(conn, CLEANUP_VERIFY_SECONDS)
        remaining = {str(row[0]) for row in conn.execute("SHOW DATABASES").fetchall()}
    if database in remaining:
        raise RuntimeError("disposable capacity database still exists after cleanup")


def _refresh_cleanup_timeouts(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    _set_cleanup_timeouts(conn, deadline.remaining(MAX_CLEANUP_SECONDS))


def _set_cleanup_timeouts(conn: psycopg.Connection[Any], seconds: int) -> None:
    timeout = f"{seconds * 1000}ms"
    conn.execute("SELECT set_config('statement_timeout', %s, false)", (timeout,))
    conn.execute("SELECT set_config('lock_timeout', %s, false)", (timeout,))


def _vector_index_job_prefixes(database: str) -> tuple[str, ...]:
    if not _is_disposable(database):
        raise RuntimeError("refusing to inspect jobs for a non-capacity database")
    return tuple(
        f"CREATE VECTOR INDEX IF NOT EXISTS {index} ON {database}.public.semantic_memory_vectors ("
        for index in sorted(QUALIFIED_VECTOR_INDEXES)
    )


def _legacy_index_drop_job_description(database: str) -> str:
    if not _is_disposable(database):
        raise RuntimeError("refusing to inspect jobs for a non-capacity database")
    return f"DROP INDEX {database}.public.semantic_memory_vectors@{LEGACY_VECTOR_INDEX}"


def _vector_index_job_predicate(prefixes: tuple[str, ...]) -> str:
    return " OR ".join(
        "substring(description, 1, length(%s::STRING)) = %s::STRING" for _prefix in prefixes
    )


def _vector_index_job_params(prefixes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value for prefix in prefixes for value in (prefix, prefix))


def _cancel_disposable_vector_index_jobs(
    conn: psycopg.Connection[Any], database: str, deadline: Deadline
) -> None:
    prefixes = _vector_index_job_prefixes(database)
    drop_description = _legacy_index_drop_job_description(database)
    predicate = _vector_index_job_predicate(prefixes)
    prefix_params = _vector_index_job_params(prefixes)
    _refresh_cleanup_timeouts(conn, deadline)
    rows = conn.execute(
        f"""
        SELECT job_id, job_type, status, description
        FROM crdb_internal.jobs
        WHERE job_type IN ('SCHEMA CHANGE', 'NEW SCHEMA CHANGE')
          AND status NOT IN ('succeeded', 'failed', 'canceled')
          AND (({predicate}) OR description = %s::STRING)
        ORDER BY job_id
        """,
        (*prefix_params, drop_description),
    ).fetchall()
    if not rows:
        return

    jobs = {int(row[0]): (str(row[1]), str(row[2]), str(row[3])) for row in rows}
    unexpected = {
        job_id: {"job_type": job_type, "status": status, "description": description}
        for job_id, (job_type, status, description) in jobs.items()
        if job_type not in CLEANUP_VECTOR_INDEX_JOB_TYPES
        or status not in CLEANUP_VECTOR_INDEX_WAIT_STATUSES
        or not (
            description == drop_description
            or any(description.startswith(prefix) for prefix in prefixes)
        )
    }
    if unexpected:
        raise RuntimeError(f"refusing to cancel unexpected capacity jobs: {unexpected}")

    cancellable_ids = sorted(
        job_id
        for job_id, (_job_type, status, description) in jobs.items()
        if status in CLEANUP_VECTOR_INDEX_CANCELLABLE_STATUSES
        and any(description.startswith(prefix) for prefix in prefixes)
    )
    if cancellable_ids:
        _refresh_cleanup_timeouts(conn, deadline)
        conn.execute(
            f"""
            CANCEL JOBS (
                SELECT job_id
                FROM crdb_internal.jobs
                WHERE job_id = ANY(%s)
                  AND job_type IN ('SCHEMA CHANGE', 'NEW SCHEMA CHANGE')
                  AND status IN (
                      'pending', 'running', 'retry-running', 'paused', 'pause-requested'
                  )
                  AND ({predicate})
            )
            """,
            (cancellable_ids, *prefix_params),
        )

    job_ids = sorted(jobs)
    while True:
        _refresh_cleanup_timeouts(conn, deadline)
        current_rows = conn.execute(
            "SELECT job_id, status FROM crdb_internal.jobs WHERE job_id = ANY(%s) ORDER BY job_id",
            (job_ids,),
        ).fetchall()
        current = {int(row[0]): str(row[1]) for row in current_rows}
        missing = sorted(set(job_ids) - set(current))
        if missing:
            raise RuntimeError(
                f"capacity cleanup jobs disappeared before terminal state: {missing}"
            )
        failed = {
            job_id: status
            for job_id, status in current.items()
            if status
            not in (CLEANUP_VECTOR_INDEX_WAIT_STATUSES | CLEANUP_VECTOR_INDEX_TERMINAL_STATUSES)
        }
        if failed:
            raise RuntimeError(f"capacity cleanup jobs entered unsafe states: {failed}")
        if all(status in CLEANUP_VECTOR_INDEX_TERMINAL_STATUSES for status in current.values()):
            return
        time.sleep(CLEANUP_POLL_SECONDS)


def _migrate(
    database_url: str,
    deadline: Deadline,
    *,
    name: str,
    through: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    command = [sys.executable, str(ROOT / "scripts/migrate.py")]
    if through is not None:
        command.extend(["--through", through])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=deadline.remaining(),
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(f"migration failed: {(detail[-1] if detail else 'unknown error')[:400]}")
    return {
        "name": name,
        "duration_seconds": round(time.monotonic() - started, 6),
        "through": through or "latest",
    }


def _tenant_id(run_id: str, number: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"hindsight-capacity:{run_id}:tenant:{number}")


def _vector_code(ordinal: int) -> int:
    if type(ordinal) is not int or not 1 <= ordinal <= ROWS_PER_TENANT:
        raise ValueError("capacity vector ordinal is outside the bounded fixture")
    return (ordinal * VECTOR_CODE_MULTIPLIER) & ((1 << VECTOR_CODE_BITS) - 1)


def _vector(number: int, ordinal: int = 0) -> str:
    if type(number) is not int or not 1 <= number <= TARGETS["tenants"]:
        raise ValueError("capacity vector tenant is outside the bounded fixture")
    if type(ordinal) is not int or not 0 <= ordinal <= ROWS_PER_TENANT:
        raise ValueError("capacity vector ordinal is outside the bounded fixture")
    values = ["0"] * VECTOR_DIMENSIONS
    values[number - 1] = "1"
    if ordinal:
        code = _vector_code(ordinal)
        for bit in range(VECTOR_CODE_BITS):
            values[VECTOR_CODE_OFFSET + bit] = (
                VECTOR_CODE_MAGNITUDE if code & (1 << bit) else f"-{VECTOR_CODE_MAGNITUDE}"
            )
    return "[" + ",".join(values) + "]"


def _namespace(number: int) -> str:
    return f"{NAMESPACE_PREFIX}.{number:02d}"


def _create_seed_staging(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    _refresh_qualification_timeout(conn, deadline)
    conn.execute(
        """
        CREATE TABLE capacity_tenant_seed (
            tenant_number INT8 PRIMARY KEY,
            tenant_id UUID NOT NULL,
            slug STRING NOT NULL,
            namespace STRING NOT NULL,
            decision_id STRING NOT NULL,
            embedding VECTOR(1024) NOT NULL
        )
        """
    )
    _refresh_qualification_timeout(conn, deadline)
    conn.execute(
        """
        CREATE TABLE capacity_seed (
            tenant_number INT8 NOT NULL,
            ordinal INT8 NOT NULL,
            memory_id UUID NOT NULL,
            PRIMARY KEY (tenant_number, ordinal)
        )
        """
    )


def _insert_tenant_staging(
    conn: psycopg.Connection[Any], *, run_id: str, tenant_number: int
) -> None:
    conn.execute(
        "INSERT INTO capacity_tenant_seed "
        "(tenant_number, tenant_id, slug, namespace, decision_id, embedding) "
        "VALUES (%s, %s, %s, %s, %s, %s::VECTOR(1024))",
        (
            tenant_number,
            _tenant_id(run_id, tenant_number),
            f"capacity-{run_id}-{tenant_number:02d}",
            _namespace(tenant_number),
            f"capacity:{run_id}:{tenant_number}",
            _vector(tenant_number),
        ),
    )


def _insert_seed_staging(
    conn: psycopg.Connection[Any], *, tenant_number: int, row_count: int
) -> None:
    conn.execute(
        "INSERT INTO capacity_seed SELECT %s, value, gen_random_uuid() "
        "FROM generate_series(1, %s) AS generated(value)",
        (tenant_number, row_count),
    )


def _remove_bulk_seed_guards(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    for table, trigger in BULK_SEED_GUARDS.items():
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(
            sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(trigger), sql.Identifier(table))
        )


def _restore_bulk_seed_guards(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    for table, trigger in BULK_SEED_GUARDS.items():
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT OR UPDATE OR DELETE ON {} "
                "FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state()"
            ).format(sql.Identifier(trigger), sql.Identifier(table))
        )


def _remove_bulk_seed_memory_triggers(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    for trigger in BULK_SEED_MEMORY_TRIGGERS:
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(
            sql.SQL("DROP TRIGGER {} ON {}").format(
                sql.Identifier(trigger), sql.Identifier("semantic_memories")
            )
        )


def _restore_bulk_seed_memory_triggers(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    for statement in BULK_SEED_MEMORY_TRIGGERS.values():
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(sql.SQL(statement))


def _prepare_seed_load(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    _refresh_qualification_timeout(conn, deadline)
    conn.execute(
        "INSERT INTO tenants (id, slug, tenant_kind) "
        "SELECT tenant_id, slug, 'diagnostic' FROM capacity_tenant_seed"
    )
    _refresh_qualification_timeout(conn, deadline)
    conn.execute(
        "INSERT INTO memory_namespaces (tenant_id, namespace) "
        "SELECT tenant_id, namespace FROM capacity_tenant_seed"
    )
    _refresh_qualification_timeout(conn, deadline)
    conn.execute(
        """
        INSERT INTO memory_decisions (
            tenant_id, id, actor, decision_kind, purpose, namespace, status
        )
        SELECT tenant_id, decision_id, 'capacity.synthetic', 'capacity_seed',
               'Bounded synthetic index qualification', namespace, 'open'
        FROM capacity_tenant_seed
        """
    )
    _remove_bulk_seed_guards(conn, deadline)
    _remove_bulk_seed_memory_triggers(conn, deadline)


def _require_atomic_copy(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    _refresh_qualification_timeout(conn, deadline)
    conn.execute("SET copy_from_atomic_enabled = true")
    row = conn.execute("SHOW copy_from_atomic_enabled").fetchone()
    if row is None or str(row[0]).lower() not in {"on", "true"}:
        raise RuntimeError("capacity vector COPY atomicity is not enabled")


def _load_seed_shard(
    database_url: str, deadline: Deadline, shard: int, mode: str | None = None
) -> tuple[int, int, list[dict[str, int]]]:
    if mode is not None:
        _configure_mode(mode)
    with _connection(database_url, deadline) as conn:
        _require_atomic_copy(conn, deadline)
        vector_inserts = 0
        vector_transactions = 0
        storage_checks: list[dict[str, int]] = []
        for tenant_number in range(shard + 1, TARGETS["tenants"] + 1, SEED_SHARDS):
            _refresh_qualification_timeout(conn, deadline)
            tenant = conn.execute(
                "SELECT tenant_id, namespace FROM capacity_tenant_seed WHERE tenant_number = %s",
                (tenant_number,),
            ).fetchone()
            if tenant is None:
                raise RuntimeError("capacity vector staging tenant is missing")
            staged_rows = conn.execute(
                "SELECT ordinal, memory_id FROM capacity_seed "
                "WHERE tenant_number = %s ORDER BY ordinal",
                (tenant_number,),
            ).fetchall()
            if len(staged_rows) != ROWS_PER_TENANT or [int(row[0]) for row in staged_rows] != list(
                range(1, ROWS_PER_TENANT + 1)
            ):
                raise RuntimeError("capacity vector staging rows are not exact")
            try:
                with conn.transaction():
                    _refresh_qualification_timeout(conn, deadline)
                    conn.execute(
                        """
                        INSERT INTO semantic_beliefs (tenant_id, id, namespace)
                        SELECT tenant.tenant_id, seed.memory_id, tenant.namespace
                        FROM capacity_seed AS seed
                        JOIN capacity_tenant_seed AS tenant USING (tenant_number)
                        WHERE seed.tenant_number = %s
                        """,
                        (tenant_number,),
                    )
                    _refresh_qualification_timeout(conn, deadline)
                    conn.execute(
                        """
                        INSERT INTO semantic_memories (
                            tenant_id, id, belief_id, version_number, namespace, content,
                            metadata, writer, source_ref, justification,
                            producer_decision_id, transition_kind, content_schema,
                            structured_payload, payload_digest, lineage_status, trust_status,
                            prompt_safety_status, prompt_safety_scanner_version,
                            prompt_safety_reason_codes
                        )
                        SELECT tenant.tenant_id, seed.memory_id, seed.memory_id, 1,
                               tenant.namespace,
                               'synthetic capacity row ' || seed.ordinal::STRING, '{}'::JSONB,
                               'capacity.synthetic', 'capacity:' || seed.ordinal::STRING,
                               'Bounded synthetic index qualification', tenant.decision_id,
                               'assertion', 'capacity.synthetic.v1',
                               jsonb_build_object('ordinal', seed.ordinal),
                               sha256(('capacity:' || seed.ordinal::STRING)::BYTES),
                               'complete', 'active', 'clear', 'capacity.synthetic.v1',
                               '[]'::JSONB
                        FROM capacity_seed AS seed
                        JOIN capacity_tenant_seed AS tenant USING (tenant_number)
                        WHERE seed.tenant_number = %s
                        """,
                        (tenant_number,),
                    )
                    with conn.cursor().copy(
                        """
                        COPY semantic_memory_vectors (
                            tenant_id, memory_id, profile_id, namespace,
                            content_digest, embedding
                        ) FROM STDIN
                        """
                    ) as copy:
                        for ordinal, memory_id in staged_rows:
                            deadline.remaining()
                            copy.write_row(
                                (
                                    tenant[0],
                                    memory_id,
                                    PROFILE_ID,
                                    tenant[1],
                                    hashlib.sha256(f"capacity:{ordinal}".encode()).hexdigest(),
                                    _vector(tenant_number, int(ordinal)),
                                )
                            )
            except Exception as error:
                raise RuntimeError(
                    f"capacity vector copy failed at shard {shard}, tenant {tenant_number}"
                ) from error
            vector_inserts += len(staged_rows)
            vector_transactions += 1
            storage_checks.append(
                _check_storage(
                    database_url,
                    deadline,
                    completion_sequence=vector_transactions,
                    completed_tenants=vector_transactions,
                )
            )
        return vector_inserts, vector_transactions, storage_checks


def _seed_shard_worker(
    database_url: str,
    expires_at: float,
    shard: int,
    results: multiprocessing.Queue,
    mode: str = "qualification",
) -> None:
    try:
        vector_inserts, vector_transactions, storage_checks = _load_seed_shard(
            database_url, Deadline(expires_at), shard, mode
        )
    except BaseException as error:
        detail = f"{type(error).__name__}: {error}"[:800]
        vector_inserts = None
        vector_transactions = None
        storage_checks = None
    else:
        detail = None
    results.put((shard, vector_inserts, vector_transactions, storage_checks, detail))


def _stop_seed_processes(processes: list[multiprocessing.Process], deadline: Deadline) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    grace_expires_at = min(deadline.expires_at, time.monotonic() + 5)
    for process in processes:
        process.join(timeout=max(0.0, grace_expires_at - time.monotonic()))
    for process in processes:
        if process.is_alive():
            process.kill()
    for process in processes:
        process.join(timeout=max(0.0, deadline.expires_at - time.monotonic()))
    if any(process.is_alive() for process in processes):
        raise RuntimeError("capacity seed processes did not terminate after cleanup")


def _run_seed_shards(
    database_url: str, deadline: Deadline, mode: str = "qualification"
) -> tuple[int, int, list[dict[str, int]]]:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_seed_shard_worker,
            args=(database_url, deadline.expires_at, shard, results, mode),
            name=f"capacity-seed-shard-{shard}",
        )
        for shard in range(SEED_SHARDS)
    ]
    started: list[multiprocessing.Process] = []
    received: set[int] = set()
    vector_inserts = 0
    vector_transactions = 0
    storage_checks: list[dict[str, int]] = []
    try:
        for process in processes:
            process.start()
            started.append(process)
        while len(received) < SEED_SHARDS:
            deadline.remaining()
            try:
                result = results.get(
                    timeout=min(1.0, max(0.01, deadline.expires_at - time.monotonic()))
                )
            except queue.Empty:
                missing = [
                    process
                    for process in processes
                    if process.exitcode is not None
                    and int(process.name.rsplit("-", 1)[1]) not in received
                ]
                if missing:
                    raise RuntimeError(f"{missing[0].name} exited without delivering a result")
                continue
            if not isinstance(result, tuple) or len(result) != 5:
                raise RuntimeError("capacity seed shard returned an invalid result")
            shard, inserted_rows, transactions, shard_storage_checks, error = result
            if type(shard) is not int or shard in received or shard not in range(SEED_SHARDS):
                raise RuntimeError("capacity seed shard returned an invalid duplicate result")
            received.add(shard)
            if error is not None:
                raise RuntimeError(f"capacity seed shard {shard} failed: {error}")
            expected_rows = ROWS_PER_TENANT * len(
                range(shard + 1, TARGETS["tenants"] + 1, SEED_SHARDS)
            )
            if type(inserted_rows) is not int or inserted_rows != expected_rows:
                raise RuntimeError("capacity seed shard returned an invalid vector row count")
            expected_transactions = len(range(shard + 1, TARGETS["tenants"] + 1, SEED_SHARDS))
            if type(transactions) is not int or transactions != expected_transactions:
                raise RuntimeError("capacity seed shard returned an invalid transaction count")
            if (
                not isinstance(shard_storage_checks, list)
                or len(shard_storage_checks) != expected_transactions
                or any(
                    not isinstance(row, dict)
                    or type(row.get("completion_sequence")) is not int
                    or type(row.get("completed_tenants")) is not int
                    or row["completed_tenants"] != row["completion_sequence"]
                    or type(row.get("bytes")) is not int
                    or not 0 < row["bytes"] <= MAX_STORAGE_BYTES
                    for row in shard_storage_checks
                )
            ):
                raise RuntimeError("capacity seed shard returned invalid storage checks")
            vector_inserts += inserted_rows
            vector_transactions += transactions
            storage_checks.extend(shard_storage_checks)
        for process in processes:
            process.join(timeout=max(0.0, deadline.expires_at - time.monotonic()))
        if any(process.is_alive() or process.exitcode != 0 for process in processes):
            raise RuntimeError("capacity seed processes did not exit successfully")
        if vector_inserts != TARGETS["vectors"]:
            raise RuntimeError("capacity seed vector row count is not exact")
        if vector_transactions != TARGETS["tenants"]:
            raise RuntimeError("capacity seed vector transaction count is not exact")
        storage_checks.sort(key=lambda row: row["completion_sequence"])
        if [row["completion_sequence"] for row in storage_checks] != list(
            range(1, TARGETS["tenants"] + 1)
        ):
            raise RuntimeError("capacity seed storage-check sequence is not exact")
        return vector_inserts, vector_transactions, storage_checks
    except BaseException:
        _stop_seed_processes(started, Deadline.after(MAX_CLEANUP_SECONDS))
        raise
    finally:
        results.close()
        results.join_thread()


def _seal_seed_decision(
    database_url: str,
    run_id: str,
    number: int,
    deadline: Deadline,
) -> None:
    tenant_id = _tenant_id(run_id, number)
    with _connection(database_url, deadline) as conn:
        conn.execute("SELECT set_config('hindsight.tenant_id', %s, false)", (str(tenant_id),))
        updated = conn.execute(
            """
            UPDATE memory_decisions
            SET status = 'sealed', sealed_at = now()
            WHERE tenant_id = %s AND id = %s AND status = 'open'
            """,
            (tenant_id, f"capacity:{run_id}:{number}"),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"tenant {number} seed decision was not sealed exactly once")


def _verify_bulk_seed_provenance(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    expected_triggers = set(BULK_SEED_GUARDS.values()) | set(BULK_SEED_MEMORY_TRIGGERS)
    _refresh_qualification_timeout(conn, deadline)
    restored_triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT trigger_name FROM information_schema.triggers "
            "WHERE trigger_name = ANY(%s)",
            (list(expected_triggers),),
        ).fetchall()
    }
    if restored_triggers != expected_triggers:
        raise RuntimeError("capacity fixture write triggers were not restored exactly")
    _refresh_qualification_timeout(conn, deadline)
    missing_producers = conn.execute(
        """
        SELECT count(*)::INT8
        FROM semantic_memories AS memory
        LEFT JOIN memory_decisions AS decision
          ON decision.tenant_id = memory.tenant_id
         AND decision.id = memory.producer_decision_id
         AND decision.status = 'open'
        WHERE decision.id IS NULL
        """
    ).fetchone()
    if int(missing_producers[0]) != 0:
        raise RuntimeError("capacity seed contains memory without an open producer decision")
    _refresh_qualification_timeout(conn, deadline)
    outbox_rows = conn.execute(
        "SELECT count(*)::INT8 FROM tenant_event_outbox WHERE aggregate_type = 'semantic_memories'"
    ).fetchone()
    if int(outbox_rows[0]) != 0:
        raise RuntimeError("capacity fixture unexpectedly emitted semantic memory outbox rows")


def _seed(
    database_url: str,
    run_id: str,
    deadline: Deadline,
    mode: str = "qualification",
) -> dict[str, Any]:
    started = time.monotonic()
    with _connection(database_url, deadline) as conn:
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                id, provider, model, dimensions, capability, encoder_revision,
                configuration, max_distance, status, activated_at
            ) VALUES (%s, 'synthetic', 'deterministic-one-hot', %s, 'semantic',
                      'capacity-v1', '{"paid_calls":false}'::JSONB, 2.0, 'active', now())
            """,
            (PROFILE_ID, VECTOR_DIMENSIONS),
        )
        conn.execute(
            "UPDATE embedding_index_state SET active_profile_id = %s, generation = 1",
            (PROFILE_ID,),
        )
        _refresh_qualification_timeout(conn, deadline)
        _create_seed_staging(conn, deadline)
        for number in range(1, TARGETS["tenants"] + 1):
            _refresh_qualification_timeout(conn, deadline)
            _insert_tenant_staging(conn, run_id=run_id, tenant_number=number)
            _insert_seed_staging(conn, tenant_number=number, row_count=ROWS_PER_TENANT)
        _prepare_seed_load(conn, deadline)
    vector_insert_rows, vector_insert_transactions, storage_checks = _run_seed_shards(
        database_url, deadline, mode
    )
    with _connection(database_url, deadline) as conn:
        _refresh_qualification_timeout(conn, deadline)
        _restore_bulk_seed_guards(conn, deadline)
        _restore_bulk_seed_memory_triggers(conn, deadline)
        _verify_bulk_seed_provenance(conn, deadline)
        _refresh_qualification_timeout(conn, deadline)
        conn.execute("DROP TABLE capacity_seed, capacity_tenant_seed")
        _refresh_qualification_timeout(conn, deadline)
        conn.execute("ANALYZE semantic_memory_vectors")
    for number in range(1, TARGETS["tenants"] + 1):
        _seal_seed_decision(database_url, run_id, number, deadline)
    return {
        "name": "vector_seed",
        "duration_seconds": round(time.monotonic() - started, 6),
        "batches": TARGETS["tenants"],
        "vector_insert_rows": vector_insert_rows,
        "vector_insert_transactions": vector_insert_transactions,
        "vector_insert_workers": SEED_SHARDS,
        "vector_insert_client_retries": 0,
        "storage_checks": storage_checks,
        "peak_storage_bytes": max(row["bytes"] for row in storage_checks),
    }


def _vector_index_names(conn: psycopg.Connection[Any], deadline: Deadline) -> frozenset[str]:
    _refresh_qualification_timeout(conn, deadline)
    rows = conn.execute(
        "SELECT DISTINCT index_name FROM [SHOW INDEXES FROM semantic_memory_vectors] "
        "WHERE index_name = ANY(%s)",
        (sorted(QUALIFIED_VECTOR_INDEXES),),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _suspend_legacy_vector_index(database_url: str, deadline: Deadline) -> dict[str, Any]:
    started = time.monotonic()
    with _connection(database_url, deadline) as conn:
        before = _vector_index_names(conn, deadline)
        if before != frozenset({LEGACY_VECTOR_INDEX}):
            raise RuntimeError("capacity seed requires only the exact legacy vector index")
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(
            sql.SQL("DROP INDEX {}@{}").format(
                sql.Identifier("semantic_memory_vectors"),
                sql.Identifier(LEGACY_VECTOR_INDEX),
            )
        )
        after = _vector_index_names(conn, deadline)
    if after:
        raise RuntimeError("capacity seed vector index suspension was not exact")
    return {
        "name": "legacy_index_suspension",
        "duration_seconds": round(time.monotonic() - started, 6),
        "removed_index": LEGACY_VECTOR_INDEX,
        "before_indexes": sorted(before),
        "after_indexes": sorted(after),
    }


def _restore_legacy_vector_index(database_url: str, deadline: Deadline) -> dict[str, Any]:
    started = time.monotonic()
    with _connection(database_url, deadline) as conn:
        before = _vector_index_names(conn, deadline)
        if before:
            raise RuntimeError("legacy vector index restore requires an index-free seed table")
        _refresh_qualification_timeout(conn, deadline)
        vector_count = int(
            conn.execute("SELECT count(*)::INT8 FROM semantic_memory_vectors").fetchone()[0]
        )
        if vector_count != TARGETS["vectors"]:
            raise RuntimeError(
                "legacy vector index restore input is not the exact populated target"
            )
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(LEGACY_VECTOR_INDEX_CREATE_SQL)
        after = _vector_index_names(conn, deadline)
        storage_bytes = _storage_bytes(database_url, deadline, conn=conn)
    if after != frozenset({LEGACY_VECTOR_INDEX}):
        raise RuntimeError("legacy vector index was not restored exactly")
    if storage_bytes > MAX_STORAGE_BYTES:
        raise RuntimeError("legacy vector index restore exceeded the storage ceiling")
    return {
        "name": "legacy_index_restore",
        "duration_seconds": round(time.monotonic() - started, 6),
        "vectors": vector_count,
        "migration": LEGACY_VECTOR_MIGRATION,
        "restored_index": LEGACY_VECTOR_INDEX,
        "before_indexes": sorted(before),
        "after_indexes": sorted(after),
        "storage_bytes": storage_bytes,
    }


def _tenant_index_build_input(database_url: str, deadline: Deadline) -> dict[str, Any]:
    with _connection(database_url, deadline) as conn:
        _refresh_qualification_timeout(conn, deadline)
        vector_count = int(
            conn.execute("SELECT count(*)::INT8 FROM semantic_memory_vectors").fetchone()[0]
        )
        indexes = _vector_index_names(conn, deadline)
    if vector_count != TARGETS["vectors"]:
        raise RuntimeError("tenant vector index build input is not the exact populated target")
    if indexes != frozenset({LEGACY_VECTOR_INDEX}):
        raise RuntimeError("tenant vector index must be absent while the legacy index stays live")
    return {
        "name": "tenant_index_build_input",
        "vectors": vector_count,
        "present_indexes": sorted(indexes),
        "absent_index": TENANT_VECTOR_INDEX,
        "next_migration": TENANT_VECTOR_MIGRATION,
    }


def _qualified_vector_indexes(database_url: str, deadline: Deadline) -> dict[str, Any]:
    with _connection(database_url, deadline) as conn:
        indexes = _vector_index_names(conn, deadline)
        storage_bytes = _storage_bytes(database_url, deadline, conn=conn)
    if indexes != QUALIFIED_VECTOR_INDEXES:
        raise RuntimeError("capacity qualification requires both live vector indexes")
    if storage_bytes > MAX_STORAGE_BYTES:
        raise RuntimeError("qualified vector indexes exceeded the storage ceiling")
    return {
        "name": "vector_indexes",
        "indexes": sorted(indexes),
        "storage_bytes": storage_bytes,
    }


def _counts(database_url: str, run_id: str, deadline: Deadline) -> tuple[int, list[dict[str, Any]]]:
    per_tenant: list[dict[str, Any]] = []
    with _connection(database_url, deadline) as conn:
        for number in range(1, TARGETS["tenants"] + 1):
            tenant_id = str(_tenant_id(run_id, number))
            conn.execute("SELECT set_config('hindsight.tenant_id', %s, false)", (tenant_id,))
            row = conn.execute(
                """
                SELECT count(*)::INT8
                FROM semantic_memory_vectors
                WHERE tenant_id = %s AND namespace = %s AND profile_id = %s
                """,
                (tenant_id, _namespace(number), PROFILE_ID),
            ).fetchone()
            per_tenant.append({"tenant_id": tenant_id, "vectors": int(row[0])})
    return sum(row["vectors"] for row in per_tenant), per_tenant


def _client_probe(
    database_url: str,
    run_id: str,
    client_number: int,
    tenant_number: int,
    statement_timeout_seconds: int,
) -> dict[str, Any]:
    started = time.monotonic()
    tenant_id = str(_tenant_id(run_id, tenant_number))
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{statement_timeout_seconds * 1000}ms",),
        )
        conn.execute("SELECT set_config('hindsight.tenant_id', %s, false)", (tenant_id,))
        plan = explain_semantic_vector_search(
            conn,
            tenant_id=tenant_id,
            namespace=_namespace(tenant_number),
            profile_id=PROFILE_ID,
            query_vector=[1.0 if i == tenant_number - 1 else 0.0 for i in range(VECTOR_DIMENSIONS)],
            limit=5,
        )
        spans = qualify_semantic_vector_plan(plan)
    return {
        "client": client_number,
        "tenant_id": tenant_id,
        "latency_ms": round((time.monotonic() - started) * 1000, 6),
        "qualified_index": TENANT_VECTOR_INDEX,
        "prefix_spans": spans,
        "plan": plan,
    }


def _exercise_clients(database_url: str, run_id: str, deadline: Deadline) -> list[dict[str, Any]]:
    statement_timeout_seconds = deadline.remaining()
    with ThreadPoolExecutor(max_workers=MAX_CLIENTS) as executor:
        probes = [
            (client_number, ((client_number - 1) % TARGETS["tenants"]) + 1)
            for client_number in range(1, TARGETS["clients"] + 1)
        ]
        rows = list(
            executor.map(
                lambda probe: _client_probe(
                    database_url,
                    run_id,
                    probe[0],
                    probe[1],
                    statement_timeout_seconds,
                ),
                probes,
            )
        )
    deadline.remaining()
    return rows


def _exercise_backlog() -> dict[str, Any]:
    started = time.monotonic()
    client_count = TARGETS["clients"]
    backlog: queue.Queue[int] = queue.Queue(maxsize=TARGETS["backlog_messages"])
    observed_max_pending = 0
    for message_id in range(TARGETS["backlog_messages"]):
        backlog.put_nowait(message_id)
        observed_max_pending = max(observed_max_pending, backlog.qsize())
    pending_before_drain = backlog.qsize()
    ready = threading.Barrier(client_count)
    initial_reads = threading.Barrier(client_count)

    def drain() -> int:
        ready.wait(timeout=10)
        backlog.get_nowait()
        backlog.task_done()
        count = 1
        initial_reads.wait(timeout=10)
        while True:
            try:
                backlog.get_nowait()
            except queue.Empty:
                return count
            backlog.task_done()
            count += 1

    with ThreadPoolExecutor(max_workers=MAX_CLIENTS) as executor:
        counts = list(executor.map(lambda _client: drain(), range(client_count)))
    backlog.join()
    pending_after_drain = backlog.qsize()
    return {
        "queue_capacity": TARGETS["backlog_messages"],
        "messages_enqueued": TARGETS["backlog_messages"],
        "messages_drained": sum(counts),
        "messages_accounted_for": sum(counts),
        "clients": len(counts),
        "per_client_counts": counts,
        "pending_before_drain": pending_before_drain,
        "pending_after_drain": pending_after_drain,
        "observed_max_pending": observed_max_pending,
        "duration_seconds": round(time.monotonic() - started, 6),
        "live_worker_invocations": 0,
        "paid_model_calls": 0,
    }


def _storage_bytes(
    database_url: str,
    deadline: Deadline,
    *,
    conn: psycopg.Connection[Any] | None = None,
) -> int:
    database = unquote(urlsplit(database_url).path.lstrip("/"))
    statement = sql.SQL(
        "SELECT coalesce(sum(range_size), 0)::INT8 FROM [SHOW RANGES FROM DATABASE {} WITH DETAILS]"
    ).format(sql.Identifier(database))
    if conn is not None:
        _refresh_qualification_timeout(conn, deadline)
        row = conn.execute(statement).fetchone()
    else:
        with _connection(database_url, deadline) as owned_conn:
            row = owned_conn.execute(statement).fetchone()
    return int(row[0])


def _check_storage(
    database_url: str,
    deadline: Deadline,
    *,
    completion_sequence: int,
    completed_tenants: int,
    conn: psycopg.Connection[Any] | None = None,
) -> dict[str, int]:
    storage_bytes = _storage_bytes(database_url, deadline, conn=conn)
    observation = {
        "completion_sequence": completion_sequence,
        "completed_tenants": completed_tenants,
        "bytes": storage_bytes,
    }
    if storage_bytes > MAX_STORAGE_BYTES:
        raise RuntimeError(
            "capacity qualification exceeded its storage ceiling after "
            f"completion {completion_sequence}"
        )
    return observation


def _run(
    database_url: str,
    run_id: str,
    execution_id: str,
    source_sha: str,
    deadline: Deadline,
    progress: CapacityAttemptProgress,
    mode: str = "qualification",
) -> tuple[dict[str, Any], dict[str, Any]]:
    _configure_mode(mode)
    started = time.monotonic()
    with progress.phase("base_migrations"):
        base_migration = _migrate(
            database_url,
            deadline,
            name="base_migrations",
            through=BASE_SCHEMA_THROUGH,
        )
    with progress.phase("legacy_index_suspension"):
        legacy_index_suspension = _suspend_legacy_vector_index(database_url, deadline)
    with progress.phase("vector_seed"):
        seed = _seed(database_url, run_id, deadline, mode)
    with progress.phase("legacy_index_restore"):
        legacy_index_restore = _restore_legacy_vector_index(database_url, deadline)
    with progress.phase("tenant_index_build_input"):
        index_build_input = _tenant_index_build_input(database_url, deadline)
    with progress.phase("post_seed_migrations"):
        post_seed_migrations = _migrate(
            database_url,
            deadline,
            name="post_seed_migrations",
        )
    with progress.phase("vector_indexes"):
        vector_indexes = _qualified_vector_indexes(database_url, deadline)
    with progress.phase("vector_counts"):
        vector_count, per_tenant = _counts(database_url, run_id, deadline)
    with progress.phase("bounded_clients"):
        clients = _exercise_clients(database_url, run_id, deadline)
    with progress.phase("synthetic_backlog"):
        backlog = _exercise_backlog()
    with progress.phase("storage"):
        storage_bytes = _storage_bytes(database_url, deadline)
    duration = round(time.monotonic() - started, 6)
    if vector_count != TARGETS["vectors"] or len(per_tenant) != TARGETS["tenants"]:
        raise RuntimeError("seeded vector counts do not match the bounded target")
    if any(row["vectors"] != ROWS_PER_TENANT for row in per_tenant):
        raise RuntimeError("tenant vector distribution is not exact")
    if storage_bytes > MAX_STORAGE_BYTES:
        raise RuntimeError("capacity qualification exceeded its storage ceiling")
    if duration > MAX_DURATION_SECONDS:
        raise RuntimeError("capacity qualification exceeded its duration ceiling")
    qualification = {
        "schema_version": SCHEMA_VERSION if mode == "qualification" else DIAGNOSTIC_SCHEMA_VERSION,
        "main_sha": source_sha,
        "execution_id": execution_id,
        "qualified": mode == "qualification",
        "observation_only": mode == "diagnostic",
        "mode": mode,
        "qualification_evidence": mode == "qualification",
        "index": TENANT_VECTOR_INDEX,
        "indexes": vector_indexes["indexes"],
        "vector_dimensions": VECTOR_DIMENSIONS,
        "vector_count": vector_count,
        "tenant_count": len(per_tenant),
        "per_tenant_counts": per_tenant,
        "plans": clients,
    }
    capacity = {
        "schema_version": SCHEMA_VERSION if mode == "qualification" else DIAGNOSTIC_SCHEMA_VERSION,
        "source_revision": source_sha,
        "execution_id": execution_id,
        "mode": mode,
        "kind": "bounded_capacity_evidence_source",
        "qualification_evidence": mode == "qualification",
        "targets": dict(TARGETS),
        "final_targets": dict(QUALIFICATION_TARGETS),
        "method": {
            "database": DATABASE_METHOD,
            "vectors": VECTOR_METHOD,
            "seeding": SEEDING_METHOD,
            "fixture_vector_indexes": FIXTURE_VECTOR_INDEX_METHOD,
            "fixture_write_triggers": "restored_and_catalog_verified_before_completion_checks",
            "clients": f"{TARGETS['clients']}_bounded_parallel_index_queries",
            "backlog": "in_process_synthetic_accounting_without_live_worker",
        },
        "environment": {
            "isolation": "run_scoped_database_and_compose_project",
            "paid_model_calls": 0,
            "live_worker_invocations": 0,
            "runtime_memory_envelope": RUNTIME_MEMORY_ENVELOPE,
        },
        "ceilings": {
            "duration_seconds": MAX_DURATION_SECONDS,
            "storage_bytes": MAX_STORAGE_BYTES,
            "clients": MAX_CLIENTS,
            "external_cost_usd": MAX_EXTERNAL_COST_USD,
        },
        "raw_measurements": [
            base_migration,
            legacy_index_suspension,
            seed,
            legacy_index_restore,
            index_build_input,
            post_seed_migrations,
            vector_indexes,
            {"name": "vector_counts", "total": vector_count, "per_tenant": per_tenant},
            {"name": "bounded_clients", "clients": clients},
            {"name": "synthetic_backlog", **backlog},
            {"name": "storage", "bytes": storage_bytes},
            {"name": "total", "duration_seconds": duration},
        ],
        "limitations": LIMITATIONS,
    }
    return qualification, capacity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=MAX_DURATION_SECONDS)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    args = parser.parse_args()
    admin_url = _validate_inputs(
        args.admin_url,
        args.run_id,
        args.execution_id,
        args.source_sha,
        args.timeout_seconds,
        args.mode,
    )
    _configure_mode(args.mode)
    _verify_checkout(args.source_sha)
    _require_empty_output_directory(args.output_dir)
    database = f"{DATABASE_PREFIX}{args.run_id}"
    database_url = _database_url(admin_url, database)
    deadline = Deadline.after(args.timeout_seconds)
    progress = CapacityAttemptProgress(
        args.output_dir / ATTEMPT_PROGRESS_FILENAME,
        source_revision=args.source_sha,
        run_id=args.run_id,
        execution_id=args.execution_id,
        database=database,
        mode=args.mode,
        targets=TARGETS,
    )
    qualification: dict[str, Any] | None = None
    capacity: dict[str, Any] | None = None
    created = False
    cleanup: dict[str, Any]
    try:
        with progress.phase("database_create"):
            _create_database(admin_url, database, deadline)
            created = True
        qualification, capacity = _run(
            database_url,
            args.run_id,
            args.execution_id,
            args.source_sha,
            deadline,
            progress,
            args.mode,
        )
        progress.mark_workload_completed()
    except BaseException as error:
        progress.record_failure(error)
        raise
    finally:
        cleanup_started = _timestamp()
        cleanup_error: str | None = None
        try:
            if created:
                _drop_database(admin_url, database, Deadline.after(MAX_CLEANUP_SECONDS))
        except Exception as error:  # cleanup evidence must survive the primary failure
            cleanup_error = f"{type(error).__name__}: {error}"
        cleanup = {
            "schema_version": (
                SCHEMA_VERSION if args.mode == "qualification" else DIAGNOSTIC_SCHEMA_VERSION
            ),
            "source_revision": args.source_sha,
            "mode": args.mode,
            "execution_id": args.execution_id,
            "database": database,
            "started_at": cleanup_started,
            "completed_at": _timestamp(),
            "database_removed": created and cleanup_error is None,
            "timeout_seconds": MAX_CLEANUP_SECONDS,
            "error": cleanup_error,
        }
        _write_json(args.output_dir / "cleanup.json", cleanup)
    if cleanup["database_removed"] is not True:
        raise RuntimeError("disposable database cleanup was not verified")
    assert qualification is not None and capacity is not None
    cleanup_path = args.output_dir / "cleanup.json"
    if args.mode == "diagnostic":
        diagnostic = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "kind": "capacity_resource_diagnostic",
            "mode": "diagnostic",
            "acceptance_eligible": False,
            "qualification_evidence": False,
            "source_revision": args.source_sha,
            "execution_id": args.execution_id,
            "targets": dict(TARGETS),
            "final_targets": dict(QUALIFICATION_TARGETS),
            "method": capacity["method"],
            "environment": capacity["environment"],
            "ceilings": capacity["ceilings"],
            "raw_measurements": capacity["raw_measurements"],
            "index_observation": {
                key: value
                for key, value in qualification.items()
                if key not in {"qualified", "qualification_evidence"}
            },
            "cleanup": {
                "database_removed": True,
                "execution_id": args.execution_id,
                "artifact_sha256": _sha256(cleanup_path),
            },
            "limitations": [
                *capacity["limitations"],
                "This diagnostic cannot be used as final capacity qualification evidence.",
            ],
        }
        _write_json(args.output_dir / "capacity-diagnostic.json", diagnostic)
        return 0
    qualification_path = args.output_dir / "index-qualification.json"
    _write_json(qualification_path, qualification)
    capacity["index_qualification"] = {
        "qualified": True,
        "main_sha": args.source_sha,
        "execution_id": args.execution_id,
        "artifact_sha256": _sha256(qualification_path),
    }
    capacity["cleanup"] = {
        "database_removed": True,
        "execution_id": args.execution_id,
        "artifact_sha256": _sha256(cleanup_path),
    }
    report_path = args.output_dir / "capacity-report.json"
    _write_json(report_path, capacity)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": args.source_sha,
        "execution_id": args.execution_id,
        "mode": "qualification",
        "kind": "capacity_artifact_manifest",
        "artifacts": {
            path.name: _sha256(path) for path in (qualification_path, report_path, cleanup_path)
        },
    }
    _write_json(args.output_dir / "artifact-manifest.json", manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
