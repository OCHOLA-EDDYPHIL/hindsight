import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "scripts/run_recovery_drill.py"
    spec = importlib.util.spec_from_file_location("run_recovery_drill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_recovery_targets_are_derived_and_name_fenced():
    drill = _module()

    targets = drill._targets("abcdef1234567890")

    assert targets.source_database == "hindsight_recovery_source_abcdef1234567890"
    assert targets.restore_database == "hindsight_recovery_restore_abcdef1234567890"
    assert targets.backup_uri == (
        "userfile://defaultdb.public.hindsight_recovery_userfile_abcdef1234567890/backup"
    )
    assert drill._is_disposable_database(targets.source_database)
    assert drill._is_disposable_database(targets.restore_database)
    assert not drill._is_disposable_database("hindsight")
    assert not drill._is_disposable_database("defaultdb")


@pytest.mark.parametrize(
    "run_id",
    (
        "short",
        "UPPERCASE123",
        "contains-dash",
        "a" * 21,
        "../../defaultdb",
    ),
)
def test_recovery_targets_reject_noncanonical_run_ids(run_id: str):
    drill = _module()

    with pytest.raises(ValueError, match="run id"):
        drill._targets(run_id)


def test_source_loss_requires_exact_database_confirmation():
    drill = _module()
    targets = drill._targets("abcdef1234567890")

    drill._confirm_source_loss(targets, f"drop:{targets.source_database}")

    with pytest.raises(RuntimeError, match="exactly equal"):
        drill._confirm_source_loss(targets, "drop:hindsight")


@pytest.mark.parametrize(
    "url",
    (
        "postgresql://root@localhost:26257/hindsight?sslmode=disable",
        "postgresql://operator@localhost:26257/defaultdb?sslmode=disable",
        "postgresql://root@production.example.com:26257/defaultdb?sslmode=verify-full",
        "https://root@localhost/defaultdb",
    ),
)
def test_admin_url_guard_rejects_nonlocal_drill_authority(url: str):
    drill = _module()

    with pytest.raises(ValueError):
        drill._validate_admin_url(url)


def test_native_backup_and_restore_statements_use_quoted_derived_targets():
    drill = _module()
    targets = drill._targets("abcdef1234567890")

    assert drill._backup_statement(targets).as_string() == (
        'BACKUP DATABASE "hindsight_recovery_source_abcdef1234567890" INTO '
        "'userfile://defaultdb.public."
        "hindsight_recovery_userfile_abcdef1234567890/backup'"
    )
    assert drill._restore_statement(targets).as_string() == (
        'RESTORE DATABASE "hindsight_recovery_source_abcdef1234567890" '
        "FROM LATEST IN 'userfile://defaultdb.public."
        "hindsight_recovery_userfile_abcdef1234567890/backup' WITH new_db_name = "
        "'hindsight_recovery_restore_abcdef1234567890'"
    )
    assert drill._show_backup_statement(targets).as_string() == (
        "SHOW BACKUP FROM LATEST IN 'userfile://defaultdb.public."
        "hindsight_recovery_userfile_abcdef1234567890/backup'"
    )


def test_canonical_digest_is_stable_and_type_sensitive():
    drill = _module()
    first = {
        "uuid": UUID("9c8657d8-20dc-40da-b7d2-c4b6ea2e709a"),
        "timestamp": datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc),
        "payload": {"b": 2, "a": b"bytes"},
    }
    reordered = {
        "payload": {"a": b"bytes", "b": 2},
        "timestamp": datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc),
        "uuid": UUID("9c8657d8-20dc-40da-b7d2-c4b6ea2e709a"),
    }

    assert drill._sha256(first) == drill._sha256(reordered)
    assert drill._sha256({"value": b"1"}) != drill._sha256({"value": "1"})


def test_manifest_summary_and_difference_are_section_scoped_and_bounded():
    drill = _module()
    source = {
        "tables": ["a", "b", "c"],
        "roles": [["hindsight_api", False]],
    }
    restored = {
        "tables": ["a", "changed", "c"],
        "roles": [["hindsight_api", False]],
    }

    summary = drill._manifest_summary(source)
    differences = drill._manifest_difference_sample(source, restored, limit=1)

    assert summary["section_counts"] == {"roles": 1, "tables": 3}
    assert set(summary["section_sha256"]) == {"roles", "tables"}
    assert differences == {
        "tables": {
            "source_only_count": 1,
            "restored_only_count": 1,
            "source_only_sample": ['"b"'],
            "restored_only_sample": ['"changed"'],
        }
    }


def test_schema_snapshot_reapplies_database_roles_before_comparison(monkeypatch):
    drill = _module()
    calls = []

    def run_repository_script(script, args, *, database_url, deadline):
        calls.append((script, args, database_url, deadline))
        output = Path(args[args.index("--output") + 1])
        output.write_text('{"tables": []}')

    monkeypatch.setattr(drill, "_run_repository_script", run_repository_script)
    deadline = drill.Deadline.after(60)

    assert drill._schema_manifest("postgresql://fixture", deadline) == {"tables": []}
    assert len(calls) == 2
    assert all(call[0] == "schema_manifest.py" for call in calls)
    assert all(call[1] == ["export", "--output", calls[0][1][2], "--apply-roles"] for call in calls)
    assert all(call[2:] == ("postgresql://fixture", deadline) for call in calls)


def test_schema_snapshot_fails_when_bounded_reads_never_converge(monkeypatch):
    drill = _module()
    calls = 0

    def run_repository_script(_script, args, **_kwargs):
        nonlocal calls
        calls += 1
        output = Path(args[args.index("--output") + 1])
        output.write_text(json.dumps({"tables": [f"version-{calls}"]}))

    monkeypatch.setattr(drill, "_run_repository_script", run_repository_script)

    with pytest.raises(RuntimeError, match="did not converge"):
        drill._schema_manifest("postgresql://fixture", drill.Deadline.after(60))

    assert calls == drill.SCHEMA_MANIFEST_MAX_READS


def test_recovery_initializes_agent_storage_as_part_of_the_backup_fixture(monkeypatch):
    drill = _module()
    calls = []
    monkeypatch.setattr(
        drill,
        "_run_repository_script",
        lambda script, args, **kwargs: calls.append((script, args, kwargs)),
    )
    deadline = drill.Deadline.after(60)

    drill._initialize_agent_storage("postgresql://fixture", deadline)

    assert calls == [
        (
            "initialize_agent_storage.py",
            [],
            {"database_url": "postgresql://fixture", "deadline": deadline},
        )
    ]


def test_clean_start_collision_never_deletes_unowned_resources(monkeypatch, tmp_path: Path):
    drill = _module()
    output = tmp_path / "evidence.json"
    run_id = "abcdef1234567890"
    targets = drill._targets(run_id)
    monkeypatch.setattr(
        drill,
        "_guard_clean_start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )
    cleanup_called = False

    def cleanup(*_args, **_kwargs):
        nonlocal cleanup_called
        cleanup_called = True
        raise AssertionError("unowned resources must not be cleaned up")

    monkeypatch.setattr(drill, "_cleanup_resources", cleanup)

    evidence = drill.run_drill(
        admin_url="postgresql://root@localhost:26257/defaultdb?sslmode=disable",
        run_id=run_id,
        source_sha="a" * 40,
        confirmation=f"drop:{targets.source_database}",
        output=output,
        timeout_seconds=60,
    )

    assert evidence["status"] == "failed"
    assert evidence["error"] == {"type": "RuntimeError", "message": "fixture failure"}
    assert evidence["cleanup"] == {"skipped_unowned_resources": True}
    assert cleanup_called is False
    assert evidence["timeline"]["started_at"].endswith("Z")
    assert evidence["timeline"]["completed_at"].endswith("Z")
    assert any("one local" in limitation.lower() for limitation in evidence["limitations"])
    assert json.loads(output.read_text()) == evidence


def test_recovery_workflow_is_owner_main_only_and_uses_ephemeral_local_storage():
    workflow_path = ROOT / ".github/workflows/recovery-drill.yml"
    workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "hindsight-recovery-drill",
        "cancel-in-progress": "false",
    }

    authorize = workflow["jobs"]["authorize"]
    authorization = authorize["steps"][0]["run"]
    for guard in (
        '"$EVENT_NAME" == "workflow_dispatch"',
        '"$REF_NAME" == "refs/heads/main"',
        '"$ACTOR" == "$REPOSITORY_OWNER"',
        '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"',
        "recovery-drill.yml@$REF_NAME",
    ):
        assert guard in authorization

    job = workflow["jobs"]["recovery_drill"]
    assert job["needs"] == "authorize"
    assert job["runs-on"] == "ubuntu-latest"
    assert "github.run_id" in job["env"]["COMPOSE_PROJECT_NAME"]
    assert "github.run_attempt" in job["env"]["COMPOSE_PROJECT_NAME"]
    assert job["env"]["COCKROACH_IMAGE"] == "cockroachdb/cockroach:v25.4.5"
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "${COCKROACH_IMAGE:-cockroachdb/cockroach:v25.4.5}" in compose
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "docker compose up -d crdb" in commands
    assert "scripts/run_recovery_drill.py" in commands
    assert "--confirm-source-loss" in commands
    assert "--source-sha" in commands
    assert "docker compose down --volumes --remove-orphans" in commands
    upload = next(step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ env.EVIDENCE_PATH }}"
    assert upload["with"]["if-no-files-found"] == "error"
    assert job["steps"][-1]["if"] == "always()"
