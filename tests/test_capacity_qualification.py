from __future__ import annotations

import hashlib
import importlib.util
import json
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
            {"name": "vector_seed", "duration_seconds": 1},
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
                "messages_accounted_for": 1_000,
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
    assert backlog["clients"] == 20
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
        validator.validate(report, source_revision=SOURCE_SHA, qualification=qualification)
    qualification["vector_count"] = 100_000
    qualification["plans"].pop()
    with pytest.raises(ValueError, match="twenty bounded clients"):
        validator.validate(report, source_revision=SOURCE_SHA, qualification=qualification)
    with pytest.raises(ValueError, match="cleanup"):
        validator.validate(
            report,
            source_revision=SOURCE_SHA,
            cleanup={
                "schema_version": validator.SCHEMA_VERSION,
                "database": "hindsight_capacity_abcdefgh",
                "database_removed": False,
                "source_revision": SOURCE_SHA,
            },
        )
    report["ceilings"] = {**validator.EXPECTED_CEILINGS, "clients": 21}
    with pytest.raises(ValueError, match="hard ceilings"):
        validator.validate(report, source_revision=SOURCE_SHA)


def test_artifact_manifest_hashes_exact_bytes(tmp_path, monkeypatch):
    validator = _script("validate_capacity_evidence")
    qualification = _qualification(validator)
    cleanup = {
        "schema_version": validator.SCHEMA_VERSION,
        "database": "hindsight_capacity_abcdefgh",
        "database_removed": True,
        "source_revision": SOURCE_SHA,
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
