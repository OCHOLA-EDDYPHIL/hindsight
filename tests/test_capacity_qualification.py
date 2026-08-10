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
        "main_sha": SOURCE_SHA,
        "index": validator.EXPECTED_INDEX,
        "vector_dimensions": 1024,
        "vector_count": 100_000,
        "tenant_count": 20,
        "per_tenant_counts": [
            {"tenant_id": f"tenant-{number}", "vectors": 5_000} for number in range(20)
        ],
        "plans": [
            {
                "client": number,
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
        "index_qualification": {
            "qualified": True,
            "artifact_sha256": "b" * 64,
            "main_sha": SOURCE_SHA,
        },
        "targets": dict(validator.TARGETS),
        "ceilings": dict(validator.EXPECTED_CEILINGS),
        "method": {"vectors": "deterministic synthetic"},
        "environment": {
            "isolation": "run_scoped_database_and_compose_project",
            "paid_model_calls": 0,
            "live_worker_invocations": 0,
        },
        "raw_measurements": [
            {
                "name": "vector_seed",
                "duration_seconds": 1,
                "batches": 20,
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
                "name": "vector_counts",
                "total": 100_000,
                "per_tenant": [
                    {"tenant_id": f"tenant-{number}", "vectors": 5_000} for number in range(20)
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
            },
            {"name": "storage", "bytes": 1_000_000},
            {"name": "total", "duration_seconds": 10},
        ],
        "limitations": ["Benchmark evidence; not production SLO claims."],
    }


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
    assert producer.SEED_SHARDS == 5
    assert {
        shard: sum(1 for number in range(1, 21) if (number - 1) % producer.SEED_SHARDS == shard)
        for shard in range(producer.SEED_SHARDS)
    } == {shard: 4 for shard in range(producer.SEED_SHARDS)}
    assert producer.MAX_DURATION_SECONDS == 1_200
    assert producer.MAX_STORAGE_BYTES == 1_500_000_000
    assert producer.MAX_EXTERNAL_COST_USD == 0
    assert producer._tenant_id("abcdefgh", 1) == producer._tenant_id("abcdefgh", 1)
    assert len({producer._namespace(number) for number in range(1, 21)}) == 20
    vector = producer._vector(20)
    assert len(vector.removeprefix("[").removesuffix("]").split(",")) == 1024
    assert vector.count("1") == 1
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
    assert source.count("JOIN capacity_tenant_seed AS tenant USING (tenant_number)") == 3
    assert "'Bounded synthetic index qualification', namespace, 'open'" in source
    assert "SET status = 'sealed', sealed_at = now()" in source
    assert "ANALYZE semantic_memory_vectors" in source
    assert "DROP TRIGGER {} ON {}" in source
    assert "CREATE TRIGGER {} BEFORE INSERT OR UPDATE OR DELETE ON {}" in source
    assert "_restore_bulk_seed_guards(conn, deadline)" in source
    assert "_restore_bulk_seed_memory_triggers(conn, deadline)" in source
    assert "_verify_bulk_seed_provenance(conn, deadline)" in source
    assert "DROP INDEX" not in source
    assert "five_set_based_shards_with_serialized_completion_checks" in source
    assert 'multiprocessing.get_context("spawn")' in source
    assert "_stop_seed_processes(started" in source


def test_producer_refuses_remote_or_unbounded_targets():
    producer = _script("run_capacity_qualification")

    with pytest.raises(ValueError, match="loopback defaultdb"):
        producer._validate_inputs(
            "postgresql://root@database.example/defaultdb", "abcdefgh", SOURCE_SHA, 600
        )
    with pytest.raises(ValueError, match="between 60 and 1200"):
        producer._validate_inputs(
            "postgresql://root@localhost/defaultdb", "abcdefgh", SOURCE_SHA, 1201
        )
    assert producer._is_disposable("hindsight_capacity_abcdefgh") is True
    assert producer._is_disposable("defaultdb") is False


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
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    digests = {
        "index-qualification.json": "b" * 64,
        "capacity-report.json": "c" * 64,
        "cleanup.json": "d" * 64,
    }

    assert (
        validator.validate(
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
        validator.validate(
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )
    qualification["vector_count"] = 100_000
    qualification["plans"].pop()
    with pytest.raises(ValueError, match="twenty bounded clients"):
        validator.validate(
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )
    qualification = _qualification(validator)
    with pytest.raises(ValueError, match="cleanup"):
        validator.validate(
            report,
            source_revision=SOURCE_SHA,
            cleanup={
                "schema_version": validator.SCHEMA_VERSION,
                "database": "hindsight_capacity_abcdefgh",
                "database_removed": False,
                "source_revision": SOURCE_SHA,
                "timeout_seconds": 120,
            },
            qualification=qualification,
            artifact_digests=digests,
        )
    report["ceilings"] = {**validator.EXPECTED_CEILINGS, "clients": 21}
    with pytest.raises(ValueError, match="hard ceilings"):
        validator.validate(
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )


def test_validator_requires_bound_supplemental_artifacts():
    validator = _script("validate_capacity_evidence")

    with pytest.raises(ValueError, match="requires qualification, cleanup, and artifact"):
        validator.validate(_report(validator), source_revision=SOURCE_SHA)


def test_validator_rejects_booleans_in_exact_numeric_evidence():
    validator = _script("validate_capacity_evidence")
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
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
        validator.validate(
            report,
            source_revision=SOURCE_SHA,
            qualification=qualification,
            cleanup=cleanup,
            artifact_digests=digests,
        )

    report = _report(validator)
    report["ceilings"]["external_cost_usd"] = False
    with pytest.raises(ValueError, match="hard ceilings"):
        validator.validate(
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
        validator.validate(
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
        validator.validate(
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
        validator.validate(
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
        validator.validate(
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
        validator.validate(
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
        validator.validate(
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


def test_seed_completion_checks_are_serialized(monkeypatch):
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
    monkeypatch.setattr(
        producer,
        "_check_storage",
        lambda _url, _deadline, *, completion_sequence, completed_tenants, conn: {
            "completion_sequence": completion_sequence,
            "completed_tenants": completed_tenants,
            "bytes": completed_tenants,
        },
    )
    assert producer._seal_and_measure("postgresql://db", "abcdefgh", 2, SimpleNamespace(), 1) == {
        "completion_sequence": 1,
        "completed_tenants": 1,
        "bytes": 1,
    }
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
        producer._seal_and_measure("postgresql://db", "abcdefgh", 1, SimpleNamespace(), 1)


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
    monkeypatch.setattr(producer, "_load_seed_shard", lambda *_args: None)
    producer._seed_shard_worker("postgresql://db", 123.0, 2, results)
    assert rows == [(2, None)]

    def fail(*_args):
        raise RuntimeError("x" * 2_000)

    monkeypatch.setattr(producer, "_load_seed_shard", fail)
    producer._seed_shard_worker("postgresql://db", 123.0, 3, results)
    assert rows[-1][0] == 3
    assert rows[-1][1].startswith("RuntimeError: ")
    assert len(rows[-1][1]) == 800


def test_seed_shard_partial_start_reaps_started_processes(monkeypatch):
    producer = _script("run_capacity_qualification")

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


def test_drop_database_applies_bounded_statement_and_lock_timeouts(monkeypatch):
    producer = _script("run_capacity_qualification")
    calls = []

    class Result:
        def fetchall(self):
            return [("defaultdb",)]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            calls.append((str(query), params))
            return Result()

    def connect(url, **kwargs):
        assert url == "postgresql://root@localhost/defaultdb"
        assert kwargs["connect_timeout"] == 5
        assert kwargs["application_name"] == "hindsight-capacity-cleanup"
        return Connection()

    monkeypatch.setattr(producer.psycopg, "connect", connect)
    deadline = SimpleNamespace(remaining=lambda maximum: 7)
    producer._drop_database(
        "postgresql://root@localhost/defaultdb",
        "hindsight_capacity_abcdefgh",
        deadline,
    )

    assert ("SELECT set_config('statement_timeout', %s, false)", ("7000ms",)) in calls
    assert ("SELECT set_config('lock_timeout', %s, false)", ("7000ms",)) in calls


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
            "abcdefgh",
            "--source-sha",
            SOURCE_SHA,
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "60",
        ],
    )

    with pytest.raises(RuntimeError, match="qualification failed"):
        producer.main()
    receipt = json.loads((tmp_path / "cleanup.json").read_text())
    assert receipt["database_removed"] is False
    assert receipt["timeout_seconds"] == producer.MAX_CLEANUP_SECONDS
    assert receipt["error"] == "TimeoutError: cleanup deadline"


def test_artifact_manifest_hashes_exact_bytes(tmp_path, monkeypatch):
    validator = _script("validate_capacity_evidence")
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "source_revision": SOURCE_SHA,
        "timeout_seconds": 120,
    }
    qualification_path = tmp_path / "index-qualification.json"
    cleanup_path = tmp_path / "cleanup.json"
    report_path = tmp_path / "capacity-report.json"
    manifest_path = tmp_path / "artifact-manifest.json"
    output_path = tmp_path / "validated.json"
    qualification_path.write_text(json.dumps(qualification))
    cleanup_path.write_text(json.dumps(cleanup))
    report = _report(validator)
    report["index_qualification"]["artifact_sha256"] = hashlib.sha256(
        qualification_path.read_bytes()
    ).hexdigest()
    report["cleanup"] = {
        "database_removed": True,
        "artifact_sha256": hashlib.sha256(cleanup_path.read_bytes()).hexdigest(),
    }
    report_path.write_text(json.dumps(report))
    artifacts = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (qualification_path, report_path, cleanup_path)
    }
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": validator.SCHEMA_VERSION,
                "source_revision": SOURCE_SHA,
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
            "--source-revision",
            SOURCE_SHA,
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
    qualification = workflow["jobs"]["qualify"]
    assert authorize["runs-on"] == "ubuntu-latest"
    assert qualification["runs-on"] == "${{ vars.HINDSIGHT_RUNNER_LABEL || 'ubuntu-latest' }}"
    assert "$ACTOR" in authorize["steps"][0]["run"]
    assert "$TRIGGERING_ACTOR" in authorize["steps"][0]["run"]
    assert "refs/heads/main" in authorize["steps"][0]["run"]
    assert qualification["timeout-minutes"] == 25
    assert qualification["env"]["COMPOSE_PROJECT_NAME"].endswith(
        "${{ github.run_id }}_${{ github.run_attempt }}"
    )
    assert "--timeout-seconds 1200" in source
    assert "scripts/run_capacity_qualification.py" in source
    assert "scripts/validate_capacity_evidence.py" in source
    cleanup = next(
        step
        for step in qualification["steps"]
        if step.get("name") == "Remove isolated CockroachDB and storage"
    )
    assert cleanup["if"] == "always()"
    assert "docker compose down --volumes --remove-orphans" in cleanup["run"]
    assert "remaining_containers" in cleanup["run"]
    assert "remaining_volumes" in cleanup["run"]
    upload = qualification["steps"][-1]
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
