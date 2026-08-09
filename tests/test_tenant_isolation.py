from __future__ import annotations

import os
import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from hindsight.db import TenantConnection
from hindsight.lifecycle import (
    begin_export,
    begin_purge,
    finalize_purge,
    purge_database_tenant,
    record_export,
    record_principal_hashes,
    record_verified_export,
    utc_now,
)

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

ROOT = Path(__file__).resolve().parents[1]


def _apply_schema(connection: psycopg.Connection) -> None:
    connection.execute((ROOT / "infra/db/roles.sql").read_text())


def _runtime_connection(url: str, *, tenant_id: str) -> TenantConnection:
    raw = psycopg.connect(url)
    raw.execute("SET ROLE hindsight_agent_writer")
    raw.commit()
    return TenantConnection(raw, tenant_id=tenant_id)


def _purge_test_tenants(url: str, tenant_ids: tuple[object, ...]) -> None:
    with psycopg.connect(url, autocommit=True) as admin:
        session_user = str(admin.execute("SELECT session_user").fetchone()[0])
        already_member = bool(
            admin.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_auth_members AS membership
                    JOIN pg_catalog.pg_roles AS granted_role
                      ON granted_role.oid = membership.roleid
                    JOIN pg_catalog.pg_roles AS member_role
                      ON member_role.oid = membership.member
                    WHERE granted_role.rolname = 'hindsight_lifecycle'
                      AND member_role.rolname = %s
                )
                """,
                (session_user,),
            ).fetchone()[0]
        )
        if not already_member:
            admin.execute(
                sql.SQL("GRANT hindsight_lifecycle TO {}").format(
                    sql.Identifier(session_user)
                )
            )
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            for raw_tenant_id in tenant_ids:
                tenant_id = str(raw_tenant_id)
                connection.execute(
                    "SELECT set_config('hindsight.tenant_id', %s, false)",
                    (tenant_id,),
                )
                if connection.execute(
                    "SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)
                ).fetchone() is None:
                    continue
                operation_id = str(uuid4())
                lease_owner = str(uuid4())
                fingerprint = hashlib.sha256(
                    f"tenant-isolation:{operation_id}".encode()
                ).hexdigest()
                preparation = begin_export(
                    connection,
                    tenant_id=tenant_id,
                    operation_id=operation_id,
                    lease_owner=lease_owner,
                )
                record_export(
                    connection,
                    operation_id=operation_id,
                    lease_owner=lease_owner,
                    snapshot_hlc="1.0000000000",
                    schema_hash=preparation.schema_identity_sha256,
                    content_hash=hashlib.sha256(operation_id.encode()).hexdigest(),
                    fingerprint=fingerprint,
                    bucket="tenant-isolation-cleanup",
                    data_key=f"{operation_id}/tenant.ndjson",
                    data_version_id="fixture-data-version",
                    manifest_key=f"{operation_id}/manifest.json",
                    manifest_version_id="fixture-manifest-version",
                    retention_until=utc_now() + timedelta(days=1),
                )
                record_verified_export(
                    connection,
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                )
                begin_purge(
                    connection,
                    operation_id=operation_id,
                    confirmed_fingerprint=fingerprint,
                    lease_owner=lease_owner,
                )
                record_principal_hashes(
                    connection,
                    operation_id=operation_id,
                    lease_owner=lease_owner,
                )
                purge_database_tenant(
                    connection,
                    operation_id=operation_id,
                    lease_owner=lease_owner,
                )
                finalize_purge(
                    connection,
                    operation_id=operation_id,
                    lease_owner=lease_owner,
                )
    finally:
        if not already_member:
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("REVOKE hindsight_lifecycle FROM {}").format(
                        sql.Identifier(session_user)
                    )
                )


@requires_db
def test_tenant_rls_relationships_connection_reuse_and_outbox_are_fail_closed():
    target_url = os.environ["DATABASE_URL"]
    first_tenant = uuid4()
    second_tenant = uuid4()
    first_service = uuid4()
    second_service = uuid4()
    first_incident = uuid4()
    suffix = uuid4().hex
    first_service_slug = f"shared-service-{suffix}"
    second_service_slug = first_service_slug
    first_incident_slug = f"incident-a-{suffix}"

    try:
        with psycopg.connect(target_url, autocommit=True) as admin:
            _apply_schema(admin)
            admin.execute(
                "INSERT INTO tenants (id, slug, tenant_kind) VALUES "
                "(%s, %s, 'diagnostic'), (%s, %s, 'diagnostic')",
                (first_tenant, f"tenant-a-{suffix}", second_tenant, f"tenant-b-{suffix}"),
            )
            admin.execute(
                """
                    INSERT INTO services (
                        id, tenant_id, slug, name, owner_team, tier
                    ) VALUES
                        (%s, %s, %s, 'Service A', 'team-a', 'core'),
                        (%s, %s, %s, 'Service B', 'team-b', 'core')
                """,
                (
                    first_service,
                    first_tenant,
                    first_service_slug,
                    second_service,
                    second_tenant,
                    second_service_slug,
                ),
            )

        connection = _runtime_connection(target_url, tenant_id=str(first_tenant))
        try:
            assert connection.execute("SELECT slug FROM services ORDER BY slug").fetchall() == [
                (first_service_slug,)
            ]
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException)
            ):
                connection.execute(
                    """
                        INSERT INTO services (
                            tenant_id, slug, name, owner_team, tier
                        ) VALUES (%s, 'forged-service', 'Forged', 'team-a', 'core')
                    """,
                    (second_tenant,),
                )
            connection.rollback()

            updated = connection.execute(
                "UPDATE services SET name = 'Hidden update' WHERE id = %s",
                (second_service,),
            )
            assert updated.rowcount == 0
            connection.commit()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("DELETE FROM services WHERE id = %s", (first_service,))
            connection.rollback()

            connection.execute(
                """
                    INSERT INTO incidents (
                        id, slug, title, severity, status, started_at, summary
                    ) VALUES (%s, %s, 'Incident A', 'sev2', 'open', now(), 'A')
                """,
                (first_incident, first_incident_slug),
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                connection.execute(
                    """
                        INSERT INTO incident_services (incident_id, service_id, impact)
                        VALUES (%s, %s, 'cross-tenant')
                    """,
                    (first_incident, second_service),
                )
            connection.rollback()

            connection.execute(
                """
                    INSERT INTO incidents (
                        id, slug, title, severity, status, started_at, summary
                    ) VALUES (%s, %s, 'Incident A', 'sev2', 'open', now(), 'A')
                """,
                (first_incident, first_incident_slug),
            )
            connection.commit()
            connection.bind_tenant(str(second_tenant))
            assert connection.execute("SELECT slug FROM services ORDER BY slug").fetchall() == [
                (second_service_slug,)
            ]
            connection.commit()
        finally:
            connection.close()

        with psycopg.connect(target_url) as missing:
            missing.execute("SET ROLE hindsight_agent_writer")
            assert missing.execute("SELECT count(*) FROM services").fetchone() == (0,)
            with pytest.raises(
                (
                    psycopg.errors.NotNullViolation,
                    psycopg.errors.InsufficientPrivilege,
                    psycopg.errors.RaiseException,
                )
            ):
                missing.execute(
                    "INSERT INTO services (slug, name, owner_team, tier) "
                    "VALUES ('missing-tenant', 'Missing', 'none', 'core')"
                )
            missing.rollback()
            missing.execute("SELECT set_config('hindsight.tenant_id', 'malformed', true)")
            with pytest.raises(psycopg.Error, match="UUID|uuid"):
                missing.execute("SELECT count(*) FROM services")

        rollback_incident = uuid4()
        connection = _runtime_connection(target_url, tenant_id=str(first_tenant))
        try:
            connection.execute(
                """
                    INSERT INTO incidents (
                        id, slug, title, severity, status, started_at, summary
                    ) VALUES (%s, 'rolled-back', 'Rolled back', 'sev3', 'open', now(), 'A')
                """,
                (rollback_incident,),
            )
            connection.rollback()
        finally:
            connection.close()

        with psycopg.connect(target_url, autocommit=True) as admin:
            assert admin.execute(
                "SELECT count(*) FROM tenant_event_outbox WHERE aggregate_id = %s",
                (str(first_incident),),
            ).fetchone() == (1,)
            assert admin.execute(
                "SELECT count(*) FROM tenant_event_outbox WHERE aggregate_id = %s",
                (str(rollback_incident),),
            ).fetchone() == (0,)
            payload = admin.execute(
                "SELECT payload FROM tenant_event_outbox WHERE aggregate_id = %s",
                (str(first_incident),),
            ).fetchone()[0]
            assert set(payload) <= {
                "id",
                "run_id",
                "incident_id",
                "status",
                "previous_status",
                "resolution_event_id",
                "consolidation_policy",
                "operation_type",
                "sequence",
                "updated_at",
            }

        with psycopg.connect(target_url) as cdc:
            cdc.execute("SET ROLE hindsight_cdc")
            assert cdc.execute(
                "SELECT count(*) FROM tenant_event_outbox WHERE aggregate_id = %s",
                (str(first_incident),),
            ).fetchone() == (1,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cdc.execute("SELECT count(*) FROM incidents")
    finally:
        _purge_test_tenants(target_url, (first_tenant, second_tenant))
