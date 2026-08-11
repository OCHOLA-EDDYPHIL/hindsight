"""Immutable lifecycle export storage and AWS cleanup contracts."""

from __future__ import annotations

import copy
import io
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import hindsight.lifecycle_aws as lifecycle_aws
from hindsight.lifecycle import (
    CognitoCredentialLocator,
    ExportPreparation,
    LifecycleConflictError,
    LifecycleOperation,
    LifecycleTable,
)


class FakeBody:
    def __init__(self, value: bytes):
        self.value = value
        self.closed = False

    def read(self) -> bytes:
        return self.value

    def iter_lines(self):
        return io.BytesIO(self.value).read().splitlines()

    def close(self) -> None:
        self.closed = True


class FakeS3:
    def __init__(self):
        self.uploads: dict[str, dict[str, Any]] = {}
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.aborted: list[str] = []
        self.counter = 0

    def create_multipart_upload(self, **kwargs):
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads[upload_id] = {"request": kwargs, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs):
        upload = self.uploads[kwargs["UploadId"]]
        upload["parts"][kwargs["PartNumber"]] = bytes(kwargs["Body"])
        return {"ETag": f'"etag-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs):
        upload = self.uploads.pop(kwargs["UploadId"])
        body = b"".join(upload["parts"][number] for number in sorted(upload["parts"]))
        return self._store(upload["request"], body)

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs["UploadId"])
        self.uploads.pop(kwargs["UploadId"], None)

    def put_object(self, **kwargs):
        return self._store(kwargs, bytes(kwargs["Body"]))

    def get_object(self, **kwargs):
        stored = self.objects[(kwargs["Key"], kwargs["VersionId"])]
        return {"Body": FakeBody(stored["body"])}

    def head_object(self, **kwargs):
        stored = self.objects[(kwargs["Key"], kwargs["VersionId"])]
        return {
            "ObjectLockMode": stored["request"]["ObjectLockMode"],
            "ObjectLockRetainUntilDate": stored["request"]["ObjectLockRetainUntilDate"],
            "ServerSideEncryption": stored["request"]["ServerSideEncryption"],
        }

    def put_object_retention(self, **kwargs):
        stored = self.objects[(kwargs["Key"], kwargs["VersionId"])]
        retention = kwargs["Retention"]
        stored["request"]["ObjectLockMode"] = retention["Mode"]
        stored["request"]["ObjectLockRetainUntilDate"] = retention[
            "RetainUntilDate"
        ]

    def _store(self, request: dict[str, Any], body: bytes):
        self.counter += 1
        version = f"version-{self.counter}"
        self.objects[(request["Key"], version)] = {"body": body, "request": request}
        return {"VersionId": version}


class FakeReadConnection:
    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        del params
        if "cluster_logical_timestamp" in str(query):
            return SimpleNamespace(fetchone=lambda: ("123456.0000000000",))
        return SimpleNamespace(fetchone=lambda: (None,))


def _table() -> LifecycleTable:
    return LifecycleTable(
        table_name="tenants",
        table_class="tenant_root",
        tenant_column="id",
        export_order=0,
        primary_key=("id",),
        columns=("id", "status"),
    )


def _operation(**overrides: Any) -> LifecycleOperation:
    values = {
        "id": "22222222-2222-2222-2222-222222222222",
        "target_tenant_id": "11111111-1111-1111-1111-111111111111",
        "tenant_identity_sha256": "a" * 64,
        "public_identity_sha256": "e" * 64,
        "status": "exporting",
        "snapshot_hlc": None,
        "schema_identity_sha256": None,
        "export_content_sha256": None,
        "export_fingerprint": None,
        "export_bucket": None,
        "export_data_key": None,
        "export_data_version_id": None,
        "export_manifest_key": None,
        "export_manifest_version_id": None,
        "export_retention_until": None,
        "export_verified_at": None,
        "confirmed_export_fingerprint": None,
        "principal_hashes": (),
        "cognito_credential_locators": (),
        "cleanup_targets_captured_at": None,
        "lease_owner": "33333333-3333-3333-3333-333333333333",
        "lease_expires_at": datetime(2026, 8, 9, 13, tzinfo=timezone.utc),
        "database_purged_at": None,
    }
    values.update(overrides)
    return LifecycleOperation(**values)


def _export(monkeypatch):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    completed_at = now + timedelta(minutes=2)
    monkeypatch.setattr(lifecycle_aws, "utc_now", lambda: completed_at)
    table = _table()
    operation = _operation()
    preparation = ExportPreparation(
        operation=operation,
        lease_owner=str(operation.lease_owner),
        schema_identity_sha256="b" * 64,
        tables=(table,),
    )
    monkeypatch.setattr(
        lifecycle_aws,
        "export_rows",
        lambda *args, **kwargs: iter(
            [(table, (operation.target_tenant_id, "archived"))]
        ),
    )
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        lifecycle_aws,
        "record_export",
        lambda *args, **kwargs: recorded.update(kwargs),
    )
    s3 = FakeS3()
    result = lifecycle_aws.export_tenant_to_s3(
        read_connection=FakeReadConnection(),
        state_connection=object(),
        preparation=preparation,
        s3_client=s3,
        bucket="lifecycle-exports",
        now=now,
    )
    assert recorded["now"] == completed_at
    exported = replace(
        operation,
        status="exported",
        snapshot_hlc=recorded["snapshot_hlc"],
        schema_identity_sha256=recorded["schema_hash"],
        export_content_sha256=recorded["content_hash"],
        export_fingerprint=recorded["fingerprint"],
        export_bucket=recorded["bucket"],
        export_data_key=recorded["data_key"],
        export_data_version_id=recorded["data_version_id"],
        export_manifest_key=recorded["manifest_key"],
        export_manifest_version_id=recorded["manifest_version_id"],
        export_retention_until=recorded["retention_until"],
    )
    return s3, result, exported, table, now


def test_export_and_verification_bind_canonical_content_schema_and_retention(monkeypatch):
    s3, result, operation, table, now = _export(monkeypatch)
    monkeypatch.setattr(lifecycle_aws, "lifecycle_tables", lambda connection: (table,))
    monkeypatch.setattr(
        lifecycle_aws,
        "schema_identity_sha256",
        lambda tables: operation.schema_identity_sha256,
    )

    verification = lifecycle_aws.verify_stored_export(
        connection=object(),
        operation=operation,
        s3_client=s3,
        now=now,
    )

    assert verification == {
        "byte_length": result.manifest["data"]["byte_length"],
        "content_sha256": operation.export_content_sha256,
        "fingerprint": result.fingerprint,
        "record_count": 1,
        "schema_identity_sha256": operation.schema_identity_sha256,
        "table_count": 1,
    }
    assert result.retention_until == now + timedelta(days=7, minutes=2)
    assert result.manifest["retention"] == {
        "minimum_days": 7,
        "mode": "GOVERNANCE",
    }
    assert all(
        stored["request"]["ServerSideEncryption"] == "AES256"
        and stored["request"]["ObjectLockMode"] == "GOVERNANCE"
        for stored in s3.objects.values()
    )


def test_verification_rejects_tampered_ndjson(monkeypatch):
    s3, _, operation, table, now = _export(monkeypatch)
    monkeypatch.setattr(lifecycle_aws, "lifecycle_tables", lambda connection: (table,))
    monkeypatch.setattr(
        lifecycle_aws,
        "schema_identity_sha256",
        lambda tables: operation.schema_identity_sha256,
    )
    stored = s3.objects[(operation.export_data_key, operation.export_data_version_id)]
    stored["body"] = stored["body"].replace(b"archived", b"tampered")

    with pytest.raises(LifecycleConflictError, match="hash|canonical"):
        lifecycle_aws.verify_stored_export(
            connection=object(), operation=operation, s3_client=s3, now=now
        )


def test_manifest_verifier_rejects_invalid_totals_digest_and_sequence(monkeypatch):
    _, result, operation, table, _ = _export(monkeypatch)

    invalid_manifests = []
    invalid_count = copy.deepcopy(result.manifest)
    invalid_count["data"]["record_count"] = True
    invalid_manifests.append(invalid_count)
    invalid_length = copy.deepcopy(result.manifest)
    invalid_length["data"]["byte_length"] = "1"
    invalid_manifests.append(invalid_length)
    invalid_digest = copy.deepcopy(result.manifest)
    invalid_digest["data"]["content_sha256"] = "A" * 64
    invalid_manifests.append(invalid_digest)
    invalid_sequence = copy.deepcopy(result.manifest)
    invalid_sequence["tables"][0]["first_sequence"] = 1
    invalid_manifests.append(invalid_sequence)

    for manifest in invalid_manifests:
        with pytest.raises(LifecycleConflictError):
            lifecycle_aws._verify_manifest_shape(  # noqa: SLF001
                manifest,
                operation=operation,
                tables=(table,),
                bucket="lifecycle-exports",
            )


def test_data_verifier_requires_exact_record_shape_and_row_primary_key(monkeypatch):
    s3, result, operation, _, _ = _export(monkeypatch)
    stored = s3.objects[(operation.export_data_key, operation.export_data_version_id)]
    original = json.loads(stored["body"])

    for changed in (
        {**original, "unexpected": True},
        {**original, "primary_key": ["wrong-tenant"]},
    ):
        stored["body"] = lifecycle_aws.canonical_json_bytes(changed) + b"\n"
        with pytest.raises(LifecycleConflictError, match="shape|primary key"):
            lifecycle_aws._verify_data_stream(  # noqa: SLF001
                s3,
                bucket="lifecycle-exports",
                key=str(operation.export_data_key),
                version_id=str(operation.export_data_version_id),
                manifest=result.manifest,
            )


def test_verification_rejects_shortened_object_retention(monkeypatch):
    s3, _, operation, table, now = _export(monkeypatch)
    monkeypatch.setattr(lifecycle_aws, "lifecycle_tables", lambda connection: (table,))
    monkeypatch.setattr(
        lifecycle_aws,
        "schema_identity_sha256",
        lambda tables: operation.schema_identity_sha256,
    )
    stored = s3.objects[(operation.export_data_key, operation.export_data_version_id)]
    stored["request"]["ObjectLockRetainUntilDate"] = now + timedelta(days=1)

    with pytest.raises(LifecycleConflictError, match="shorter than recorded"):
        lifecycle_aws.verify_stored_export(
            connection=object(), operation=operation, s3_client=s3, now=now
        )


def test_multipart_failure_aborts_incomplete_upload():
    class FailingS3(FakeS3):
        def complete_multipart_upload(self, **kwargs):
            raise RuntimeError("failed")

    s3 = FailingS3()
    writer = lifecycle_aws.MultipartObjectWriter(
        s3,
        bucket="bucket",
        key="data.ndjson",
        retention_until=datetime.now(timezone.utc) + timedelta(days=7),
        content_type="application/x-ndjson",
    )

    with pytest.raises(RuntimeError, match="failed"):
        writer.complete()

    assert s3.aborted == ["upload-1"]


class FakeTable:
    def __init__(self, name: str, key_fields: tuple[str, ...], items: list[dict[str, Any]]):
        self.name = name
        self.key_fields = key_fields
        self.items = list(items)
        self.deleted: list[dict[str, Any]] = []
        self.meta = SimpleNamespace(
            client=SimpleNamespace(
                exceptions=SimpleNamespace(
                    ConditionalCheckFailedException=type(
                        "ConditionalCheckFailedException", (Exception,), {}
                    )
                )
            )
        )

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        assert kwargs["ConsistentRead"] is True
        item = next(
            (
                item
                for item in self.items
                if all(item.get(field) == value for field, value in key.items())
            ),
            None,
        )
        return {"Item": item} if item is not None else {}

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        key = tuple(item.get(field) for field in self.key_fields)
        self.items = [
            existing
            for existing in self.items
            if tuple(existing.get(field) for field in self.key_fields) != key
        ]
        self.items.append(item)

    def query(self, **kwargs):
        tenant_id = kwargs["ExpressionAttributeValues"][":tenant"]
        return {"Items": [item for item in self.items if item.get("tenant_id") == tenant_id]}

    def scan(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        return {"Items": list(self.items)}

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        if "ExpressionAttributeValues" in kwargs:
            assert kwargs["ConditionExpression"] == "#tenant = :tenant"
            assert kwargs["ExpressionAttributeValues"][":tenant"]
        else:
            assert kwargs["ConditionExpression"] is not None
        self.deleted.append(key)
        self.items = [
            item
            for item in self.items
            if any(item[field] != key[field] for field in self.key_fields)
        ]


def test_aws_cleanup_uses_strong_scan_and_directly_confirms_cognito_deletion():
    tenant_id = "11111111-1111-1111-1111-111111111111"
    other_tenant_id = "22222222-2222-2222-2222-222222222222"
    other_ticket = {
        "ticket_digest": "other-ticket",
        "tenant_id": other_tenant_id,
        "access_class": "viewer",
        "expires_at": 1_900_000_000,
    }
    other_subscription = {
        "topic_key": f"tenant:{other_tenant_id}:namespace:preserved",
        "connection_id": "other-connection",
        "tenant_id": other_tenant_id,
        "expires_at": 1_900_000_000,
    }
    other_connection = {
        "connection_id": "other-connection",
        "tenant_id": other_tenant_id,
        "namespace": "preserved",
        "expires_at": 1_900_000_000,
    }
    other_ticket_snapshot = dict(other_ticket)
    other_subscription_snapshot = dict(other_subscription)
    other_connection_snapshot = dict(other_connection)
    tickets = FakeTable(
        "tickets",
        ("ticket_digest",),
        [
            {"ticket_digest": "ticket-1", "tenant_id": tenant_id},
            other_ticket,
        ],
    )
    subscriptions = FakeTable(
        "subscriptions",
        ("topic_key", "connection_id"),
        [
            {
                "topic_key": "tenant:one:run:run-1",
                "connection_id": "connection-1",
                "tenant_id": tenant_id,
            },
            {
                "topic_key": f"tenant:{tenant_id}:namespace:legacy",
                "connection_id": "legacy-connection",
            },
            other_subscription,
        ],
    )
    connections = FakeTable(
        "connections",
        ("connection_id",),
        [
            {"connection_id": "connection-1", "tenant_id": tenant_id},
            other_connection,
        ],
    )

    class Cognito:
        class UserNotFoundException(Exception):
            pass

        class ResourceNotFoundException(Exception):
            pass

        exceptions = SimpleNamespace(
            UserNotFoundException=UserNotFoundException,
            ResourceNotFoundException=ResourceNotFoundException,
        )

        def __init__(self):
            self.users = {
                ("pool", "matching-user"): {"Username": "matching-user"},
                ("pool", "other-user"): {"Username": "other-user"},
            }
            self.calls = []

        def admin_get_user(self, **kwargs):
            self.calls.append(("get", kwargs))
            if kwargs["UserPoolId"] == "retired-pool":
                raise self.exceptions.ResourceNotFoundException()
            user = self.users.get((kwargs["UserPoolId"], kwargs["Username"]))
            if user is None:
                raise self.exceptions.UserNotFoundException()
            return user

        def admin_delete_user(self, **kwargs):
            self.calls.append(("delete", kwargs))
            key = (kwargs["UserPoolId"], kwargs["Username"])
            if self.users.pop(key, None) is None:
                raise self.exceptions.UserNotFoundException()

    class Websocket:
        exceptions = SimpleNamespace(GoneException=type("GoneException", (Exception,), {}))

        def __init__(self):
            self.deleted = []

        def delete_connection(self, **kwargs):
            assert any(
                item.get("connection_id") == kwargs["ConnectionId"]
                for item in connections.items
            )
            self.deleted.append(kwargs["ConnectionId"])

    cognito = Cognito()
    websocket = Websocket()
    cleaner = lifecycle_aws.AwsTenantCleaner(
        ticket_table=tickets,
        subscription_table=subscriptions,
        connection_table=connections,
        cognito_client=cognito,
        websocket_client=websocket,
    )

    heartbeats = []
    result = cleaner.cleanup(
        tenant_id=tenant_id,
        cognito_credential_locators=(
            CognitoCredentialLocator("pool", "matching-user"),
            CognitoCredentialLocator("retired-pool", "historical-user"),
        ),
        heartbeat=lambda: heartbeats.append(True),
        sleeper=lambda seconds: heartbeats.append(seconds),
    )

    assert result == lifecycle_aws.CleanupResult(1, 2, 1, 1)
    assert websocket.deleted == ["connection-1"]
    assert set(cognito.users) == {("pool", "other-user")}
    assert [name for name, _kwargs in cognito.calls] == [
        "get",
        "delete",
        "get",
        "get",
        "get",
        "get",
    ]
    assert heartbeats == [
        True,
        lifecycle_aws.TENANT_STATE_QUIESCENCE_SECONDS,
        True,
        True,
        True,
        True,
        True,
    ]
    fence = connections.get_item(
        Key={
            "connection_id": lifecycle_aws.tenant_lifecycle_fence_key(tenant_id)
        },
        ConsistentRead=True,
    )["Item"]
    assert fence == {
        "connection_id": lifecycle_aws.tenant_lifecycle_fence_key(tenant_id),
        "lifecycle_fence": True,
    }
    assert "tenant_id" not in fence
    assert next(
        item for item in tickets.items if item.get("tenant_id") == other_tenant_id
    ) == other_ticket_snapshot
    assert next(
        item
        for item in subscriptions.items
        if item.get("tenant_id") == other_tenant_id
    ) == other_subscription_snapshot
    assert next(
        item for item in connections.items if item.get("tenant_id") == other_tenant_id
    ) == other_connection_snapshot
    assert {tuple(sorted(key.items())) for key in tickets.deleted} == {
        (("ticket_digest", "ticket-1"),)
    }
    assert {tuple(sorted(key.items())) for key in subscriptions.deleted} == {
        (("connection_id", "connection-1"), ("topic_key", "tenant:one:run:run-1")),
        (
            ("connection_id", "legacy-connection"),
            ("topic_key", f"tenant:{tenant_id}:namespace:legacy"),
        ),
    }
    assert {tuple(sorted(key.items())) for key in connections.deleted} == {
        (("connection_id", "connection-1"),)
    }
