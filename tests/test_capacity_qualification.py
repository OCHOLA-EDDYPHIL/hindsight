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
        "targets": validator.TARGETS,
        "ceilings": validator.EXPECTED_CEILINGS,
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
                    {"tenants_seeded": number, "bytes": number * 1_000}
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

    assert producer.TARGETS == {
        "vectors": 100_000,
        "tenants": 20,
        "clients": 20,
        "backlog_messages": 1_000,
    }
    assert producer.MAX_CLIENTS == 20
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


def test_storage_is_checked_during_seeding_and_fails_at_ceiling(monkeypatch):
    producer = _script("run_capacity_qualification")
    measurements = iter([producer.MAX_STORAGE_BYTES, producer.MAX_STORAGE_BYTES + 1])
    monkeypatch.setattr(producer, "_storage_bytes", lambda *_args: next(measurements))
    deadline = SimpleNamespace()

    assert producer._check_storage("postgresql://db", deadline, tenants_seeded=1) == {
        "tenants_seeded": 1,
        "bytes": producer.MAX_STORAGE_BYTES,
    }
    with pytest.raises(RuntimeError, match="after tenant 2"):
        producer._check_storage("postgresql://db", deadline, tenants_seeded=2)


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
