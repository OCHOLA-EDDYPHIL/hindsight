from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evidence_reuse", ROOT / "scripts/evidence_reuse.py"
)
assert SPEC is not None and SPEC.loader is not None
evidence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evidence
SPEC.loader.exec_module(evidence)

REPOSITORY = "owner/project"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Evidence Test")
    _git(repository, "config", "user.email", "evidence@example.com")
    for path in evidence.AUTHORIZED_DOCUMENT_PATHS:
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path}\n")
    (repository / "app.py").write_text("VALUE = 1\n")
    decision = repository / "docs/adr/0001-decision.md"
    decision.parent.mkdir(parents=True, exist_ok=True)
    decision.write_text("tracked input\n")
    migrations = repository / "migrations"
    migrations.mkdir()
    (migrations / "0001_initial.sql").write_text("SELECT 1;\n")
    return repository, _commit(repository, "source")


def _workflow(domain: str, source_revision: str, *, run_id: int = 41) -> dict:
    path = evidence.DOMAIN_SPECS[domain]["workflow_path"]
    return evidence._workflow_identity(
        repository=REPOSITORY,
        workflow_path=path,
        source_revision=source_revision,
        run_id=run_id,
        run_attempt=2,
        actor="owner",
        triggering_actor="owner",
        event_name="workflow_dispatch",
        ref_name="refs/heads/main",
        workflow_ref=f"{REPOSITORY}/{path}@refs/heads/main",
    )


def _migration_workload(source_revision: str, *, run_id: int = 41) -> dict:
    historical = {
        "schema_version": evidence.MIGRATION_HISTORY_SCHEMA,
        "status": "passed",
        "source_revision": source_revision,
        "workflow_run": {"id": run_id, "attempt": 2},
        "cases": [
            {"name": name, "return_code": 0, "succeeded": True}
            for name in evidence.MIGRATION_CASES
        ],
    }
    return evidence.complete_migration_workload(
        historical,
        source_revision=source_revision,
        run_id=run_id,
        run_attempt=2,
    )


def _recovery_workload(source_revision: str, *, run_id: int = 42) -> dict:
    token = evidence._expected_recovery_token(run_id, 2)
    schema_summary = {
        "sha256": "a" * 64,
        "section_counts": {"tables": 1},
        "section_sha256": {"tables": "b" * 64},
    }
    tables = {
        "app_meta": {
            "columns": ["key", "value"],
            "row_count": 1,
            "row_sha256": "c" * 64,
        }
    }
    data_summary = {"table_count": 1, "row_count": 1, "tables": tables}
    data_snapshot = {
        **data_summary,
        "sha256": evidence._document_sha256(data_summary),
    }
    return {
        "schema_version": evidence.RECOVERY_WORKLOAD_SCHEMA,
        "status": "passed",
        "source_sha": source_revision,
        "run_id": token,
        "scope": {
            "source_database": f"hindsight_recovery_source_{token}",
            "restore_database": f"hindsight_recovery_restore_{token}",
            "backup_uri": (
                f"userfile://defaultdb.public.hindsight_recovery_userfile_{token}/backup"
            ),
            "destructive_scope": "derived disposable resources only",
        },
        "timeline": {
            "started_at": "2026-08-11T00:00:00Z",
            "source_database_created_at": "2026-08-11T00:00:01Z",
            "migrations_completed_at": "2026-08-11T00:00:02Z",
            "agent_storage_initialized_at": "2026-08-11T00:00:03Z",
            "pre_backup_marker_at": "2026-08-11T00:00:04Z",
            "backup_started_at": "2026-08-11T00:00:05Z",
            "backup_completed_at": "2026-08-11T00:00:07Z",
            "backup_restore_point_at": "2026-08-11T00:00:06Z",
            "post_backup_marker_at": "2026-08-11T00:00:08Z",
            "source_loss_started_at": "2026-08-11T00:00:09Z",
            "source_loss_completed_at": "2026-08-11T00:00:10Z",
            "restore_started_at": "2026-08-11T00:00:11Z",
            "restore_completed_at": "2026-08-11T00:00:12Z",
            "validation_completed_at": "2026-08-11T00:00:13Z",
            "cleanup_completed_at": "2026-08-11T00:00:14Z",
            "completed_at": "2026-08-11T00:00:15Z",
        },
        "limitations": evidence.RECOVERY_LIMITATIONS,
        "engine": {
            "product": "CockroachDB",
            "version_string": "CockroachDB CCL v25.4.5",
            "cluster_setting_version": "25.4",
            "cluster_id": "9c8657d8-20dc-40da-b7d2-c4b6ea2e709a",
        },
        "topology": {"mode": "local-single-node", "node_count": 1},
        "migrations": {
            "count": 1,
            "last_filename": "0001_initial.sql",
            "filenames_sha256": evidence._document_sha256(["0001_initial.sql"]),
        },
        "cleanup": {
            "source_database_absent": True,
            "restore_database_absent": True,
            "userfile_tables_absent": True,
        },
        "validation": {
            "markers": {"pre_backup_present": True, "post_backup_absent": True},
            "schema_identity": {
                "matches": True,
                "source": schema_summary,
                "restored": schema_summary,
                "differing_sections": [],
                "difference_sample": {},
            },
            "data_identity": {
                "matches": True,
                "source": data_snapshot,
                "restored": data_snapshot,
            },
        },
        "measurements": {
            "backup_seconds": 2.0,
            "recovery_point_gap_seconds": 3.0,
            "recovery_point_gap_basis": (
                "CockroachDB backup end_time restore point through the start of "
                "simulated source loss"
            ),
            "first_unrestored_write_age_seconds": 1.0,
            "restore_to_validation_seconds": 2.0,
            "source_loss_to_validation_seconds": 4.0,
        },
        "elapsed_seconds": 15.0,
    }


def _bundle(
    repository: Path,
    source_revision: str,
    domain: str,
    tmp_path: Path,
) -> tuple[dict[str, bytes], dict]:
    run_id = 41 if domain == "migration" else 42
    workflow = _workflow(domain, source_revision, run_id=run_id)
    directory = tmp_path / f"{domain}-bundle"
    directory.mkdir()
    evidence.write_git_manifest(
        repository, source_revision, directory / evidence.INPUT_MANIFEST_FILENAME
    )
    workload = (
        _migration_workload(source_revision, run_id=run_id)
        if domain == "migration"
        else _recovery_workload(source_revision, run_id=run_id)
    )
    evidence._write_json(
        directory / evidence.DOMAIN_SPECS[domain]["workload_filename"], workload
    )
    evidence._write_json(
        directory / evidence.INFRASTRUCTURE_CLEANUP_FILENAME,
        {
            "source_revision": source_revision,
            "down_status": 0,
            "container_query_status": 0,
            "volume_query_status": 0,
            "remaining_containers": 0,
            "remaining_volumes": 0,
            "compose_state_removed": True,
        },
    )
    evidence.finalize_domain_evidence(
        domain,
        evidence_dir=directory,
        repository_root=repository,
        source_revision=source_revision,
        workflow=workflow,
    )
    return (
        {path.name: path.read_bytes() for path in directory.iterdir()},
        workflow,
    )


def _archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, value in sorted(files.items()):
            bundle.writestr(name, value)
    return output.getvalue()


def _run_metadata(domain: str, source_revision: str, *, run_id: int) -> dict:
    return {
        "id": run_id,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": source_revision,
        "path": evidence.DOMAIN_SPECS[domain]["workflow_path"],
        "actor": {"login": "owner"},
        "triggering_actor": {"login": "owner"},
        "repository": {"full_name": REPOSITORY},
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    }


def _artifact_metadata(
    domain: str,
    source_revision: str,
    archive: bytes,
    *,
    run_id: int,
) -> dict:
    return {
        "total_count": 1,
        "artifacts": [
            {
                "id": run_id + 100,
                "name": f"{evidence.DOMAIN_SPECS[domain]['artifact_prefix']}-{run_id}-2",
                "size_in_bytes": len(archive),
                "expired": False,
                "digest": f"sha256:{hashlib.sha256(archive).hexdigest()}",
                "archive_download_url": (
                    f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{run_id + 100}/zip"
                ),
                "created_at": "2026-08-11T00:00:00Z",
                "updated_at": "2026-08-11T00:00:01Z",
                "workflow_run": {
                    "id": run_id,
                    "head_branch": "main",
                    "head_sha": source_revision,
                },
            }
        ],
    }


def test_git_manifest_excludes_only_exact_authorized_documents(tmp_path: Path):
    repository, source_revision = _repository(tmp_path)

    manifest = evidence.build_git_manifest(repository, source_revision)

    assert manifest["excluded_paths"] == list(evidence.AUTHORIZED_DOCUMENT_PATHS)
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert set(evidence.AUTHORIZED_DOCUMENT_PATHS).isdisjoint(entries)
    assert "docs/adr/0001-decision.md" in entries
    assert entries["app.py"] == {
        "path": "app.py",
        "mode": "100644",
        "object_type": "blob",
        "object_id": _git(repository, "rev-parse", f"{source_revision}:app.py"),
        "content_size_bytes": len(b"VALUE = 1\n"),
        "content_identity_sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest(),
    }
    assert manifest["entry_count"] == len(manifest["entries"])
    without_digest = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    assert manifest["manifest_sha256"] == evidence._document_sha256(without_digest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "renamed.py"),
        ("mode", "100755"),
        ("object_type", "commit"),
        ("object_id", "b" * 40),
        ("content_identity_sha256", "c" * 64),
    ],
)
def test_git_manifest_fails_closed_on_entry_tampering(
    tmp_path: Path, field: str, value: str
):
    repository, source_revision = _repository(tmp_path)
    manifest = evidence.build_git_manifest(repository, source_revision)
    manifest["entries"][0][field] = value

    with pytest.raises(ValueError, match="exact repository tree"):
        evidence.validate_git_manifest(
            manifest,
            repository_root=repository,
            revision=source_revision,
        )


def test_git_manifest_rejects_numeric_type_coercion(tmp_path: Path):
    repository, source_revision = _repository(tmp_path)
    manifest = evidence.build_git_manifest(repository, source_revision)
    manifest["entry_count"] = float(manifest["entry_count"])

    with pytest.raises(ValueError, match="exact repository tree"):
        evidence.validate_git_manifest(
            manifest,
            repository_root=repository,
            revision=source_revision,
        )


def test_docs_delta_accepts_only_regular_content_modification(tmp_path: Path):
    repository, source_revision = _repository(tmp_path)
    (repository / "README.md").write_text("updated\n")
    target_revision = _commit(repository, "docs")

    delta = evidence.verify_documentation_delta(
        repository,
        source_revision=source_revision,
        target_revision=target_revision,
    )

    assert [change["path"] for change in delta] == ["README.md"]
    assert delta[0]["old_mode"] == delta[0]["new_mode"] == "100644"
    assert delta[0]["old_content_sha256"] != delta[0]["new_content_sha256"]
    assert (
        evidence.build_git_manifest(repository, source_revision)["inputs_sha256"]
        == evidence.build_git_manifest(repository, target_revision)["inputs_sha256"]
    )


@pytest.mark.parametrize("anomaly", ["content", "mode", "type", "add", "delete", "rename"])
def test_docs_delta_rejects_path_mode_type_and_status_anomalies(
    tmp_path: Path, anomaly: str
):
    repository, source_revision = _repository(tmp_path)
    if anomaly == "content":
        (repository / "app.py").write_text("VALUE = 2\n")
    elif anomaly == "mode":
        (repository / "README.md").chmod(0o755)
    elif anomaly == "type":
        (repository / "README.md").unlink()
        (repository / "README.md").symlink_to("app.py")
    elif anomaly == "add":
        (repository / "README.md").unlink()
        source_revision = _commit(repository, "without authorized document")
        (repository / "README.md").write_text("added\n")
    elif anomaly == "delete":
        (repository / "README.md").unlink()
    else:
        (repository / "README.md").rename(repository / "renamed.md")
    target_revision = _commit(repository, anomaly)

    with pytest.raises(ValueError, match="disallowed path, mode, type, or status"):
        evidence.verify_documentation_delta(
            repository,
            source_revision=source_revision,
            target_revision=target_revision,
        )


def test_domain_bundles_bind_manifest_workload_cleanup_and_receipt_hashes(tmp_path: Path):
    repository, source_revision = _repository(tmp_path)

    for domain in evidence.DOMAIN_SPECS:
        files, workflow = _bundle(repository, source_revision, domain, tmp_path)
        receipt = evidence.validate_domain_bundle(
            domain,
            files,
            repository_root=repository,
            source_revision=source_revision,
            workflow=workflow,
        )
        assert receipt["status"] == "passed"
        assert receipt["source_revision"] == source_revision
        assert receipt["verification"] == {
            "workload_passed": True,
            "domain_cleanup_verified": True,
            "infrastructure_cleanup_verified": True,
        }


@pytest.mark.parametrize(
    "tamper",
    [
        "extra_field",
        "engine",
        "topology",
        "migration_digest",
        "measurement_bound",
        "limitations",
        "timeline",
        "schema_summary",
        "data_summary",
    ],
)
def test_recovery_workload_fails_closed_on_structured_evidence_tampering(
    tmp_path: Path, tamper: str
):
    repository, source_revision = _repository(tmp_path)
    workload = copy.deepcopy(_recovery_workload(source_revision))
    if tamper == "extra_field":
        workload["forged"] = True
    elif tamper == "engine":
        workload["engine"]["product"] = "OtherDB"
    elif tamper == "topology":
        workload["topology"]["node_count"] = 2
    elif tamper == "migration_digest":
        workload["migrations"]["filenames_sha256"] = "f" * 64
    elif tamper == "measurement_bound":
        workload["measurements"]["backup_seconds"] = 1801
    elif tamper == "limitations":
        workload["limitations"] = workload["limitations"][:-1]
    elif tamper == "timeline":
        workload["timeline"]["completed_at"] = "2026-08-10T00:00:00Z"
    elif tamper == "schema_summary":
        workload["validation"]["schema_identity"]["restored"] = {
            **workload["validation"]["schema_identity"]["restored"],
            "sha256": "f" * 64,
        }
    else:
        workload["validation"]["data_identity"]["source"]["sha256"] = "f" * 64
        workload["validation"]["data_identity"]["restored"]["sha256"] = "f" * 64

    with pytest.raises(ValueError):
        evidence._validate_workload(
            "recovery",
            workload,
            repository_root=repository,
            source_revision=source_revision,
            run_id=42,
            run_attempt=2,
        )


@pytest.mark.parametrize("target", ["workload", "cleanup", "receipt", "manifest"])
def test_domain_bundle_fails_closed_on_internal_tampering(tmp_path: Path, target: str):
    repository, source_revision = _repository(tmp_path)
    files, workflow = _bundle(repository, source_revision, "migration", tmp_path)
    name = {
        "workload": "migration-compatibility.json",
        "cleanup": evidence.INFRASTRUCTURE_CLEANUP_FILENAME,
        "receipt": "migration-receipt.json",
        "manifest": evidence.ARTIFACT_MANIFEST_FILENAME,
    }[target]
    document = json.loads(files[name])
    document["tampered"] = True
    files[name] = json.dumps(document, sort_keys=True).encode()

    with pytest.raises(ValueError):
        evidence.validate_domain_bundle(
            "migration",
            files,
            repository_root=repository,
            source_revision=source_revision,
            workflow=workflow,
        )


def test_cleanup_and_workload_reject_bool_float_integer_coercion(tmp_path: Path):
    repository, source_revision = _repository(tmp_path)
    cleanup = {
        "source_revision": source_revision,
        "down_status": False,
        "container_query_status": 0.0,
        "volume_query_status": 0,
        "remaining_containers": False,
        "remaining_volumes": 0.0,
        "compose_state_removed": 1,
    }
    with pytest.raises(ValueError, match="cleanup"):
        evidence._validate_infrastructure_cleanup(
            cleanup, source_revision=source_revision
        )

    workload = _migration_workload(source_revision)
    workload["workflow_run"]["attempt"] = 2.0
    with pytest.raises(ValueError):
        evidence._validate_workload(
            "migration",
            workload,
            repository_root=repository,
            source_revision=source_revision,
            run_id=41,
            run_attempt=2,
        )


def test_receipt_rejects_boolean_integer_coercion_even_with_rehashed_manifest(
    tmp_path: Path,
):
    repository, source_revision = _repository(tmp_path)
    files, workflow = _bundle(repository, source_revision, "migration", tmp_path)
    receipt_name = evidence.DOMAIN_SPECS["migration"]["receipt_filename"]
    receipt = json.loads(files[receipt_name])
    receipt["verification"]["workload_passed"] = 1
    files[receipt_name] = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    artifact_manifest = json.loads(files[evidence.ARTIFACT_MANIFEST_FILENAME])
    artifact_manifest["files"][receipt_name] = evidence._file_record(files[receipt_name])
    files[evidence.ARTIFACT_MANIFEST_FILENAME] = (
        json.dumps(artifact_manifest, indent=2, sort_keys=True) + "\n"
    ).encode()

    with pytest.raises(ValueError, match="receipt"):
        evidence.validate_domain_bundle(
            "migration",
            files,
            repository_root=repository,
            source_revision=source_revision,
            workflow=workflow,
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("event",), "push"),
        (("head_branch",), "feature"),
        (("path",), ".github/workflows/other.yml"),
        (("status",), "in_progress"),
        (("conclusion",), "failure"),
        (("run_attempt",), 3),
        (("actor", "login"), "intruder"),
        (("triggering_actor", "login"), "intruder"),
        (("repository", "full_name"), "other/project"),
    ],
)
def test_source_run_metadata_fails_closed(
    tmp_path: Path, path: tuple[str, ...], value: object
):
    _repository_path, source_revision = _repository(tmp_path)
    payload = _run_metadata("migration", source_revision, run_id=41)
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValueError, match="source workflow metadata"):
        evidence.verify_source_run(
            payload,
            domain="migration",
            repository=REPOSITORY,
            run_id=41,
            run_attempt=2,
        )


@pytest.mark.parametrize(
    "tamper", ["name", "digest", "size", "expired", "head_sha", "archive"]
)
def test_source_artifact_identity_and_digest_fail_closed(tmp_path: Path, tamper: str):
    _repository_path, source_revision = _repository(tmp_path)
    archive = b"exact archive bytes"
    payload = _artifact_metadata(
        "migration", source_revision, archive, run_id=41
    )
    if tamper == "name":
        payload["artifacts"][0]["name"] = "other"
    elif tamper == "digest":
        payload["artifacts"][0]["digest"] = f"sha256:{'f' * 64}"
    elif tamper == "size":
        payload["artifacts"][0]["size_in_bytes"] += 1
    elif tamper == "expired":
        payload["artifacts"][0]["expired"] = True
    elif tamper == "head_sha":
        payload["artifacts"][0]["workflow_run"]["head_sha"] = "f" * 40
    else:
        archive += b"tampered"

    with pytest.raises(ValueError):
        evidence.verify_source_artifact(
            payload,
            archive,
            domain="migration",
            source_revision=source_revision,
            run_id=41,
            run_attempt=2,
        )


def test_source_artifact_accepts_api_digest_for_exact_downloaded_zip(tmp_path: Path):
    repository, source_revision = _repository(tmp_path)
    files, _workflow_value = _bundle(repository, source_revision, "migration", tmp_path)
    archive = _archive(files)
    payload = _artifact_metadata(
        "migration", source_revision, archive, run_id=41
    )

    result = evidence.verify_source_artifact(
        payload,
        archive,
        domain="migration",
        source_revision=source_revision,
        run_id=41,
        run_attempt=2,
    )

    assert result["digest"] == f"sha256:{hashlib.sha256(archive).hexdigest()}"
    assert result["archive_sha256"] == hashlib.sha256(archive).hexdigest()


def test_reuse_binds_both_source_runs_without_expensive_execution(tmp_path: Path):
    repository, source_revision = _repository(tmp_path)
    source_data = {}
    for domain, run_id in (("migration", 41), ("recovery", 42)):
        files, _workflow_value = _bundle(repository, source_revision, domain, tmp_path)
        archive = _archive(files)
        source_data[domain] = {
            "run_id": run_id,
            "run_attempt": 2,
            "run_metadata": _run_metadata(domain, source_revision, run_id=run_id),
            "artifact_metadata": _artifact_metadata(
                domain, source_revision, archive, run_id=run_id
            ),
            "archive": archive,
        }
    (repository / "README.md").write_text("target docs\n")
    (repository / "docs/operations.md").write_text("target operations\n")
    target_revision = _commit(repository, "documentation target")
    target_workflow = evidence._workflow_identity(
        repository=REPOSITORY,
        workflow_path=".github/workflows/evidence-reuse.yml",
        source_revision=target_revision,
        run_id=99,
        run_attempt=1,
        actor="owner",
        triggering_actor="owner",
        event_name="workflow_dispatch",
        ref_name="refs/heads/main",
        workflow_ref=f"{REPOSITORY}/.github/workflows/evidence-reuse.yml@refs/heads/main",
    )
    output = tmp_path / "reuse"

    receipt = evidence.create_reuse_evidence(
        repository_root=repository,
        repository=REPOSITORY,
        target_revision=target_revision,
        target_workflow=target_workflow,
        sources=source_data,
        output_dir=output,
    )

    assert set(receipt["sources"]) == {"migration", "recovery"}
    assert receipt["target_revision"] == target_revision
    for source in receipt["sources"].values():
        assert source["workflow"]["status"] == "completed"
        assert source["workflow"]["conclusion"] == "success"
        assert source["workflow"]["head_branch"] == "main"
        assert source["workflow"]["head_sha"] == source_revision
    assert receipt["execution"] == {
        "migration_rerun": False,
        "recovery_rerun": False,
        "database_started": False,
    }
    assert set(path.name for path in output.iterdir()) == {
        evidence.TARGET_MANIFEST_FILENAME,
        evidence.REUSE_RECEIPT_FILENAME,
        evidence.REUSE_CHECKSUM_FILENAME,
    }
    checksum, filename = (output / evidence.REUSE_CHECKSUM_FILENAME).read_text().split()
    assert filename == evidence.REUSE_RECEIPT_FILENAME
    assert checksum == hashlib.sha256(
        (output / evidence.REUSE_RECEIPT_FILENAME).read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("workflow_name", "job_name", "receipt_name"),
    [
        ("migration-compatibility.yml", "migration_compatibility", "migration-receipt.json"),
        ("recovery-drill.yml", "recovery_drill", "recovery-receipt.json"),
    ],
)
def test_domain_workflows_finalize_only_after_verified_cleanup_then_upload(
    workflow_name: str, job_name: str, receipt_name: str
):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text())
    authorization = workflow["jobs"]["authorize"]["steps"][0]["run"]
    for guard in (
        '"$EVENT_NAME" == "workflow_dispatch"',
        '"$REF_NAME" == "refs/heads/main"',
        '"$ACTOR" == "$REPOSITORY_OWNER"',
        '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"',
        f"{workflow_name}@$REF_NAME",
    ):
        assert guard in authorization
    job = workflow["jobs"][job_name]
    assert job["needs"] == "authorize"
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"] == {
        "ref": "${{ needs.authorize.outputs.source_sha }}",
        "persist-credentials": False,
    }
    cleanup_index = next(
        index for index, step in enumerate(steps) if "infrastructure-cleanup.json" in step.get("run", "")
    )
    finalize_index = next(
        index for index, step in enumerate(steps) if "finalize-domain" in step.get("run", "")
    )
    upload_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == "actions/upload-artifact@v4"
    )

    assert cleanup_index < finalize_index < upload_index
    assert steps[cleanup_index]["if"] == "always()"
    assert steps[upload_index]["if"] == "always()"
    assert "container_query_status" in steps[cleanup_index]["run"]
    assert "volume_query_status" in steps[cleanup_index]["run"]
    assert "remaining_containers" in steps[cleanup_index]["run"]
    assert "remaining_volumes" in steps[cleanup_index]["run"]
    assert receipt_name in evidence._bundle_names(
        "migration" if job_name == "migration_compatibility" else "recovery"
    )
