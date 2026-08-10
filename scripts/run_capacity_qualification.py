"""Produce bounded capacity evidence in one disposable local CockroachDB database."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
SCHEMA_VERSION = "hindsight.capacity_qualification.v1"
TARGETS = {"vectors": 100_000, "tenants": 20, "clients": 20, "backlog_messages": 1_000}
VECTOR_DIMENSIONS = 1024
ROWS_PER_TENANT = TARGETS["vectors"] // TARGETS["tenants"]
MAX_DURATION_SECONDS = 1_200
MAX_STORAGE_BYTES = 1_500_000_000
MAX_EXTERNAL_COST_USD = 0
MAX_CLIENTS = 20
SEED_SHARDS = 5
MAX_CLEANUP_SECONDS = 120
RUN_ID_PATTERN = re.compile(r"[a-z0-9]{8,20}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DATABASE_PREFIX = "hindsight_capacity_"
PROFILE_ID = "capacity-synthetic-v1"
NAMESPACE_PREFIX = "capacity.synthetic"
LIMITATIONS = [
    "This is a single-node local CockroachDB benchmark, not a production topology.",
    "The deterministic synthetic vectors and in-process backlog do not model live traffic.",
    "Synthetic fixture loading restores write triggers before qualification; it does not "
    "measure application ingestion throughput or realtime outbox load.",
    "The measurements are benchmark evidence and are not production SLO claims.",
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_inputs(admin_url: str, run_id: str, source_sha: str, timeout_seconds: int) -> str:
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
    if SHA_PATTERN.fullmatch(source_sha) is None:
        raise ValueError("source SHA must be a full lowercase Git SHA")
    if not 60 <= timeout_seconds <= MAX_DURATION_SECONDS:
        raise ValueError(f"timeout must be between 60 and {MAX_DURATION_SECONDS} seconds")
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
        _refresh_cleanup_timeouts(conn, deadline)
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database)))
        _refresh_cleanup_timeouts(conn, deadline)
        remaining = {str(row[0]) for row in conn.execute("SHOW DATABASES").fetchall()}
    if database in remaining:
        raise RuntimeError("disposable capacity database still exists after cleanup")


def _refresh_cleanup_timeouts(conn: psycopg.Connection[Any], deadline: Deadline) -> None:
    timeout = f"{deadline.remaining(MAX_CLEANUP_SECONDS) * 1000}ms"
    conn.execute("SELECT set_config('statement_timeout', %s, false)", (timeout,))
    conn.execute("SELECT set_config('lock_timeout', %s, false)", (timeout,))


def _migrate(database_url: str, deadline: Deadline) -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/migrate.py")],
        cwd=ROOT,
        env={**__import__("os").environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=deadline.remaining(600),
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(f"migration failed: {(detail[-1] if detail else 'unknown error')[:400]}")


def _tenant_id(run_id: str, number: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"hindsight-capacity:{run_id}:tenant:{number}")


def _vector(number: int) -> str:
    values = ["0"] * VECTOR_DIMENSIONS
    values[(number - 1) % VECTOR_DIMENSIONS] = "1"
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


def _load_seed_shard(database_url: str, deadline: Deadline, shard: int) -> None:
    with _connection(database_url, deadline) as conn:
        shard_params = (SEED_SHARDS, shard)
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(
            """
            INSERT INTO semantic_beliefs (tenant_id, id, namespace)
            SELECT tenant.tenant_id, seed.memory_id, tenant.namespace
            FROM capacity_seed AS seed
            JOIN capacity_tenant_seed AS tenant USING (tenant_number)
            WHERE mod(seed.tenant_number - 1, %s) = %s
            """,
            shard_params,
        )
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(
            """
            INSERT INTO semantic_memories (
                tenant_id, id, belief_id, version_number, namespace, content, metadata,
                writer, source_ref, justification, producer_decision_id, transition_kind,
                content_schema, structured_payload, payload_digest, lineage_status,
                trust_status, prompt_safety_status, prompt_safety_scanner_version,
                prompt_safety_reason_codes
            )
            SELECT tenant.tenant_id, seed.memory_id, seed.memory_id, 1, tenant.namespace,
                   'synthetic capacity row ' || seed.ordinal::STRING, '{}'::JSONB,
                   'capacity.synthetic', 'capacity:' || seed.ordinal::STRING,
                   'Bounded synthetic index qualification', tenant.decision_id, 'assertion',
                   'capacity.synthetic.v1', jsonb_build_object('ordinal', seed.ordinal),
                   sha256(('capacity:' || seed.ordinal::STRING)::BYTES),
                   'complete', 'active', 'clear', 'capacity.synthetic.v1', '[]'::JSONB
            FROM capacity_seed AS seed
            JOIN capacity_tenant_seed AS tenant USING (tenant_number)
            WHERE mod(seed.tenant_number - 1, %s) = %s
            """,
            shard_params,
        )
        _refresh_qualification_timeout(conn, deadline)
        conn.execute(
            """
            INSERT INTO semantic_memory_vectors (
                tenant_id, memory_id, profile_id, namespace, content_digest, embedding
            )
            SELECT tenant.tenant_id, seed.memory_id, %s, tenant.namespace,
                   sha256(('capacity:' || seed.ordinal::STRING)::BYTES), tenant.embedding
            FROM capacity_seed AS seed
            JOIN capacity_tenant_seed AS tenant USING (tenant_number)
            WHERE mod(seed.tenant_number - 1, %s) = %s
            """,
            (PROFILE_ID, *shard_params),
        )


def _seed_shard_worker(
    database_url: str,
    expires_at: float,
    shard: int,
    results: multiprocessing.Queue,
) -> None:
    try:
        _load_seed_shard(database_url, Deadline(expires_at), shard)
    except BaseException as error:
        detail = f"{type(error).__name__}: {error}"[:800]
    else:
        detail = None
    results.put((shard, detail))


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


def _run_seed_shards(database_url: str, deadline: Deadline) -> None:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_seed_shard_worker,
            args=(database_url, deadline.expires_at, shard, results),
            name=f"capacity-seed-shard-{shard}",
        )
        for shard in range(SEED_SHARDS)
    ]
    started: list[multiprocessing.Process] = []
    received: set[int] = set()
    try:
        for process in processes:
            process.start()
            started.append(process)
        while len(received) < SEED_SHARDS:
            deadline.remaining()
            try:
                shard, error = results.get(
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
            if shard in received or shard not in range(SEED_SHARDS):
                raise RuntimeError("capacity seed shard returned an invalid duplicate result")
            received.add(shard)
            if error is not None:
                raise RuntimeError(f"capacity seed shard {shard} failed: {error}")
        for process in processes:
            process.join(timeout=max(0.0, deadline.expires_at - time.monotonic()))
        if any(process.is_alive() or process.exitcode != 0 for process in processes):
            raise RuntimeError("capacity seed processes did not exit successfully")
    except BaseException:
        _stop_seed_processes(started, Deadline.after(MAX_CLEANUP_SECONDS))
        raise
    finally:
        results.close()
        results.join_thread()


def _seal_and_measure(
    database_url: str,
    run_id: str,
    number: int,
    deadline: Deadline,
    completion_sequence: int,
) -> dict[str, int]:
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
        return _check_storage(
            database_url,
            deadline,
            completion_sequence=completion_sequence,
            completed_tenants=completion_sequence,
            conn=conn,
        )


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


def _seed(database_url: str, run_id: str, deadline: Deadline) -> dict[str, Any]:
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
    _run_seed_shards(database_url, deadline)
    storage_checks: list[dict[str, int]] = []
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
        storage_checks.append(
            _seal_and_measure(
                database_url,
                run_id,
                number,
                deadline,
                len(storage_checks) + 1,
            )
        )
    return {
        "name": "vector_seed",
        "duration_seconds": round(time.monotonic() - started, 6),
        "batches": TARGETS["tenants"],
        "storage_checks": storage_checks,
        "peak_storage_bytes": max(row["bytes"] for row in storage_checks),
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
    database_url: str, run_id: str, number: int, statement_timeout_seconds: int
) -> dict[str, Any]:
    started = time.monotonic()
    tenant_id = str(_tenant_id(run_id, number))
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{statement_timeout_seconds * 1000}ms",),
        )
        conn.execute("SELECT set_config('hindsight.tenant_id', %s, false)", (tenant_id,))
        plan = explain_semantic_vector_search(
            conn,
            tenant_id=tenant_id,
            namespace=_namespace(number),
            profile_id=PROFILE_ID,
            query_vector=[1.0 if i == number - 1 else 0.0 for i in range(VECTOR_DIMENSIONS)],
            limit=5,
        )
        spans = qualify_semantic_vector_plan(plan)
    return {
        "client": number,
        "tenant_id": tenant_id,
        "latency_ms": round((time.monotonic() - started) * 1000, 6),
        "qualified_index": TENANT_VECTOR_INDEX,
        "prefix_spans": spans,
        "plan": plan,
    }


def _exercise_clients(database_url: str, run_id: str, deadline: Deadline) -> list[dict[str, Any]]:
    statement_timeout_seconds = deadline.remaining()
    with ThreadPoolExecutor(max_workers=MAX_CLIENTS) as executor:
        rows = list(
            executor.map(
                lambda number: _client_probe(
                    database_url, run_id, number, statement_timeout_seconds
                ),
                range(1, 21),
            )
        )
    deadline.remaining()
    return rows


def _exercise_backlog() -> dict[str, Any]:
    started = time.monotonic()
    backlog: queue.Queue[int] = queue.Queue(maxsize=TARGETS["backlog_messages"])
    observed_max_pending = 0
    for message_id in range(TARGETS["backlog_messages"]):
        backlog.put_nowait(message_id)
        observed_max_pending = max(observed_max_pending, backlog.qsize())
    pending_before_drain = backlog.qsize()
    ready = threading.Barrier(MAX_CLIENTS)
    initial_reads = threading.Barrier(MAX_CLIENTS)

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
        counts = list(executor.map(lambda _client: drain(), range(MAX_CLIENTS)))
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
    database_url: str, run_id: str, source_sha: str, deadline: Deadline
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    _migrate(database_url, deadline)
    seed = _seed(database_url, run_id, deadline)
    vector_count, per_tenant = _counts(database_url, run_id, deadline)
    clients = _exercise_clients(database_url, run_id, deadline)
    backlog = _exercise_backlog()
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
        "schema_version": SCHEMA_VERSION,
        "main_sha": source_sha,
        "qualified": True,
        "index": TENANT_VECTOR_INDEX,
        "vector_dimensions": VECTOR_DIMENSIONS,
        "vector_count": vector_count,
        "tenant_count": len(per_tenant),
        "per_tenant_counts": per_tenant,
        "plans": clients,
    }
    capacity = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": source_sha,
        "targets": TARGETS,
        "method": {
            "database": "disposable_local_single_node_cockroachdb",
            "vectors": "deterministic_synthetic_one_hot_1024d",
            "seeding": "five_set_based_shards_with_serialized_completion_checks",
            "fixture_write_triggers": "restored_and_catalog_verified_before_completion_checks",
            "clients": "twenty_bounded_parallel_index_queries",
            "backlog": "in_process_synthetic_accounting_without_live_worker",
        },
        "environment": {
            "isolation": "run_scoped_database_and_compose_project",
            "paid_model_calls": 0,
            "live_worker_invocations": 0,
        },
        "ceilings": {
            "duration_seconds": MAX_DURATION_SECONDS,
            "storage_bytes": MAX_STORAGE_BYTES,
            "clients": MAX_CLIENTS,
            "external_cost_usd": MAX_EXTERNAL_COST_USD,
        },
        "raw_measurements": [
            seed,
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
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=MAX_DURATION_SECONDS)
    args = parser.parse_args()
    admin_url = _validate_inputs(args.admin_url, args.run_id, args.source_sha, args.timeout_seconds)
    _verify_checkout(args.source_sha)
    database = f"{DATABASE_PREFIX}{args.run_id}"
    database_url = _database_url(admin_url, database)
    deadline = Deadline.after(args.timeout_seconds)
    qualification: dict[str, Any] | None = None
    capacity: dict[str, Any] | None = None
    created = False
    cleanup: dict[str, Any]
    try:
        _create_database(admin_url, database, deadline)
        created = True
        qualification, capacity = _run(database_url, args.run_id, args.source_sha, deadline)
    finally:
        cleanup_started = _timestamp()
        cleanup_error: str | None = None
        try:
            if created:
                _drop_database(admin_url, database, Deadline.after(MAX_CLEANUP_SECONDS))
        except Exception as error:  # cleanup evidence must survive the primary failure
            cleanup_error = f"{type(error).__name__}: {error}"
        cleanup = {
            "schema_version": SCHEMA_VERSION,
            "source_revision": args.source_sha,
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
    qualification_path = args.output_dir / "index-qualification.json"
    cleanup_path = args.output_dir / "cleanup.json"
    _write_json(qualification_path, qualification)
    capacity["index_qualification"] = {
        "qualified": True,
        "main_sha": args.source_sha,
        "artifact_sha256": _sha256(qualification_path),
    }
    capacity["cleanup"] = {
        "database_removed": True,
        "artifact_sha256": _sha256(cleanup_path),
    }
    report_path = args.output_dir / "capacity-report.json"
    _write_json(report_path, capacity)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": args.source_sha,
        "artifacts": {
            path.name: _sha256(path) for path in (qualification_path, report_path, cleanup_path)
        },
    }
    _write_json(args.output_dir / "artifact-manifest.json", manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
