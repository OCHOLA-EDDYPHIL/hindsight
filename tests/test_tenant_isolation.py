from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from hindsight.db import TenantConnection

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

ROOT = Path(__file__).resolve().parents[1]


def _apply_schema(connection: psycopg.Connection) -> None:
    connection.execute((ROOT / "infra/db/roles.sql").read_text())


def _runtime_connection(url: str, *, tenant_id: str) -> TenantConnection:
    raw = psycopg.connect(url)
    raw.execute("SET ROLE hindsight_agent_writer")
    raw.commit()
    return TenantConnection(raw, tenant_id=tenant_id)


@requires_db
def test_tenant_rls_relationships_connection_reuse_and_outbox_are_fail_closed():
    target_url = os.environ["DATABASE_URL"]
    first_tenant = uuid4()
    second_tenant = uuid4()
    first_service = uuid4()
    second_service = uuid4()
    first_incident = uuid4()
    suffix = uuid4().hex
    first_service_slug = f"service-a-{suffix}"
    second_service_slug = f"service-b-{suffix}"
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
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
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
                (psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege)
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
        with psycopg.connect(target_url, autocommit=True) as admin:
            admin.execute(
                "DELETE FROM incidents WHERE tenant_id IN (%s, %s)",
                (first_tenant, second_tenant),
            )
            admin.execute(
                "DELETE FROM tenant_event_outbox WHERE tenant_id IN (%s, %s)",
                (first_tenant, second_tenant),
            )
            admin.execute(
                "DELETE FROM services WHERE tenant_id IN (%s, %s)",
                (first_tenant, second_tenant),
            )
            admin.execute(
                "DELETE FROM tenants WHERE id IN (%s, %s)",
                (first_tenant, second_tenant),
            )
