"""Fenced tenant export and purge primitives.

The public application never imports this module.  It is used by the privileged
lifecycle CLI with a database identity that can inspect the lifecycle catalog and
execute the guarded tenant-root delete.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping, Sequence
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from hindsight.db import database_url_with_tls_roots
from hindsight.server_tenants import (
    ACCEPTANCE_TENANT_ID,
    LEARNING_TENANT_ID,
    PUBLIC_DEMO_TENANT_ID,
)
from hindsight.tenant import normalize_tenant_id

LIFECYCLE_OPERATION_SETTING = "hindsight.lifecycle_operation_id"
LIFECYCLE_OWNER_SETTING = "hindsight.lifecycle_lease_owner"
TENANT_SETTING = "hindsight.tenant_id"
TENANT_HASH_DOMAIN = b"hindsight.tenant-purge.v1\0"
MANIFEST_FORMAT = "hindsight.tenant-export.v1"
DEFAULT_LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 1800
PROTECTED_TENANT_IDS = frozenset(
    {PUBLIC_DEMO_TENANT_ID, ACCEPTANCE_TENANT_ID, LEARNING_TENANT_ID}
)


class LifecycleError(RuntimeError):
    """Base class for safe, actionable lifecycle failures."""


class LifecycleConflictError(LifecycleError):
    """The requested transition conflicts with durable lifecycle state."""


class LifecycleLeaseError(LifecycleError):
    """The lifecycle operation is owned by another unexpired worker."""


class LifecycleCompletenessError(LifecycleError):
    """The database schema is not completely covered by lifecycle policy."""

    def __init__(self, issues: Sequence[tuple[str, str]]):
        self.issues = tuple(issues)
        summary = ", ".join(f"{table}:{code}" for table, code in self.issues[:10])
        if len(self.issues) > 10:
            summary += f", and {len(self.issues) - 10} more"
        super().__init__(f"tenant lifecycle schema coverage is incomplete: {summary}")


@dataclass(frozen=True)
class LifecycleOperation:
    id: str
    target_tenant_id: str
    tenant_identity_sha256: str
    public_identity_sha256: str | None
    status: str
    snapshot_hlc: str | None
    schema_identity_sha256: str | None
    export_content_sha256: str | None
    export_fingerprint: str | None
    export_bucket: str | None
    export_data_key: str | None
    export_data_version_id: str | None
    export_manifest_key: str | None
    export_manifest_version_id: str | None
    export_retention_until: datetime | None
    export_verified_at: datetime | None
    confirmed_export_fingerprint: str | None
    principal_hashes: tuple[str, ...]
    cognito_credential_locators: tuple[CognitoCredentialLocator, ...]
    cleanup_targets_captured_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    database_purged_at: datetime | None


@dataclass(frozen=True, order=True)
class CognitoCredentialLocator:
    user_pool_id: str
    username: str


@dataclass(frozen=True)
class PrincipalCleanupTargets:
    principal_hashes: tuple[str, ...]
    cognito_credential_locators: tuple[CognitoCredentialLocator, ...]


@dataclass(frozen=True)
class PublicIdentitySentinel:
    tenant_rows: int
    principal_mapping_rows: int
    credential_locator_rows: int
    sha256: str


@dataclass(frozen=True)
class LifecycleColumn:
    name: str
    data_type: str
    udt_name: str
    crdb_sql_type: str
    nullable: bool
    character_maximum_length: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    datetime_precision: int | None


@dataclass(frozen=True)
class LifecycleTable:
    table_name: str
    table_class: str
    tenant_column: str | None
    export_order: int
    primary_key: tuple[str, ...]
    columns: tuple[str, ...]
    column_definitions: tuple[LifecycleColumn, ...] = ()


@dataclass(frozen=True)
class ExportPreparation:
    operation: LifecycleOperation
    lease_owner: str
    schema_identity_sha256: str
    tables: tuple[LifecycleTable, ...]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def tenant_identity_sha256(tenant_id: str | UUID) -> str:
    normalized = normalize_tenant_id(tenant_id)
    return hashlib.sha256(TENANT_HASH_DOMAIN + normalized.encode()).hexdigest()


def assert_purgeable_tenant(tenant_id: str | UUID) -> str:
    normalized = normalize_tenant_id(tenant_id)
    if normalized in PROTECTED_TENANT_IDS:
        raise LifecycleConflictError("server-owned tenants cannot be purged")
    return normalized


def canonical_json_value(value: Any) -> Any:
    """Convert database values to a lossless, stable JSON representation."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return {"$timestamp": normalized.astimezone(timezone.utc).isoformat(timespec="microseconds")}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimals cannot be exported")
        return {"$decimal": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be exported")
        return {"$float": value.hex()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {
            str(key): canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonical_json_value(item) for item in value]
    if isinstance(value, set):
        converted = [canonical_json_value(item) for item in value]
        return sorted(converted, key=canonical_json_bytes)
    if hasattr(value, "tolist"):
        return canonical_json_value(value.tolist())
    raise TypeError(f"unsupported export value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()


def public_demo_identity_sentinel(
    connection: psycopg.Connection,
) -> PublicIdentitySentinel:
    """Digest the bounded public-demo identity surface without exposing it."""

    with connection.transaction():
        _set_tenant_context(connection, PUBLIC_DEMO_TENANT_ID)
        tenants = connection.execute(
            """
                SELECT id, slug, tenant_kind, status
                FROM tenants
                WHERE id = %s
                ORDER BY id
            """,
            (PUBLIC_DEMO_TENANT_ID,),
        ).fetchall()
        principals = connection.execute(
            """
                SELECT id, principal_hash, provisioning_key, tenant_id, role, status
                FROM product_principal_roles
                WHERE tenant_id = %s
                ORDER BY provisioning_key, principal_hash, id
            """,
            (PUBLIC_DEMO_TENANT_ID,),
        ).fetchall()
        locators = connection.execute(
            """
                SELECT id, provisioning_key, tenant_id, user_pool_id,
                       cognito_username, role, principal_hash, status
                FROM product_credential_locators
                WHERE tenant_id = %s
                ORDER BY provisioning_key, user_pool_id, cognito_username, id
            """,
            (PUBLIC_DEMO_TENANT_ID,),
        ).fetchall()

    if len(tenants) != 1 or tuple(str(value) for value in tenants[0]) != (
        PUBLIC_DEMO_TENANT_ID,
        "public-demo",
        "public_demo",
        "active",
    ):
        raise LifecycleConflictError("public-demo tenant identity is unavailable")
    if len(principals) != 2 or {str(row[4]) for row in principals} != {
        "viewer",
        "operator",
    }:
        raise LifecycleConflictError("public-demo principal mappings are incomplete")
    if any(str(row[5]) != "active" for row in principals):
        raise LifecycleConflictError("public-demo principal mapping is not active")

    active_locators = {
        (str(row[1]), str(row[6]), str(row[5]))
        for row in locators
        if str(row[7]) == "active" and row[6] is not None
    }
    expected_locators = {
        (str(row[2]), str(row[1]), str(row[4])) for row in principals
    }
    if not expected_locators.issubset(active_locators):
        raise LifecycleConflictError(
            "public-demo principal mapping lacks an active credential locator"
        )

    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "credential_locators": locators,
                "principal_mappings": principals,
                "tenants": tenants,
            }
        )
    ).hexdigest()
    return PublicIdentitySentinel(
        tenant_rows=len(tenants),
        principal_mapping_rows=len(principals),
        credential_locator_rows=len(locators),
        sha256=digest,
    )


def connect_lifecycle(database_url: str) -> psycopg.Connection:
    return psycopg.connect(
        database_url_with_tls_roots(database_url),
        autocommit=True,
        connect_timeout=5,
        application_name="hindsight-tenant-lifecycle",
    )


def assert_lifecycle_completeness(connection: psycopg.Connection) -> None:
    rows = connection.execute(
        """
            SELECT table_name, issue_code
            FROM tenant_lifecycle_completeness_issues
            ORDER BY table_name, issue_code
        """
    ).fetchall()
    issues = [(str(row[0]), str(row[1])) for row in rows]
    catalog = connection.execute(
        """
            SELECT table_name, table_class
            FROM tenant_lifecycle_tables
            WHERE table_class IN ('tenant_owned', 'tenant_root')
            ORDER BY table_name
        """
    ).fetchall()
    for table_name, table_class in catalog:
        result = connection.execute(
            sql.SQL("SHOW TRIGGERS FROM {}").format(sql.Identifier(str(table_name)))
        )
        triggers = {
            str(row[0]): bool(row[1])
            for row in result.fetchall()
            if len(row) >= 2
        }
        definitions = {
            trigger_name: " ".join(
                str(
                    connection.execute(
                        sql.SQL("SHOW CREATE TRIGGER {} ON {}").format(
                            sql.Identifier(trigger_name),
                            sql.Identifier(str(table_name)),
                        )
                    ).fetchone()[1]
                )
                .upper()
                .split()
            )
            for trigger_name, enabled in triggers.items()
            if enabled
        }
        if str(table_class) == "tenant_owned":
            guarded = any(
                name.endswith("_tenant_lifecycle_state")
                and " BEFORE " in f" {definition} "
                and " INSERT " in f" {definition} "
                and " UPDATE " in f" {definition} "
                and " DELETE " in f" {definition} "
                and "GUARD_TENANT_LIFECYCLE_ROW_STATE()" in definition
                for name, definition in definitions.items()
            )
            if not guarded:
                issues.append((str(table_name), "lifecycle_state_trigger_missing"))
        else:
            required_triggers = {
                "tenants_lifecycle_delete_guard",
                "tenants_lifecycle_status_guard",
                "tenants_purge_identity_guard",
            }
            for trigger in sorted(required_triggers):
                expected_function = (
                    "GUARD_TENANT_CATALOG_DELETE()"
                    if trigger == "tenants_lifecycle_delete_guard"
                    else (
                        "GUARD_TENANT_PURGE_IDENTITY()"
                        if trigger == "tenants_purge_identity_guard"
                        else "GUARD_TENANT_LIFECYCLE_STATUS_TRANSITION()"
                    )
                )
                expected_event = (
                    "BEFORE DELETE"
                    if trigger == "tenants_lifecycle_delete_guard"
                    else (
                        "BEFORE INSERT"
                        if trigger == "tenants_purge_identity_guard"
                        else "BEFORE UPDATE"
                    )
                )
                definition = definitions.get(trigger, "")
                if expected_function not in definition or expected_event not in definition:
                    issues.append((str(table_name), f"{trigger}_missing"))
    if issues:
        raise LifecycleCompletenessError(issues)


def lifecycle_tables(connection: psycopg.Connection) -> tuple[LifecycleTable, ...]:
    assert_lifecycle_completeness(connection)
    catalog = connection.execute(
        """
            SELECT table_name, table_class, tenant_column, export_order
            FROM tenant_lifecycle_tables
            WHERE table_class IN ('tenant_owned', 'tenant_root')
            ORDER BY export_order, table_name
        """
    ).fetchall()
    tables: list[LifecycleTable] = []
    for table_name, table_class, tenant_column, export_order in catalog:
        column_rows = connection.execute(
            """
                SELECT column_name, data_type, udt_name, crdb_sql_type, is_nullable,
                       character_maximum_length, numeric_precision,
                       numeric_scale, datetime_precision
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """,
            (table_name,),
        ).fetchall()
        columns = tuple(str(row[0]) for row in column_rows)
        column_definitions = tuple(
            LifecycleColumn(
                name=str(row[0]),
                data_type=str(row[1]),
                udt_name=str(row[2]),
                crdb_sql_type=str(row[3]),
                nullable=str(row[4]).upper() == "YES",
                character_maximum_length=(int(row[5]) if row[5] is not None else None),
                numeric_precision=(int(row[6]) if row[6] is not None else None),
                numeric_scale=(int(row[7]) if row[7] is not None else None),
                datetime_precision=(int(row[8]) if row[8] is not None else None),
            )
            for row in column_rows
        )
        primary_key = tuple(
            str(row[0])
            for row in connection.execute(
                """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_catalog = kcu.constraint_catalog
                     AND tc.constraint_schema = kcu.constraint_schema
                     AND tc.constraint_name = kcu.constraint_name
                    WHERE tc.table_schema = 'public'
                      AND tc.table_name = %s
                      AND tc.constraint_type = 'PRIMARY KEY'
                    ORDER BY kcu.ordinal_position
                """,
                (table_name,),
            ).fetchall()
        )
        if not columns or not primary_key:
            raise LifecycleCompletenessError([(str(table_name), "missing_export_shape")])
        tables.append(
            LifecycleTable(
                table_name=str(table_name),
                table_class=str(table_class),
                tenant_column=str(tenant_column) if tenant_column is not None else None,
                export_order=int(export_order),
                primary_key=primary_key,
                columns=columns,
                column_definitions=column_definitions,
            )
        )
    return tuple(tables)


def schema_identity_sha256(tables: Iterable[LifecycleTable]) -> str:
    shape = [
        {
            "columns": list(table.columns),
            "column_schema": lifecycle_column_schema(table),
            "export_order": table.export_order,
            "primary_key": list(table.primary_key),
            "table": table.table_name,
            "table_class": table.table_class,
            "tenant_column": table.tenant_column,
        }
        for table in tables
    ]
    return hashlib.sha256(canonical_json_bytes(shape)).hexdigest()


def lifecycle_column_schema(table: LifecycleTable) -> list[dict[str, Any]]:
    return [
        {
            "character_maximum_length": column.character_maximum_length,
            "crdb_sql_type": column.crdb_sql_type,
            "data_type": column.data_type,
            "datetime_precision": column.datetime_precision,
            "name": column.name,
            "nullable": column.nullable,
            "numeric_precision": column.numeric_precision,
            "numeric_scale": column.numeric_scale,
            "udt_name": column.udt_name,
        }
        for column in table.column_definitions
    ]


def begin_export(
    connection: psycopg.Connection,
    *,
    tenant_id: str,
    public_identity_sha256: str,
    operation_id: str | None = None,
    lease_owner: str | None = None,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ExportPreparation:
    """Archive one tenant and acquire the durable export lease."""

    normalized_tenant = assert_purgeable_tenant(tenant_id)
    owner = str(UUID(lease_owner)) if lease_owner else str(uuid4())
    operation = str(UUID(operation_id)) if operation_id else str(uuid4())
    current_time = (now or utc_now()).astimezone(timezone.utc)
    lease_expiry = current_time + timedelta(seconds=_lease_seconds(lease_seconds))
    tables = lifecycle_tables(connection)
    schema_hash = schema_identity_sha256(tables)
    tenant_hash = tenant_identity_sha256(normalized_tenant)
    public_identity_hash = _sha256_digest(
        public_identity_sha256,
        label="public identity baseline",
    )

    with connection.transaction():
        _set_tenant_context(connection, normalized_tenant)
        if connection.execute(
            "SELECT 1 FROM tenant_purge_tombstones WHERE purge_id = %s",
            (operation,),
        ).fetchone() is not None:
            raise LifecycleConflictError("operation identifier is already a purge tombstone")
        tenant = connection.execute(
            "SELECT status FROM tenants WHERE id = %s FOR UPDATE",
            (normalized_tenant,),
        ).fetchone()
        if tenant is None:
            raise LifecycleConflictError("tenant does not exist")
        if str(tenant[0]) not in {"active", "archived"}:
            raise LifecycleConflictError("tenant is already in a purge transition")
        existing = connection.execute(
            """
                SELECT id FROM tenant_lifecycle_operations
                WHERE target_tenant_id = %s
                  AND status NOT IN ('completed', 'aborted')
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
            """,
            (normalized_tenant,),
        ).fetchone()
        if existing is not None and str(existing[0]) != operation:
            raise LifecycleConflictError("tenant already has an unfinished lifecycle operation")
        connection.execute(
            """
                INSERT INTO tenant_lifecycle_operations (
                    id, target_tenant_id, tenant_identity_sha256,
                    public_identity_sha256, status,
                    schema_identity_sha256, lease_owner, lease_expires_at,
                    attempt_count, updated_at
                ) VALUES (%s, %s, %s, %s, 'exporting', %s, %s, %s, 1, %s)
                ON CONFLICT (id) DO UPDATE
                SET status = 'exporting',
                    schema_identity_sha256 = excluded.schema_identity_sha256,
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    attempt_count = tenant_lifecycle_operations.attempt_count + 1,
                    failure_code = NULL,
                    failure_detail = NULL,
                    updated_at = excluded.updated_at
                WHERE tenant_lifecycle_operations.target_tenant_id = excluded.target_tenant_id
                  AND tenant_lifecycle_operations.tenant_identity_sha256 = excluded.tenant_identity_sha256
                  AND tenant_lifecycle_operations.public_identity_sha256 = excluded.public_identity_sha256
                  AND tenant_lifecycle_operations.status IN (
                      'pending_export', 'exporting', 'failed'
                  )
                  AND (
                      tenant_lifecycle_operations.lease_owner IS NULL
                      OR tenant_lifecycle_operations.lease_owner = excluded.lease_owner
                      OR tenant_lifecycle_operations.lease_expires_at <= %s
                  )
            """,
            (
                operation,
                normalized_tenant,
                tenant_hash,
                public_identity_hash,
                schema_hash,
                owner,
                lease_expiry,
                current_time,
                current_time,
            ),
        )
        claimed = connection.execute(
            """
                SELECT lease_owner, public_identity_sha256
                FROM tenant_lifecycle_operations WHERE id = %s
            """,
            (operation,),
        ).fetchone()
        if claimed is not None and str(claimed[1]) != public_identity_hash:
            raise LifecycleConflictError("public identity baseline does not match export")
        if claimed is None or str(claimed[0]) != owner:
            raise LifecycleLeaseError("tenant export lease is held by another worker")
        _set_lifecycle_context(connection, operation_id=operation, lease_owner=owner)
        connection.execute(
            "UPDATE tenants SET status = 'archived', updated_at = %s WHERE id = %s",
            (current_time, normalized_tenant),
        )
    resolved = get_operation(connection, operation)
    if resolved is None:
        raise LifecycleConflictError("lifecycle operation was not persisted")
    return ExportPreparation(
        operation=resolved,
        lease_owner=owner,
        schema_identity_sha256=schema_hash,
        tables=tables,
    )


def heartbeat_lease(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    lease_owner: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> datetime:
    current_time = (now or utc_now()).astimezone(timezone.utc)
    expires_at = current_time + timedelta(seconds=_lease_seconds(lease_seconds))
    row = connection.execute(
        """
            UPDATE tenant_lifecycle_operations
            SET lease_expires_at = %s, updated_at = %s
            WHERE id = %s AND lease_owner = %s AND lease_expires_at > %s
              AND status IN ('exporting', 'purging', 'database_purged')
            RETURNING lease_expires_at
        """,
        (expires_at, current_time, operation_id, lease_owner, current_time),
    ).fetchone()
    connection.commit()
    if row is None:
        raise LifecycleLeaseError("tenant lifecycle lease was lost")
    return row[0]


def export_rows(
    connection: psycopg.Connection,
    *,
    tenant_id: str,
    tables: Sequence[LifecycleTable],
    fetch_size: int = 500,
) -> Iterator[tuple[LifecycleTable, tuple[Any, ...]]]:
    """Yield rows in catalog/table/primary-key order from one DB snapshot."""

    normalized_tenant = normalize_tenant_id(tenant_id)
    for table in tables:
        columns = sql.SQL(", ").join(sql.Identifier(name) for name in table.columns)
        order_by = sql.SQL(", ").join(sql.Identifier(name) for name in table.primary_key)
        if table.table_class == "tenant_root":
            predicate = sql.SQL("id = %s")
        else:
            if not table.tenant_column:
                raise LifecycleCompletenessError([(table.table_name, "missing_tenant_column")])
            predicate = sql.SQL("{} = %s").format(sql.Identifier(table.tenant_column))
        query = sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY {}").format(
            columns,
            sql.Identifier(table.table_name),
            predicate,
            order_by,
        )
        cursor_name = f"lifecycle_{table.export_order}_{uuid4().hex}"
        with connection.cursor(name=cursor_name) as cursor:
            cursor.itersize = fetch_size
            cursor.execute(query, (normalized_tenant,))
            for row in cursor:
                yield table, tuple(row)


def record_export(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    lease_owner: str,
    snapshot_hlc: str,
    schema_hash: str,
    content_hash: str,
    fingerprint: str,
    bucket: str,
    data_key: str,
    data_version_id: str,
    manifest_key: str,
    manifest_version_id: str,
    retention_until: datetime,
    now: datetime | None = None,
) -> LifecycleOperation:
    current_time = (now or utc_now()).astimezone(timezone.utc)
    row = connection.execute(
        """
            UPDATE tenant_lifecycle_operations
            SET status = 'exported', snapshot_hlc = %s,
                schema_identity_sha256 = %s, export_content_sha256 = %s,
                export_fingerprint = %s, export_bucket = %s,
                export_data_key = %s, export_data_version_id = %s,
                export_manifest_key = %s, export_manifest_version_id = %s,
                export_retention_until = %s, updated_at = %s
            WHERE id = %s AND status = 'exporting' AND lease_owner = %s
              AND lease_expires_at > %s
            RETURNING id
        """,
        (
            snapshot_hlc,
            schema_hash,
            content_hash,
            fingerprint,
            bucket,
            data_key,
            data_version_id,
            manifest_key,
            manifest_version_id,
            retention_until,
            current_time,
            operation_id,
            lease_owner,
            current_time,
        ),
    ).fetchone()
    connection.commit()
    if row is None:
        raise LifecycleLeaseError("export result could not be recorded with the active lease")
    operation = get_operation(connection, operation_id)
    if operation is None:
        raise LifecycleConflictError("exported operation disappeared")
    return operation


def record_verified_export(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    fingerprint: str,
    now: datetime | None = None,
) -> LifecycleOperation:
    current_time = (now or utc_now()).astimezone(timezone.utc)
    row = connection.execute(
        """
            UPDATE tenant_lifecycle_operations
            SET status = 'verified', export_verified_at = %s, updated_at = %s,
                lease_owner = NULL, lease_expires_at = NULL
            WHERE id = %s AND status IN ('exported', 'verified')
              AND export_fingerprint = %s
            RETURNING id
        """,
        (current_time, current_time, operation_id, fingerprint),
    ).fetchone()
    connection.commit()
    if row is None:
        raise LifecycleConflictError("verified fingerprint does not match the export")
    operation = get_operation(connection, operation_id)
    if operation is None:
        raise LifecycleConflictError("verified operation disappeared")
    return operation


def begin_purge(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    confirmed_fingerprint: str,
    public_identity_sha256: str,
    lease_owner: str | None = None,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LifecycleOperation:
    if len(confirmed_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in confirmed_fingerprint
    ):
        raise ValueError("confirmed export fingerprint must be a SHA-256 digest")
    public_identity_hash = _sha256_digest(
        public_identity_sha256,
        label="public identity baseline",
    )
    owner = str(UUID(lease_owner)) if lease_owner else str(uuid4())
    current_time = (now or utc_now()).astimezone(timezone.utc)
    lease_expiry = current_time + timedelta(seconds=_lease_seconds(lease_seconds))
    with connection.transaction():
        row = connection.execute(
            """
                UPDATE tenant_lifecycle_operations
                SET status = CASE
                        WHEN status = 'database_purged' THEN status
                        ELSE 'purging'
                    END,
                    confirmed_export_fingerprint = %s,
                    purge_confirmed_at = COALESCE(purge_confirmed_at, %s),
                    lease_owner = %s,
                    lease_expires_at = %s, attempt_count = attempt_count + 1,
                    updated_at = %s
                WHERE id = %s
                  AND status IN ('verified', 'purging', 'database_purged')
                  AND export_verified_at IS NOT NULL
                  AND export_fingerprint = %s
                  AND public_identity_sha256 = %s
                  AND (
                      status IN ('purging', 'database_purged')
                      OR export_retention_until > %s
                  )
                  AND (
                      lease_owner IS NULL OR lease_owner = %s OR lease_expires_at <= %s
                  )
                RETURNING target_tenant_id
            """,
            (
                confirmed_fingerprint,
                current_time,
                owner,
                lease_expiry,
                current_time,
                operation_id,
                confirmed_fingerprint,
                public_identity_hash,
                current_time,
                owner,
                current_time,
            ),
        ).fetchone()
        if row is None:
            raise LifecycleLeaseError(
                "purge requires the matching verified export and an available lease"
            )
        tenant_id = str(row[0])
        _set_tenant_context(connection, tenant_id)
        _set_lifecycle_context(connection, operation_id=operation_id, lease_owner=owner)
        tenant = connection.execute(
            "SELECT status FROM tenants WHERE id = %s FOR UPDATE", (tenant_id,)
        ).fetchone()
        if tenant is not None:
            if str(tenant[0]) not in {"archived", "purge_pending", "purging"}:
                raise LifecycleConflictError("tenant is not archived for purge")
            if str(tenant[0]) == "archived":
                connection.execute(
                    "UPDATE tenants SET status = 'purge_pending', updated_at = %s "
                    "WHERE id = %s",
                    (current_time, tenant_id),
                )
            connection.execute(
                "UPDATE tenants SET status = 'purging', updated_at = %s WHERE id = %s",
                (current_time, tenant_id),
            )
    operation = get_operation(connection, operation_id)
    if operation is None:
        raise LifecycleConflictError("purge operation disappeared")
    return operation


def record_principal_cleanup_targets(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    lease_owner: str,
    now: datetime | None = None,
) -> PrincipalCleanupTargets:
    current_time = (now or utc_now()).astimezone(timezone.utc)
    with connection.transaction():
        operation = get_operation(connection, operation_id, for_update=True)
        if operation is None:
            raise LifecycleConflictError("purge operation does not exist")
        if (
            operation.status not in {"purging", "database_purged"}
            or operation.lease_owner != lease_owner
            or operation.lease_expires_at is None
            or operation.lease_expires_at <= current_time
        ):
            raise LifecycleLeaseError("principal cleanup is not owned by the active lease")
        if operation.cleanup_targets_captured_at is not None:
            return PrincipalCleanupTargets(
                principal_hashes=operation.principal_hashes,
                cognito_credential_locators=operation.cognito_credential_locators,
            )
        if operation.status == "database_purged":
            raise LifecycleConflictError(
                "database purge is missing its captured principal cleanup targets"
            )
        _set_tenant_context(connection, operation.target_tenant_id)
        _set_lifecycle_context(
            connection, operation_id=operation_id, lease_owner=lease_owner
        )
        rows = connection.execute(
            """
                SELECT principal_hash, provisioning_key
                FROM product_principal_roles
                WHERE tenant_id = %s ORDER BY principal_hash, provisioning_key
            """,
            (operation.target_tenant_id,),
        ).fetchall()
        hashes = tuple(str(row[0]) for row in rows)
        locator_rows = connection.execute(
            """
                SELECT provisioning_key, user_pool_id, cognito_username
                FROM product_credential_locators
                WHERE tenant_id = %s
                ORDER BY provisioning_key, user_pool_id, cognito_username, id
            """,
            (operation.target_tenant_id,),
        ).fetchall()
        mapped_keys = {str(row[1]) for row in rows}
        locator_keys = {str(row[0]) for row in locator_rows}
        if not mapped_keys.issubset(locator_keys):
            raise LifecycleConflictError(
                "tenant principal mapping lacks a durable Cognito credential locator"
            )
        locators = tuple(
            CognitoCredentialLocator(
                user_pool_id=str(row[1]), username=str(row[2])
            )
            for row in locator_rows
        )
        updated = connection.execute(
            """
                UPDATE tenant_lifecycle_operations
                SET principal_hashes = %s::JSONB,
                    cognito_credential_locators = %s::JSONB,
                    cleanup_targets_captured_at = %s,
                    updated_at = %s
                WHERE id = %s AND status = 'purging' AND lease_owner = %s
                  AND lease_expires_at > %s
                RETURNING id
            """,
            (
                json.dumps(hashes),
                json.dumps(
                    [
                        {
                            "user_pool_id": locator.user_pool_id,
                            "username": locator.username,
                        }
                        for locator in locators
                    ]
                ),
                current_time,
                current_time,
                operation_id,
                lease_owner,
                current_time,
            ),
        ).fetchone()
    if updated is None:
        raise LifecycleLeaseError("principal cleanup state could not be fenced")
    return PrincipalCleanupTargets(
        principal_hashes=hashes,
        cognito_credential_locators=locators,
    )


def record_principal_hashes(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    lease_owner: str,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Capture all external cleanup targets while preserving the legacy API."""

    return record_principal_cleanup_targets(
        connection,
        operation_id=operation_id,
        lease_owner=lease_owner,
        now=now,
    ).principal_hashes


def purge_database_tenant(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    lease_owner: str,
    now: datetime | None = None,
) -> LifecycleOperation:
    current_time = (now or utc_now()).astimezone(timezone.utc)
    with connection.transaction():
        operation = get_operation(connection, operation_id, for_update=True)
        if operation is None:
            raise LifecycleConflictError("purge operation does not exist")
        if operation.status == "database_purged":
            return operation
        if (
            operation.status != "purging"
            or operation.lease_owner != lease_owner
            or operation.lease_expires_at is None
            or operation.lease_expires_at <= current_time
            or operation.export_fingerprint is None
            or operation.export_fingerprint != operation.confirmed_export_fingerprint
            or operation.public_identity_sha256 is None
            or operation.export_verified_at is None
            or operation.cleanup_targets_captured_at is None
        ):
            raise LifecycleLeaseError("database purge is not authorized by the active lease")
        current_schema_hash = schema_identity_sha256(lifecycle_tables(connection))
        if current_schema_hash != operation.schema_identity_sha256:
            raise LifecycleConflictError("database schema changed after the verified export")
        _set_tenant_context(connection, operation.target_tenant_id)
        _set_lifecycle_context(
            connection, operation_id=operation_id, lease_owner=lease_owner
        )
        deleted = connection.execute(
            "DELETE FROM tenants WHERE id = %s RETURNING id",
            (operation.target_tenant_id,),
        ).fetchone()
        if deleted is None:
            raise LifecycleConflictError("tenant catalog row was already absent")
        connection.execute(
            """
                INSERT INTO tenant_purge_tombstones (
                    purge_id, tenant_identity_sha256, export_fingerprint,
                    schema_identity_sha256, public_identity_sha256,
                    database_purged_at, purged_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (purge_id) DO NOTHING
            """,
            (
                operation.id,
                operation.tenant_identity_sha256,
                operation.export_fingerprint,
                operation.schema_identity_sha256,
                operation.public_identity_sha256,
                current_time,
                current_time,
            ),
        )
        connection.execute(
            """
                UPDATE tenant_lifecycle_operations
                SET status = 'database_purged', database_purged_at = %s,
                    updated_at = %s
                WHERE id = %s AND lease_owner = %s
            """,
            (current_time, current_time, operation_id, lease_owner),
        )
    operation = get_operation(connection, operation_id)
    if operation is None:
        raise LifecycleConflictError("database-purged operation disappeared")
    return operation


def finalize_purge(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    lease_owner: str,
    public_identity_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = (now or utc_now()).astimezone(timezone.utc)
    public_identity_hash = _sha256_digest(
        public_identity_sha256,
        label="public identity baseline",
    )
    with connection.transaction():
        operation = get_operation(connection, operation_id, for_update=True)
        if operation is None:
            tombstone = connection.execute(
                """
                    SELECT purge_id, tenant_identity_sha256, export_fingerprint,
                           schema_identity_sha256, public_identity_sha256,
                           database_purged_at, purged_at
                    FROM tenant_purge_tombstones WHERE purge_id = %s
                """,
                (operation_id,),
            ).fetchone()
            if tombstone is None:
                raise LifecycleConflictError("purge operation does not exist")
            payload = _tombstone_payload(tombstone)
            if payload["public_identity_sha256"] != public_identity_hash:
                raise LifecycleConflictError("public identity baseline does not match purge")
            return payload
        if operation.public_identity_sha256 != public_identity_hash:
            raise LifecycleConflictError("public identity baseline does not match purge")
        if (
            operation.status != "database_purged"
            or operation.lease_owner != lease_owner
            or operation.lease_expires_at is None
            or operation.lease_expires_at <= current_time
            or operation.database_purged_at is None
            or operation.export_fingerprint is None
            or operation.schema_identity_sha256 is None
        ):
            raise LifecycleLeaseError("purge cannot be finalized from its current state")
        connection.execute(
            """
                INSERT INTO tenant_purge_tombstones (
                    purge_id, tenant_identity_sha256, export_fingerprint,
                    schema_identity_sha256, public_identity_sha256,
                    database_purged_at, purged_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (purge_id) DO NOTHING
            """,
            (
                operation.id,
                operation.tenant_identity_sha256,
                operation.export_fingerprint,
                operation.schema_identity_sha256,
                operation.public_identity_sha256,
                operation.database_purged_at,
                current_time,
            ),
        )
        connection.execute(
            "DELETE FROM tenant_lifecycle_operations WHERE id = %s",
            (operation_id,),
        )
        tombstone = connection.execute(
            """
                SELECT purge_id, tenant_identity_sha256, export_fingerprint,
                       schema_identity_sha256, public_identity_sha256,
                       database_purged_at, purged_at
                FROM tenant_purge_tombstones WHERE purge_id = %s
            """,
            (operation_id,),
        ).fetchone()
    if tombstone is None:
        raise LifecycleConflictError("purge tombstone was not persisted")
    payload = _tombstone_payload(tombstone)
    if payload["public_identity_sha256"] != public_identity_hash:
        raise LifecycleConflictError("public identity baseline does not match purge")
    return payload


def abort_operation(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    now: datetime | None = None,
) -> LifecycleOperation:
    current_time = (now or utc_now()).astimezone(timezone.utc)
    abort_owner = str(uuid4())
    abort_lease_expires_at = current_time + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    with connection.transaction():
        operation = get_operation(connection, operation_id, for_update=True)
        if operation is None or operation.status not in {
            "pending_export",
            "exporting",
            "exported",
            "verified",
            "failed",
        }:
            raise LifecycleConflictError("operation cannot be aborted after purge begins")
        if operation.lease_expires_at is not None and operation.lease_expires_at > current_time:
            raise LifecycleLeaseError("operation cannot be aborted while its lease is active")
        claimed = connection.execute(
            """
                UPDATE tenant_lifecycle_operations
                SET lease_owner = %s, lease_expires_at = %s, updated_at = %s
                WHERE id = %s
                  AND (lease_owner IS NULL OR lease_expires_at <= %s)
                RETURNING id
            """,
            (
                abort_owner,
                abort_lease_expires_at,
                current_time,
                operation_id,
                current_time,
            ),
        ).fetchone()
        if claimed is None:
            raise LifecycleLeaseError("operation abort could not acquire its lease")
        _set_tenant_context(connection, operation.target_tenant_id)
        _set_lifecycle_context(
            connection,
            operation_id=operation_id,
            lease_owner=abort_owner,
        )
        connection.execute(
            """
                UPDATE tenants SET status = 'active', updated_at = %s
                WHERE id = %s AND status = 'archived'
            """,
            (current_time, operation.target_tenant_id),
        )
        row = connection.execute(
            """
                UPDATE tenant_lifecycle_operations
                SET status = 'aborted', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = %s
                WHERE id = %s
                RETURNING id
            """,
            (current_time, operation_id),
        ).fetchone()
    if row is None:
        raise LifecycleConflictError("operation cannot be aborted after purge begins")
    operation = get_operation(connection, operation_id)
    if operation is None:
        raise LifecycleConflictError("aborted operation disappeared")
    return operation


def get_operation(
    connection: psycopg.Connection,
    operation_id: str,
    *,
    for_update: bool = False,
) -> LifecycleOperation | None:
    suffix = sql.SQL(" FOR UPDATE") if for_update else sql.SQL("")
    row = connection.execute(
        sql.SQL(
            """
                SELECT id, target_tenant_id, tenant_identity_sha256,
                       public_identity_sha256, status, snapshot_hlc,
                       schema_identity_sha256,
                       export_content_sha256, export_fingerprint,
                       export_bucket, export_data_key, export_data_version_id,
                       export_manifest_key, export_manifest_version_id,
                       export_retention_until, export_verified_at,
                       confirmed_export_fingerprint, principal_hashes,
                       cognito_credential_locators,
                       cleanup_targets_captured_at,
                       lease_owner, lease_expires_at, database_purged_at
                FROM tenant_lifecycle_operations WHERE id = %s
            """
        )
        + suffix,
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    principal_hashes = row[17] if isinstance(row[17], list) else []
    credential_locators = _parse_cognito_credential_locators(row[18])
    return LifecycleOperation(
        id=str(row[0]),
        target_tenant_id=str(row[1]),
        tenant_identity_sha256=str(row[2]),
        public_identity_sha256=(str(row[3]) if row[3] is not None else None),
        status=str(row[4]),
        snapshot_hlc=str(row[5]) if row[5] is not None else None,
        schema_identity_sha256=str(row[6]) if row[6] is not None else None,
        export_content_sha256=str(row[7]) if row[7] is not None else None,
        export_fingerprint=str(row[8]) if row[8] is not None else None,
        export_bucket=str(row[9]) if row[9] is not None else None,
        export_data_key=str(row[10]) if row[10] is not None else None,
        export_data_version_id=str(row[11]) if row[11] is not None else None,
        export_manifest_key=str(row[12]) if row[12] is not None else None,
        export_manifest_version_id=str(row[13]) if row[13] is not None else None,
        export_retention_until=row[14],
        export_verified_at=row[15],
        confirmed_export_fingerprint=(str(row[16]) if row[16] is not None else None),
        principal_hashes=tuple(str(value) for value in principal_hashes),
        cognito_credential_locators=credential_locators,
        cleanup_targets_captured_at=row[19],
        lease_owner=str(row[20]) if row[20] is not None else None,
        lease_expires_at=row[21],
        database_purged_at=row[22],
    )


def get_tombstone(
    connection: psycopg.Connection, operation_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """
            SELECT purge_id, tenant_identity_sha256, export_fingerprint,
                   schema_identity_sha256, public_identity_sha256,
                   database_purged_at, purged_at
            FROM tenant_purge_tombstones WHERE purge_id = %s
        """,
        (operation_id,),
    ).fetchone()
    return _tombstone_payload(row) if row is not None else None


def _set_lifecycle_context(
    connection: psycopg.Connection, *, operation_id: str, lease_owner: str
) -> None:
    connection.execute(
        "SELECT set_config(%s, %s, true), set_config(%s, %s, true)",
        (LIFECYCLE_OPERATION_SETTING, operation_id, LIFECYCLE_OWNER_SETTING, lease_owner),
    )


def _set_tenant_context(connection: psycopg.Connection, tenant_id: str) -> None:
    connection.execute(
        "SELECT set_config(%s, %s, true)",
        (TENANT_SETTING, normalize_tenant_id(tenant_id)),
    )


def _lease_seconds(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("lease_seconds must be an integer")
    if value < 30 or value > MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be between 30 and 1800")
    return value


def _sha256_digest(value: str, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _tombstone_payload(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "purge_id": str(row[0]),
        "tenant_identity_sha256": str(row[1]),
        "export_fingerprint": str(row[2]),
        "schema_identity_sha256": str(row[3]),
        "public_identity_sha256": str(row[4]) if row[4] is not None else None,
        "database_purged_at": row[5].isoformat(),
        "purged_at": row[6].isoformat(),
    }


def _parse_cognito_credential_locators(
    value: Any,
) -> tuple[CognitoCredentialLocator, ...]:
    if not isinstance(value, list):
        raise LifecycleConflictError("captured Cognito credential locators are invalid")
    locators: list[CognitoCredentialLocator] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"user_pool_id", "username"}:
            raise LifecycleConflictError(
                "captured Cognito credential locators are invalid"
            )
        user_pool_id = item.get("user_pool_id")
        username = item.get("username")
        if (
            not isinstance(user_pool_id, str)
            or not user_pool_id
            or not isinstance(username, str)
            or not username
        ):
            raise LifecycleConflictError(
                "captured Cognito credential locators are invalid"
            )
        locators.append(
            CognitoCredentialLocator(
                user_pool_id=user_pool_id,
                username=username,
            )
        )
    if len(locators) != len(set(locators)):
        raise LifecycleConflictError("captured Cognito credential locators are duplicated")
    return tuple(sorted(locators))
