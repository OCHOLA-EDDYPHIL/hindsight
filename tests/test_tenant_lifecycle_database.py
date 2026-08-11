from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hindsight.lifecycle import (
    LifecycleCompletenessError,
    LifecycleLeaseError,
    abort_operation,
    assert_lifecycle_completeness,
    begin_export,
    begin_purge,
    finalize_purge,
    public_demo_identity_sentinel,
    purge_database_tenant,
    record_export,
    record_principal_hashes,
    record_verified_export,
    utc_now,
)
from hindsight.server_tenants import PUBLIC_DEMO_TENANT_ID


requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
PUBLIC_IDENTITY_SHA256 = "e" * 64


@pytest.fixture(scope="module")
def lifecycle_database_url() -> str:
    database_url = os.environ["DATABASE_URL"]
    login_role = f"hindsight_lifecycle_test_{uuid4().hex}"
    password = f"Lifecycle-{uuid4().hex}"
    sslmode = parse_qs(urlsplit(database_url).query).get("sslmode", [None])[0]
    with psycopg.connect(database_url, autocommit=True) as admin:
        if sslmode == "disable":
            admin.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN NOBYPASSRLS").format(sql.Identifier(login_role))
            )
            lifecycle_url = make_conninfo(database_url, user=login_role)
        else:
            admin.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s NOBYPASSRLS").format(
                    sql.Identifier(login_role)
                ),
                (password,),
            )
            lifecycle_url = make_conninfo(database_url, user=login_role, password=password)
        admin.execute(sql.SQL("GRANT hindsight_lifecycle TO {}").format(sql.Identifier(login_role)))
    with psycopg.connect(lifecycle_url) as lifecycle:
        assert lifecycle.execute(
            "SELECT session_user, pg_has_role(current_user, 'hindsight_lifecycle', 'member')"
        ).fetchone() == (login_role, True)
    try:
        yield lifecycle_url
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login_role)))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _create_tenant(database_url: str) -> tuple[str, str, str]:
    tenant_id = str(uuid4())
    principal_hash = _digest(f"principal:{tenant_id}")
    provisioning_key = _digest(f"provision:{tenant_id}")
    benchmark_experiment_id = str(uuid4())
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("SELECT set_config('hindsight.tenant_id', %s, false)", (tenant_id,))
        connection.execute(
            """
                INSERT INTO tenants (id, slug, tenant_kind)
                VALUES (%s, %s, 'diagnostic')
            """,
            (tenant_id, f"lifecycle-{tenant_id}"),
        )
        connection.execute(
            """
                INSERT INTO services (
                    tenant_id, slug, name, owner_team, tier
                ) VALUES (%s, 'checkout', 'Checkout', 'payments', 'core')
            """,
            (tenant_id,),
        )
        connection.execute(
            """
                INSERT INTO incidents (
                    tenant_id, slug, title, severity, status,
                    started_at, summary
                ) VALUES (
                    %s, 'checkout-latency', 'Checkout latency', 'sev2',
                    'open', now(), 'Synthetic lifecycle fixture'
                )
            """,
            (tenant_id,),
        )
        connection.execute(
            """
                INSERT INTO product_principal_roles (
                    principal_hash, provisioning_key, tenant_id, role
                ) VALUES (%s, %s, %s, 'operator')
            """,
            (principal_hash, provisioning_key, tenant_id),
        )
        connection.execute(
            """
                INSERT INTO product_credential_locators (
                    provisioning_key, tenant_id, user_pool_id,
                    cognito_username, role, principal_hash, status
                ) VALUES (
                    %s, %s, 'us-east-1_lifecycle', %s,
                    'operator', %s, 'active'
                )
            """,
            (
                provisioning_key,
                tenant_id,
                f"lifecycle-{tenant_id}",
                principal_hash,
            ),
        )
        connection.execute(
            """
                INSERT INTO benchmark_experiments (
                    id, tenant_id, experiment_kind, manifest, manifest_sha256,
                    provider, model
                ) VALUES (
                    %s, %s, 'ci_smoke', '{}'::JSONB, %s,
                    'fixture', 'fixture-v1'
                )
            """,
            (
                benchmark_experiment_id,
                tenant_id,
                _digest(f"manifest:{benchmark_experiment_id}"),
            ),
        )
    return tenant_id, principal_hash, benchmark_experiment_id


def _record_verified_fixture(
    connection: psycopg.Connection,
    *,
    operation_id: str,
    lease_owner: str,
    schema_hash: str,
    fingerprint: str,
) -> None:
    record_export(
        connection,
        operation_id=operation_id,
        lease_owner=lease_owner,
        snapshot_hlc="1.0000000000",
        schema_hash=schema_hash,
        content_hash=_digest(f"content:{operation_id}"),
        fingerprint=fingerprint,
        bucket="lifecycle-acceptance",
        data_key=f"exports/{operation_id}/tenant.ndjson",
        data_version_id="data-version-1",
        manifest_key=f"exports/{operation_id}/manifest.json",
        manifest_version_id="manifest-version-1",
        retention_until=utc_now() + timedelta(days=1),
    )
    record_verified_export(
        connection,
        operation_id=operation_id,
        fingerprint=fingerprint,
    )


def _complete_purge(
    connection: psycopg.Connection,
    *,
    tenant_id: str,
) -> dict[str, object]:
    operation_id = str(uuid4())
    lease_owner = str(uuid4())
    fingerprint = _digest(f"export:{operation_id}")
    preparation = begin_export(
        connection,
        tenant_id=tenant_id,
        operation_id=operation_id,
        lease_owner=lease_owner,
        public_identity_sha256=PUBLIC_IDENTITY_SHA256,
    )
    _record_verified_fixture(
        connection,
        operation_id=operation_id,
        lease_owner=lease_owner,
        schema_hash=preparation.schema_identity_sha256,
        fingerprint=fingerprint,
    )
    begin_purge(
        connection,
        operation_id=operation_id,
        confirmed_fingerprint=fingerprint,
        lease_owner=lease_owner,
        public_identity_sha256=PUBLIC_IDENTITY_SHA256,
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
    return finalize_purge(
        connection,
        operation_id=operation_id,
        lease_owner=lease_owner,
        public_identity_sha256=PUBLIC_IDENTITY_SHA256,
    )


@requires_db
def test_legacy_null_public_identity_baseline_cannot_be_backfilled(
    lifecycle_database_url: str,
):
    operation_id = str(uuid4())
    target_tenant_id = str(uuid4())
    with psycopg.connect(lifecycle_database_url, autocommit=True) as connection:
        connection.execute(
            """
                INSERT INTO tenant_lifecycle_operations (
                    id, target_tenant_id, tenant_identity_sha256, status
                ) VALUES (%s, %s, %s, 'failed')
            """,
            (operation_id, target_tenant_id, _digest(target_tenant_id)),
        )
        try:
            assert connection.execute(
                """
                    SELECT public_identity_sha256
                    FROM tenant_lifecycle_operations
                    WHERE id = %s
                """,
                (operation_id,),
            ).fetchone() == (None,)
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="public identity baseline is immutable",
            ):
                connection.execute(
                    """
                        UPDATE tenant_lifecycle_operations
                        SET public_identity_sha256 = %s
                        WHERE id = %s
                    """,
                    (PUBLIC_IDENTITY_SHA256, operation_id),
                )
            assert connection.execute(
                """
                    SELECT public_identity_sha256
                    FROM tenant_lifecycle_operations
                    WHERE id = %s
                """,
                (operation_id,),
            ).fetchone() == (None,)
        finally:
            connection.execute(
                "DELETE FROM tenant_lifecycle_operations WHERE id = %s",
                (operation_id,),
            )


@requires_db
def test_public_demo_identity_sentinel_is_stable_for_lifecycle_role(
    lifecycle_database_url: str,
):
    admin_url = os.environ["DATABASE_URL"]
    marker = uuid4().hex
    pool_id = f"lifecycle-sentinel-{marker}"
    mappings = tuple(
        (
            _digest(f"sentinel:{marker}:principal:{role}"),
            _digest(f"sentinel:{marker}:provisioning:{role}"),
            role,
            f"sentinel-{marker}-{role}",
        )
        for role in ("operator", "viewer")
    )
    created_fixture = False
    try:
        with psycopg.connect(admin_url) as admin:
            admin.execute(
                "SELECT set_config('hindsight.tenant_id', %s, false)",
                (PUBLIC_DEMO_TENANT_ID,),
            )
            existing = admin.execute(
                """
                    SELECT
                        (SELECT count(*) FROM product_principal_roles
                         WHERE tenant_id = %s),
                        (SELECT count(*) FROM product_credential_locators
                         WHERE tenant_id = %s)
                """,
                (PUBLIC_DEMO_TENANT_ID, PUBLIC_DEMO_TENANT_ID),
            ).fetchone()
            assert existing is not None
            if existing == (0, 0):
                for principal_hash, provisioning_key, role, username in mappings:
                    admin.execute(
                        """
                            INSERT INTO product_principal_roles (
                                principal_hash, provisioning_key, tenant_id, role
                            ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            principal_hash,
                            provisioning_key,
                            PUBLIC_DEMO_TENANT_ID,
                            role,
                        ),
                    )
                    admin.execute(
                        """
                            INSERT INTO product_credential_locators (
                                provisioning_key, tenant_id, user_pool_id,
                                cognito_username, role, principal_hash, status
                            ) VALUES (%s, %s, %s, %s, %s, %s, 'active')
                        """,
                        (
                            provisioning_key,
                            PUBLIC_DEMO_TENANT_ID,
                            pool_id,
                            username,
                            role,
                            principal_hash,
                        ),
                    )
                created_fixture = True

        with psycopg.connect(lifecycle_database_url, autocommit=True) as connection:
            first = public_demo_identity_sentinel(connection)
            second = public_demo_identity_sentinel(connection)

        assert first == second
        assert first.tenant_rows == 1
        assert first.principal_mapping_rows == 2
        assert first.credential_locator_rows >= 2
        assert len(first.sha256) == 64
    finally:
        if created_fixture:
            with psycopg.connect(admin_url) as admin:
                admin.execute(
                    "SELECT set_config('hindsight.tenant_id', %s, false)",
                    (PUBLIC_DEMO_TENANT_ID,),
                )
                admin.execute(
                    "DELETE FROM product_credential_locators WHERE user_pool_id = %s",
                    (pool_id,),
                )
                admin.execute(
                    """
                        DELETE FROM product_principal_roles
                        WHERE provisioning_key IN (%s, %s)
                    """,
                    (mappings[0][1], mappings[1][1]),
                )


@requires_db
def test_verified_fenced_purge_cascades_and_database_purged_retries(
    lifecycle_database_url: str,
):
    admin_url = os.environ["DATABASE_URL"]
    tenant_id, principal_hash, benchmark_experiment_id = _create_tenant(admin_url)
    operation_id = str(uuid4())
    lease_owner = str(uuid4())
    fingerprint = _digest(f"export:{operation_id}")

    with psycopg.connect(admin_url) as admin:
        admin.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="verified fenced lifecycle purge",
        ):
            admin.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        admin.rollback()

        admin.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="immutable tenant records can be deleted only",
        ):
            admin.execute(
                "DELETE FROM benchmark_experiments WHERE id = %s",
                (benchmark_experiment_id,),
            )
        admin.rollback()

    with psycopg.connect(lifecycle_database_url, autocommit=True) as connection:
        preparation = begin_export(
            connection,
            tenant_id=tenant_id,
            operation_id=operation_id,
            lease_owner=lease_owner,
            public_identity_sha256=PUBLIC_IDENTITY_SHA256,
        )
        assert preparation.operation.public_identity_sha256 == PUBLIC_IDENTITY_SHA256
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="public identity baseline is immutable",
        ):
            connection.execute(
                """
                    UPDATE tenant_lifecycle_operations
                    SET public_identity_sha256 = %s
                    WHERE id = %s
                """,
                ("f" * 64, operation_id),
            )
        assert len(preparation.tables) == 52
        assert connection.execute(
            "SELECT count(*) FROM tenant_lifecycle_completeness_issues"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM tenant_lifecycle_schema_change_blockers"
        ).fetchone() == (0,)
        with psycopg.connect(admin_url) as admin:
            admin.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="frozen for lifecycle processing",
            ):
                admin.execute(
                    """
                        INSERT INTO services (
                            tenant_id, slug, name, owner_team, tier
                        ) VALUES (%s, 'late-write', 'Late', 'payments', 'core')
                    """,
                    (tenant_id,),
                )
            admin.rollback()
            admin.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="tenant root fields are frozen",
            ):
                admin.execute(
                    "UPDATE tenants SET slug = %s WHERE id = %s",
                    (f"rewritten-{tenant_id}", tenant_id),
                )
            admin.rollback()
            admin.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="tenant lifecycle status requires the lifecycle role",
            ):
                admin.execute(
                    "UPDATE tenants SET updated_at = now() WHERE id = %s",
                    (tenant_id,),
                )
            admin.rollback()

        preparation = begin_export(
            connection,
            tenant_id=tenant_id,
            operation_id=operation_id,
            lease_owner=lease_owner,
            public_identity_sha256=PUBLIC_IDENTITY_SHA256,
        )
        _record_verified_fixture(
            connection,
            operation_id=operation_id,
            lease_owner=lease_owner,
            schema_hash=preparation.schema_identity_sha256,
            fingerprint=fingerprint,
        )

        with pytest.raises(LifecycleLeaseError):
            begin_purge(
                connection,
                operation_id=operation_id,
                confirmed_fingerprint="f" * 64,
                lease_owner=lease_owner,
                public_identity_sha256=PUBLIC_IDENTITY_SHA256,
            )

        begin_purge(
            connection,
            operation_id=operation_id,
            confirmed_fingerprint=fingerprint,
            lease_owner=lease_owner,
            public_identity_sha256=PUBLIC_IDENTITY_SHA256,
        )
        blocker = connection.execute(
            """
                SELECT operation_id, status, lease_active
                FROM tenant_lifecycle_schema_change_blockers
            """
        ).fetchone()
        assert blocker is not None
        assert (str(blocker[0]), blocker[1], blocker[2]) == (
            operation_id,
            "purging",
            True,
        )
        with psycopg.connect(admin_url) as admin:
            admin.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="tenant root fields are frozen",
            ):
                admin.execute(
                    "UPDATE tenants SET tenant_kind = 'acceptance' WHERE id = %s",
                    (tenant_id,),
                )
            admin.rollback()
        with psycopg.connect(lifecycle_database_url, autocommit=True) as unfenced:
            unfenced.execute(
                "SELECT set_config('hindsight.tenant_id', %s, false), "
                "set_config('hindsight.lifecycle_operation_id', %s, false), "
                "set_config('hindsight.lifecycle_lease_owner', %s, false)",
                (tenant_id, operation_id, lease_owner),
            )
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="tenant deletion requires a verified fenced lifecycle purge",
            ):
                unfenced.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        assert record_principal_hashes(
            connection,
            operation_id=operation_id,
            lease_owner=lease_owner,
        ) == (principal_hash,)
        captured = connection.execute(
            """
                SELECT cognito_credential_locators,
                       cleanup_targets_captured_at IS NOT NULL
                FROM tenant_lifecycle_operations WHERE id = %s
            """,
            (operation_id,),
        ).fetchone()
        assert captured == (
            [
                {
                    "user_pool_id": "us-east-1_lifecycle",
                    "username": f"lifecycle-{tenant_id}",
                }
            ],
            True,
        )
        with psycopg.connect(admin_url, autocommit=True) as unfenced_admin:
            unfenced_admin.execute(
                "SELECT set_config('hindsight.tenant_id', %s, false), "
                "set_config('hindsight.lifecycle_operation_id', %s, false), "
                "set_config('hindsight.lifecycle_lease_owner', %s, false)",
                (tenant_id, operation_id, lease_owner),
            )
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="tenant deletion requires a verified fenced lifecycle purge",
            ):
                unfenced_admin.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        assert (
            purge_database_tenant(
                connection,
                operation_id=operation_id,
                lease_owner=lease_owner,
            ).status
            == "database_purged"
        )
        blocker = connection.execute(
            """
                SELECT operation_id, status
                FROM tenant_lifecycle_schema_change_blockers
            """
        ).fetchone()
        assert blocker is not None
        assert (str(blocker[0]), blocker[1]) == (operation_id, "database_purged")
        assert connection.execute(
            "SELECT count(*) FROM tenant_purge_tombstones WHERE purge_id = %s",
            (operation_id,),
        ).fetchone() == (1,)

        resumed = begin_purge(
            connection,
            operation_id=operation_id,
            confirmed_fingerprint=fingerprint,
            lease_owner=lease_owner,
            public_identity_sha256=PUBLIC_IDENTITY_SHA256,
        )
        assert resumed.status == "database_purged"
        assert (
            purge_database_tenant(
                connection,
                operation_id=operation_id,
                lease_owner=lease_owner,
            ).status
            == "database_purged"
        )

        tombstone = finalize_purge(
            connection,
            operation_id=operation_id,
            lease_owner=lease_owner,
            public_identity_sha256=PUBLIC_IDENTITY_SHA256,
        )
        assert (
            finalize_purge(
                connection,
                operation_id=operation_id,
                lease_owner=lease_owner,
                public_identity_sha256=PUBLIC_IDENTITY_SHA256,
            )
            == tombstone
        )
        assert connection.execute(
            "SELECT count(*) FROM tenant_lifecycle_schema_change_blockers"
        ).fetchone() == (0,)

    with psycopg.connect(admin_url) as connection:
        assert connection.execute(
            """
                SELECT
                    (SELECT count(*) FROM tenants WHERE id = %s),
                    (SELECT count(*) FROM services WHERE tenant_id = %s),
                    (SELECT count(*) FROM incidents WHERE tenant_id = %s),
                    (SELECT count(*) FROM tenant_event_outbox WHERE tenant_id = %s),
                    (SELECT count(*) FROM product_principal_roles WHERE tenant_id = %s),
                    (SELECT count(*) FROM product_credential_locators WHERE tenant_id = %s),
                    (SELECT count(*) FROM benchmark_experiments WHERE tenant_id = %s),
                    (SELECT count(*) FROM tenant_lifecycle_operations WHERE id = %s)
            """,
            (
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                operation_id,
            ),
        ).fetchone() == (0, 0, 0, 0, 0, 0, 0, 0)
        columns = tuple(
            row[0]
            for row in connection.execute(
                """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'tenant_purge_tombstones'
                    ORDER BY ordinal_position
                """
            ).fetchall()
        )
        assert columns == (
            "purge_id",
            "tenant_identity_sha256",
            "export_fingerprint",
            "schema_identity_sha256",
            "database_purged_at",
            "purged_at",
            "public_identity_sha256",
        )
        assert tombstone["public_identity_sha256"] == PUBLIC_IDENTITY_SHA256
        assert tenant_id not in repr(tombstone)
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="tenant purge tombstones are immutable",
        ):
            connection.execute(
                """
                    UPDATE tenant_purge_tombstones
                    SET export_fingerprint = export_fingerprint
                    WHERE purge_id = %s
                """,
                (operation_id,),
            )
        connection.rollback()
        connection.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="purged tenant identities cannot be recreated",
        ):
            connection.execute(
                """
                    INSERT INTO tenants (id, slug, tenant_kind)
                    VALUES (%s, %s, 'diagnostic')
                """,
                (tenant_id, f"resurrected-{tenant_id}"),
            )


@requires_db
@pytest.mark.parametrize("prior_state", ("expired_export", "verified"))
def test_abort_claims_a_fresh_lease_and_reactivates_archived_tenant(
    lifecycle_database_url: str,
    prior_state: str,
):
    tenant_id, _principal_hash, _benchmark_id = _create_tenant(os.environ["DATABASE_URL"])
    operation_id = str(uuid4())
    lease_owner = str(uuid4())

    with psycopg.connect(lifecycle_database_url, autocommit=True) as connection:
        preparation = begin_export(
            connection,
            tenant_id=tenant_id,
            operation_id=operation_id,
            lease_owner=lease_owner,
            public_identity_sha256=PUBLIC_IDENTITY_SHA256,
        )
        if prior_state == "verified":
            _record_verified_fixture(
                connection,
                operation_id=operation_id,
                lease_owner=lease_owner,
                schema_hash=preparation.schema_identity_sha256,
                fingerprint=_digest(f"export:{operation_id}"),
            )
        else:
            connection.execute(
                """
                    UPDATE tenant_lifecycle_operations
                    SET lease_expires_at = now() - INTERVAL '1 second'
                    WHERE id = %s
                """,
                (operation_id,),
            )
        aborted = abort_operation(connection, operation_id=operation_id)
        assert aborted.status == "aborted"
        assert aborted.lease_owner is None
        assert aborted.lease_expires_at is None
        with connection.transaction():
            connection.execute("SELECT set_config('hindsight.tenant_id', %s, true)", (tenant_id,))
            assert connection.execute(
                "SELECT status FROM tenants WHERE id = %s", (tenant_id,)
            ).fetchone() == ("active",)

        tombstone = _complete_purge(connection, tenant_id=tenant_id)
        assert tombstone["purge_id"] != operation_id


@requires_db
def test_lifecycle_completeness_fails_closed_for_an_unclassified_tenant_table():
    table_name = f"lifecycle_unclassified_{uuid4().hex}"
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        connection.execute(
            sql.SQL(
                "CREATE TABLE {} ("
                "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
                "tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE)"
            ).format(sql.Identifier(table_name))
        )
        try:
            issues = set(
                connection.execute(
                    """
                        SELECT issue_code
                        FROM tenant_lifecycle_completeness_issues
                        WHERE table_name = %s
                    """,
                    (table_name,),
                ).fetchall()
            )
            assert issues == {
                ("tenant_column_not_tenant_owned",),
                ("unclassified_table",),
            }
            with pytest.raises(LifecycleCompletenessError) as raised:
                assert_lifecycle_completeness(connection)
            assert (table_name, "unclassified_table") in raised.value.issues
        finally:
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table_name))
            )
        assert_lifecycle_completeness(connection)


def test_lifecycle_database_url_targets_a_named_database():
    if os.environ.get("DATABASE_URL"):
        assert urlsplit(os.environ["DATABASE_URL"]).path not in {"", "/", "/defaultdb"}
