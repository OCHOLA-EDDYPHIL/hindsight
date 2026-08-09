"""Safety and output contracts for the privileged lifecycle CLI."""

import json
import pathlib
from argparse import Namespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hindsight.lifecycle_aws import CleanupResult
from scripts import tenant_lifecycle


def test_cli_rejects_noncanonical_fingerprint_before_any_side_effect(capsys):
    with pytest.raises(SystemExit) as exc_info:
        tenant_lifecycle.main(
            [
                "purge",
                "--operation-id",
                "11111111-1111-1111-1111-111111111111",
                "--confirm-fingerprint",
                "A" * 64,
            ]
        )

    assert exc_info.value.code == 2
    assert "lowercase SHA-256" in capsys.readouterr().err


def test_status_output_excludes_tenant_id_and_object_locations(monkeypatch, capsys):
    operation = tenant_lifecycle.LifecycleOperation(
        id="22222222-2222-2222-2222-222222222222",
        target_tenant_id="11111111-1111-1111-1111-111111111111",
        tenant_identity_sha256="a" * 64,
        status="verified",
        snapshot_hlc="123",
        schema_identity_sha256="b" * 64,
        export_content_sha256="c" * 64,
        export_fingerprint="d" * 64,
        export_bucket="secret-bucket",
        export_data_key="secret/data.ndjson",
        export_data_version_id="secret-version",
        export_manifest_key="secret/manifest.json",
        export_manifest_version_id="secret-version-2",
        export_retention_until=None,
        export_verified_at=None,
        confirmed_export_fingerprint=None,
        principal_hashes=(),
        cognito_credential_locators=(),
        cleanup_targets_captured_at=None,
        lease_owner=None,
        lease_expires_at=None,
        database_purged_at=None,
    )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setattr(tenant_lifecycle, "load_dotenv", lambda: None)
    monkeypatch.setattr(tenant_lifecycle, "connect_lifecycle", lambda value: Connection())
    monkeypatch.setattr(tenant_lifecycle, "get_operation", lambda *args: operation)

    assert (
        tenant_lifecycle.main(
            [
                "status",
                "--operation-id",
                "22222222-2222-2222-2222-222222222222",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["tenant_identity_sha256"] == "a" * 64
    assert operation.target_tenant_id not in output
    assert "secret-bucket" not in output
    assert "secret/data.ndjson" not in output


def test_lifecycle_workflow_is_owner_main_protected_and_requires_typed_purge():
    workflow = pathlib.Path(".github/workflows/tenant-lifecycle.yml").read_text()

    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.actor == github.repository_owner" in workflow
    assert "github.triggering_actor == github.repository_owner" in workflow
    assert "environment: ${{ inputs.deployment_environment }}" in workflow
    assert "role-to-assume: ${{ vars.AWS_LIFECYCLE_ROLE_ARN }}" in workflow
    assert 'test "$REQUESTED_CONFIRMATION" = "purge-$REQUESTED_OPERATION_ID"' in workflow
    assert "--confirm-fingerprint \"$REQUESTED_FINGERPRINT\"" in workflow
    assert "aws ssm get-parameter" in workflow
    assert "HINDSIGHT_LIFECYCLE_EXPORT_BUCKET" in workflow
    assert 'test "$DEPLOYED_STAGE" = "$REQUESTED_ENVIRONMENT"' in workflow
    assert '"/hindsight/$DEPLOYED_STAGE/lifecycle-database-url"' in workflow
    assert "|| '/hindsight/demo/lifecycle-database-url'" not in workflow
    assert "GITHUB_SHA" in workflow
    assert "if: always()" in workflow
    assert "Lifecycle export operation ID" in workflow


@pytest.mark.parametrize(
    ("status", "verification_calls", "database_purge_calls"),
    [("purging", 0, 1), ("database_purged", 0, 0)],
)
def test_purge_cli_resumes_after_the_purge_transition(
    monkeypatch,
    capsys,
    status,
    verification_calls,
    database_purge_calls,
):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    fingerprint = "d" * 64
    operation = tenant_lifecycle.LifecycleOperation(
        id="22222222-2222-2222-2222-222222222222",
        target_tenant_id="11111111-1111-1111-1111-111111111111",
        tenant_identity_sha256="a" * 64,
        status=status,
        snapshot_hlc="123",
        schema_identity_sha256="b" * 64,
        export_content_sha256="c" * 64,
        export_fingerprint=fingerprint,
        export_bucket="bucket",
        export_data_key="data",
        export_data_version_id="data-version",
        export_manifest_key="manifest",
        export_manifest_version_id="manifest-version",
        export_retention_until=now + timedelta(days=1),
        export_verified_at=now,
        confirmed_export_fingerprint=fingerprint,
        principal_hashes=(),
        cognito_credential_locators=(),
        cleanup_targets_captured_at=now,
        lease_owner="33333333-3333-3333-3333-333333333333",
        lease_expires_at=now + timedelta(minutes=5),
        database_purged_at=now if status == "database_purged" else None,
    )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Session:
        def client(self, *args, **kwargs):
            return object()

    class Cleaner:
        def cleanup(self, **kwargs):
            assert kwargs["tenant_id"] == operation.target_tenant_id
            assert kwargs["cognito_credential_locators"] == ()
            return CleanupResult(0, 0, 0, 0)

        def assert_clean(self, **kwargs):
            assert kwargs["tenant_id"] == operation.target_tenant_id
            assert kwargs["cognito_credential_locators"] == ()

    calls = {"verify": 0, "database_purge": 0}
    monkeypatch.setattr(tenant_lifecycle, "_aws_session", lambda args: Session())
    monkeypatch.setattr(tenant_lifecycle, "_aws_cleaner", lambda session: Cleaner())
    monkeypatch.setattr(
        tenant_lifecycle, "connect_lifecycle", lambda database_url: Connection()
    )
    monkeypatch.setattr(
        tenant_lifecycle, "get_operation", lambda *args: operation
    )
    monkeypatch.setattr(tenant_lifecycle, "get_tombstone", lambda *args: None)

    def verify(**kwargs):
        del kwargs
        calls["verify"] += 1
        return {"fingerprint": fingerprint}

    monkeypatch.setattr(tenant_lifecycle, "verify_stored_export", verify)
    monkeypatch.setattr(tenant_lifecycle, "begin_purge", lambda *args, **kwargs: operation)
    monkeypatch.setattr(
        tenant_lifecycle,
        "record_principal_cleanup_targets",
        lambda *args, **kwargs: SimpleNamespace(
            principal_hashes=(), cognito_credential_locators=()
        ),
    )

    def purge_database(*args, **kwargs):
        del args, kwargs
        calls["database_purge"] += 1
        return replace(operation, status="database_purged", database_purged_at=now)

    monkeypatch.setattr(tenant_lifecycle, "purge_database_tenant", purge_database)
    monkeypatch.setattr(tenant_lifecycle, "heartbeat_lease", lambda *args, **kwargs: now)
    monkeypatch.setattr(
        tenant_lifecycle,
        "finalize_purge",
        lambda *args, **kwargs: {
            "purge_id": operation.id,
            "purged_at": now.isoformat(),
            "tenant_identity_sha256": operation.tenant_identity_sha256,
        },
    )

    tenant_lifecycle._run_purge(  # noqa: SLF001 - privileged orchestration contract
        Namespace(
            operation_id=operation.id,
            confirm_fingerprint=fingerprint,
            profile=None,
            region="us-east-1",
        ),
        database_url="postgresql://unused",
    )

    assert calls == {
        "verify": verification_calls,
        "database_purge": database_purge_calls,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_purge_cli_replays_a_completed_tombstone_without_aws(monkeypatch, capsys):
    operation_id = "22222222-2222-2222-2222-222222222222"
    fingerprint = "d" * 64

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        tenant_lifecycle, "connect_lifecycle", lambda database_url: Connection()
    )
    monkeypatch.setattr(tenant_lifecycle, "get_operation", lambda *args: None)
    monkeypatch.setattr(
        tenant_lifecycle,
        "get_tombstone",
        lambda *args: {
            "purge_id": operation_id,
            "export_fingerprint": fingerprint,
            "purged_at": "2026-08-09T12:00:00+00:00",
            "tenant_identity_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        tenant_lifecycle,
        "_aws_session",
        lambda args: pytest.fail("completed purge attempted AWS access"),
    )

    tenant_lifecycle._run_purge(  # noqa: SLF001 - privileged orchestration contract
        Namespace(
            operation_id=operation_id,
            confirm_fingerprint=fingerprint,
            profile=None,
            region="us-east-1",
        ),
        database_url="postgresql://unused",
    )

    assert json.loads(capsys.readouterr().out) == {
        "operation_id": operation_id,
        "purged_at": "2026-08-09T12:00:00+00:00",
        "status": "completed",
        "tenant_identity_sha256": "a" * 64,
    }
