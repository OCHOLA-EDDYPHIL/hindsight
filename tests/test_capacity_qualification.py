from __future__ import annotations

import hashlib
import importlib.util
import json
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40


def _execution_id(mode: str) -> str:
    return f"capacity_123_1_{mode}"


def _database_for(mode: str) -> str:
    digest = hashlib.sha256(_execution_id(mode).encode()).hexdigest()[:16]
    return f"hindsight_capacity_{digest}"


def _tenant_uuid(number: int) -> str:
    return f"00000000-0000-0000-0000-{number + 1:012d}"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _qualification(validator):
    return {
        "schema_version": validator.SCHEMA_VERSION,
        "qualified": True,
        "observation_only": False,
        "mode": "qualification",
        "qualification_evidence": True,
        "main_sha": SOURCE_SHA,
        "execution_id": _execution_id("qualification"),
        "index": validator.EXPECTED_INDEX,
        "indexes": validator.EXPECTED_INDEXES,
        "vector_dimensions": 1024,
        "vector_count": 100_000,
        "tenant_count": 20,
        "per_tenant_counts": [
            {"tenant_id": _tenant_uuid(number), "vectors": 5_000} for number in range(20)
        ],
        "plans": [
            {
                "client": number,
                "tenant_id": _tenant_uuid(number - 1),
                "qualified_index": validator.EXPECTED_INDEX,
                "prefix_spans": "[/tenant - /tenant]",
                "plan": f"vector search table: semantic_memory_vectors@{validator.EXPECTED_INDEX}",
            }
            for number in range(1, 21)
        ],
    }


def _report(validator):
    return {
        "schema_version": validator.SCHEMA_VERSION,
        "source_revision": SOURCE_SHA,
        "execution_id": _execution_id("qualification"),
        "kind": "bounded_capacity_evidence_source",
        "mode": "qualification",
        "qualification_evidence": True,
        "index_qualification": {
            "qualified": True,
            "artifact_sha256": "b" * 64,
            "main_sha": SOURCE_SHA,
            "execution_id": _execution_id("qualification"),
        },
        "cleanup": {
            "database_removed": True,
            "execution_id": _execution_id("qualification"),
            "artifact_sha256": "d" * 64,
        },
        "targets": dict(validator.TARGETS),
        "final_targets": dict(validator.TARGETS),
        "ceilings": dict(validator.EXPECTED_CEILINGS),
        "method": {
            "database": validator.EXPECTED_DATABASE_METHOD,
            "vectors": validator.EXPECTED_VECTOR_METHOD,
            "seeding": validator.EXPECTED_SEEDING_METHOD,
            "fixture_vector_indexes": validator.EXPECTED_FIXTURE_VECTOR_INDEXES,
            "clients": "20_bounded_parallel_index_queries",
        },
        "environment": {
            "isolation": "run_scoped_database_and_compose_project",
            "paid_model_calls": 0,
            "live_worker_invocations": 0,
            "runtime_memory_envelope": validator.EXPECTED_RUNTIME_MEMORY_ENVELOPE,
        },
        "raw_measurements": [
            {
                "name": "base_migrations",
                "duration_seconds": 1,
                "through": validator.BASE_SCHEMA_THROUGH,
            },
            {
                "name": "legacy_index_suspension",
                "duration_seconds": 1,
                "removed_index": validator.LEGACY_INDEX,
                "before_indexes": [validator.LEGACY_INDEX],
                "after_indexes": [],
            },
            {
                "name": "vector_seed",
                "duration_seconds": 1,
                "batches": 20,
                "vector_insert_rows": 100_000,
                "vector_insert_transactions": 20,
                "vector_insert_workers": validator.EXPECTED_VECTOR_INSERT_WORKERS,
                "vector_insert_client_retries": 0,
                "storage_checks": [
                    {
                        "completion_sequence": number,
                        "completed_tenants": number,
                        "bytes": number * 1_000,
                    }
                    for number in range(1, 21)
                ],
                "peak_storage_bytes": 20_000,
            },
            {
                "name": "legacy_index_restore",
                "duration_seconds": 1,
                "vectors": 100_000,
                "migration": validator.LEGACY_VECTOR_MIGRATION,
                "restored_index": validator.LEGACY_INDEX,
                "before_indexes": [],
                "after_indexes": [validator.LEGACY_INDEX],
                "storage_bytes": 500_000,
            },
            {
                "name": "tenant_index_build_input",
                "vectors": 100_000,
                "present_indexes": [validator.LEGACY_INDEX],
                "absent_index": validator.EXPECTED_INDEX,
                "next_migration": validator.TENANT_VECTOR_MIGRATION,
            },
            {
                "name": "post_seed_migrations",
                "duration_seconds": 1,
                "through": "latest",
            },
            {
                "name": "vector_indexes",
                "indexes": validator.EXPECTED_INDEXES,
                "storage_bytes": 800_000,
            },
            {
                "name": "vector_counts",
                "total": 100_000,
                "per_tenant": [
                    {"tenant_id": _tenant_uuid(number), "vectors": 5_000} for number in range(20)
                ],
            },
            {"name": "bounded_clients", "clients": _qualification(validator)["plans"]},
            {
                "name": "synthetic_backlog",
                "messages_enqueued": 1_000,
                "messages_drained": 1_000,
                "messages_accounted_for": 1_000,
                "queue_capacity": 1_000,
                "pending_before_drain": 1_000,
                "pending_after_drain": 0,
                "observed_max_pending": 1_000,
                "clients": 20,
                "per_client_counts": [50] * 20,
                "live_worker_invocations": 0,
                "paid_model_calls": 0,
                "duration_seconds": 1,
            },
            {"name": "storage", "bytes": 1_000_000},
            {"name": "total", "duration_seconds": 10},
        ],
        "limitations": ["Benchmark evidence; not production SLO claims."],
    }


def _runtime(validator, *, mode="qualification", peak_bytes=3 * 1024**3, deltas=None):
    before = {
        "low": 0,
        "high": 0,
        "max": 828_396,
        "oom": 0,
        "oom_kill": 0,
        "oom_group_kill": 0,
    }
    event_deltas = {key: 0 for key in before}
    if deltas is not None:
        event_deltas.update(deltas)
    after = {key: before[key] + event_deltas[key] for key in before}
    project = f"hindsight_capacity_123_1_{mode}"
    return {
        "schema_version": validator.RUNTIME_SCHEMA_VERSION,
        "source_revision": SOURCE_SHA,
        "mode": mode,
        "execution_id": _execution_id(mode),
        "compose_project": project,
        "configured": validator.EXPECTED_RUNTIME_MEMORY_ENVELOPE,
        "effective_process": {
            "path": "/cockroach/cockroach.sh",
            "args": validator.EXPECTED_PROCESS_ARGS,
            "configured_command": validator.EXPECTED_PROCESS_ARGS,
            "image": validator.EXPECTED_RUNTIME_MEMORY_ENVELOPE["image"],
            "compose_project": project,
            "compose_service": "crdb",
            "running": True,
            "live_argv": validator.EXPECTED_LIVE_PROCESS_ARGS,
            "effective_memory": {
                "go_limit_bytes": 3 * 1024**3,
                "store_capacity_bytes": 2 * 1024**3,
                "store_count": 1,
            },
        },
        "container_cgroup": {
            "version": 2,
            "memory_max": "max",
            "memory_current_bytes": 100,
            "memory_peak_bytes": peak_bytes,
            "events": {key: 0 for key in before},
        },
        "cgroup": {
            "version": 2,
            "memory_max_bytes": 4 * 1024**3,
            "cpu_quota_us": 150_000,
            "cpu_period_us": 100_000,
            "memory_current_before_bytes": 100,
            "memory_current_after_bytes": 200,
            "kernel_memory_peak_before_bytes": 4 * 1024**3,
            "kernel_memory_peak_after_bytes": 4 * 1024**3,
            "sample_count": 10,
            "sampled_peak_bytes": peak_bytes,
            "events_before": before,
            "events_after": after,
            "event_deltas": event_deltas,
            "pressure_events_zero": all(value == 0 for value in event_deltas.values()),
        },
    }


def _infrastructure_cleanup(validator, *, mode="qualification"):
    return {
        "schema_version": validator.INFRASTRUCTURE_CLEANUP_SCHEMA_VERSION,
        "source_revision": SOURCE_SHA,
        "mode": mode,
        "execution_id": _execution_id(mode),
        "compose_project": f"hindsight_capacity_123_1_{mode}",
        "down_status": 0,
        "container_query_status": 0,
        "volume_query_status": 0,
        "network_query_status": 0,
        "remaining_containers": 0,
        "remaining_volumes": 0,
        "remaining_networks": 0,
        "compose_state_removed": True,
    }


def _validate(validator, document, **kwargs):
    qualification = kwargs.get("qualification")
    cleanup = kwargs.get("cleanup")
    digests = kwargs.get("artifact_digests")
    execution_id = kwargs.setdefault("execution_id", _execution_id("qualification"))
    if isinstance(cleanup, dict):
        cleanup.setdefault("mode", "qualification")
        cleanup.setdefault("execution_id", execution_id)
        if cleanup.get("database") == "hindsight_capacity_abcdefgh":
            cleanup["database"] = _database_for("qualification")
    if qualification is not None and cleanup is not None and digests is not None:
        kwargs.setdefault("runtime", _runtime(validator))
        kwargs.setdefault("infrastructure_cleanup", _infrastructure_cleanup(validator))
        if isinstance(digests, dict):
            digests.setdefault("runtime-pressure.json", "e" * 64)
            digests.setdefault("infrastructure-cleanup.json", "f" * 64)
    return validator.validate(document, **kwargs)


def _diagnostic_bundle(validator, *, duration=10, peak_bytes=3 * 1024**3):
    protocol = _script("validate_capacity_evidence")
    tenant_ids = [_tenant_uuid(number) for number in range(15)]
    plans = [
        {
            "client": number,
            "tenant_id": tenant_ids[(number - 1) % len(tenant_ids)],
            "qualified_index": validator.EXPECTED_INDEX,
            "prefix_spans": "[/tenant - /tenant]",
            "plan": (f"vector search table: semantic_memory_vectors@{validator.EXPECTED_INDEX}"),
        }
        for number in range(1, 21)
    ]
    per_tenant = [{"tenant_id": tenant_id, "vectors": 5_000} for tenant_id in tenant_ids]
    measurements = [
        {
            "name": "base_migrations",
            "duration_seconds": 1,
            "through": protocol.BASE_SCHEMA_THROUGH,
        },
        {
            "name": "legacy_index_suspension",
            "duration_seconds": 1,
            "removed_index": protocol.LEGACY_INDEX,
            "before_indexes": [protocol.LEGACY_INDEX],
            "after_indexes": [],
        },
        {
            "name": "vector_seed",
            "duration_seconds": 1,
            "batches": 15,
            "vector_insert_rows": 75_000,
            "vector_insert_transactions": 15,
            "vector_insert_workers": protocol.EXPECTED_VECTOR_INSERT_WORKERS,
            "vector_insert_client_retries": 0,
            "storage_checks": [
                {
                    "completion_sequence": number,
                    "completed_tenants": number,
                    "bytes": number * 1_000,
                }
                for number in range(1, 16)
            ],
            "peak_storage_bytes": 15_000,
        },
        {
            "name": "legacy_index_restore",
            "duration_seconds": 1,
            "vectors": 75_000,
            "migration": protocol.LEGACY_VECTOR_MIGRATION,
            "restored_index": protocol.LEGACY_INDEX,
            "before_indexes": [],
            "after_indexes": [protocol.LEGACY_INDEX],
            "storage_bytes": 500_000,
        },
        {
            "name": "tenant_index_build_input",
            "vectors": 75_000,
            "present_indexes": [protocol.LEGACY_INDEX],
            "absent_index": validator.EXPECTED_INDEX,
            "next_migration": protocol.TENANT_VECTOR_MIGRATION,
        },
        {"name": "post_seed_migrations", "duration_seconds": 1, "through": "latest"},
        {
            "name": "vector_indexes",
            "indexes": validator.EXPECTED_INDEXES,
            "storage_bytes": 800_000,
        },
        {"name": "vector_counts", "total": 75_000, "per_tenant": per_tenant},
        {"name": "bounded_clients", "clients": plans},
        {
            "name": "synthetic_backlog",
            "messages_enqueued": 1_000,
            "messages_drained": 1_000,
            "messages_accounted_for": 1_000,
            "queue_capacity": 1_000,
            "pending_before_drain": 1_000,
            "pending_after_drain": 0,
            "observed_max_pending": 1_000,
            "clients": 20,
            "per_client_counts": [50] * 20,
            "live_worker_invocations": 0,
            "paid_model_calls": 0,
            "duration_seconds": 1,
        },
        {"name": "storage", "bytes": 1_000_000},
        {"name": "total", "duration_seconds": duration},
    ]
    cleanup_digest = "d" * 64
    document = {
        "schema_version": validator.DIAGNOSTIC_SCHEMA_VERSION,
        "kind": "capacity_resource_diagnostic",
        "mode": "diagnostic",
        "acceptance_eligible": False,
        "qualification_evidence": False,
        "source_revision": SOURCE_SHA,
        "execution_id": _execution_id("diagnostic"),
        "targets": dict(validator.DIAGNOSTIC_TARGETS),
        "final_targets": dict(validator.TARGETS),
        "method": {
            "database": validator.EXPECTED_DATABASE_METHOD,
            "vectors": validator.EXPECTED_VECTOR_METHOD,
            "seeding": validator.EXPECTED_SEEDING_METHOD,
            "fixture_vector_indexes": validator.EXPECTED_FIXTURE_VECTOR_INDEXES,
            "clients": "20_bounded_parallel_index_queries",
        },
        "environment": {
            "isolation": "run_scoped_database_and_compose_project",
            "paid_model_calls": 0,
            "live_worker_invocations": 0,
            "runtime_memory_envelope": validator.EXPECTED_RUNTIME_MEMORY_ENVELOPE,
        },
        "ceilings": dict(validator.EXPECTED_CEILINGS),
        "raw_measurements": measurements,
        "index_observation": {
            "schema_version": validator.DIAGNOSTIC_SCHEMA_VERSION,
            "main_sha": SOURCE_SHA,
            "execution_id": _execution_id("diagnostic"),
            "observation_only": True,
            "mode": "diagnostic",
            "index": validator.EXPECTED_INDEX,
            "indexes": validator.EXPECTED_INDEXES,
            "vector_dimensions": 1024,
            "vector_count": 75_000,
            "tenant_count": 15,
            "per_tenant_counts": per_tenant,
            "plans": plans,
        },
        "cleanup": {
            "database_removed": True,
            "execution_id": _execution_id("diagnostic"),
            "artifact_sha256": cleanup_digest,
        },
        "limitations": [
            "Benchmark evidence; not production SLO claims.",
            "This diagnostic cannot be used as final capacity qualification evidence.",
        ],
    }
    cleanup = {
        "schema_version": validator.DIAGNOSTIC_SCHEMA_VERSION,
        "source_revision": SOURCE_SHA,
        "mode": "diagnostic",
        "execution_id": _execution_id("diagnostic"),
        "database": _database_for("diagnostic"),
        "database_removed": True,
        "error": None,
        "timeout_seconds": 120,
    }
    artifacts = {
        "capacity-diagnostic.json": "a" * 64,
        "cleanup.json": cleanup_digest,
        "runtime-pressure.json": "e" * 64,
        "infrastructure-cleanup.json": "f" * 64,
    }
    return (
        document,
        cleanup,
        _runtime(protocol, mode="diagnostic", peak_bytes=peak_bytes),
        _infrastructure_cleanup(protocol, mode="diagnostic"),
        artifacts,
    )


def test_producer_is_exact_bounded_and_deterministic():
    producer = _script("run_capacity_qualification")
    source = (ROOT / "scripts" / "run_capacity_qualification.py").read_text()

    assert producer.TARGETS == {
        "vectors": 100_000,
        "tenants": 20,
        "clients": 20,
        "backlog_messages": 1_000,
    }
    assert producer.MAX_CLIENTS == 20
    assert producer.SEED_SHARDS == 1
    assert {
        shard: sum(1 for number in range(1, 21) if (number - 1) % producer.SEED_SHARDS == shard)
        for shard in range(producer.SEED_SHARDS)
    } == {0: 20}
    assert producer.MAX_DURATION_SECONDS == 1_200
    assert producer.MAX_STORAGE_BYTES == 1_500_000_000
    assert producer.MAX_EXTERNAL_COST_USD == 0
    assert producer._tenant_id("abcdefgh", 1) == producer._tenant_id("abcdefgh", 1)
    assert len({producer._namespace(number) for number in range(1, 21)}) == 20
    vector = producer._vector(20)
    values = vector.removeprefix("[").removesuffix("]").split(",")
    assert len(values) == 1024
    assert values.count("1") == 1
    assert values.count("0") == 1023
    assert producer.VECTOR_CODE_OFFSET + producer.VECTOR_CODE_BITS <= producer.VECTOR_DIMENSIONS
    assert producer.ROWS_PER_TENANT < 1 << producer.VECTOR_CODE_BITS
    assert (
        len({producer._vector_code(ordinal) for ordinal in range(1, producer.ROWS_PER_TENANT + 1)})
        == producer.ROWS_PER_TENANT
    )
    first = producer._vector(1, 1).removeprefix("[").removesuffix("]").split(",")
    second = producer._vector(1, 2).removeprefix("[").removesuffix("]").split(",")
    assert first != second
    assert first[0] == second[0] == "1"
    assert set(first[producer.VECTOR_CODE_OFFSET : producer.VECTOR_CODE_OFFSET + 13]) == {
        producer.VECTOR_CODE_MAGNITUDE,
        f"-{producer.VECTOR_CODE_MAGNITUDE}",
    }
    assert sum(value != "0" for value in first) == 14
    assert producer.VECTOR_METHOD == "deterministic_tenant_anchored_13bit_1024d"
    backlog = producer._exercise_backlog()
    assert backlog["messages_enqueued"] == backlog["messages_accounted_for"] == 1_000
    assert backlog["messages_drained"] == 1_000
    assert backlog["pending_before_drain"] == backlog["observed_max_pending"] == 1_000
    assert backlog["pending_after_drain"] == 0
    assert backlog["clients"] == 20
    assert all(count > 0 for count in backlog["per_client_counts"])
    assert backlog["live_worker_invocations"] == backlog["paid_model_calls"] == 0
    assert "CREATE TEMP TABLE" not in source.upper()
    assert "encode(sha256" not in source
    assert "CREATE TABLE capacity_seed" in source
    assert source.count("JOIN capacity_tenant_seed AS tenant USING (tenant_number)") == 2
    assert "'Bounded synthetic index qualification', namespace, 'open'" in source
    assert "SET status = 'sealed', sealed_at = now()" in source
    assert "ANALYZE semantic_memory_vectors" in source
    assert "DROP TRIGGER {} ON {}" in source
    assert "CREATE TRIGGER {} BEFORE INSERT OR UPDATE OR DELETE ON {}" in source
    assert "_restore_bulk_seed_guards(conn, deadline)" in source
    assert "_restore_bulk_seed_memory_triggers(conn, deadline)" in source
    assert "_verify_bulk_seed_provenance(conn, deadline)" in source
    assert source.count('sql.SQL("DROP INDEX {}@{}")') == 1
    assert "DROP INDEX IF EXISTS" not in source
    suspension = source.split("def _suspend_legacy_vector_index", 1)[1].split(
        "def _restore_legacy_vector_index", 1
    )[0]
    assert "LEGACY_VECTOR_INDEX" in suspension
    assert "TENANT_VECTOR_INDEX" not in suspension
    assert producer.SEEDING_METHOD == (
        "single_bounded_writer_one_atomic_copy_transaction_per_tenant_"
        "between_exact_legacy_index_drop_and_restore"
    )
    assert "COPY semantic_memory_vectors" in source
    assert "copy.write_row" in source
    assert "executemany" not in source
    assert 'multiprocessing.get_context("spawn")' in source
    assert "_stop_seed_processes(started" in source


def test_producer_refuses_remote_or_unbounded_targets():
    producer = _script("run_capacity_qualification")

    with pytest.raises(ValueError, match="loopback defaultdb"):
        producer._validate_inputs(
            "postgresql://root@database.example/defaultdb",
            hashlib.sha256(_execution_id("qualification").encode()).hexdigest()[:16],
            _execution_id("qualification"),
            SOURCE_SHA,
            600,
        )
    with pytest.raises(ValueError, match="between 60 and 1200"):
        producer._validate_inputs(
            "postgresql://root@localhost/defaultdb",
            hashlib.sha256(_execution_id("qualification").encode()).hexdigest()[:16],
            _execution_id("qualification"),
            SOURCE_SHA,
            1201,
        )
    assert producer._is_disposable("hindsight_capacity_abcdefgh") is True
    assert producer._is_disposable("defaultdb") is False


@pytest.mark.parametrize(
    ("tenant_number", "ordinal"),
    [(0, 0), (21, 0), (True, 0), (1, -1), (1, 5_001), (1, True)],
)
def test_capacity_vector_refuses_out_of_bounds_identity(tenant_number, ordinal):
    producer = _script("run_capacity_qualification")

    with pytest.raises(ValueError, match="bounded fixture"):
        producer._vector(tenant_number, ordinal)

    if tenant_number == 1 and ordinal != 0:
        with pytest.raises(ValueError, match="bounded fixture"):
            producer._vector_code(ordinal)


def test_migrations_stop_before_tenant_index_then_resume_latest(monkeypatch):
    producer = _script("run_capacity_qualification")
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(producer.subprocess, "run", run)
    deadline = producer.Deadline.after(10)
    base = producer._migrate(
        "postgresql://db",
        deadline,
        name="base_migrations",
        through=producer.BASE_SCHEMA_THROUGH,
    )
    latest = producer._migrate(
        "postgresql://db",
        deadline,
        name="post_seed_migrations",
    )

    assert commands[0][-2:] == ["--through", producer.BASE_SCHEMA_THROUGH]
    assert commands[1][-1].endswith("scripts/migrate.py")
    assert base["through"] == producer.BASE_SCHEMA_THROUGH
    assert latest["through"] == "latest"


def test_tenant_index_build_requires_exact_populated_legacy_index(monkeypatch):
    producer = _script("run_capacity_qualification")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement, _params=None):
            return SimpleNamespace(fetchone=lambda: (producer.TARGETS["vectors"],))

    monkeypatch.setattr(producer, "_connection", lambda *_args: Connection())
    monkeypatch.setattr(
        producer,
        "_vector_index_names",
        lambda *_args: frozenset({producer.LEGACY_VECTOR_INDEX}),
    )
    measurement = producer._tenant_index_build_input("postgresql://db", producer.Deadline.after(10))
    assert measurement == {
        "name": "tenant_index_build_input",
        "vectors": 100_000,
        "present_indexes": [producer.LEGACY_VECTOR_INDEX],
        "absent_index": producer.TENANT_VECTOR_INDEX,
        "next_migration": producer.TENANT_VECTOR_MIGRATION,
    }

    monkeypatch.setattr(
        producer,
        "_vector_index_names",
        lambda *_args: producer.QUALIFIED_VECTOR_INDEXES,
    )
    with pytest.raises(RuntimeError, match="must be absent"):
        producer._tenant_index_build_input("postgresql://db", producer.Deadline.after(10))


def test_legacy_vector_index_is_suspended_and_restored_exactly(monkeypatch):
    producer = _script("run_capacity_qualification")
    executed = []

    class Result:
        def fetchone(self):
            return (producer.TARGETS["vectors"],)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=None):
            executed.append(
                statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
            )
            return Result()

    monkeypatch.setattr(producer, "_connection", lambda *_args: Connection())
    states = iter((frozenset({producer.LEGACY_VECTOR_INDEX}), frozenset()))
    monkeypatch.setattr(producer, "_vector_index_names", lambda *_args: next(states))
    suspended = producer._suspend_legacy_vector_index(
        "postgresql://db", producer.Deadline.after(10)
    )
    assert suspended["before_indexes"] == [producer.LEGACY_VECTOR_INDEX]
    assert suspended["after_indexes"] == []
    assert executed[-1] == (
        f'DROP INDEX "semantic_memory_vectors"@"{producer.LEGACY_VECTOR_INDEX}"'
    )

    states = iter((frozenset(), frozenset({producer.LEGACY_VECTOR_INDEX})))
    monkeypatch.setattr(producer, "_vector_index_names", lambda *_args: next(states))
    restored = producer._restore_legacy_vector_index("postgresql://db", producer.Deadline.after(10))
    assert restored["vectors"] == producer.TARGETS["vectors"]
    assert restored["before_indexes"] == []
    assert restored["after_indexes"] == [producer.LEGACY_VECTOR_INDEX]
    assert producer.LEGACY_VECTOR_INDEX_CREATE_SQL in executed

    migration = " ".join(
        (ROOT / "migrations" / producer.LEGACY_VECTOR_MIGRATION).read_text().split()
    )
    assert producer.LEGACY_VECTOR_INDEX_CREATE_SQL in migration


def test_bulk_seed_lifecycle_guards_are_removed_and_restored_exactly():
    producer = _script("run_capacity_qualification")
    statements = []

    class Connection:
        def execute(self, statement, _params=None):
            if hasattr(statement, "as_string"):
                statements.append(statement.as_string(None))

    conn = Connection()
    deadline = producer.Deadline.after(10)
    producer._remove_bulk_seed_guards(conn, deadline)
    producer._restore_bulk_seed_guards(conn, deadline)
    assert len(statements) == 6
    for table, trigger in producer.BULK_SEED_GUARDS.items():
        assert f'DROP TRIGGER "{trigger}" ON "{table}"' in statements
        assert (
            f'CREATE TRIGGER "{trigger}" BEFORE INSERT OR UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION guard_tenant_lifecycle_row_state()" in statements
        )


def test_bulk_seed_memory_triggers_are_removed_and_restored_exactly():
    producer = _script("run_capacity_qualification")
    statements = []

    class Connection:
        def execute(self, statement, _params=None):
            if hasattr(statement, "as_string"):
                statements.append(statement.as_string(None))

    conn = Connection()
    deadline = producer.Deadline.after(10)
    producer._remove_bulk_seed_memory_triggers(conn, deadline)
    producer._restore_bulk_seed_memory_triggers(conn, deadline)
    assert len(statements) == 4
    for trigger, create_statement in producer.BULK_SEED_MEMORY_TRIGGERS.items():
        assert f'DROP TRIGGER "{trigger}" ON "semantic_memories"' in statements
        assert create_statement in statements


def test_validator_rejects_forged_counts_plans_cleanup_and_ceilings():
    validator = _script("validate_capacity_evidence")
    report = _report(validator)
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "mode": "qualification",
        "execution_id": _execution_id("qualification"),
        "database": _database_for("qualification"),
        "database_removed": True,
        "error": None,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    digests = {
        "index-qualification.json": "b" * 64,
        "capacity-report.json": "c" * 64,
        "cleanup.json": "d" * 64,
    }

    assert (
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )["kind"]
        == "bounded_capacity_evidence"
    )

    qualification["vector_count"] = 99_999
    with pytest.raises(ValueError, match="exact populated target"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )
    qualification["vector_count"] = 100_000
    qualification["plans"].pop()
    with pytest.raises(ValueError, match="twenty bounded clients"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )
    qualification = _qualification(validator)
    with pytest.raises(ValueError, match="cleanup"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            cleanup={
                "schema_version": validator.SCHEMA_VERSION,
                "database": "hindsight_capacity_abcdefgh",
                "database_removed": False,
                "error": None,
                "source_revision": SOURCE_SHA,
                "timeout_seconds": 120,
            },
            qualification=qualification,
            artifact_digests=digests,
        )
    report["ceilings"] = {**validator.EXPECTED_CEILINGS, "clients": 21}
    with pytest.raises(ValueError, match="hard ceilings"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )


def test_validator_requires_bound_supplemental_artifacts():
    validator = _script("validate_capacity_evidence")

    with pytest.raises(ValueError, match="requires qualification, cleanup, and artifact"):
        _validate(validator, _report(validator), source_revision=SOURCE_SHA)


def test_validator_rejects_duplicate_tenants_phase_time_and_cleanup_forgery():
    validator = _script("validate_capacity_evidence")
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "error": None,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    digests = {
        "index-qualification.json": "b" * 64,
        "capacity-report.json": "c" * 64,
        "cleanup.json": "d" * 64,
    }

    report = _report(validator)
    counts = next(row for row in report["raw_measurements"] if row["name"] == "vector_counts")[
        "per_tenant"
    ]
    counts[1]["tenant_id"] = counts[0]["tenant_id"]
    with pytest.raises(ValueError, match="exact vector and tenant counts"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    seed = next(row for row in report["raw_measurements"] if row["name"] == "vector_seed")
    seed["duration_seconds"] = 1_201
    with pytest.raises(ValueError, match="phase durations"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    report["raw_measurements"][-1]["duration_seconds"] = 5
    with pytest.raises(ValueError, match="phase durations exceed"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    with pytest.raises(ValueError, match="cleanup"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup={**cleanup, "database": "hindsight_capacity_", "error": "failed"},
            artifact_digests=digests,
        )

    report = _report(validator)
    missing_error = dict(cleanup)
    del missing_error["error"]
    with pytest.raises(ValueError, match="cleanup"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=missing_error,
            artifact_digests=digests,
        )

    report = _report(validator)
    report["cleanup"]["artifact_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="manifest"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )


def test_capacity_schema_contract_is_shared_by_producer_and_validator():
    producer = _script("run_capacity_qualification")
    validator = _script("validate_capacity_evidence")

    assert producer.SCHEMA_VERSION == validator.SCHEMA_VERSION
    assert producer.TARGETS == validator.TARGETS
    assert producer.MAX_DURATION_SECONDS == validator.EXPECTED_CEILINGS["duration_seconds"]
    assert producer.MAX_STORAGE_BYTES == validator.EXPECTED_CEILINGS["storage_bytes"]
    assert producer.MAX_CLIENTS == validator.EXPECTED_CEILINGS["clients"]
    assert producer.MAX_EXTERNAL_COST_USD == validator.EXPECTED_CEILINGS["external_cost_usd"]
    assert producer.SEED_SHARDS == validator.EXPECTED_VECTOR_INSERT_WORKERS
    assert producer.VECTOR_METHOD == validator.EXPECTED_VECTOR_METHOD
    assert producer.SEEDING_METHOD == validator.EXPECTED_SEEDING_METHOD
    assert producer.FIXTURE_VECTOR_INDEX_METHOD == validator.EXPECTED_FIXTURE_VECTOR_INDEXES
    assert producer.DATABASE_METHOD == validator.EXPECTED_DATABASE_METHOD
    assert producer.BASE_SCHEMA_THROUGH == validator.BASE_SCHEMA_THROUGH
    assert producer.LEGACY_VECTOR_INDEX == validator.LEGACY_INDEX
    assert producer.TENANT_VECTOR_INDEX == validator.EXPECTED_INDEX
    assert sorted(producer.QUALIFIED_VECTOR_INDEXES) == validator.EXPECTED_INDEXES
    assert producer.LEGACY_VECTOR_MIGRATION == validator.LEGACY_VECTOR_MIGRATION
    assert producer.TENANT_VECTOR_MIGRATION == validator.TENANT_VECTOR_MIGRATION


def test_validator_requires_populated_index_build_and_both_live_indexes():
    validator = _script("validate_capacity_evidence")
    report = _report(validator)
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "error": None,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    digests = {
        "index-qualification.json": "b" * 64,
        "capacity-report.json": "c" * 64,
        "cleanup.json": "d" * 64,
    }

    build_input = next(
        row for row in report["raw_measurements"] if row["name"] == "tenant_index_build_input"
    )
    build_input["vectors"] = 99_999
    with pytest.raises(ValueError, match="populated tenant-index build"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    qualification["indexes"] = [validator.EXPECTED_INDEX]
    with pytest.raises(ValueError, match="exact populated target"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )


def test_validator_rejects_booleans_in_exact_numeric_evidence():
    validator = _script("validate_capacity_evidence")
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "error": None,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    digests = {
        "index-qualification.json": "b" * 64,
        "capacity-report.json": "c" * 64,
        "cleanup.json": "d" * 64,
    }

    report = _report(validator)
    report["environment"]["paid_model_calls"] = False
    with pytest.raises(ValueError, match="environment"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    report["ceilings"]["external_cost_usd"] = False
    with pytest.raises(ValueError, match="hard ceilings"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    clients = next(row for row in report["raw_measurements"] if row["name"] == "bounded_clients")
    clients["clients"][0]["client"] = True
    qualification["plans"][0]["client"] = True
    with pytest.raises(ValueError, match="twenty qualified clients"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )


def test_validator_rejects_unobserved_backlog_and_seed_storage():
    validator = _script("validate_capacity_evidence")
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "error": None,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    digests = {
        "index-qualification.json": "b" * 64,
        "capacity-report.json": "c" * 64,
        "cleanup.json": "d" * 64,
    }
    report = _report(validator)
    backlog = next(row for row in report["raw_measurements"] if row["name"] == "synthetic_backlog")
    backlog["observed_max_pending"] = 999
    with pytest.raises(ValueError, match="synthetic backlog"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    seed = next(row for row in report["raw_measurements"] if row["name"] == "vector_seed")
    seed["storage_checks"][-1]["bytes"] = validator.EXPECTED_CEILINGS["storage_bytes"] + 1
    seed["peak_storage_bytes"] = seed["storage_checks"][-1]["bytes"]
    with pytest.raises(ValueError, match="enforced storage ceiling"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    for field, invalid in (
        ("vector_insert_rows", 99_999),
        ("vector_insert_rows", True),
        ("vector_insert_transactions", 19),
        ("vector_insert_transactions", True),
        ("vector_insert_workers", 5),
        ("vector_insert_client_retries", 1),
    ):
        report = _report(validator)
        seed = next(row for row in report["raw_measurements"] if row["name"] == "vector_seed")
        seed[field] = invalid
        with pytest.raises(ValueError, match="exact bounded vector insertion"):
            _validate(
                validator,
                report,
                source_revision=SOURCE_SHA,
                qualification=qualification,
                cleanup=cleanup,
                artifact_digests=digests,
            )

    report = _report(validator)
    report["method"]["vectors"] = "twenty_repeated_vectors"
    with pytest.raises(ValueError, match="deterministic vector fixture"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    report["method"]["seeding"] = "bulk_vector_insert"
    with pytest.raises(ValueError, match="bounded vector seeding method"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    report["method"] = ["truthy", "but", "not", "an", "object"]
    with pytest.raises(ValueError, match="requires method and environment"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    seed = next(row for row in report["raw_measurements"] if row["name"] == "vector_seed")
    seed["storage_checks"] = [{**row, "bytes": True} for row in seed["storage_checks"]]
    seed["peak_storage_bytes"] = True
    with pytest.raises(ValueError, match="enforced storage ceiling"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    seed = next(row for row in report["raw_measurements"] if row["name"] == "vector_seed")
    seed["storage_checks"][0]["completed_tenants"] = 20
    with pytest.raises(ValueError, match="enforced storage ceiling"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    seed = next(row for row in report["raw_measurements"] if row["name"] == "vector_seed")
    seed["storage_checks"][0].update(
        {"completion_sequence": True, "completed_tenants": True, "bytes": True}
    )
    with pytest.raises(ValueError, match="enforced storage ceiling"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )


def test_storage_is_checked_during_seeding_and_fails_at_ceiling(monkeypatch):
    producer = _script("run_capacity_qualification")
    measurements = iter([producer.MAX_STORAGE_BYTES, producer.MAX_STORAGE_BYTES + 1])
    monkeypatch.setattr(producer, "_storage_bytes", lambda *_args, **_kwargs: next(measurements))
    deadline = SimpleNamespace()

    assert producer._check_storage(
        "postgresql://db",
        deadline,
        completion_sequence=1,
        completed_tenants=1,
    ) == {
        "completion_sequence": 1,
        "completed_tenants": 1,
        "bytes": producer.MAX_STORAGE_BYTES,
    }
    with pytest.raises(RuntimeError, match="after completion 2"):
        producer._check_storage(
            "postgresql://db",
            deadline,
            completion_sequence=2,
            completed_tenants=2,
        )


def test_seed_decision_seals_are_serialized(monkeypatch):
    producer = _script("run_capacity_qualification")

    events = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            events.append((str(statement), params))
            return SimpleNamespace(rowcount=1)

    monkeypatch.setattr(producer, "_connection", lambda *_args: Connection())
    assert producer._seal_seed_decision("postgresql://db", "abcdefgh", 2, SimpleNamespace()) is None
    assert "set_config('hindsight.tenant_id'" in events[0][0]
    assert "SET status = 'sealed', sealed_at = now()" in events[1][0]
    assert events[1][1][1] == "capacity:abcdefgh:2"


@pytest.mark.parametrize("rowcount", [0, 2])
def test_seed_completion_fails_unless_decision_is_sealed_once(monkeypatch, rowcount):
    producer = _script("run_capacity_qualification")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            return SimpleNamespace(rowcount=rowcount)

    monkeypatch.setattr(producer, "_connection", lambda *_args: Connection())
    with pytest.raises(RuntimeError, match="not sealed exactly once"):
        producer._seal_seed_decision("postgresql://db", "abcdefgh", 1, SimpleNamespace())


def test_seed_process_cleanup_terminates_then_kills():
    producer = _script("run_capacity_qualification")

    class Process:
        def __init__(self):
            self.alive = True
            self.events = []

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.events.append("terminate")

        def join(self, timeout):
            self.events.append(("join", timeout))

        def kill(self):
            self.events.append("kill")
            self.alive = False

    process = Process()
    producer._stop_seed_processes([process], producer.Deadline.after(10))
    assert process.events[0] == "terminate"
    assert "kill" in process.events
    assert process.events[-1][0] == "join"


def test_seed_shard_worker_reports_bounded_result(monkeypatch):
    producer = _script("run_capacity_qualification")
    rows = []
    results = SimpleNamespace(put=rows.append)
    storage_checks = [
        {"completion_sequence": number, "completed_tenants": number, "bytes": number}
        for number in range(1, 6)
    ]
    monkeypatch.setattr(producer, "_load_seed_shard", lambda *_args: (25_000, 5, storage_checks))
    producer._seed_shard_worker("postgresql://db", 123.0, 2, results)
    assert rows == [(2, 25_000, 5, storage_checks, None)]

    def fail(*_args):
        raise RuntimeError("x" * 2_000)

    monkeypatch.setattr(producer, "_load_seed_shard", fail)
    producer._seed_shard_worker("postgresql://db", 123.0, 3, results)
    assert rows[-1][0] == 3
    assert rows[-1][1] is None
    assert rows[-1][2] is None
    assert rows[-1][3] is None
    assert rows[-1][4].startswith("RuntimeError: ")
    assert len(rows[-1][4]) == 800


def test_seed_copy_atomicity_is_pinned_and_verified(monkeypatch):
    producer = _script("run_capacity_qualification")
    calls = []

    class Connection:
        def execute(self, statement, _params=None):
            calls.append(statement)
            return SimpleNamespace(fetchone=lambda: ("off",))

    monkeypatch.setattr(producer, "_refresh_qualification_timeout", lambda *_args: None)
    with pytest.raises(RuntimeError, match="COPY atomicity"):
        producer._require_atomic_copy(Connection(), producer.Deadline.after(10))
    assert calls == [
        "SET copy_from_atomic_enabled = true",
        "SHOW copy_from_atomic_enabled",
    ]


def test_seed_shard_uses_exact_per_tenant_vector_copy(monkeypatch):
    producer = _script("run_capacity_qualification")
    calls = []
    copied_rows = []

    class Result:
        rowcount = 1

        def __init__(self, rows=()):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return list(self.rows)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            statement = str(statement)
            calls.append((statement, params))
            if statement == "SHOW copy_from_atomic_enabled":
                return Result([("on",)])
            if statement.startswith("SELECT tenant_id, namespace"):
                return Result([("tenant-id", "capacity.synthetic.01")])
            if statement.startswith("SELECT ordinal, memory_id"):
                return Result([(1, "memory-1"), (2, "memory-2")])
            return Result()

        def cursor(self):
            return Cursor()

        def transaction(self):
            return self

    class Cursor:
        def copy(self, statement):
            calls.append((statement, None))
            return Copy()

    class Copy:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write_row(self, row):
            copied_rows.append(row)

    monkeypatch.setattr(producer, "TARGETS", {**producer.TARGETS, "tenants": 2, "vectors": 4})
    monkeypatch.setattr(producer, "ROWS_PER_TENANT", 2)
    monkeypatch.setattr(producer, "SEED_SHARDS", 2)
    monkeypatch.setattr(producer, "_connection", lambda *_args: Connection())
    monkeypatch.setattr(producer, "_refresh_qualification_timeout", lambda *_args: None)
    monkeypatch.setattr(
        producer,
        "_check_storage",
        lambda *_args, **_kwargs: {
            "completion_sequence": 1,
            "completed_tenants": 1,
            "bytes": 100,
        },
    )

    assert producer._load_seed_shard("postgresql://db", producer.Deadline.after(10), 0) == (
        2,
        1,
        [{"completion_sequence": 1, "completed_tenants": 1, "bytes": 100}],
    )
    vector_calls = [call for call in calls if "COPY semantic_memory_vectors" in call[0]]
    assert len(vector_calls) == 1
    assert [row[1] for row in copied_rows] == ["memory-1", "memory-2"]
    assert [row[4] for row in copied_rows] == [
        hashlib.sha256(b"capacity:1").hexdigest(),
        hashlib.sha256(b"capacity:2").hexdigest(),
    ]
    assert [row[5] for row in copied_rows] == [
        producer._vector(1, 1),
        producer._vector(1, 2),
    ]
    assert copied_rows[0][5] != copied_rows[1][5]


def test_seed_shard_does_not_retry_a_surfaced_vector_copy_error(monkeypatch):
    producer = _script("run_capacity_qualification")
    vector_attempts = []

    class Result:
        rowcount = 1

        def __init__(self, rows=()):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return list(self.rows)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=None):
            statement = str(statement)
            if statement == "SHOW copy_from_atomic_enabled":
                return Result([("on",)])
            if statement.startswith("SELECT tenant_id, namespace"):
                return Result([("tenant-id", "capacity.synthetic.01")])
            if statement.startswith("SELECT ordinal, memory_id"):
                return Result([(1, "memory-1")])
            return Result()

        def cursor(self):
            return Cursor()

        def transaction(self):
            return self

    class Cursor:
        def copy(self, _statement):
            return Copy()

    class Copy:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write_row(self, row):
            vector_attempts.append(row)
            raise RuntimeError("database detail must remain chained")

    monkeypatch.setattr(producer, "TARGETS", {**producer.TARGETS, "tenants": 1, "vectors": 1})
    monkeypatch.setattr(producer, "ROWS_PER_TENANT", 1)
    monkeypatch.setattr(producer, "SEED_SHARDS", 1)
    monkeypatch.setattr(producer, "_connection", lambda *_args: Connection())
    monkeypatch.setattr(producer, "_refresh_qualification_timeout", lambda *_args: None)

    with pytest.raises(RuntimeError, match="shard 0, tenant 1"):
        producer._load_seed_shard("postgresql://db", producer.Deadline.after(10), 0)
    assert len(vector_attempts) == 1


def test_seed_shard_partial_start_reaps_started_processes(monkeypatch):
    producer = _script("run_capacity_qualification")
    monkeypatch.setattr(producer, "SEED_SHARDS", 2)

    class ResultQueue:
        def __init__(self):
            self.closed = False
            self.joined = False

        def close(self):
            self.closed = True

        def join_thread(self):
            self.joined = True

    class Process:
        def __init__(self, *, name, **_kwargs):
            self.name = name

        def start(self):
            if self.name == "capacity-seed-shard-1":
                raise RuntimeError("spawn failed")

    result_queue = ResultQueue()
    context = SimpleNamespace(
        Queue=lambda: result_queue,
        Process=lambda **kwargs: Process(**kwargs),
    )
    stopped = []
    monkeypatch.setattr(producer.multiprocessing, "get_context", lambda _method: context)
    monkeypatch.setattr(
        producer,
        "_stop_seed_processes",
        lambda processes, _deadline: stopped.extend(processes),
    )

    with pytest.raises(RuntimeError, match="spawn failed"):
        producer._run_seed_shards("postgresql://db", producer.Deadline.after(10))
    assert [process.name for process in stopped] == ["capacity-seed-shard-0"]
    assert result_queue.closed is result_queue.joined is True


def test_seed_shard_orchestrator_rejects_inexact_vector_count_and_reaps(monkeypatch):
    producer = _script("run_capacity_qualification")

    class ResultQueue:
        def __init__(self):
            storage_checks = [
                {
                    "completion_sequence": number,
                    "completed_tenants": number,
                    "bytes": number,
                }
                for number in range(1, 21)
            ]
            self.results = [(0, 99_999, 20, storage_checks, None)]
            self.closed = False
            self.joined = False

        def get(self, timeout):
            assert timeout > 0
            return self.results.pop(0)

        def close(self):
            self.closed = True

        def join_thread(self):
            self.joined = True

    class Process:
        def __init__(self, *, name, **_kwargs):
            self.name = name
            self.exitcode = 0

        def start(self):
            return None

        def is_alive(self):
            return False

    result_queue = ResultQueue()
    context = SimpleNamespace(
        Queue=lambda: result_queue,
        Process=lambda **kwargs: Process(**kwargs),
    )
    reaped = []
    monkeypatch.setattr(producer.multiprocessing, "get_context", lambda _method: context)
    monkeypatch.setattr(
        producer,
        "_stop_seed_processes",
        lambda processes, _deadline: reaped.extend(processes),
    )

    with pytest.raises(RuntimeError, match="invalid vector row count"):
        producer._run_seed_shards("postgresql://db", producer.Deadline.after(10))
    assert len(reaped) == producer.SEED_SHARDS
    assert result_queue.closed is result_queue.joined is True


def test_drop_database_applies_bounded_statement_and_lock_timeouts(monkeypatch):
    producer = _script("run_capacity_qualification")
    calls = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            statement = str(query)
            calls.append((statement, params))
            if "FROM crdb_internal.jobs" in statement:
                return Result([])
            if statement == "SHOW DATABASES":
                return Result([("defaultdb",)])
            return Result([])

    def connect(url, **kwargs):
        assert url == "postgresql://root@localhost/defaultdb"
        assert kwargs["connect_timeout"] == 5
        assert kwargs["application_name"] == "hindsight-capacity-cleanup"
        return Connection()

    monkeypatch.setattr(producer.psycopg, "connect", connect)
    deadline = SimpleNamespace(
        expires_at=producer.time.monotonic() + producer.MAX_CLEANUP_SECONDS,
        remaining=lambda maximum: min(maximum, producer.MAX_CLEANUP_SECONDS),
    )
    producer._drop_database(
        "postgresql://root@localhost/defaultdb",
        "hindsight_capacity_abcdefgh",
        deadline,
    )

    for seconds in (
        producer.CLEANUP_JOB_SECONDS,
        producer.CLEANUP_DROP_SECONDS,
        producer.CLEANUP_VERIFY_SECONDS,
    ):
        timeout = (f"{seconds * 1000}ms",)
        assert ("SELECT set_config('statement_timeout', %s, false)", timeout) in calls
        assert ("SELECT set_config('lock_timeout', %s, false)", timeout) in calls


def test_cleanup_vector_job_prefixes_are_exact_and_disposable():
    producer = _script("run_capacity_qualification")
    database = "hindsight_capacity_abcdefgh"

    prefixes = producer._vector_index_job_prefixes(database)

    assert len(prefixes) == 2
    assert all(
        prefix.startswith("CREATE VECTOR INDEX IF NOT EXISTS semantic_memory_vectors_")
        and f" ON {database}.public.semantic_memory_vectors (" in prefix
        for prefix in prefixes
    )
    assert producer._legacy_index_drop_job_description(database) == (
        f"DROP INDEX {database}.public.semantic_memory_vectors@{producer.LEGACY_VECTOR_INDEX}"
    )
    with pytest.raises(RuntimeError, match="non-capacity database"):
        producer._vector_index_job_prefixes("hindsight")


def test_cleanup_cancels_only_captured_exact_vector_jobs(monkeypatch):
    producer = _script("run_capacity_qualification")
    database = "hindsight_capacity_abcdefgh"
    calls = []
    canceled = False

    class Result:
        def __init__(self, rows=()):
            self.rows = rows

        def fetchall(self):
            return list(self.rows)

    class Connection:
        def execute(self, query, params=None):
            nonlocal canceled
            statement = str(query)
            calls.append((statement, params))
            if "SELECT job_id, job_type, status" in statement:
                return Result(
                    [
                        (
                            42,
                            "NEW SCHEMA CHANGE",
                            "running",
                            producer._vector_index_job_prefixes(database)[1] + "embedding)",
                        )
                    ]
                )
            if "CANCEL JOBS" in statement:
                assert params[0] == [42]
                canceled = True
                return Result()
            if "SELECT job_id, status" in statement:
                assert canceled is True
                return Result([(42, "canceled")])
            return Result()

    monkeypatch.setattr(producer.time, "sleep", lambda _seconds: None)
    producer._cancel_disposable_vector_index_jobs(
        Connection(),
        database,
        producer.Deadline.after(30),
    )

    selection = next(statement for statement, _params in calls if "job_type, status" in statement)
    cancellation, params = next(
        (statement, params) for statement, params in calls if "CANCEL JOBS" in statement
    )
    assert "substring(description, 1, length(%s::STRING)) = %s::STRING" in selection
    assert " LIKE " not in selection
    assert "job_id = ANY(%s)" in cancellation
    assert all(database in prefix for prefix in params[1::2])


def test_cleanup_waits_for_non_cancelable_exact_legacy_drop(monkeypatch):
    producer = _script("run_capacity_qualification")
    database = "hindsight_capacity_abcdefgh"
    calls = []

    class Result:
        def __init__(self, rows=()):
            self.rows = rows

        def fetchall(self):
            return list(self.rows)

    class Connection:
        def execute(self, query, params=None):
            statement = str(query)
            calls.append(statement)
            if "SELECT job_id, job_type, status" in statement:
                return Result(
                    [
                        (
                            43,
                            "NEW SCHEMA CHANGE",
                            "running",
                            producer._legacy_index_drop_job_description(database),
                        )
                    ]
                )
            if "SELECT job_id, status" in statement:
                return Result([(43, "succeeded")])
            return Result()

    monkeypatch.setattr(producer.time, "sleep", lambda _seconds: None)
    producer._cancel_disposable_vector_index_jobs(
        Connection(), database, producer.Deadline.after(30)
    )

    assert not any("CANCEL JOBS" in statement for statement in calls)


def test_cleanup_rejects_nonterminal_vector_job_state():
    producer = _script("run_capacity_qualification")

    class Result:
        def fetchall(self):
            return [
                (
                    42,
                    "NEW SCHEMA CHANGE",
                    "revert-failed",
                    producer._vector_index_job_prefixes("hindsight_capacity_abcdefgh")[0]
                    + "embedding)",
                )
            ]

    class Connection:
        def execute(self, _query, _params=None):
            return Result()

    with pytest.raises(RuntimeError, match="unexpected capacity jobs"):
        producer._cancel_disposable_vector_index_jobs(
            Connection(),
            "hindsight_capacity_abcdefgh",
            producer.Deadline.after(30),
        )


def test_primary_failure_preserves_bounded_cleanup_receipt(tmp_path, monkeypatch):
    producer = _script("run_capacity_qualification")
    monkeypatch.setattr(producer, "_verify_checkout", lambda _source_sha: None)
    monkeypatch.setattr(producer, "_create_database", lambda *_args: None)
    monkeypatch.setattr(
        producer,
        "_run",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("qualification failed")),
    )
    monkeypatch.setattr(
        producer,
        "_drop_database",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("cleanup deadline")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_capacity_qualification.py",
            "--admin-url",
            "postgresql://root@localhost/defaultdb",
            "--run-id",
            hashlib.sha256(_execution_id("qualification").encode()).hexdigest()[:16],
            "--execution-id",
            _execution_id("qualification"),
            "--source-sha",
            SOURCE_SHA,
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "60",
            "--mode",
            "qualification",
        ],
    )

    with pytest.raises(RuntimeError, match="qualification failed"):
        producer.main()
    receipt = json.loads((tmp_path / "cleanup.json").read_text())
    assert receipt["database_removed"] is False
    assert receipt["timeout_seconds"] == producer.MAX_CLEANUP_SECONDS
    assert receipt["error"] == "TimeoutError: cleanup deadline"


def test_attempt_progress_preserves_completed_phase_timings_on_failure(tmp_path, monkeypatch):
    producer = _script("run_capacity_qualification")
    monkeypatch.setattr(producer, "_verify_checkout", lambda _source_sha: None)
    monkeypatch.setattr(producer, "_create_database", lambda *_args: None)
    monkeypatch.setattr(producer, "_drop_database", lambda *_args: None)

    def migrate(_database_url, _deadline, *, name, through=None):
        if name == "post_seed_migrations":
            raise TimeoutError("tenant index build reached the global deadline")
        return {"name": name, "duration_seconds": 1, "through": through or "latest"}

    monkeypatch.setattr(producer, "_migrate", migrate)
    monkeypatch.setattr(
        producer,
        "_suspend_legacy_vector_index",
        lambda *_args: {"name": "legacy_index_suspension", "duration_seconds": 1},
    )
    monkeypatch.setattr(
        producer,
        "_seed",
        lambda *_args: {"name": "vector_seed", "duration_seconds": 1},
    )
    monkeypatch.setattr(
        producer,
        "_restore_legacy_vector_index",
        lambda *_args: {"name": "legacy_index_restore", "duration_seconds": 1},
    )
    monkeypatch.setattr(
        producer,
        "_tenant_index_build_input",
        lambda *_args: {"name": "tenant_index_build_input", "vectors": 100_000},
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_capacity_qualification.py",
            "--admin-url",
            "postgresql://root@localhost/defaultdb",
            "--run-id",
            hashlib.sha256(_execution_id("qualification").encode()).hexdigest()[:16],
            "--execution-id",
            _execution_id("qualification"),
            "--source-sha",
            SOURCE_SHA,
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "60",
            "--mode",
            "qualification",
        ],
    )

    with pytest.raises(TimeoutError, match="tenant index build"):
        producer.main()

    progress = json.loads((tmp_path / producer.ATTEMPT_PROGRESS_FILENAME).read_text())
    assert progress["schema_version"] == producer.ATTEMPT_PROGRESS_SCHEMA_VERSION
    assert progress["kind"] == "capacity_attempt_diagnostic"
    assert progress["qualification_evidence"] is False
    assert progress["status"] == "failed"
    assert progress["current_phase"] is None
    assert [row["name"] for row in progress["completed_phases"]] == [
        "database_create",
        "base_migrations",
        "legacy_index_suspension",
        "vector_seed",
        "legacy_index_restore",
        "tenant_index_build_input",
    ]
    assert all(row["duration_seconds"] > 0 for row in progress["completed_phases"])
    assert progress["failure"]["phase"] == "post_seed_migrations"
    assert progress["failure"]["type"] == "TimeoutError"
    assert progress["failure"]["duration_seconds"] > 0
    assert not list(tmp_path.glob(f".{producer.ATTEMPT_PROGRESS_FILENAME}.*.tmp"))


def test_attempt_progress_failure_write_does_not_mask_primary_error(tmp_path, monkeypatch, capsys):
    producer = _script("run_capacity_qualification")
    monkeypatch.setattr(producer, "_verify_checkout", lambda _source_sha: None)
    monkeypatch.setattr(producer, "_create_database", lambda *_args: None)
    monkeypatch.setattr(producer, "_drop_database", lambda *_args: None)
    monkeypatch.setattr(
        producer,
        "_run",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("primary qualification failure")),
    )
    write_json_atomic = producer._write_json_atomic

    def fail_failure_checkpoint(path, value):
        if value.get("status") == "failed":
            raise OSError("diagnostic storage unavailable")
        write_json_atomic(path, value)

    monkeypatch.setattr(producer, "_write_json_atomic", fail_failure_checkpoint)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_capacity_qualification.py",
            "--admin-url",
            "postgresql://root@localhost/defaultdb",
            "--run-id",
            hashlib.sha256(_execution_id("qualification").encode()).hexdigest()[:16],
            "--execution-id",
            _execution_id("qualification"),
            "--source-sha",
            SOURCE_SHA,
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "60",
            "--mode",
            "qualification",
        ],
    )

    with pytest.raises(RuntimeError, match="primary qualification failure"):
        producer.main()
    assert "diagnostic failure checkpoint could not be written: OSError" in capsys.readouterr().err


def test_attempt_progress_success_is_diagnostic_and_excluded_from_manifest(tmp_path, monkeypatch):
    producer = _script("run_capacity_qualification")
    monkeypatch.setattr(producer, "_verify_checkout", lambda _source_sha: None)
    monkeypatch.setattr(producer, "_create_database", lambda *_args: None)
    monkeypatch.setattr(producer, "_drop_database", lambda *_args: None)
    monkeypatch.setattr(
        producer,
        "_run",
        lambda *_args: (
            {"schema_version": producer.SCHEMA_VERSION},
            {"schema_version": producer.SCHEMA_VERSION},
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_capacity_qualification.py",
            "--admin-url",
            "postgresql://root@localhost/defaultdb",
            "--run-id",
            hashlib.sha256(_execution_id("qualification").encode()).hexdigest()[:16],
            "--execution-id",
            _execution_id("qualification"),
            "--source-sha",
            SOURCE_SHA,
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "60",
            "--mode",
            "qualification",
        ],
    )

    assert producer.main() == 0
    progress = json.loads((tmp_path / producer.ATTEMPT_PROGRESS_FILENAME).read_text())
    manifest = json.loads((tmp_path / "artifact-manifest.json").read_text())
    assert progress["status"] == "workload_completed"
    assert progress["qualification_evidence"] is False
    assert "qualified" not in progress
    assert set(manifest["artifacts"]) == {
        "index-qualification.json",
        "capacity-report.json",
        "cleanup.json",
    }
    assert producer.ATTEMPT_PROGRESS_FILENAME not in manifest["artifacts"]


def test_artifact_manifest_hashes_exact_bytes(tmp_path, monkeypatch):
    validator = _script("validate_capacity_evidence")
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "mode": "qualification",
        "execution_id": _execution_id("qualification"),
        "database": _database_for("qualification"),
        "database_removed": True,
        "error": None,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    qualification_path = tmp_path / "index-qualification.json"
    cleanup_path = tmp_path / "cleanup.json"
    report_path = tmp_path / "capacity-report.json"
    manifest_path = tmp_path / "artifact-manifest.json"
    runtime_path = tmp_path / "runtime-pressure.json"
    infrastructure_path = tmp_path / "infrastructure-cleanup.json"
    output_path = tmp_path / "validated.json"
    qualification_path.write_text(json.dumps(qualification))
    cleanup_path.write_text(json.dumps(cleanup))
    runtime_path.write_text(json.dumps(_runtime(validator)))
    infrastructure_path.write_text(json.dumps(_infrastructure_cleanup(validator)))
    report = _report(validator)
    report["index_qualification"]["artifact_sha256"] = hashlib.sha256(
        qualification_path.read_bytes()
    ).hexdigest()
    report["cleanup"] = {
        "database_removed": True,
        "execution_id": _execution_id("qualification"),
        "artifact_sha256": hashlib.sha256(cleanup_path.read_bytes()).hexdigest(),
    }
    report_path.write_text(json.dumps(report))
    artifacts = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            qualification_path,
            report_path,
            cleanup_path,
            runtime_path,
            infrastructure_path,
        )
    }
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": validator.SCHEMA_VERSION,
                "source_revision": SOURCE_SHA,
                "execution_id": _execution_id("qualification"),
                "mode": "qualification",
                "kind": "capacity_artifact_manifest",
                "artifacts": artifacts,
            }
        )
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "validate_capacity_evidence.py",
            "--input",
            str(report_path),
            "--qualification",
            str(qualification_path),
            "--cleanup",
            str(cleanup_path),
            "--manifest",
            str(manifest_path),
            "--runtime",
            str(runtime_path),
            "--infrastructure-cleanup",
            str(infrastructure_path),
            "--source-revision",
            SOURCE_SHA,
            "--execution-id",
            _execution_id("qualification"),
            "--output",
            str(output_path),
        ],
    )
    assert validator.main() == 0
    report_path.write_text(report_path.read_text() + " ")
    with pytest.raises(ValueError, match="hashes"):
        validator.main()


def test_workflow_is_owner_only_exact_main_bounded_and_always_cleans_up():
    path = ROOT / ".github/workflows/capacity-qualification.yml"
    source = path.read_text()
    workflow = yaml.safe_load(source)

    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "hindsight-capacity-qualification",
        "cancel-in-progress": False,
    }
    authorize = workflow["jobs"]["authorize"]
    diagnostic = workflow["jobs"]["diagnostic"]
    qualification = workflow["jobs"]["qualify"]
    runner = "${{ vars.HINDSIGHT_RUNNER_LABEL }}"
    assert authorize["runs-on"] == runner
    assert diagnostic["runs-on"] == runner
    assert qualification["runs-on"] == runner
    assert "$ACTOR" in authorize["steps"][0]["run"]
    assert "$TRIGGERING_ACTOR" in authorize["steps"][0]["run"]
    assert "refs/heads/main" in authorize["steps"][0]["run"]
    assert '"$RUN_ATTEMPT" == "1"' in authorize["steps"][0]["run"]
    assert qualification["needs"] == ["authorize", "diagnostic"]
    assert diagnostic["timeout-minutes"] == 35
    assert qualification["timeout-minutes"] == 35
    for job in (diagnostic, qualification):
        rerun_guard = job["steps"][0]
        assert rerun_guard["name"] == "Reject downstream rerun attempts"
        assert 'test "$RUN_ATTEMPT" = "1"' in rerun_guard["run"]
        assert 'test "$COMPOSE_PROJECT_NAME" = "hindsight_$EXECUTION_ID"' in rerun_guard["run"]
    assert qualification["env"]["COMPOSE_PROJECT_NAME"].endswith(
        "${{ github.run_id }}_${{ github.run_attempt }}_qualification"
    )
    expected_args = (
        "--store=type=mem,size=2GiB --cache=128MiB --max-sql-memory=128MiB "
        "--max-tsdb-memory=64MiB --max-go-memory=3GiB"
    )
    assert diagnostic["env"]["COCKROACH_START_ARGS"] == expected_args
    assert qualification["env"]["COCKROACH_START_ARGS"] == expected_args
    assert diagnostic["env"]["CAPACITY_MODE"] == "diagnostic"
    assert qualification["env"]["CAPACITY_MODE"] == "qualification"
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "start-single-node --insecure ${COCKROACH_START_ARGS:-}" in compose
    assert "--timeout-seconds 1200" in source
    assert "scripts/run_capacity_qualification.py" in source
    assert "scripts/capture_capacity_runtime.py" in source
    assert "scripts/validate_capacity_diagnostic.py" in source
    assert "scripts/validate_capacity_evidence.py" in source
    assert source.index("validate_capacity_diagnostic.py") < source.index("qualify:")
    cleanup = next(
        step
        for step in qualification["steps"]
        if step.get("name") == "Remove isolated CockroachDB and storage"
    )
    assert cleanup["if"] == "always()"
    assert "docker compose down --volumes --remove-orphans" in cleanup["run"]
    assert "remaining_containers" in cleanup["run"]
    assert "remaining_volumes" in cleanup["run"]
    assert "remaining_networks" in cleanup["run"]
    assert "container_query_status" in cleanup["run"]
    assert "volume_query_status" in cleanup["run"]
    assert "network_query_status" in cleanup["run"]
    upload = qualification["steps"][-1]
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"


def test_diagnostic_profile_is_exact_and_keeps_twenty_clients():
    producer = _script("run_capacity_qualification")
    producer._configure_mode("diagnostic")
    assert producer.TARGETS == {
        "vectors": 75_000,
        "tenants": 15,
        "clients": 20,
        "backlog_messages": 1_000,
    }
    assert producer.ROWS_PER_TENANT == 5_000
    assert producer.VECTOR_CODE_OFFSET == 15
    producer._configure_mode("qualification")
    assert producer.TARGETS == producer.QUALIFICATION_TARGETS


def test_spawned_seed_worker_receives_the_diagnostic_profile(monkeypatch):
    producer = _script("run_capacity_qualification")
    captured = []

    class ResultQueue:
        def close(self):
            return None

        def join_thread(self):
            return None

    class Process:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self.name = kwargs["name"]

        def start(self):
            raise RuntimeError("stop after inspecting spawn arguments")

    context = SimpleNamespace(Queue=ResultQueue, Process=Process)
    monkeypatch.setattr(producer.multiprocessing, "get_context", lambda _method: context)
    monkeypatch.setattr(producer, "_stop_seed_processes", lambda *_args: None)
    with pytest.raises(RuntimeError, match="spawn arguments"):
        producer._run_seed_shards("postgresql://db", producer.Deadline.after(10), mode="diagnostic")
    assert captured[0]["target"] is producer._seed_shard_worker
    assert captured[0]["args"][-1] == "diagnostic"


def test_diagnostic_clients_probe_fifteen_tenants_round_robin(monkeypatch):
    producer = _script("run_capacity_qualification")
    producer._configure_mode("diagnostic")

    def probe(_url, _run_id, client, tenant, _timeout):
        return {"client": client, "tenant": tenant}

    monkeypatch.setattr(producer, "_client_probe", probe)
    rows = producer._exercise_clients("postgresql://db", "abcdefgh", producer.Deadline.after(10))
    assert [row["client"] for row in rows] == list(range(1, 21))
    assert [row["tenant"] for row in rows] == [*range(1, 16), *range(1, 6)]


def test_diagnostic_producer_never_writes_final_artifacts(tmp_path, monkeypatch):
    producer = _script("run_capacity_qualification")
    monkeypatch.setattr(producer, "_verify_checkout", lambda _source_sha: None)
    monkeypatch.setattr(producer, "_create_database", lambda *_args: None)
    monkeypatch.setattr(producer, "_drop_database", lambda *_args: None)
    monkeypatch.setattr(
        producer,
        "_run",
        lambda *_args: (
            {
                "schema_version": producer.DIAGNOSTIC_SCHEMA_VERSION,
                "qualified": False,
                "qualification_evidence": False,
                "observation_only": True,
                "mode": "diagnostic",
            },
            {
                "method": {},
                "environment": {},
                "ceilings": {},
                "raw_measurements": [],
                "limitations": [],
            },
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_capacity_qualification.py",
            "--admin-url",
            "postgresql://root@localhost/defaultdb",
            "--run-id",
            hashlib.sha256(_execution_id("diagnostic").encode()).hexdigest()[:16],
            "--execution-id",
            _execution_id("diagnostic"),
            "--source-sha",
            SOURCE_SHA,
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "60",
            "--mode",
            "diagnostic",
        ],
    )
    assert producer.main() == 0
    assert (tmp_path / "capacity-diagnostic.json").is_file()
    for name in (
        "index-qualification.json",
        "capacity-report.json",
        "artifact-manifest.json",
        "validated-capacity-report.json",
    ):
        assert not (tmp_path / name).exists()
    diagnostic = json.loads((tmp_path / "capacity-diagnostic.json").read_text())
    assert diagnostic["qualification_evidence"] is False
    assert diagnostic["acceptance_eligible"] is False
    assert "qualified" not in diagnostic["index_observation"]


def test_final_validator_rejects_diagnostic_identity_even_with_final_counts():
    validator = _script("validate_capacity_evidence")
    report = _report(validator)
    report.update(
        {
            "schema_version": validator.DIAGNOSTIC_SCHEMA_VERSION,
            "kind": "capacity_resource_diagnostic",
            "mode": "diagnostic",
            "qualification_evidence": False,
        }
    )
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "mode": "qualification",
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "error": None,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    with pytest.raises(ValueError, match="schema version"):
        _validate(
            validator,
            report,
            source_revision=SOURCE_SHA,
            qualification=_qualification(validator),
            cleanup=cleanup,
            artifact_digests={
                "index-qualification.json": "b" * 64,
                "capacity-report.json": "c" * 64,
                "cleanup.json": "d" * 64,
            },
        )


def test_runtime_rejects_every_memory_argument_mutation():
    validator = _script("validate_capacity_evidence")
    expected = list(validator.EXPECTED_PROCESS_ARGS)
    mutations = [
        expected[:-1],
        [*expected, expected[-1]],
        [*expected, "--max-disk-temp-storage=100MiB"],
        [*expected[:2], expected[3], expected[2], *expected[4:]],
        [*expected[:2], "--store=type=mem,size=2048MiB", *expected[3:]],
        [*expected[:2], "--store=type=mem,size=50%", *expected[3:]],
    ]
    for mutation in mutations:
        runtime = _runtime(validator)
        runtime["effective_process"]["args"] = mutation
        with pytest.raises(ValueError, match="reviewed memory arguments"):
            validator._validate_runtime(
                runtime,
                source_revision=SOURCE_SHA,
                mode="qualification",
                execution_id=_execution_id("qualification"),
            )


@pytest.mark.parametrize("event_key", ["low", "high", "max", "oom", "oom_kill", "oom_group_kill"])
def test_runtime_rejects_each_positive_pressure_delta(event_key):
    validator = _script("validate_capacity_evidence")
    runtime = _runtime(validator, deltas={event_key: 1})
    with pytest.raises(ValueError, match="memory-pressure events"):
        validator._validate_runtime(
            runtime,
            source_revision=SOURCE_SHA,
            mode="qualification",
            execution_id=_execution_id("qualification"),
        )


def test_runtime_accepts_nonzero_unchanged_history_and_rejects_counter_regression():
    validator = _script("validate_capacity_evidence")
    runtime = _runtime(validator)
    assert runtime["cgroup"]["events_before"]["max"] == 828_396
    validator._validate_runtime(
        runtime,
        source_revision=SOURCE_SHA,
        mode="qualification",
        execution_id=_execution_id("qualification"),
    )
    runtime["cgroup"]["events_after"]["max"] -= 1
    runtime["cgroup"]["event_deltas"]["max"] = -1
    with pytest.raises(ValueError, match="memory-pressure events"):
        validator._validate_runtime(
            runtime,
            source_revision=SOURCE_SHA,
            mode="qualification",
            execution_id=_execution_id("qualification"),
        )


def test_runtime_rejects_peak_at_limit_missing_counter_and_cpu_change():
    validator = _script("validate_capacity_evidence")
    runtime = _runtime(validator, peak_bytes=4 * 1024**3)
    with pytest.raises(ValueError, match="cgroup telemetry|container cgroup"):
        validator._validate_runtime(
            runtime,
            source_revision=SOURCE_SHA,
            mode="qualification",
            execution_id=_execution_id("qualification"),
        )
    runtime = _runtime(validator)
    for section in ("events_before", "events_after", "event_deltas"):
        runtime["cgroup"][section].pop("oom_kill")
    with pytest.raises(ValueError, match="cgroup telemetry"):
        validator._validate_runtime(
            runtime,
            source_revision=SOURCE_SHA,
            mode="qualification",
            execution_id=_execution_id("qualification"),
        )
    runtime = _runtime(validator)
    runtime["cgroup"]["cpu_quota_us"] = 200_000
    with pytest.raises(ValueError, match="cgroup telemetry"):
        validator._validate_runtime(
            runtime,
            source_revision=SOURCE_SHA,
            mode="qualification",
            execution_id=_execution_id("qualification"),
        )


def test_infrastructure_cleanup_rejects_any_remaining_compose_state():
    validator = _script("validate_capacity_evidence")
    for field in ("remaining_containers", "remaining_volumes", "remaining_networks"):
        cleanup = _infrastructure_cleanup(validator)
        cleanup[field] = 1
        cleanup["compose_state_removed"] = False
        with pytest.raises(ValueError, match="Compose cleanup"):
            validator._validate_infrastructure_cleanup(
                cleanup,
                source_revision=SOURCE_SHA,
                mode="qualification",
                execution_id=_execution_id("qualification"),
                project="hindsight_capacity_123_1_qualification",
            )
    for field in ("container_query_status", "volume_query_status", "network_query_status"):
        cleanup = _infrastructure_cleanup(validator)
        cleanup[field] = 1
        cleanup["compose_state_removed"] = False
        with pytest.raises(ValueError, match="Compose cleanup"):
            validator._validate_infrastructure_cleanup(
                cleanup,
                source_revision=SOURCE_SHA,
                mode="qualification",
                execution_id=_execution_id("qualification"),
                project="hindsight_capacity_123_1_qualification",
            )


def test_diagnostic_timing_gate_uses_conservative_projection_and_headroom():
    validator = _script("validate_capacity_diagnostic")
    bundle = _diagnostic_bundle(validator, duration=7680 / 11)
    report = validator.validate(
        bundle[0],
        source_revision=SOURCE_SHA,
        execution_id=_execution_id("diagnostic"),
        cleanup=bundle[1],
        runtime=bundle[2],
        infrastructure_cleanup=bundle[3],
        artifact_digests=bundle[4],
    )
    assert report["projection"]["projected_final_duration_seconds"] == 960
    assert report["projection"]["minimum_headroom_seconds"] == 240
    bundle = _diagnostic_bundle(validator, duration=699)
    with pytest.raises(ValueError, match="timing headroom"):
        validator.validate(
            bundle[0],
            source_revision=SOURCE_SHA,
            execution_id=_execution_id("diagnostic"),
            cleanup=bundle[1],
            runtime=bundle[2],
            infrastructure_cleanup=bundle[3],
            artifact_digests=bundle[4],
        )


def test_diagnostic_rejects_memory_peak_above_its_headroom_gate():
    validator = _script("validate_capacity_diagnostic")
    bundle = _diagnostic_bundle(
        validator, peak_bytes=validator.MAX_DIAGNOSTIC_SAMPLED_PEAK_BYTES + 1
    )
    with pytest.raises(ValueError, match="memory headroom"):
        validator.validate(
            bundle[0],
            source_revision=SOURCE_SHA,
            execution_id=_execution_id("diagnostic"),
            cleanup=bundle[1],
            runtime=bundle[2],
            infrastructure_cleanup=bundle[3],
            artifact_digests=bundle[4],
        )


def test_runtime_collector_inspects_the_entrypoint_and_exact_command(monkeypatch):
    capture = _script("capture_capacity_runtime")
    container_id = "a" * 64
    inspection = [
        {
            "Path": "/cockroach/cockroach.sh",
            "Args": list(capture.EXPECTED_PROCESS_ARGS),
            "Config": {
                "Cmd": list(capture.EXPECTED_PROCESS_ARGS),
                "Image": capture.EXPECTED_IMAGE,
                "Labels": {
                    "com.docker.compose.project": ("hindsight_capacity_123_1_diagnostic"),
                    "com.docker.compose.service": "crdb",
                },
            },
            "State": {"Running": True},
        }
    ]
    pid_ready = False

    def run(command, **_kwargs):
        nonlocal pid_ready
        if command[:4] == ["docker", "compose", "ps", "-q"]:
            return SimpleNamespace(returncode=0, stdout=f"{container_id}\n")
        if command[:4] == ["docker", "compose", "exec", "-T"]:
            if command[-1] == "/cockroach/server_pid":
                if not pid_ready:
                    return SimpleNamespace(returncode=1, stdout="")
                return SimpleNamespace(returncode=0, stdout="42\n")
            assert command[-1] == "/proc/42/cmdline"
            return SimpleNamespace(
                returncode=0, stdout="\0".join(capture.EXPECTED_LIVE_PROCESS_ARGS)
            )
        assert command == ["docker", "inspect", container_id]
        return SimpleNamespace(returncode=0, stdout=json.dumps(inspection))

    monkeypatch.setattr(capture.subprocess, "run", run)
    assert capture._inspect_container("hindsight_capacity_123_1_diagnostic") is None
    pid_ready = True
    process = capture._inspect_container("hindsight_capacity_123_1_diagnostic")
    assert process["path"] == "/cockroach/cockroach.sh"
    assert process["args"] == list(capture.EXPECTED_PROCESS_ARGS)
    assert process["live_argv"] == list(capture.EXPECTED_LIVE_PROCESS_ARGS)
    inspection[0]["Config"]["Cmd"][-1] = "--max-go-memory=2GiB"
    with pytest.raises(RuntimeError, match="reviewed envelope"):
        capture._inspect_container("hindsight_capacity_123_1_diagnostic")


def test_runtime_collector_retries_zero_effective_metric_and_requires_exact_values(
    monkeypatch,
):
    capture = _script("capture_capacity_runtime")
    monkeypatch.setattr(capture, "_query_single_integer", lambda _statement: 0)
    assert capture._inspect_effective_memory() is None

    def exact(statement):
        if "node_metrics" in statement:
            return 3 * 1024**3
        return 2 * 1024**3

    monkeypatch.setattr(capture, "_query_single_integer", exact)
    assert capture._inspect_effective_memory() == {
        "go_limit_bytes": 3 * 1024**3,
        "store_capacity_bytes": 2 * 1024**3,
        "store_count": 1,
    }
    monkeypatch.setattr(
        capture,
        "_query_single_integer",
        lambda statement: 2 * 1024**3 if "node_metrics" in statement else 2 * 1024**3,
    )
    with pytest.raises(RuntimeError, match="effective database memory"):
        capture._inspect_effective_memory()


def test_runtime_finalizer_uses_deltas_and_omits_local_cgroup_identity(tmp_path, monkeypatch):
    capture = _script("capture_capacity_runtime")
    project = "hindsight_capacity_123_1_diagnostic"
    configured = capture._configured_envelope()
    before_events = {
        "low": 0,
        "high": 0,
        "max": 828_396,
        "oom": 0,
        "oom_kill": 0,
        "oom_group_kill": 0,
    }
    baseline = {
        "schema_version": capture.BASELINE_SCHEMA_VERSION,
        "source_revision": SOURCE_SHA,
        "mode": "diagnostic",
        "execution_id": _execution_id("diagnostic"),
        "compose_project": project,
        "configured": configured,
        "cgroup": {
            "identity": {"device": 1, "inode": 2},
            "memory_max_bytes": 4 * 1024**3,
            "cpu_quota_us": 150_000,
            "cpu_period_us": 100_000,
            "memory_current_bytes": 100,
            "kernel_memory_peak_bytes": 4 * 1024**3,
            "events": before_events,
        },
    }
    samples = {
        "schema_version": capture.SAMPLES_SCHEMA_VERSION,
        "source_revision": SOURCE_SHA,
        "mode": "diagnostic",
        "execution_id": _execution_id("diagnostic"),
        "compose_project": project,
        "configured": configured,
        "cgroup_identity": {"device": 1, "inode": 2},
        "sample_count": 5,
        "sampled_peak_bytes": 3 * 1024**3,
        "effective_process": {"reviewed": True},
        "container_cgroup": {"version": 2},
        "error": None,
    }
    baseline_path = tmp_path / "baseline.json"
    samples_path = tmp_path / "samples.json"
    output_path = tmp_path / "runtime.json"
    baseline_path.write_text(json.dumps(baseline))
    samples_path.write_text(json.dumps(samples))
    final_snapshot = {
        "identity": {"device": 1, "inode": 2},
        "memory_max_bytes": 4 * 1024**3,
        "cpu_quota_us": 150_000,
        "cpu_period_us": 100_000,
        "memory_current_bytes": 200,
        "kernel_memory_peak_bytes": 4 * 1024**3,
        "events": dict(before_events),
    }
    monkeypatch.setattr(capture, "_validate_invocation", lambda *_args: None)
    monkeypatch.setattr(capture, "_current_cgroup_directory", lambda: tmp_path)
    monkeypatch.setattr(capture, "_snapshot", lambda _directory, **_kwargs: final_snapshot)
    args = SimpleNamespace(
        source_revision=SOURCE_SHA,
        mode="diagnostic",
        execution_id=_execution_id("diagnostic"),
        compose_project=project,
        baseline=baseline_path,
        samples=samples_path,
        output=output_path,
    )
    assert capture._finalize(args) == 0
    runtime = json.loads(output_path.read_text())
    assert "identity" not in runtime["cgroup"]
    assert runtime["cgroup"]["events_before"]["max"] == 828_396
    assert set(runtime["cgroup"]["event_deltas"].values()) == {0}
