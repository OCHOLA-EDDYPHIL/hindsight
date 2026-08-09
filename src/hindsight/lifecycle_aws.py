"""AWS export storage and external tenant-state cleanup."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

import psycopg
from boto3.dynamodb.conditions import Attr

from hindsight.lifecycle import (
    MANIFEST_FORMAT,
    TENANT_SETTING,
    CognitoCredentialLocator,
    ExportPreparation,
    LifecycleConflictError,
    LifecycleOperation,
    LifecycleTable,
    canonical_json_bytes,
    export_rows,
    lifecycle_column_schema,
    lifecycle_tables,
    manifest_fingerprint,
    record_export,
    schema_identity_sha256,
    utc_now,
)
from hindsight.tenant import tenant_lifecycle_fence_key

MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024
DEFAULT_PART_BYTES = 8 * 1024 * 1024
EXPORT_RETENTION_DAYS = 7
TENANT_INDEX_NAME = "tenant-id-index"
TENANT_STATE_WRITER_TIMEOUT_SECONDS = 30
TENANT_STATE_QUIESCENCE_SECONDS = TENANT_STATE_WRITER_TIMEOUT_SECONDS + 2


@dataclass(frozen=True)
class StoredExport:
    manifest: dict[str, Any]
    fingerprint: str
    data_version_id: str
    manifest_version_id: str
    retention_until: datetime


@dataclass(frozen=True)
class CleanupResult:
    tickets_deleted: int
    subscriptions_deleted: int
    connections_deleted: int
    cognito_users_deleted: int


class MultipartObjectWriter:
    """Write one immutable S3 object with bounded in-memory buffering."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        key: str,
        retention_until: datetime,
        content_type: str,
        part_bytes: int = DEFAULT_PART_BYTES,
        metadata: Mapping[str, str] | None = None,
    ):
        if part_bytes < MIN_MULTIPART_PART_BYTES:
            raise ValueError("multipart part size must be at least five MiB")
        self._client = client
        self._bucket = bucket
        self._key = key
        self._part_bytes = part_bytes
        self._buffer = bytearray()
        self._parts: list[dict[str, Any]] = []
        self._closed = False
        response = client.create_multipart_upload(
            Bucket=bucket,
            Key=key,
            ContentType=content_type,
            ServerSideEncryption="AES256",
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=retention_until,
            Metadata=dict(metadata or {}),
        )
        upload_id = response.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise RuntimeError("S3 did not return a multipart upload identifier")
        self._upload_id = upload_id
        self._bytes_written = 0

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def write(self, value: bytes) -> None:
        if self._closed:
            raise RuntimeError("multipart writer is closed")
        if not isinstance(value, bytes):
            raise TypeError("multipart writer accepts bytes")
        self._buffer.extend(value)
        self._bytes_written += len(value)
        while len(self._buffer) >= self._part_bytes:
            part = bytes(self._buffer[: self._part_bytes])
            del self._buffer[: self._part_bytes]
            self._upload_part(part)

    def complete(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("multipart writer is closed")
        try:
            if self._buffer or not self._parts:
                self._upload_part(bytes(self._buffer))
                self._buffer.clear()
            response = self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=self._key,
                UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts},
            )
        except Exception:
            self.abort()
            raise
        self._closed = True
        return response

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.abort_multipart_upload(
            Bucket=self._bucket,
            Key=self._key,
            UploadId=self._upload_id,
        )

    def _upload_part(self, body: bytes) -> None:
        part_number = len(self._parts) + 1
        response = self._client.upload_part(
            Bucket=self._bucket,
            Key=self._key,
            UploadId=self._upload_id,
            PartNumber=part_number,
            Body=body,
        )
        etag = response.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise RuntimeError("S3 did not return an uploaded-part ETag")
        self._parts.append({"ETag": etag, "PartNumber": part_number})


def export_tenant_to_s3(
    *,
    read_connection: psycopg.Connection,
    state_connection: psycopg.Connection,
    preparation: ExportPreparation,
    s3_client: Any,
    bucket: str,
    now: datetime | None = None,
    retention_days: int = EXPORT_RETENTION_DAYS,
    part_bytes: int = DEFAULT_PART_BYTES,
    heartbeat: Callable[[], None] | None = None,
) -> StoredExport:
    """Stream canonical NDJSON from one DB snapshot into locked S3 versions."""

    if retention_days < EXPORT_RETENTION_DAYS:
        raise ValueError("tenant exports must be retained for at least seven days")
    operation = preparation.operation
    if operation.status != "exporting" or operation.lease_owner != preparation.lease_owner:
        raise LifecycleConflictError("export preparation does not own the active lease")
    current_time = (now or utc_now()).astimezone(timezone.utc)
    provisional_retention_until = current_time + timedelta(days=retention_days)
    prefix = (
        f"tenant-exports/{operation.tenant_identity_sha256}/{operation.id}/"
        f"attempts/{preparation.lease_owner}"
    )
    data_key = f"{prefix}/data.ndjson"
    manifest_key = f"{prefix}/manifest.json"
    writer = MultipartObjectWriter(
        s3_client,
        bucket=bucket,
        key=data_key,
        retention_until=provisional_retention_until,
        content_type="application/x-ndjson",
        part_bytes=part_bytes,
        metadata={"operation-id": operation.id, "format": MANIFEST_FORMAT},
    )
    content_digest = hashlib.sha256()
    table_digests: dict[str, Any] = {}
    table_counts: dict[str, int] = {}
    table_bytes: dict[str, int] = {}
    sequence = 0
    table_sequences: dict[str, int] = {}
    last_heartbeat_at = time.monotonic()
    try:
        with read_connection.transaction():
            read_connection.execute("SET TRANSACTION READ ONLY")
            read_connection.execute(
                "SELECT set_config(%s, %s, true)",
                (TENANT_SETTING, operation.target_tenant_id),
            )
            snapshot_hlc = str(
                read_connection.execute("SELECT cluster_logical_timestamp()").fetchone()[0]
            )
            for table, row in export_rows(
                read_connection,
                tenant_id=operation.target_tenant_id,
                tables=preparation.tables,
            ):
                record = _export_record(table, row, sequence=sequence)
                line = canonical_json_bytes(record) + b"\n"
                writer.write(line)
                content_digest.update(line)
                digest = table_digests.setdefault(table.table_name, hashlib.sha256())
                digest.update(line)
                table_counts[table.table_name] = table_counts.get(table.table_name, 0) + 1
                table_bytes[table.table_name] = table_bytes.get(table.table_name, 0) + len(line)
                table_sequences.setdefault(table.table_name, sequence)
                sequence += 1
                if heartbeat is not None and time.monotonic() - last_heartbeat_at >= 30:
                    heartbeat()
                    last_heartbeat_at = time.monotonic()
        completed = writer.complete()
    except Exception:
        writer.abort()
        raise
    data_version_id = _required_version_id(completed, label="export data")
    table_manifests = [
        {
            "byte_length": table_bytes.get(table.table_name, 0),
            "columns": list(table.columns),
            "column_schema": lifecycle_column_schema(table),
            "content_sha256": table_digests.get(
                table.table_name, hashlib.sha256()
            ).hexdigest(),
            "export_order": table.export_order,
            "first_sequence": table_sequences.get(table.table_name),
            "primary_key": list(table.primary_key),
            "row_count": table_counts.get(table.table_name, 0),
            "table": table.table_name,
        }
        for table in preparation.tables
    ]
    manifest: dict[str, Any] = {
        "data": {
            "bucket": bucket,
            "byte_length": writer.bytes_written,
            "content_sha256": content_digest.hexdigest(),
            "key": data_key,
            "record_count": sequence,
            "version_id": data_version_id,
        },
        "format": MANIFEST_FORMAT,
        "operation_id": operation.id,
        "retention": {
            "minimum_days": retention_days,
            "mode": "GOVERNANCE",
        },
        "schema_identity_sha256": preparation.schema_identity_sha256,
        "snapshot_hlc": snapshot_hlc,
        "tables": table_manifests,
        "tenant_identity_sha256": operation.tenant_identity_sha256,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    fingerprint = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_response = s3_client.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=manifest_bytes,
        ContentType="application/json",
        ServerSideEncryption="AES256",
        ObjectLockMode="GOVERNANCE",
        ObjectLockRetainUntilDate=provisional_retention_until,
        IfNoneMatch="*",
        Metadata={
            "operation-id": operation.id,
            "manifest-sha256": fingerprint,
            "format": MANIFEST_FORMAT,
        },
    )
    manifest_version_id = _required_version_id(manifest_response, label="export manifest")
    # Extend both exact versions only after every upload has completed. This
    # makes the seven-day window start after object creation, not export start.
    retention_until = utc_now().astimezone(timezone.utc) + timedelta(
        days=retention_days
    )
    _set_object_retention(
        s3_client,
        bucket=bucket,
        key=data_key,
        version_id=data_version_id,
        retention_until=retention_until,
    )
    _set_object_retention(
        s3_client,
        bucket=bucket,
        key=manifest_key,
        version_id=manifest_version_id,
        retention_until=retention_until,
    )
    record_export(
        state_connection,
        operation_id=operation.id,
        lease_owner=preparation.lease_owner,
        snapshot_hlc=snapshot_hlc,
        schema_hash=preparation.schema_identity_sha256,
        content_hash=content_digest.hexdigest(),
        fingerprint=fingerprint,
        bucket=bucket,
        data_key=data_key,
        data_version_id=data_version_id,
        manifest_key=manifest_key,
        manifest_version_id=manifest_version_id,
        retention_until=retention_until,
        now=utc_now(),
    )
    return StoredExport(
        manifest=manifest,
        fingerprint=fingerprint,
        data_version_id=data_version_id,
        manifest_version_id=manifest_version_id,
        retention_until=retention_until,
    )


def verify_stored_export(
    *,
    connection: psycopg.Connection,
    operation: LifecycleOperation,
    s3_client: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify locked object versions, canonical content, ordering, and schema."""

    if operation.status not in {"exported", "verified"}:
        raise LifecycleConflictError("operation does not have an exported artifact")
    required = {
        "bucket": operation.export_bucket,
        "data_key": operation.export_data_key,
        "data_version": operation.export_data_version_id,
        "manifest_key": operation.export_manifest_key,
        "manifest_version": operation.export_manifest_version_id,
        "fingerprint": operation.export_fingerprint,
        "content_hash": operation.export_content_sha256,
        "schema_hash": operation.schema_identity_sha256,
    }
    if any(value is None for value in required.values()):
        raise LifecycleConflictError("export metadata is incomplete")
    bucket = str(required["bucket"])
    manifest_raw = _read_s3_object(
        s3_client,
        bucket=bucket,
        key=str(required["manifest_key"]),
        version_id=str(required["manifest_version"]),
    )
    try:
        manifest = json.loads(manifest_raw)
    except json.JSONDecodeError as exc:
        raise LifecycleConflictError("export manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or canonical_json_bytes(manifest) != manifest_raw:
        raise LifecycleConflictError("export manifest is not canonical")
    fingerprint = manifest_fingerprint(manifest)
    if fingerprint != required["fingerprint"]:
        raise LifecycleConflictError("export manifest fingerprint does not match")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise LifecycleConflictError("export manifest format is unsupported")
    if manifest.get("operation_id") != operation.id:
        raise LifecycleConflictError("export manifest operation identity does not match")
    if manifest.get("tenant_identity_sha256") != operation.tenant_identity_sha256:
        raise LifecycleConflictError("export manifest tenant identity does not match")
    tables = lifecycle_tables(connection)
    current_schema_hash = schema_identity_sha256(tables)
    if manifest.get("schema_identity_sha256") != current_schema_hash:
        raise LifecycleConflictError("database schema changed after export")
    if current_schema_hash != required["schema_hash"]:
        raise LifecycleConflictError("recorded schema identity does not match")
    _verify_manifest_shape(
        manifest,
        operation=operation,
        tables=tables,
        bucket=bucket,
    )

    current_time = (now or utc_now()).astimezone(timezone.utc)
    expected_retention = operation.export_retention_until
    if expected_retention is None:
        raise LifecycleConflictError("recorded export retention is missing")
    _verify_object_controls(
        s3_client,
        bucket=bucket,
        key=str(required["data_key"]),
        version_id=str(required["data_version"]),
        current_time=current_time,
        expected_retention=expected_retention,
    )
    _verify_object_controls(
        s3_client,
        bucket=bucket,
        key=str(required["manifest_key"]),
        version_id=str(required["manifest_version"]),
        current_time=current_time,
        expected_retention=expected_retention,
    )
    verification = _verify_data_stream(
        s3_client,
        bucket=bucket,
        key=str(required["data_key"]),
        version_id=str(required["data_version"]),
        manifest=manifest,
    )
    if verification["content_sha256"] != required["content_hash"]:
        raise LifecycleConflictError("export data hash does not match durable state")
    return {
        "fingerprint": fingerprint,
        "schema_identity_sha256": current_schema_hash,
        **verification,
    }


class AwsTenantCleaner:
    """Idempotently remove a tenant's ephemeral AWS state and Cognito users."""

    def __init__(
        self,
        *,
        ticket_table: Any,
        subscription_table: Any,
        connection_table: Any,
        cognito_client: Any,
        websocket_client: Any | None = None,
    ):
        self.ticket_table = ticket_table
        self.subscription_table = subscription_table
        self.connection_table = connection_table
        self.cognito_client = cognito_client
        self.websocket_client = websocket_client

    def cleanup(
        self,
        *,
        tenant_id: str,
        cognito_credential_locators: Sequence[CognitoCredentialLocator],
        heartbeat: Callable[[], None] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> CleanupResult:
        self._fence_realtime_writers(tenant_id=tenant_id)
        if heartbeat is not None:
            heartbeat()
        # WebSocket writes check the durable fence before mutating state. A
        # bounded wait drains ticket and WebSocket invocations that passed that
        # check before the fence; Terraform caps those writers at 30 seconds.
        (sleeper or time.sleep)(TENANT_STATE_QUIESCENCE_SECONDS)
        if heartbeat is not None:
            heartbeat()
        tickets = self._delete_indexed(
            self.ticket_table,
            tenant_id=tenant_id,
            key_fields=("ticket_digest",),
        )
        if heartbeat is not None:
            heartbeat()
        subscriptions = self._delete_indexed(
            self.subscription_table,
            tenant_id=tenant_id,
            key_fields=("topic_key", "connection_id"),
        )
        subscriptions += self._delete_legacy_subscriptions(tenant_id=tenant_id)
        if heartbeat is not None:
            heartbeat()
        connections = self._delete_connections(tenant_id=tenant_id)
        if heartbeat is not None:
            heartbeat()
        users = self._delete_cognito_users(
            credential_locators=cognito_credential_locators
        )
        if heartbeat is not None:
            heartbeat()
        self.assert_clean(
            tenant_id=tenant_id,
            cognito_credential_locators=cognito_credential_locators,
        )
        return CleanupResult(
            tickets_deleted=tickets,
            subscriptions_deleted=subscriptions,
            connections_deleted=connections,
            cognito_users_deleted=users,
        )

    def assert_clean(
        self,
        *,
        tenant_id: str,
        cognito_credential_locators: Sequence[CognitoCredentialLocator],
    ) -> None:
        if not _tenant_realtime_fenced(self.connection_table, tenant_id=tenant_id):
            raise LifecycleConflictError("tenant realtime lifecycle fence is missing")
        for table in (
            self.ticket_table,
            self.subscription_table,
            self.connection_table,
        ):
            if next(_scan_tenant_items(table, tenant_id=tenant_id), None) is not None:
                raise LifecycleConflictError("tenant AWS state remains after cleanup")
        if next(
            _scan_legacy_subscription_items(
                self.subscription_table, tenant_id=tenant_id
            ),
            None,
        ) is not None:
            raise LifecycleConflictError("legacy tenant subscription remains after cleanup")
        for locator in sorted(set(cognito_credential_locators)):
            self._assert_cognito_user_absent(locator)

    def _fence_realtime_writers(self, *, tenant_id: str) -> None:
        self.connection_table.put_item(
            Item={
                "connection_id": tenant_lifecycle_fence_key(tenant_id),
                "lifecycle_fence": True,
            }
        )

    def _delete_indexed(
        self,
        table: Any,
        *,
        tenant_id: str,
        key_fields: tuple[str, ...],
    ) -> int:
        deleted = 0
        items = _deduplicate_items(
            [
                *_query_tenant_items(table, tenant_id=tenant_id),
                *_scan_tenant_items(table, tenant_id=tenant_id),
            ],
            key_fields=key_fields,
        )
        for item in items:
            if _delete_tenant_item(
                table,
                key={field: item[field] for field in key_fields},
                tenant_id=tenant_id,
            ):
                deleted += 1
        return deleted

    def _delete_connections(self, *, tenant_id: str) -> int:
        deleted = 0
        items = _deduplicate_items(
            [
                *_query_tenant_items(self.connection_table, tenant_id=tenant_id),
                *_scan_tenant_items(self.connection_table, tenant_id=tenant_id),
            ],
            key_fields=("connection_id",),
        )
        for item in items:
            connection_id = str(item["connection_id"])
            if self.websocket_client is not None:
                try:
                    self.websocket_client.delete_connection(ConnectionId=connection_id)
                except self.websocket_client.exceptions.GoneException:
                    pass
            if not _delete_tenant_item(
                self.connection_table,
                key={"connection_id": connection_id},
                tenant_id=tenant_id,
            ):
                continue
            deleted += 1
        return deleted

    def _delete_legacy_subscriptions(self, *, tenant_id: str) -> int:
        """Remove pre-upgrade rows that encoded the tenant only in topic_key."""

        prefix = f"tenant:{tenant_id}:"
        deleted = 0
        for item in _scan_legacy_subscription_items(
            self.subscription_table, tenant_id=tenant_id
        ):
            topic_key = str(item.get("topic_key") or "")
            connection_id = str(item.get("connection_id") or "")
            try:
                self.subscription_table.delete_item(
                    Key={
                        "topic_key": topic_key,
                        "connection_id": connection_id,
                    },
                    ConditionExpression=(
                        Attr("tenant_id").not_exists()
                        & Attr("topic_key").begins_with(prefix)
                    ),
                )
            except self.subscription_table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
            deleted += 1
        return deleted

    def _delete_cognito_users(
        self,
        *,
        credential_locators: Sequence[CognitoCredentialLocator],
    ) -> int:
        deleted = 0
        for locator in sorted(set(credential_locators)):
            try:
                self.cognito_client.admin_get_user(
                    UserPoolId=locator.user_pool_id,
                    Username=locator.username,
                )
            except (
                self.cognito_client.exceptions.UserNotFoundException,
                self.cognito_client.exceptions.ResourceNotFoundException,
            ):
                continue
            try:
                self.cognito_client.admin_delete_user(
                    UserPoolId=locator.user_pool_id,
                    Username=locator.username,
                )
                deleted += 1
            except (
                self.cognito_client.exceptions.UserNotFoundException,
                self.cognito_client.exceptions.ResourceNotFoundException,
            ):
                pass
            self._assert_cognito_user_absent(locator)
        return deleted

    def _assert_cognito_user_absent(
        self, locator: CognitoCredentialLocator
    ) -> None:
        try:
            self.cognito_client.admin_get_user(
                UserPoolId=locator.user_pool_id,
                Username=locator.username,
            )
        except (
            self.cognito_client.exceptions.UserNotFoundException,
            self.cognito_client.exceptions.ResourceNotFoundException,
        ):
            return
        raise LifecycleConflictError("tenant Cognito identity remains after cleanup")


def _export_record(
    table: LifecycleTable,
    row: tuple[Any, ...],
    *,
    sequence: int,
) -> dict[str, Any]:
    values = dict(zip(table.columns, row, strict=True))
    return {
        "primary_key": [values[column] for column in table.primary_key],
        "row": values,
        "sequence": sequence,
        "table": table.table_name,
    }


def _required_version_id(response: Mapping[str, Any], *, label: str) -> str:
    value = response.get("VersionId")
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"versioned S3 {label} did not return a version identifier")
    return value


def _set_object_retention(
    client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
    retention_until: datetime,
) -> None:
    client.put_object_retention(
        Bucket=bucket,
        Key=key,
        VersionId=version_id,
        Retention={
            "Mode": "GOVERNANCE",
            "RetainUntilDate": retention_until,
        },
    )


def _read_s3_object(client: Any, *, bucket: str, key: str, version_id: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    body = response["Body"]
    try:
        return body.read()
    finally:
        body.close()


def _verify_object_controls(
    client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
    current_time: datetime,
    expected_retention: datetime,
) -> None:
    head = client.head_object(Bucket=bucket, Key=key, VersionId=version_id)
    retention = head.get("ObjectLockRetainUntilDate")
    if head.get("ServerSideEncryption") != "AES256":
        raise LifecycleConflictError("export object is not encrypted with SSE-S3")
    if head.get("ObjectLockMode") != "GOVERNANCE":
        raise LifecycleConflictError("export object is not governance locked")
    if not isinstance(retention, datetime) or retention.astimezone(timezone.utc) <= current_time:
        raise LifecycleConflictError("export object retention has expired")
    if retention.astimezone(timezone.utc) < expected_retention.astimezone(timezone.utc):
        raise LifecycleConflictError("export object retention is shorter than recorded")


def _verify_manifest_shape(
    manifest: Mapping[str, Any],
    *,
    operation: LifecycleOperation,
    tables: Sequence[LifecycleTable],
    bucket: str,
) -> None:
    data = manifest.get("data")
    table_entries = manifest.get("tables")
    if not isinstance(data, dict) or not isinstance(table_entries, list):
        raise LifecycleConflictError("export manifest data shape is invalid")
    expected_data = {
        "bucket": bucket,
        "key": operation.export_data_key,
        "version_id": operation.export_data_version_id,
    }
    if any(data.get(key) != value for key, value in expected_data.items()):
        raise LifecycleConflictError("export manifest data identity does not match")
    for field in ("byte_length", "record_count"):
        value = data.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LifecycleConflictError("export manifest data count is invalid")
    if not _is_sha256_digest(data.get("content_sha256")):
        raise LifecycleConflictError("export manifest data digest is invalid")
    if manifest.get("snapshot_hlc") != operation.snapshot_hlc:
        raise LifecycleConflictError("export manifest snapshot does not match")
    retention_policy = manifest.get("retention")
    if (
        not isinstance(retention_policy, dict)
        or retention_policy.get("mode") != "GOVERNANCE"
        or not isinstance(retention_policy.get("minimum_days"), int)
        or isinstance(retention_policy.get("minimum_days"), bool)
        or retention_policy["minimum_days"] < EXPORT_RETENTION_DAYS
    ):
        raise LifecycleConflictError("export manifest retention policy does not match")
    if len(table_entries) != len(tables):
        raise LifecycleConflictError("export manifest table inventory is incomplete")
    cumulative_rows = 0
    cumulative_bytes = 0
    for entry, table in zip(table_entries, tables, strict=True):
        if not isinstance(entry, dict):
            raise LifecycleConflictError("export manifest table shape is invalid")
        expected_shape = {
            "column_schema": lifecycle_column_schema(table),
            "columns": list(table.columns),
            "export_order": table.export_order,
            "primary_key": list(table.primary_key),
            "table": table.table_name,
        }
        if any(entry.get(key) != value for key, value in expected_shape.items()):
            raise LifecycleConflictError("export manifest table identity does not match")
        for field in ("byte_length", "row_count"):
            value = entry.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LifecycleConflictError("export manifest table count is invalid")
        row_count = entry["row_count"]
        expected_first_sequence = cumulative_rows if row_count > 0 else None
        first_sequence = entry.get("first_sequence")
        if (
            first_sequence != expected_first_sequence
            or isinstance(first_sequence, bool)
        ):
            raise LifecycleConflictError(
                "export manifest table sequence boundary is invalid"
            )
        if not _is_sha256_digest(entry.get("content_sha256")):
            raise LifecycleConflictError("export manifest table digest is invalid")
        cumulative_rows += row_count
        cumulative_bytes += entry["byte_length"]
    if cumulative_rows != data["record_count"]:
        raise LifecycleConflictError("export manifest record totals do not match")
    if cumulative_bytes != data["byte_length"]:
        raise LifecycleConflictError("export manifest byte totals do not match")


def _verify_data_stream(
    client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    expected_tables = manifest.get("tables")
    data = manifest.get("data")
    if not isinstance(expected_tables, list) or not isinstance(data, dict):
        raise LifecycleConflictError("export manifest data shape is invalid")
    expected_by_name = {
        str(item.get("table")): item for item in expected_tables if isinstance(item, dict)
    }
    expected_order = [str(item.get("table")) for item in expected_tables]
    counts = {name: 0 for name in expected_order}
    lengths = {name: 0 for name in expected_order}
    digests = {name: hashlib.sha256() for name in expected_order}
    overall = hashlib.sha256()
    seen_order: list[str] = []
    sequence = 0
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    body = response["Body"]
    try:
        for raw_line in body.iter_lines():
            if not raw_line:
                raise LifecycleConflictError("export data contains an empty record")
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise LifecycleConflictError("export data contains invalid JSON") from exc
            if not isinstance(record, dict) or canonical_json_bytes(record) != raw_line:
                raise LifecycleConflictError("export data record is not canonical")
            if set(record) != {"primary_key", "row", "sequence", "table"}:
                raise LifecycleConflictError("export data record shape is invalid")
            table = record.get("table")
            if not isinstance(table, str) or table not in expected_by_name:
                raise LifecycleConflictError("export data references an unknown table")
            if record.get("sequence") != sequence:
                raise LifecycleConflictError("export data sequence is not contiguous")
            expected_table = expected_by_name[table]
            row = record.get("row")
            primary_key = record.get("primary_key")
            columns = expected_table.get("columns")
            primary_key_columns = expected_table.get("primary_key")
            if (
                not isinstance(row, dict)
                or not isinstance(columns, list)
                or set(row) != set(columns)
                or not isinstance(primary_key, list)
                or not isinstance(primary_key_columns, list)
                or any(column not in row for column in primary_key_columns)
                or primary_key != [row[column] for column in primary_key_columns]
            ):
                raise LifecycleConflictError(
                    "export data primary key does not match its row"
                )
            if not seen_order or seen_order[-1] != table:
                if table in seen_order:
                    raise LifecycleConflictError("export table records are not contiguous")
                seen_order.append(table)
            line = raw_line + b"\n"
            overall.update(line)
            digests[table].update(line)
            counts[table] += 1
            lengths[table] += len(line)
            sequence += 1
    finally:
        body.close()
    if seen_order != [name for name in expected_order if counts[name] > 0]:
        raise LifecycleConflictError("export tables are not in manifest order")
    for name in expected_order:
        expected = expected_by_name[name]
        if counts[name] != expected.get("row_count"):
            raise LifecycleConflictError(f"export row count differs for {name}")
        if lengths[name] != expected.get("byte_length"):
            raise LifecycleConflictError(f"export byte length differs for {name}")
        if digests[name].hexdigest() != expected.get("content_sha256"):
            raise LifecycleConflictError(f"export content hash differs for {name}")
    if overall.hexdigest() != data.get("content_sha256"):
        raise LifecycleConflictError("export data content hash differs from manifest")
    if sequence != data.get("record_count"):
        raise LifecycleConflictError("export record total differs from manifest")
    if sum(lengths.values()) != data.get("byte_length"):
        raise LifecycleConflictError("export byte total differs from manifest")
    return {
        "byte_length": sum(lengths.values()),
        "content_sha256": overall.hexdigest(),
        "record_count": sequence,
        "table_count": len(expected_order),
    }


def _is_sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _query_tenant_items(table: Any, *, tenant_id: str) -> Iterator[dict[str, Any]]:
    request: dict[str, Any] = {
        "IndexName": TENANT_INDEX_NAME,
        "KeyConditionExpression": "#tenant = :tenant",
        "ExpressionAttributeNames": {"#tenant": "tenant_id"},
        "ExpressionAttributeValues": {":tenant": tenant_id},
    }
    while True:
        page = table.query(**request)
        for item in page.get("Items", []):
            yield item
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        request["ExclusiveStartKey"] = last_key


def _scan_tenant_items(table: Any, *, tenant_id: str) -> Iterator[dict[str, Any]]:
    for item in _scan_all_items(table):
        if str(item.get("tenant_id") or "") == tenant_id:
            yield item


def _scan_all_items(table: Any) -> Iterator[dict[str, Any]]:
    request: dict[str, Any] = {"ConsistentRead": True}
    while True:
        page = table.scan(**request)
        for item in page.get("Items", []):
            yield item
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        request["ExclusiveStartKey"] = last_key


def _scan_legacy_subscription_items(
    table: Any, *, tenant_id: str
) -> Iterator[dict[str, Any]]:
    prefix = f"tenant:{tenant_id}:"
    for item in _scan_all_items(table):
        if (
            not item.get("tenant_id")
            and str(item.get("topic_key") or "").startswith(prefix)
            and item.get("connection_id")
        ):
            yield item


def _deduplicate_items(
    items: Sequence[Mapping[str, Any]], *, key_fields: tuple[str, ...]
) -> list[Mapping[str, Any]]:
    deduplicated: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for item in items:
        key = tuple(str(item[field]) for field in key_fields)
        deduplicated[key] = item
    return [deduplicated[key] for key in sorted(deduplicated)]


def _delete_tenant_item(table: Any, *, key: Mapping[str, Any], tenant_id: str) -> bool:
    try:
        table.delete_item(
            Key=dict(key),
            ConditionExpression="#tenant = :tenant",
            ExpressionAttributeNames={"#tenant": "tenant_id"},
            ExpressionAttributeValues={":tenant": tenant_id},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _tenant_realtime_fenced(table: Any, *, tenant_id: str) -> bool:
    response = table.get_item(
        Key={"connection_id": tenant_lifecycle_fence_key(tenant_id)},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return isinstance(item, Mapping) and item.get("lifecycle_fence") is True
